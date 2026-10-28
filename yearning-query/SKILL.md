---
name: yearning-query
description: 通过 Yearning（v3.0.x）SQL 审核平台对生产/测试数据库执行只读 SQL 查询：列数据源、库、表，定位表所在数据源，分析并改写查询 SQL，经 Yearning 查询通道执行并整理结果；也用于 ELK 日志排查时核对业务数据。当用户提到"Yearning"、"SQL 平台"、"查一下生产库/线上数据"、"帮我写个 SQL 查…"、"执行这条 SQL"、"查某张表的数据/表结构"，或排查日志时需要"看下数据库里这条数据/这个订单/这个诊所的状态"时使用。 Run read-only SQL against production or staging databases through the Yearning SQL audit platform - list data sources, schemas and tables, locate a table across data sources, review and rewrite SQL, execute it and summarize results; also used to verify business data during ELK log triage. Use when the user mentions Yearning, a SQL platform, querying the production database, running a SQL statement, looking up table data or table schema, or checking the state of a record, order or clinic while debugging logs.
---

# Yearning 只读查询

所有调用走 `scripts/yearning.py`（Python 标准库，无需安装依赖），协议依据 Yearning v3.0.4 源码：HTTP 接口取数据源/库/表，查询通过 WebSocket `/api/v2/query/results` + msgpack 执行，与网页「查询」页面完全一致（会留下 Yearning 查询审计记录，受实例的脱敏字段、查询条数上限、只读引擎校验约束）。

命令一律用字面路径 `python3 ~/.claude/skills/yearning-query/scripts/yearning.py ...`。

## 0. 配置（由用户自己维护，Claude 不写入、不索要凭据）

- 配置文件：`~/.config/yearning/config.json`（权限 600），模板见本目录 `config.example.json`；支持多 profile，`--profile <名>` 切换。
- 环境变量可覆盖：`YEARNING_URL`、`YEARNING_USERNAME`、`YEARNING_PASSWORD`、`YEARNING_LDAP`（LDAP 账号设为 true）、`YEARNING_TOKEN`（直接使用网页登录后的 JWT）、`YEARNING_PROFILE`、`YEARNING_CONFIG`。
- 登录后 JWT 缓存在 `~/.config/yearning/.cache/`（服务端签发 8 小时有效），过期或 401 自动重新登录。
- 内网地址直连，不走本机 `http_proxy`。
- 可选 `prefer_sources`：`{"prod": ["数据源名"], "staging": [...]}`，标记各环境默认使用的数据源（如只读副本），`locate-table` 会把它们排在最前并标 `★默认`。
- 不要读取、打印或回显密码/token；确认配置用 `python3 ~/.claude/skills/yearning-query/scripts/yearning.py config`（已脱敏）。未配置时请用户自行编辑配置文件。

## 1. 标准流程

1. **连通检查**：`python3 ~/.claude/skills/yearning-query/scripts/yearning.py check` —— 输出当前用户、查询审核是否开启、查询会话状态、可查询数据源（名称 / source_id / 环境）。
2. **定位数据源与库表**（用户只给模糊描述时）：
   - 已知表名：`python3 ~/.claude/skills/yearning-query/scripts/yearning.py locate-table <表名> [--env prod|staging] [--like]` —— 在所有可查询数据源中找表所在的数据源和库（表清单缓存 24 小时，表刚新建时加 `--refresh`）
   - `python3 ~/.claude/skills/yearning-query/scripts/yearning.py sources`
   - `python3 ~/.claude/skills/yearning-query/scripts/yearning.py schemas -s <数据源名或source_id>`
   - `python3 ~/.claude/skills/yearning-query/scripts/yearning.py tables -s <数据源> -d <库> [-f 关键字]`
   - 表结构：`query -s <数据源> -d <库> --sql "SHOW CREATE TABLE <表>" --format vertical`（或 `SHOW INDEX FROM <表>`）
3. **分析 SQL**（执行前必须做，见第 2 节），给用户简要说明要执行的 SQL 及理由。
4. **执行**：
   ```bash
   python3 ~/.claude/skills/yearning-query/scripts/yearning.py query -s <数据源> -d <库> --sql "SELECT ..."
   ```
   多行 SQL 用 `--file <path>` 或 `--sql -` 从标准输入读取；多条语句以 `;` 分隔，返回多个结果集。
5. **整理结果**：概括结论，必要时给出关键行；结果大时用 `--out <文件> --format csv` 保存后告诉用户路径。

### query 参数

