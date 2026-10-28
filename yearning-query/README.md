# yearning-query

通过 [Yearning](https://github.com/cookieY/Yearning)（v3.0.x）SQL 审核平台执行只读 SQL 查询，可与 ELK 日志排查联动核对业务数据。

- [技能说明](SKILL.md)
- [脚本](scripts/yearning.py)（仅依赖 Python 标准库）
- [配置模板](config.example.json)
- [回归测试](tests/test_yearning.py)

## 功能

- 列出当前账号可查询的数据源、库、表；`locate-table` 跨所有数据源定位一张表在哪个数据源和库（表清单缓存 24 小时）。
- 经 Yearning 查询通道执行 SQL（与网页「查询」页一致：WebSocket `/api/v2/query/results` + msgpack），结果支持 `table` / `vertical` / `json` / `csv` / `tsv` 输出，可写入文件。
- 客户端只读校验：只放行 `SELECT / SHOW / DESC / EXPLAIN / WITH`，拒绝加锁读、导出到文件、`SLEEP` 等；SELECT 无 LIMIT 时自动追加（默认 100）。
- 查询会话处理：实例未开启查询审核时自动开启会话；开启审核时提示申请，`apply` 默认只预演，加 `--yes` 才提交。
- 查询会留下 Yearning 审计记录，并受实例的脱敏字段、查询条数上限和 SQL 引擎校验约束。

## 安装

1. 将本仓库克隆到自己维护的目录，把 `yearning-query` 目录安装（或软链接）到 `~/.claude/skills/yearning-query`。SKILL.md 中命令使用该字面路径。
2. 复制配置模板并自行填写 Yearning 地址与账号：

   ```bash
   mkdir -p ~/.config/yearning
   cp yearning-query/config.example.json ~/.config/yearning/config.json
   chmod 600 ~/.config/yearning/config.json
   ```

3. 检查连通与权限：

   ```bash
   python3 ~/.claude/skills/yearning-query/scripts/yearning.py check
   ```

## 配置

`~/.config/yearning/config.json` 支持多个 profile（`--profile <名>` 切换）：

| 字段 | 说明 |
|---|---|
| `url` | Yearning 地址，内网地址直连、不走 `http_proxy` |
| `username` / `password` | 登录账号；LDAP 账号同时设 `"ldap": true` |
| `prefer_sources` | 可选，`{"prod": [...], "staging": [...]}`，各环境默认数据源（如只读副本），`locate-table` 会排在最前并标 `★默认` |

环境变量可覆盖配置：`YEARNING_URL`、`YEARNING_USERNAME`、`YEARNING_PASSWORD`、`YEARNING_LDAP`、`YEARNING_TOKEN`（直接使用网页登录后的 JWT）、`YEARNING_PROFILE`、`YEARNING_CONFIG`。登录后的 JWT 缓存在 `~/.config/yearning/.cache/`。

凭据只放在该配置文件或环境变量中，不要放进 skill 目录或本仓库（仓库已忽略 `config.json`）。

## 命令

```bash
python3 ~/.claude/skills/yearning-query/scripts/yearning.py config                                   # 查看配置（已脱敏）
python3 ~/.claude/skills/yearning-query/scripts/yearning.py sources                                  # 可查询数据源
python3 ~/.claude/skills/yearning-query/scripts/yearning.py schemas -s <数据源>
python3 ~/.claude/skills/yearning-query/scripts/yearning.py tables -s <数据源> -d <库> [-f 关键字]
python3 ~/.claude/skills/yearning-query/scripts/yearning.py locate-table <表名> [--env prod|staging] [--like] [--refresh]
python3 ~/.claude/skills/yearning-query/scripts/yearning.py query -s <数据源> -d <库> --sql "SELECT ..." [--format vertical] [--out result.csv --format csv]
python3 ~/.claude/skills/yearning-query/scripts/yearning.py apply -s <数据源> --reason "<原因>" [--yes]
```

退出码：`0` 成功，`1` 请求或查询失败，`2` 配置或鉴权问题，`3` 无生效的查询会话（需申请并等待审批）。执行前的 SQL 分析要求、与 ELK 联动流程、结果脱敏等规则见 [SKILL.md](SKILL.md)。

## 依赖与范围

- Python 3.9+，无第三方依赖。
- 协议依据 Yearning v3.0.4 源码实现；其他版本的接口或消息格式可能不同，使用前先用 `check` 和一条简单查询验证。
- 与 ELK 联动需要另外安装 `elk-log-analysis` skill；未安装时本 skill 可独立使用。

## 验证

在仓库根目录执行：

```bash
python3 -B -m unittest discover -s yearning-query/tests -v
```

测试使用本地模拟的 Yearning 服务（HTTP + WebSocket + msgpack），不连接真实实例、不需要凭据。
