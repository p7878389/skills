---
name: worktree-dev-context
description: Git worktree 开发全流程，Claude Code 与 Codex 通用。每次开发前结合用户输入、会话上下文、最近分支切换和提交内容，判断是新需求还是某分支上的续改：新需求、修复一律使用独立 worktree，不直接在主检出的当前分支上修改；续改选择相关任务最新开发分支并优先复用其 worktree。支持 Agent 配置链接、可选 Trellis 同步和 GitNexus 索引、按授权预演集成及清理。用于开发前、worktree 首次读写代码前、提交收尾和删除前；普通提交不自动触发集成部署。 Git worktree workflow for Claude Code and Codex - before each change, decide from the request, session context, recent branch switches and commits whether it is a new requirement or a continuation on an existing branch; new requirements and fixes always get their own worktree and are never edited on the current branch of the main checkout, while continuations reuse the latest related branch and its worktree; optional Trellis sync and GitNexus index, dry-run integration and cleanup only when authorized. Use before development, before first code access in a worktree, after committing, and before removing a worktree.
---

# Worktree 开发上下文

## 核心能力与可选集成

必需依赖只有 Git 和 Python 标准库。Trellis、GitNexus、Zadig 都不是前提；三者均未安装时，文件重叠检查、worktree 创建/复用、Agent 配置链接、合并预演和清理仍可独立使用。**以下各章节中的集成指令都受本节条件约束，包括开工、开发、收尾、删除前检查和 hook 提示。**

- **Trellis（可选）**：项目使用 `.trellis` 时才执行带入、回合、状态检查及相关任务流程；没有 `.trellis` 时全部跳过。已带入的数据仍须妥善处理，不能将同步冲突当作“未启用”忽略。
- **GitNexus（可选）**：本地工具可用且本次需要索引分析时才执行索引选择、构建、刷新及影响分析；否则用 `rg`、源码阅读和测试替代，不要求安装或建索引。仅清理实际创建的独立索引；工具失效时报告残留，不阻断普通 Git 操作。
- **Zadig（可选）**：用户要求通过 Zadig 部署，且 `zadig-deploy` skill 与服务配置可用时才执行部署预演和部署。未启用时跳过部署，合并及清理可独立完成。明确要求部署但条件不具备时，说明部署步骤未完成，不擅自安装或配置。

汇报和确认只展示实际适用的步骤，不因未安装可选工具而要求用户补齐依赖。

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

用户级 SessionStart hook（Claude Code：`~/.claude/settings.json`；Codex：`~/.codex/hooks.json`）都会运行 `~/.claude/skills/worktree-dev-context/scripts/session_start_hint.py`：会话目录是 linked worktree 时先自动执行 `worktree_advise.py link`，再注入 `[worktree-dev-context]` 提示（含 Trellis 是否已带入、独立索引名称、新链接的配置）；是主仓库检出时注入“新需求不要直接在当前分支修改，先 `check` 并进入 worktree”的提示；非 git 目录等不适用的情况静默。两者输入输出格式相同（`cwd` → `hookSpecificOutput.additionalContext`）。本 skill 由 cc-switch 管理，实体在 `~/.cc-switch/skills/worktree-dev-context`，`~/.claude/skills/` 与 `~/.codex/skills/` 下都是指向它的软链；修改只改这一份，不要在软链位置放实体目录。

## 0. 开发前：判断任务关联，再选择分支与 worktree

**口径：开发新需求、新功能、修复（bug 修复）一律在 worktree 中进行，不直接在主检出的当前分支上修改；续改某个已有任务分支时，复用该分支的 worktree。** 主检出保持干净，方便随时切换、对照和并行处理其他任务；基线/集成分支（main、master、beta、js-main、js-master、js-beta、develop、dev）上永远不直接开发。

每次收到开发请求，均在**修改代码前**判断，包括当前已在 linked worktree 的情况；只读任务（问答、查日志、代码审查、分析调研）跳过。新需求必须使用独立任务分支及其 worktree，不能因为当前已在 worktree 就混入无关任务。

### 0.1 判断新需求或续改

由 agent 结合语义与 Git 证据判断，脚本 `check` 只检查检出位置和文件冲突，不会理解用户输入。`IN_WORKTREE` 也不证明当前分支与任务相关。