| 参数 | 说明 |
|---|---|
| `-s/--source` | 数据源名称、source_id 或名称中唯一的子串 |
| `-d/--schema` | 库名（必填） |
| `--limit N` | SELECT/WITH 无 LIMIT 时自动追加 `LIMIT N`，默认 100；`0` 关闭（仅在用户明确需要且已确认数据量时） |
| `--format` | `table`（默认）/ `vertical`（类似 `\G`，宽行、DDL、单行详情用）/ `json` / `csv` / `tsv` |
| `--max-rows` / `--max-width` | table/vertical 显示的行数（默认 50）；table 单元格宽度（默认 60，超出截断） |
| `--out FILE` | 完整结果写入文件，终端只打印摘要 |
| `--timeout` | 秒，默认 120 |

## 2. 执行前的 SQL 分析（生产库必做）

- **只读**：脚本只放行 `SELECT / SHOW / DESC / DESCRIBE / EXPLAIN / WITH`，并拒绝 `FOR UPDATE`、`LOCK IN SHARE MODE`、`INTO OUTFILE/@var`、`SLEEP/BENCHMARK/GET_LOCK`。任何写操作（INSERT/UPDATE/DELETE/DDL）都不走本 skill，提醒用户在 Yearning 提交 DML/DDL 工单。
- **先看表结构与索引**：不确定字段名、索引时先 `SHOW CREATE TABLE`，不要猜字段。
- **控制扫描量**：
  - WHERE 条件优先命中索引（主键、唯一键、带索引的时间字段）；大表查询必须有选择性条件或时间范围。
  - 对不确定的大表查询先 `EXPLAIN`：`type=ALL` 且 `rows` 很大时先收窄条件，再执行。
  - 避免对索引列套函数（`DATE(create_time)=...` 改写为范围条件）、前导通配 `LIKE '%x'`、隐式类型转换（字符串列用数字比较）。
  - 只取需要的列，少用 `SELECT *`；统计用 `COUNT`/`GROUP BY` 而不是拉明细。
  - 分页避免大 OFFSET，用主键游标 `WHERE id > ? ORDER BY id LIMIT n`。
  - 多表 JOIN 确认关联字段有索引；子查询能改 JOIN 就改。
- **用户直接给的 SQL**：先按上面检查，有明显全表扫描/锁风险时说明风险并给出改写建议，经用户同意再执行改写版；无风险直接执行。
- 自动追加 LIMIT 后，若结果正好等于 LIMIT，告诉用户结果可能被截断。

## 3. 与 ELK 日志排查联动

日志只能说明“发生了什么”，业务数据能说明“为什么”（状态不对、余额为 0、配置缺失、数据重复等）。但**查库不是排查的默认步骤**：先用日志和代码分析，只有确实需要数据才能下结论时才接入本 skill。

### 先判断：是否真的需要查库

每次准备查库前先回答“这次查询要验证什么假设？结果不同会不会改变结论或修复方案？”答不上来就不查。

**需要查**（满足任一，且日志和代码无法替代）：
- 代码分支取决于数据状态，日志没打出这些值：如“状态非法 / 余额不足 / 记录不存在 / 配置未开启”，需要确认数据库里的实际值。
- 怀疑数据本身有问题：重复记录、脏数据、关联缺失、历史数据不符合新逻辑。
- 需要确定影响范围：多少用户/订单处于异常状态，用于评估修复或补数据。
- 用户明确要求核对某条数据。

**不需要查**（直接基于日志和代码下结论）：
- 异常本身已说明根因：NPE、参数校验失败、下游超时/连接拒绝、序列化错误、配置/代码缺陷等，与库中数据无关。
- 日志已打印了判断所需的数据值，或 traceId 链路已能完整还原过程。
- 查询结果不会改变结论或修复方案，只是“顺便看看”。
- 还没有定位到具体业务 ID 和表，只能做大范围扫描。

需要查时，先在回复里用一句话说明要验证的假设、查哪张表、用什么条件，再执行；查生产库仍需满足下面的授权要求。查询以验证假设为止，不扩大成数据浏览。

### 联动步骤

