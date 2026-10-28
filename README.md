# Skills

用于 Claude Code 与 Codex 的个人开发技能集合。

## worktree-dev-context

按文件重叠判断是否需要 Git worktree；同步被忽略的 Trellis 上下文、选择 GitNexus 索引，并支持带预演校验的集成分支合并。

- [技能说明](worktree-dev-context/SKILL.md)
- [脚本](worktree-dev-context/scripts/)
- [回归测试](worktree-dev-context/tests/test_regressions.py)

本技能保留个人团队约定：默认主线为 main/master，集成分支为 beta/js-beta；多产品线必须明确指定目标。实际合并会推送远端，执行前须检查预演并提供 `--target`、`--expect-plan` 和 `--yes`。

## yearning-query

通过 Yearning（v3.0.x）SQL 审核平台执行只读查询：列数据源、库、表，`locate-table` 跨数据源定位表，校验只读并自动补 LIMIT 后经 Yearning 查询通道（WebSocket + msgpack）执行；可与 ELK 日志排查联动核对业务数据。

- [技能说明](yearning-query/SKILL.md)
- [脚本](yearning-query/scripts/yearning.py)（仅依赖 Python 标准库）
- [回归测试](yearning-query/tests/test_yearning.py)

安装到 `~/.claude/skills/yearning-query` 后，复制 `yearning-query/config.example.json` 为 `~/.config/yearning/config.json`（权限 600），自行填写 Yearning 地址与账号。凭据只放在该文件或环境变量中，不要放进 skill 目录或本仓库。查询会留下 Yearning 审计记录，受实例脱敏与查询限制约束。

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
python3 -B -m unittest discover -s yearning-query/tests -v
```

worktree-dev-context 的测试使用临时本地 Git 仓库，不需要 GitHub 或 Zadig 凭据，不会推送真实远端；yearning-query 的测试使用本地模拟的 Yearning 服务，不连接真实实例。