1. **收集候选**：先读会话中的当前任务、工单号和用户明确指定的分支，再查看 `git status --short --branch`、`git worktree list --porcelain`、`git for-each-ref --sort=-committerdate --format='%(refname:short) %(objectname:short) %(committerdate:iso8601) %(subject)' refs/heads`。在当前检出、主检出以及相关候选 worktree 中用 `git reflog -n 20 --date=iso HEAD` 查看最近切换、提交；HEAD reflog 属于各自检出，不以当前目录的一份记录代替全部。无匹配再扩大范围，不扫描无关仓库。
2. **核实关联**：对候选分支查看 `git log -n 10 --date=iso --format=fuller --name-status <候选分支>`，必要时读取相关提交差异和源码。当前输入是同一需求的补充、修正、测试或对刚提交成果的后续调整，且会话/工单/提交内容能对应时，按续改处理，不要求用户必须说“继续”。只有同模块、同文件、相似分支名或时间接近不够；明确提出独立功能或无关问题时按新任务处理。
3. **选择相关任务的最新开发分支**：用户明确指定优先；否则优先本会话已确认的任务分支。同一任务有后继分支时，用 `git merge-base --is-ancestor <旧分支> <候选分支>`、提交差异及任务上下文确认其包含已有成果，再选择仍用于该任务的最新开发分支。最近切换/提交时间用于排序候选，不能覆盖语义或提交继承关系。main/master、beta、develop 等基线/集成分支不作为续改开发分支。存在多个分叉候选且证据不足时，列出关键差异并只询问一次；无法建立关联的明确新任务直接新建。
4. **落到正确检出**：相关分支已有 worktree 时复用，后续命令显式设置该路径为 workdir；已有本地分支但未检出时，用 `create --branch <已有分支> --name <目录名>` 为它创建 worktree，保留现有历史。目标分支在主检出且确认为续改时，按 `check --continue <路径...>` 处理；遇到并发冲突，不能强制重复检出同一分支，应从已确认任务分支创建独立续改分支，保留主检出改动。续改分支仅在远端时，核实远端引用后从该引用创建本地任务分支/worktree。发现本地与远端分叉，不自动 reset、强推或合并。当前在其他 worktree 时，先完成任务关联选择，再进入目标或创建新 worktree，不能把 `IN_WORKTREE` 当作放行依据。

明确相关后自主继续，用一句话说明目标分支、worktree 和关联依据；不重复请求许可。不相关的新任务走下方创建流程，默认从项目基线创建，不自动叠加在最近开发分支上。只有需求明确依赖其未集成成果时，才将已核实的开发分支作为新任务基线。任务已提交不等于续改必须新建分支；已有分支已删除时，不自动恢复过时分支，应从包含该成果的项目基线创建新修复/需求 worktree。

### 0.2 执行检查与创建

**可以原地修改的例外**（只有这两种）：
- **继续已有任务**：按 0.1 确认当前分支就是本任务的 `feature/`、`fix/`、`hotfix/` 分支，包含继续修改已提交成果的情形；只接续已核实属于本任务的未提交改动。当前分支是基线/集成分支时不算，仍要建 worktree。
- **用户明确要求**“就在这里改 / 不用 worktree”。用户明确要求 worktree 时直接创建。

不因“改动很小”“只改一个文件”而原地改：小改动也是新任务，照样走 worktree。

1. **判断任务性质并定任务分支**：
   - 新需求、新功能、新的修复：需要新分支，按下方“分支命名”规范命名，传 `--branch`；
   - 续改已选定任务分支：优先复用其 worktree；该分支在主检出时才走例外的 `--continue`。不得仅因 `--branch` 恰好等于当前分支，就把新需求当作续改。

   **分支命名**（新建与改名都适用）：`<类型>/<简述>`，不带 hash、日期、随机后缀。
   - 需求、功能、优化：`feature/`，如 `feature/ZHENSUO-2833`、`feature/showBindOrUnbind支持铭牌号`；
   - bug：常规修复用 `fix/`，线上紧急修复（直接基于 master 发版）用 `hotfix/`，如 `hotfix/BASE-2429`；
   - 有工单号优先用工单号，没有就用简短的中文或英文描述，与仓库已有分支风格保持一致（可先看 `git branch -a` 与 `git log --merges` 中的分支名）。
2. **检查**：`python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py check --branch <任务分支> [--base <需求指定的基线>]`。新任务不需要先定位要改的文件，直接得到 `WORKTREE_REQUIRED`（输出 `REASON` 行）。按最后一行 `VERDICT` 处理：
   - `WORKTREE_REQUIRED`：**不询问**，直接创建并用一句话告知理由（引用 `REASON`/`CONFLICT` 行）。
   - `USE_EXISTING <路径>`：任务分支已在该 worktree 检出，直接进入，不新建。
   - `IN_WORKTREE`：跳到第 1 节；`NOT_APPLICABLE`：跳过本 skill 的 worktree 部分。
   - 例外情形才会出现 `STAY` / `PATHS_REQUIRED`：
     - 继续进行中的任务：`check --continue <要改的文件或目录...>`。当前分支是基线/集成分支时仍返回 `WORKTREE_REQUIRED`。
     - 用户明确要求原地改：`check --in-place <要改的文件或目录...>`（在基线/集成分支上会提示确认用户知晓）。
     - `STAY`：原地修改。主检出有其他会话或未提交改动时（输出 `NOTE`），提交只 `git add <自己改的文件>`，不用 `git add -A` / `commit -a`；提交前带同样路径再 `check` 一次，期间被别人改到同一文件就停下告知用户。
     - `PATHS_REQUIRED`：没传要改的文件；补上路径再 `check`。
     - `WORKTREE_REQUIRED`（`CONFLICT` 行）：要改的文件与主检出未提交改动重叠、工作区被占用时需换分支、或有未完成的 merge/rebase，直接创建 worktree。`--continue` 且没有其他会话时，主检出的未提交改动视为本任务自己的，不算重叠。
     - 原地开发中途发现还要改别的文件：带新路径再 `check`，出现 `WORKTREE_REQUIRED` 时停下告知用户，由用户决定是否把已做的改动挪到 worktree。