1. **从日志取线索**：用 elk 工具（`elk_search` / `elk_trace`）拿到代表性的错误日志，提取**业务 ID**（诊所 ID、订单号、设备号、用户 ID 等，注意生产日志里手机号、身份证已被服务端脱敏，不能直接拿来查）和**时间窗口**。
2. **确定表和字段**：不要凭字段名猜表。用 `code_locate` 片段，或 GitNexus `context`（远程模式传 `code_prepare` 返回的 `repo`）找到报错代码访问的 Mapper/Entity/SQL，确认表名、字段和查询条件；需要时再 `query --sql "SHOW CREATE TABLE <表>" --format vertical` 看索引。
3. **确定数据源**：`locate-table <表名> --env <prod|staging>`。环境要和日志一致：ELK 的 prod 对应 `[prod]` 数据源，stag/staging 对应 `[staging]` 数据源；同一张表出现在多个数据源时，按服务的数据源配置（`application-<env>.yml` 中的库名）选；标 `★默认` 的是配置里 `prefer_sources` 指定的默认数据源（通常是只读副本），没有更明确依据时优先用它。拿不准时列出候选让用户确认。
4. **查数据**：按主键/唯一键/索引列精确查询（`WHERE id IN (...)`），只取和问题相关的列；需要看变化过程时查对应的变更日志/历史表，并用时间窗口过滤。
5. **合并结论**：把日志事实和数据事实对上，写明：ELK 环境与时间范围、Yearning 数据源/库、执行的 SQL、关键数据（已打码）、两者如何互相印证，以及代码位置与 commit。

联动时的注意事项：

- **生产授权分开算**：查生产日志（`confirm_production: true`）和查生产库都要用户明确要求；用户只说“查日志”时，查生产库前先说明要查什么、为什么，得到同意再执行。
- **时间不做时区转换**：日志时间和数据库时间直接按原值使用，ELK 查询（`since`/`until`）和 SQL 条件都用原始时间参数，不要自行加减 8 小时。日期格式统一写成 `yyyy-MM-dd HH:mm:ss`（如 `2026-10-28 15:30:00`），**不要带 `T`**（日志里出现 `2026-10-28T15:30:00` 这类写法时，把 `T` 换成空格再用）。必要时在 SQL 里放宽前后几分钟。
- **只读副本延迟**：只读副本类数据源可能比主库晚几秒到几分钟；排查刚发生的问题时，数据“不存在/未更新”不一定是 bug，必要时说明并对照主库数据源。
- **批量 ID**：日志里提取的 ID 较多时，先用 `elk_agg` 按 ID 字段聚合去重，再分批 `IN (...)`（每批不超过 200 个）。
- **数据不出会话**：查到的生产数据只用于本次分析，不写进代码、提交、工单或日志查询条件以外的外部系统。

## 4. 查询会话与权限

- Yearning 查询需要「查询会话」（一条已批准的查询工单，按实例配置的分钟数过期）。
- 实例**未开启查询审核**：脚本像网页一样自动开启会话，无需处理。
- 实例**开启查询审核**且无生效会话：`query` 以退出码 3 结束并提示。此时提交申请会给审核人发通知，**必须先征得用户同意**：
  ```bash
  python3 ~/.claude/skills/yearning-query/scripts/yearning.py apply -s <数据源> --reason "<用户给的原因>"        # 预演
  python3 ~/.claude/skills/yearning-query/scripts/yearning.py apply -s <数据源> --reason "<用户给的原因>" --yes  # 用户确认后提交
  ```
  批准后再执行查询；不要轮询等待审批。
- 数据源列表只包含当前账号有查询权限的库；找不到目标库时告诉用户去 Yearning 申请权限，不要换其他数据源凑数。

## 5. 结果处理与安全

- 结果中的手机号、身份证、银行卡、token、地址等敏感信息，回复时打码（如 `138****1234`）；Yearning 配置的脱敏字段会显示为 `****脱敏字段`。
- 不把生产数据写入代码仓库、提交记录或外部服务；需要落盘时写到会话临时目录或用户指定位置。
- 结论写明：数据源、库、执行的最终 SQL（含自动追加的 LIMIT）、耗时与行数。
- 生产环境查询只在用户明确要求时执行；用户未指定数据源且存在多个同名/相近数据源（测试/生产）时，先列出让用户确认。

## 6. 退出码与常见错误

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 请求/查询失败（SQL 错误、引擎校验不通过、网络问题等，见错误信息） |
| 2 | 配置或鉴权问题（未配置、账号密码错误 1301、token 失效） |
| 3 | 无生效的查询会话（开启审核时需先 apply 并等待批准） |

- `查询失败：...`：多为 Yearning 引擎（Juno）校验未通过（非查询语句、超出限制等）或 MySQL 报错，按提示修正 SQL。
- `rpc调用失败`：Yearning 服务端的 SQL 引擎不可用，告诉用户联系 Yearning 管理员。
- `-v` 打印请求 URL 便于排查。
