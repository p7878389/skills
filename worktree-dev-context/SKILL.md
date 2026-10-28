---
name: worktree-dev-context
description: git worktree 开发全流程，Claude Code 与 Codex 通用：在主仓库检出中修改代码前，检查要改的文件是否正被同时修改，重叠时才改用 worktree 并按约定创建（简单修改原地做）；在 linked worktree（如 Claude Code 的 .claude/worktrees/*、Codex 托管的 worktree、git worktree add 出来的目录）中带入并回合被 .gitignore 忽略的 Trellis（.trellis）任务与 spec，判断并准备与 worktree 分支一致的 GitNexus 索引，按 feature/、fix/、hotfix/ 规范命名分支；需要集成或部署时，预演并核对目标后一次确认收尾；普通提交不自动进入部署流程。主仓库检出中开工前、第一次在 worktree 中读写代码前、代码提交后汇报完成前、删除 worktree 前都要用。
---

# Worktree 开发上下文（Trellis + GitNexus）

## 为什么需要

- `.trellis` 与 `.gitnexus` 一般都被 `.gitignore` 忽略，新建的 worktree 里两者都不存在。
- Trellis 装在 `.claude/`（hooks、agents、commands、skills、settings.json）、`.codex`、`.agents`、`AGENTS.md` 等位置，同样被忽略。项目 hook 以相对路径 `python3 .claude/hooks/*.py` 注册，会话进入 worktree 后每次提问、调用子代理都报 `can't open file '<worktree>/.claude/hooks/...'`，项目级子代理（trellis-implement 等）也找不到。用 `worktree_advise.py link` 把这些项软链到主检出解决（`create` 与 SessionStart hook 会自动执行）；hook 按 `cwd` 定位 `.trellis`，链接后读的仍是 worktree 的 `.trellis`。
- 项目 Trellis SessionStart hook 可能按主仓库解析（Claude Code 读 `CLAUDE_PROJECT_DIR`）：注入的分支、当前任务、dirty 状态可能是主仓库的，不代表 worktree，以 worktree 内 `task.py current` 与 `git status` 为准。
- GitNexus 的“索引落后 N 个提交”比对的是**注册路径（主仓库检出）的 HEAD**，不是 worktree；主仓库索引可能建在另一个分支上，worktree 新增的符号在其中不存在（`impact` 报 not found、`detect_changes` 漏报）。
- 实测（gitnexus 1.6.12）：在 worktree 执行 `analyze --branch X` **不会**写入主仓库 `.gitnexus/branches/`，而是在 worktree 建一份完整独立索引，并以与主仓库**相同的名称**注册，导致按名称调用歧义；MCP 的 `branch` 参数只认主仓库检出时建的分支槽，worktree 无法使用。所以 worktree 统一用“独立索引 + 独立名称”，不用 `--branch`。
- 命名空间：本地 worktree 索引一律 `<仓库>@wt-<分支>-<稳定摘要>`；`<仓库>@<分支>`、`<仓库>@<commit前10位>` 归 ELK MCP 的部署环境只读副本（`code_prepare` 返回），两者不要混用或手工改名占用。

判定是否处于 worktree：`git rev-parse --git-dir` ≠ `git rev-parse --git-common-dir`。例外：`~/.cache/elk-log-analysis/worktrees/*` 是 ELK MCP 的只读副本（detached），不适用本 skill，不带入 `.trellis`、不建/改索引、不删除。
GitNexus 分工：日志排查与已推送代码的调用链优先用远程 `gitnexus-remote`；本 skill 的索引选择、`impact`、`detect_changes` 针对本机工作区，只能用本地 GitNexus（远程看不到本机未推送改动）。

**脚本一律写字面路径** `python3 ~/.claude/skills/worktree-dev-context/scripts/<脚本>.py ...`，不要写 `S=...; python3 $S/...`：Claude Code 隔离在 worktree 的会话会拒绝执行带运行时变量的命令（`runs python with a value computed at runtime (the variable S)`）。同理，多条命令拆开写，不要把 `cd`、变量和 git 拼进一条复杂命令。

用户级 SessionStart hook（Claude Code：`~/.claude/settings.json`；Codex：`~/.codex/hooks.json`）都会运行 `~/.claude/skills/worktree-dev-context/scripts/session_start_hint.py`：会话目录是 linked worktree 时先自动执行 `worktree_advise.py link`，再注入 `[worktree-dev-context]` 提示（含 Trellis 是否已带入、独立索引名称、新链接的配置）；是主仓库检出且有其他会话、未提交改动或未完成的 git 操作时，注入“改代码前检查文件是否重叠”的提示；其余情况静默。两者输入输出格式相同（`cwd` → `hookSpecificOutput.additionalContext`）。本 skill 由 cc-switch 管理，实体在 `~/.cc-switch/skills/worktree-dev-context`，`~/.claude/skills/` 与 `~/.codex/skills/` 下都是指向它的软链；修改只改这一份，不要在软链位置放实体目录。

## 0. 是否需要 worktree（主仓库检出中开工前）

**口径：简单修改在主检出原地做，只有本次要改的文件正被同时修改时才用 worktree。** 不按任务大小、是否有其他会话来决定；其他会话或未提交改动只要不和本次要改的文件重叠，就原地改。

只在主仓库检出（非 linked worktree）中、**修改代码前**判断；只读任务（问答、查日志、代码审查、分析调研）跳过。用户明确说“就在这里改/不用 worktree”时照做；明确要求 worktree 时直接创建。

1. **定任务分支**：继续主检出当前分支上的工作（包括处理它现有的未提交改动）不传 `--branch`；新需求、独立修复需要新分支时按下方“分支命名”规范命名，传 `--branch`。

   **分支命名**（新建与改名都适用）：`<类型>/<简述>`，不带 hash、日期、随机后缀。
   - 需求、功能、优化：`feature/`，如 `feature/ZHENSUO-2833`、`feature/showBindOrUnbind支持铭牌号`；
   - bug：常规修复用 `fix/`，线上紧急修复（直接基于 master 发版）用 `hotfix/`，如 `hotfix/BASE-2429`；
   - 有工单号优先用工单号，没有就用简短的中文或英文描述，与仓库已有分支风格保持一致（可先看 `git branch -a` 与 `git log --merges` 中的分支名）。
2. **定位要改的文件**：先只读定位代码（GitNexus 查询、读源码），确定本次要修改/新增的文件或目录。
3. **检查重叠**：`python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py check [--branch <任务分支>] [--base <需求指定的基线>] <要改的文件或目录...>`，按最后一行 `VERDICT` 处理：
   - `STAY`：原地修改。主检出有其他会话或未提交改动时（输出 `NOTE`），提交只 `git add <自己改的文件>`，不用 `git add -A` / `commit -a`；提交前带同样路径再 `check` 一次，期间被别人改到同一文件就停下告知用户。
   - `WORKTREE_REQUIRED`（`CONFLICT` 行）：要改的文件与主检出未提交改动重叠、工作区被占用时需要换分支、或有未完成的 merge/rebase。例外：重叠的正是本任务自己在继续的改动（如用户让你接着改这些未提交文件）时原地改；否则**不询问**，直接创建并用一句话告知理由（引用 `CONFLICT` 行）。
   - `PATHS_REQUIRED`：没传要改的文件；补上路径再 `check`。
   - `USE_EXISTING <路径>`：任务分支已在该 worktree 检出，直接进入，不新建。
   - `IN_WORKTREE`：跳到第 1 节；`NOT_APPLICABLE`：跳过本 skill 的 worktree 部分。
   - 工作区未被占用（无其他会话、无未提交改动）时，需要新分支就在主检出 `git switch -c <分支> <基线>` 原地切换。
   - 开发中途发现还要改别的文件：带新路径再 `check`，出现 `WORKTREE_REQUIRED` 时停下告知用户，由用户决定是否把已做的改动挪到 worktree。
4. **基线**：需求指定的优先；否则从 `main`/`master` 切出（`create` 会先 fetch，用 `origin/<分支>` 最新）。`BASE: ASK`（仓库存在 `js-main`/`js-master` 两条产品线，或没有 main/master）时，即使是 `WORKTREE_REQUIRED` 也要先问用户基线。
5. **创建**：`python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py create --name <目录名> --branch <分支> [--base <基线>]`（目录名用分支名把 `/` 换成 `-`）。脚本在 `<主仓库>/.claude/worktrees/<目录名>` 建 worktree，新分支不跟踪基线（`--no-track`，避免 pull/push 误作用到 main），目录未被忽略时写入本地 `info/exclude`，并自动链接 Agent 配置（输出 `LINKED`）。
   - Claude Code：用 `EnterWorktree(path=<WORKTREE>)` 切入；不要用 `EnterWorktree(name=…)` 新建，它的基线不受上述约定控制。
   - Codex：会话目录无法切换，之后所有命令的 workdir 设为 worktree 路径、文件用绝对路径；改动较大时也可建议用户在该目录开新会话。
   - 主检出的未提交改动不会带到新 worktree；若用户要带过去，先征得同意再处理。
6. 进入 worktree 后立即执行第 1 节。

## 1. 开工前（第一次读写代码前）

0. **检查分支名**：worktree 当前分支不符合第 0 节“分支命名”规范时（例如 Claude Code 应用自动建的 `claude/<描述>-<hash>`），按任务性质定好 `feature/`、`fix/` 或 `hotfix/` 名称，在第一次提交前 `git branch -m <旧名> <新名>` 改名，并告知用户。分支已推送到远程时先问用户再改。
1. **链接 Agent 配置**：`python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py link`（幂等，`UP_TO_DATE` 表示无需处理）。用 `create` 建的或 SessionStart 时已处理过的会直接 `UP_TO_DATE`；Code tab、`EnterWorktree(name=…)`、Codex 等其他方式建的 worktree 靠这一步补齐。只链接主检出中被忽略、未跟踪且 worktree 缺失的 `.claude/*`（跳过 `.claude/worktrees`）、`.codex`、`.agents`、`.cursor`、`.opencode`、`.gemini`、`AGENTS.md`、`CLAUDE.md`、`GEMINI.md`；worktree 已有的不动。会话启动时不存在的 project settings/agents 如仍未生效，告知用户重开会话。
2. **带入 Trellis**：`python3 ~/.claude/skills/worktree-dev-context/scripts/trellis_sync.py pull`
   - 复制主仓库 `.trellis`（含 `.runtime`，`task.py current` 才能沿用会话任务指针），并在 `$GIT_DIR/trellis-base` 建三方合并基线。worktree 已有 `.trellis` 时不覆盖。
   - 之后 `task.py`、`get_context.py`、spec 读取、子代理的 `Active task` 与项目路径都用 worktree 路径。
   - 先确认本次需求是否属于 `task.py current` 的任务：属于就沿用；不属于按 Trellis Request Triage 询问是否新建，不要默认记进主仓库当前任务。
3. **选 GitNexus 索引**：`python3 ~/.claude/skills/worktree-dev-context/scripts/index_check.py <本次要分析/修改的文件或目录...>`，按最后一行 `VERDICT` 行动：
   - `USE_WORKTREE <name>` / `USE_MAIN <name>`：MCP 调用一律传 `repo: "<name>"`；
   - `REFRESH <name>` / `BUILD <name>`：执行输出中的 `REFRESH_CMD` / `BUILD_CMD`（`--index-only --name <仓库>@wt-<分支>`，本仓库首次约 1 分钟、约 300MB，之后增量刷新秒级），完成后用 `<name>`；耗时长时放后台运行。
   - `RENAME_REQUIRED <name>`：已有索引重名，不可复用；按输出命令改名后重新检查。新索引名称附加分支名的稳定摘要，避免路径字符替换后碰撞。
   - 不传路径时只要索引之后有任何代码变化就会建议建索引，结论偏保守；明确知道改动范围时传路径。

## 2. 开发中

- 代码修改后影响分析、`detect_changes` 都传同一个 `repo` 名；`detect_changes` 另传 `worktree: <worktree 绝对路径>`。
- 改动了被分析的符号后需要再次分析时，先 `index_check.py <路径>`，`REFRESH` 后再查，不用旧索引下结论。
- 所有 `analyze` 必须带 `--index-only`，否则会改写 `AGENTS.md` / `CLAUDE.md`；首次建 worktree 索引必须带 `--name`。
- before-dev / check / update-spec 照常执行，产物写在 worktree 的 `.trellis`。

## 3. 完成后的可选集成与部署

普通开发、仅提交、仅审查、没有集成分支的仓库不自动进入部署准备。用户要求集成/部署，或项目已明确约定该收尾方式时才执行本节。用户只要求提交时，完成提交与必要的 Trellis 回合后汇报即可，不主动查询 Zadig。

### 3.1 准备

1. 在任务分支执行 `python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py integrate [--target <集成分支>]`。预演会 fetch 更新远端引用，但不会推送或修改工作区文件。
   - 只有一个集成分支时可作为预演候选；beta/js-beta 同时存在时必须明确 `--target`，不根据提交距离或分支名称猜测产品线。
   - 记录 `TARGET`、`PLAN_SHA256`、`TASK_BRANCH_PUBLISHED`、`REPO`、提交列表及 `CHANGED_DIRS`。`UP_TO_DATE` 仅表示集成分支已包含提交，不代表任务分支已发布。
   - `CHANGED_DIRS` 为空表示没有相对目标的新文件差异，不能据此猜部署服务；重新部署须使用用户指定或已核实的服务。
   - `TARGET_ASK`：确认目标后带 `--target` 重跑。`CONFLICT`/`BLOCKED`：停止后续合并、清理和部署。不要把 beta 合回任务分支。
2. 在 worktree 内执行 `trellis_sync.py merge` 预演；有冲突或删除待决策项时先解决，不进入自动收尾。
3. 需要部署时，按 zadig-deploy skill 定位项目、工作流和服务。明确指定集成分支；产品工作流必须提供 `-s svc@<集成分支>` 和已核实的 `--repo`。产品 `preset` 不返回构建分支，不能据此认定默认就是 beta。无法核实代码库映射时暂停部署准备。
4. Zadig dry-run 保存完整命令、请求体和 `PLAN_SHA256`；计划与输出放在不会随 worktree 删除的位置。只合并时跳过部署预演。

### 3.2 授权范围

已有授权覆盖上述具体操作时直接执行；只补问尚未授权的部分。需要确认时展示任务分支、集成目标、待合并提交、Trellis 写回项、待清理 worktree/索引、部署项目/环境/服务/分支与 dry-run 摘要。

可选项为“合并、清理并部署”“只合并并清理”“暂不合并”；允许用户保留 worktree。没有部署要求时不默认选部署。用户只选合并不得执行部署；未明确授权清理不得删除 worktree。生产环境另需明确授权。实际目标、提交或部署参数变化时重新预演，不能沿用旧摘要。

### 3.3 执行与失败处理

1. 执行 `python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py integrate --target <已确认目标> --expect-plan <合并 PLAN_SHA256> --yes`。脚本校验远端地址、任务分支/HEAD、目标分支/提交；不匹配则在 push 前停止。临时 worktree 使用已核对的提交；目标远端推进导致推送失败时停止并重新预演，不自动合入新内容。
2. `MERGED` 或 `UP_TO_DATE` 且 `TASK_BRANCH_PUBLISHED: yes` 才满足发布前提。`UP_TO_DATE` 的执行仍会推送任务分支。
3. 有 worktree 时按 3.4 回合 Trellis。冲突、删除待决策或同步失败：停止清理与部署，保留 worktree，报告已推送结果。用户解决或明确授权跳过同步后才能继续。
4. 按第 4 节执行已授权清理。清理失败只阻断清理；代码已成功推送且 Trellis 无待处理项时，可以继续已经授权的部署，并汇报保留的路径。用户要求保留 worktree 时直接跳过清理。
5. 使用 dry-run 的完整命令加 `--expect-plan <部署 PLAN_SHA256> --yes --watch`。任何摘要不匹配都重新预演，不自动读取新摘要并直接重试。部署失败后停止，不自动重试或回滚，报告 task_id、页面 URL 与失败状态。
6. 汇报任务分支发布状态、集成提交、Trellis 写回、清理状态及部署结果。主检出原地开发跳过 worktree 同步/清理；任务分支保留供后续 MR 使用。

### 3.4 回合 Trellis

1. `python3 ~/.claude/skills/worktree-dev-context/scripts/trellis_sync.py merge` 预演（3.1 已做过且之后没再改 `.trellis` 时可略过）。
2. `python3 ~/.claude/skills/worktree-dev-context/scripts/trellis_sync.py merge --apply` 写回：
   - 主仓库未改动的文件直接覆盖，双方都改过的走 `git merge-file` 三方合并，仅无冲突时写回；写入采用共享目录锁、内容复核与逐文件原子替换。锁目录为主仓库 `.trellis.sync-lock`，进程异常退出后确认无同步任务再清理；
   - `[跳过-conflict]`：展示主仓库与 worktree 的差异，交用户决策，不覆盖、不写冲突标记；
   - `[跳过-delete]`（含 `task.py archive` 造成的移动）：列出后征得用户同意，再在主仓库执行对应操作；主仓库也删除后 `status` 不再报告，再次 `merge --apply` 时清理基线残留；
   - 已写回文件的基线自动推进，未解决的保留原基线，可再次 merge。
3. `python3 ~/.claude/skills/worktree-dev-context/scripts/trellis_sync.py status` 应只剩用户决策项或 `[main-only]`；汇报写入、跳过、冲突各哪些文件。
4. 提醒：`.trellis` 不受 git 管理，以上只影响本机主仓库，分支合并时不会随代码带过去。

## 4. 删除 worktree（按授权清理）

只删除 worktree 目录，保留本地分支：提交都在分支上，之后提 MR 直接用分支名。分支是否删除由用户决定。仅在本次授权包含清理、且不再需要继续开发时删除；普通提交不触发删除。

1. **前置检查**，任一不满足不删除；Trellis 待处理项同时阻断部署，其余情况按 3.3 处理：
   - `git -C <worktree> status --porcelain` 为空：没有未提交或未跟踪的改动；
   - 3.4 已完成，`trellis_sync.py status` 无待合并项；
   - 任务分支已推送且已合入集成分支（执行输出 `MERGED`/`UP_TO_DATE` 且 `TASK_BRANCH_PUBLISHED: yes`），或用户明确授权仅清理。合并一律走 `integrate`，不在本 worktree 里切换分支。
2. **清理索引**：`gitnexus remove "<实际独立索引全名（含摘要）>" -f` 删除独立索引与注册项（`gitnexus list` 可查名称；没建过独立索引则跳过）。`$GIT_DIR/trellis-base` 随 worktree 一起删除，无需单独处理。
3. **删除 worktree**：
   - 会话不在该 worktree 内：`git -C <主仓库> worktree remove <worktree 路径>`，再 `git -C <主仓库> worktree prune`；
   - Claude Code 会话正处于该 worktree：用 `ExitWorktree` 退出并删除，之后的部署命令在主检出执行即可；如果该 worktree 由 Claude Code 应用（Code tab）为会话创建、无法在会话内删除，照常部署，并告知用户归档会话即可清理，或在会话结束后执行上一条命令；
   - Codex：托管 worktree 使用原生归档工具；普通 worktree 的后续命令显式使用主检出 workdir，再执行 `git -C <主仓库> worktree remove <worktree 路径>`。无法安全清理时保留并报告。
   - 不要用 `--force` 绕过未提交改动；`worktree remove` 报错时把原因告诉用户。
4. 汇报：已删除的 worktree 路径、保留的分支名、清理的索引名；随后回到 3.3 第 5 步部署。

## 失败处理

- `trellis_sync.py` 报“缺少基线”：worktree 曾手工复制过 `.trellis`，不要直接重建基线；先 `diff -rq` 对比主仓库与 worktree，经用户确认后再处理。
- `analyze` 报索引被占用（graph DB held by another process）：另一个进程正在读写同一份索引，稍后重试；不要删除锁文件。
- 主仓库没有 `.trellis` 或未建索引：跳过对应步骤并在汇报中说明。
- `PreToolUse:Agent hook error` / `UserPromptSubmit` hook 报 `can't open file '<worktree>/.claude/hooks/...'`：worktree 缺 Agent 配置，执行第 1 节第 1 步 `link`。
- `This session is isolated in the worktree ... computed at runtime (the variable X)`：命令里用了 shell 变量，改成字面路径、拆成简单命令再执行。
- `integrate` 报 `merge-tree` 失败：需要 git ≥ 2.38；`push` 被拒且非快进：远端任务分支有本地没有的提交，先与用户确认如何同步，不要强推。