3. **基线**：独立新任务以需求指定的优先，否则从 `main`/`master` 切出（`create` 会先 fetch，用 `origin/<分支>` 最新）。`BASE: ASK`（仓库存在 `js-main`/`js-master` 两条产品线，或没有 main/master）时，若明确任务上下文已指定产品线/基线，带 `--base` 重跑；否则先问用户。续改已有分支保留其历史，不重新从 main/master 起步；确需新建续改分支时显式以已选定的相关开发分支为 `--base`。
4. **创建**：`python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py create --name <目录名> --branch <分支> [--base <基线>]`（目录名用分支名把 `/` 换成 `-`）。脚本在 `<主仓库>/.claude/worktrees/<目录名>` 建 worktree，新分支不跟踪基线（`--no-track`，避免 pull/push 误作用到 main），目录未被忽略时写入本地 `info/exclude`，并自动链接 Agent 配置（输出 `LINKED`）。
   - Claude Code：用 `EnterWorktree(path=<WORKTREE>)` 切入；不要用 `EnterWorktree(name=…)` 新建，它的基线不受上述约定控制。
   - Codex：会话目录无法切换，之后所有命令的 workdir 设为 worktree 路径、文件用绝对路径；改动较大时也可建议用户在该目录开新会话。
   - 主检出的未提交改动不会带到新 worktree；若用户要带过去，先征得同意再处理。
5. 进入 worktree 后立即执行第 1 节。

## 1. 开工前（第一次读写代码前）

0. **检查分支名**：worktree 当前分支不符合第 0 节“分支命名”规范时（例如 Claude Code 应用自动建的 `claude/<描述>-<hash>`），按任务性质定好 `feature/`、`fix/` 或 `hotfix/` 名称，在第一次提交前 `git branch -m <旧名> <新名>` 改名，并告知用户。分支已推送到远程时先问用户再改。
1. **链接 Agent 配置**：`python3 ~/.claude/skills/worktree-dev-context/scripts/worktree_advise.py link`（幂等，`UP_TO_DATE` 表示无需处理）。用 `create` 建的或 SessionStart 时已处理过的会直接 `UP_TO_DATE`；Code tab、`EnterWorktree(name=…)`、Codex 等其他方式建的 worktree 靠这一步补齐。只链接主检出中被忽略、未跟踪且 worktree 缺失的 `.claude/*`（跳过 `.claude/worktrees`）、`.codex`、`.agents`、`.cursor`、`.opencode`、`.gemini`、`AGENTS.md`、`CLAUDE.md`、`GEMINI.md`；worktree 已有的不动。会话启动时不存在的 project settings/agents 如仍未生效，告知用户重开会话。
2. **带入 Trellis（仅项目使用时）**：`python3 ~/.claude/skills/worktree-dev-context/scripts/trellis_sync.py pull`
   - 复制主仓库 `.trellis`（含 `.runtime`，`task.py current` 才能沿用会话任务指针），并在 `$GIT_DIR/trellis-base` 建三方合并基线。worktree 已有 `.trellis` 时不覆盖。
   - 之后 `task.py`、`get_context.py`、spec 读取、子代理的 `Active task` 与项目路径都用 worktree 路径。
   - 先确认本次需求是否属于 `task.py current` 的任务：属于就沿用；不属于按 Trellis Request Triage 询问是否新建，不要默认记进主仓库当前任务。
3. **选 GitNexus 索引（仅本地工具可用且本次需要时）**：`python3 ~/.claude/skills/worktree-dev-context/scripts/index_check.py <本次要分析/修改的文件或目录...>`，按最后一行 `VERDICT` 行动：
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

### 3.4 回合 Trellis（仅已带入时）

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
   - 已带入 Trellis 时，3.4 已完成且 `trellis_sync.py status` 无待合并项；未使用时跳过；
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
