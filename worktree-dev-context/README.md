# worktree-dev-context

开发新需求、新功能、独立修复时一律使用 Git worktree，不直接在主检出的当前分支上修改；同步被忽略的 Trellis 上下文、选择 GitNexus 索引，并支持带预演校验的集成分支合并。

每次开发前结合用户输入、会话上下文、各检出的最近切换记录和提交内容判断任务关联。续改选择相关任务最新且包含已有成果的开发分支，优先复用其 worktree；时间最近本身不代表相关。独立新任务另建分支和 worktree，当前已在 worktree 时也不混入无关任务。具体判断与命令见技能说明第 0 节。

- [技能说明](SKILL.md)
- [脚本](scripts/)
- [回归测试](tests/test_regressions.py)

原地修改只有两种例外：继续主检出当前任务分支上已在进行的工作（`check --continue`），或用户明确要求原地改（`check --in-place`）；在 main、master、beta 等基线/集成分支上始终要求 worktree。

本技能保留个人团队约定：默认主线为 main/master，集成分支为 beta/js-beta；多产品线必须明确指定目标。实际合并会推送远端，执行前须检查预演并提供 `--target`、`--expect-plan` 和 `--yes`。

## 安装

将本仓库克隆到自己维护的目录后，把 `worktree-dev-context` 目录安装到 `~/.claude/skills/worktree-dev-context`。也可使用 cc-switch 统一管理，并让 Claude Code、Codex 的 skills 目录链接到同一个实体目录。

Codex 使用时还需让 `~/.codex/skills/worktree-dev-context` 指向该目录。当前文档和 hook 提示使用 `~/.claude/skills/worktree-dev-context/scripts/` 字面路径，因此仅使用 Codex 时也需保留该路径的链接。安装前检查已有目录或链接，勿覆盖现有配置。

本仓库不会自动安装 hook。`session_start_hint.py` 是可选 SessionStart 入口；需按客户端的 hook 配置方式手动接入。SKILL.md 中关于个人环境已配置 hook 的说明，不代表其他机器安装后也已启用。

## 依赖与范围

**必需依赖只有 Python 和 Git。Trellis、GitNexus、Zadig 均为可选集成，三者都未安装时核心 worktree 流程仍可独立使用。**

- Python 3.9+、Git；合并预演需要 Git 2.38+。
- Trellis（可选）：项目使用 `.trellis` 时才同步，否则跳过相关步骤。
- GitNexus（可选）：本地工具可用且需要索引分析时才使用；缺失时用文本检索、源码阅读和测试替代，不要求安装或建索引。
- Zadig 部署是可选衔接，需要另外安装 `zadig-deploy` skill 并配置服务；本仓库不包含部署技能或凭据。未安装时跳过部署，不影响 worktree 和同步脚本。
- 内含个人环境路径约定和 Claude Code/Codex 操作说明；其他系统应先检查路径、软链接与客户端支持情况。

## 验证

在仓库根目录执行：

```bash
python3 -B -m unittest discover -s worktree-dev-context/tests -v
```

测试使用临时本地 Git 仓库，不需要 GitHub 或 Zadig 凭据，不会推送真实远端。
