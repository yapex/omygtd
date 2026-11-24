# GTD 系统 · 接口契约 v1

> 定稿日期：2026-11-22。本文件是唯一权威设计源，代码与 skill 均以它为准。

## 一、目标

单人 GTD 系统。用户通过对话（文字）操作，Agent 调 CLI 执行。图片用于「看」，Markdown/JSON 用于「改」和程序对接。

## 二、不做（一期明确排除）

- 日历 / 提醒同步（二期）
- 推送机制（留接口，不实现）
- cron / launchd 常驻
- 多设备访问（手机只通过 Agent）
- 项目 / 目标聚合
- someday 状态
- 独立回顾功能
- 等待独立视图

## 三、架构分层

```
gtd/
├── store.py      数据层：SQLite 读写、约束校验、幂等判定
├── render.py     渲染层：把统一条目流渲染成 png / md / json
├── commands.py   命令层：CLI 参数解析与调度
└── cli.py        入口
```

**关键：渲染层接收「统一条目流」，不直接查库。**

条目结构：`{kind: "task"|"event", id, title, slot, category, tags[], ...}`

一期 `kind` 只有 `task`。二期接日历后，`event` 从日历读取、只显示不可勾，渲染层不需要改。

`sync` 层二期新增，不改 store / render / commands。

## 四、数据模型

### tasks

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 全局 id |
| title | TEXT NOT NULL | 任务本身 |
| capture_id | TEXT | 指向 capture.jsonl 的行 id |
| status | TEXT | inbox / next / done / dropped |
| category | TEXT | 八大类 + 其他，默认「其他」 |
| due | TEXT | YYYY-MM-DD 可空 |
| slot | TEXT | all / am / pm / before:HH:MM / at:HH:MM，默认 all |
| waiting_on | TEXT | 等谁，可空 |
| defer | TEXT | 开始考虑日，可空 |
| note | TEXT | 备注 |
| idem_key | TEXT | 内容指纹，CLI 内部生成 |
| created_at / updated_at / completed_at | TEXT | ISO |
| **external_id** | TEXT | 【二期预留】EventKit identifier |
| **external_kind** | TEXT | 【二期预留】calendar / reminder |
| **sync_state** | TEXT | 【二期预留】none / pending / synced / failed，默认 none |
| **last_synced_at** | TEXT | 【二期预留】 |
| reminder_id | TEXT | 已同步到提醒事项的 EventKit id（UUID 字符串）；NULL = 未同步 |
| reminder_synced_at | TEXT | 同步成功的时刻（ISO，本地时区） |

二期四个预留字段一期建表时就存在，一期代码不读不写。

### 分类词表（封闭，不可自行增删）

```python
CATEGORIES = ("购物", "投资", "家庭", "健康", "家务", "学习", "工作", "预约", "其他")
DEFAULT_CATEGORY = "其他"
```

判定不出分类时一律落「其他」，不允许编新词。要新增分类必须经用户明确同意并同步改这里。

### meta
key / value。存 schema_version。

### daily_seq
day / seq / task_id。UNIQUE(day, seq)、UNIQUE(day, task_id)。

### tasks_archive
结构与 tasks 相同。done 超过 90 天的记录移入。

## 五、三条硬约束（写入时校验，违反即拒绝）

1. status = next → due 或 defer 至少一个非空
2. waiting_on 非空 → due 必须非空
3. status = inbox → due / defer 均可空

## 六、CLI

```
gtd add "<标题>" [--due D] [--slot S] [--category C] [--waiting-on W] [--defer D] [--note T] [--dup-ok]
gtd clarify <id> --action "<具体动作>" [--due] [--slot] [--category]
gtd done <id...>
gtd drop <id...>
gtd edit <id> [--title] [--due] [--slot] [--category] [--note]
gtd show <id>
gtd board --view <today|inbox|categories|all> --format <png|md|json> --out <path>
gtd undo
gtd doctor
gtd backup
gtd init
gtd sync          # 一期空桩：打印「二期未实现」并 exit 0
```

**没有 `gtd list`**，查询走 `board --view all --format md|json`。

**`edit` 不允许改 status**，状态只能通过 clarify / done / drop 变更。

**序号解析**：`done 1 3 4` 按「今日」daily_seq 解析。跨天用 `--day YYYY-MM-DD` 显式指定。

## 六之二、三个 PNG 视图的职责

三个视图不是「同一份数据的不同排版」，是三种不同的动作。**模板必须分开实现。**

### today · 执行

- 顶部三格：`逾期 → 今日完成 → 收件箱 · 最老 N 天`（**顺序固定**，逾期数字红色）
- 无大标题，顶栏只有 `GTD 每日看板` + 日期时刻
- 区块顺序：今日 → 逾期 → 下一步（按分类分组）→ 收件箱
- **今日与逾期不折叠**；下一步每分类 2 条、收件箱 2 条，超出用折叠行
- 折叠行格式：`投资共 4 条，这里 2 条 · 说「看投资」给全部`（必须带分类名）

### inbox · 澄清

- 顶部：`N 条待澄清 · 最老 X 天` + 例句 `说「1 归到购物」「2、3 删掉」就能处理。`
- **不要 today 的三格**
- **全量列，一条不折**
- 每条带从 1 开始的连续编号
- 每条右侧标「记了 N 天」，超过 3 天标红

### categories · 归类

- 顶部：`N 条还没归类` + 例句 `说「1、5、7 归到购物」就能批量归。归错了说「3 改到家庭」。`
- **不要 today 的三格**
- **编号跨分类连续 1..N**，不在每个分类里从 1 重置（否则「3、11、15 都归购物」说不通）
- 「其他」区**排最前**，浅米底 + 细边框框出，数量标红
- **不折叠**
- 其余分类按 `CATEGORIES` 顺序排列

## 六之三、视觉规范

版式是逐轮确认过的结果，不是自由发挥的空间。

| 项 | 值 |
|---|---|
| 画布宽 | 1080 CSS px，截图 @2x |
| 底色 | `#f7f5f0` 米白 |
| 主文字 | `#1b1815` 暖黑 |
| 强调 | `#123b52` 墨蓝（标题、数字） |
| 警示 | `#b4472e` 砖红（逾期、时间已过） |
| 等待标签 | `#41627c` 蓝（`等对方`） |
| 次级文字 | `#8a8078` |
| 序号 | `#c3b9ae` 灰，**只负责指认，不承担任何状态** |
| 区块分隔 | `1px solid #eae5dc`，**不要卡片框、不要圆角、不要阴影** |
| 标签 | 浅底小圆角，字号 20 |

**三条硬规则**：

1. **序号永远是灰的**。状态全部由标签承载——红＝过期未完成，灰＝正常，蓝＝等对方。
2. **红色只用在需要立刻决定的地方**：顶部逾期数、已过期的时间标签、逾期标签。不许拿来装饰。
3. **无大标题**。today 顶部直接是三格，inbox/categories 顶部是数字+例句。

## 七、输出契约

写操作统一返回 JSON：

```json
{"ok": true, "id": 47, "duplicate": false,
 "changed": {"title": "买鸡蛋", "category": "购物", "due": "2026-11-23"},
 "stats": {"overdue": 3, "done_today": 2, "inbox": 5,
           "inbox_oldest_days": 5, "uncategorized_ratio": 0.12,
           "write_failures": 0}}
```

- `stats` 供调用方判断要不要推图，不需要二次查询
- `uncategorized_ratio` = 分类为「其他」的未完成条目 / 全部未完成条目
- `write_failures` = 当日写库失败累计次数

Markdown：带 `#`（当日序号）与 `id`（真实 id）两列，**不折叠，全量**。

PNG：只有 today / inbox / categories 三张，折叠生效。

退出码：0 成功（含幂等命中）/ 1 业务失败（号不对、已完成、写失败）/ 2 参数错（时间词听不出、参数不合法）。

## 八、时间规则

| slot | 含义 | 今天显示 | 非今天显示 |
|---|---|---|---|
| all | 整天 | 今天 | 12/6 |
| am | 12:00 前 | 上午 | 12/6 上午 |
| pm | 18:00 前 | 下午 | 12/6 下午 |
| before:HH:MM | 该时刻前开始都行 | 16:00 前 | 12/6 16:00 前 |
| at:HH:MM | 该时刻准点开始 | 14:00 | 12/6 14:00 |

- 「前」表示时间窗口（之前开始都行），不带「前」表示准点
- 今天/明天/后天用相对词（±2 天），更远用日期
- 过期判定按「生成时刻」求值，过了点未完成 → 标签变红

## 九、幂等

- 指纹 = sha256(title|due|slot|category|status)[:16]，CLI 内部生成
- 120 秒内同指纹 → 返回原 id，duplicate: true，退出码 0
- 超窗口可新建；`--dup-ok` 跳过检查
- `done` 重复执行 → completed_at 保持首次值
- `board` 同日多次 → 覆盖同名文件
- `init` 重复执行安全

## 十、序号与折叠

- 序号当天首次分配后锁死，状态变化不换号，次日作废
- 断号不补、不复用、不参与排序
- 排序：有时刻按时间，无时刻按拿号顺序
- 完成项沉到当日区底部一行，号保留
- 折叠只出现在 PNG：下一步每分类 2 条、收件箱 2 条
- 折叠行格式：`投资共 4 条，这里 2 条 · 说「看投资」给全部`

## 十一、捕获与防丢

`add` 执行顺序固定：

1. 先 append 一行到 `capture.jsonl`（含时间戳、原始输入、生成的行 id）
2. 再解析入库
3. 入库失败 → 原话仍在日志里，可人工重放

`capture.jsonl` 需要轮转：超过 10 MB 时改名为 `capture-YYYYMM.jsonl`。

`gtd backup`：有变化的当天，`sqlite3 .backup` 出 `data/backup/gtd-YYYYMMDD.db`，同时导出 `snapshots/YYYYMMDD.md`。

## 十二、错误处理与调用协议

CLI 内部对写库失败**重试两次**（间隔 200ms / 500ms），两次都失败则返回 `{"ok": false, "error": "...", "capture_id": "..."}`，退出码 1（业务失败；2 只留给参数错）。

Agent 侧失败时必须在对话里说清三件事：

1. 没记上
2. 失败原因
3. 原话已留在 capture.jsonl，并复述原话

示例：
> 没记上。重试两次都失败（database is locked）。你的原话我留着：「买鸡蛋」。要我过会儿再存一遍吗？

**并发**：SQLite 必须开 WAL 模式，避免对话与备份同时写时 SQLITE_BUSY。

## 十三、二期接口预留

二期接 Mac 日历与提醒（EventKit），同步到 iPhone。一期预留：

1. 表内四个 external_* / sync_* 字段
2. `gtd sync` 空桩命令
3. 渲染层 `kind` 字段，支持 `event` 类型条目（只显示不可勾）
4. `gtd://task/{id}` 作为同步标识，写入事件的 notes 字段
5. 冲突仲裁走 `updated_at` 与 EventKit `lastModifiedDate` 对比
6. TCC 权限必须由用户在**自己开的终端**里授权一次；生产形态为签名 .app + helper，不用裸 CLI（已在探针卡 t_4dbc9dda 证实）

## 十四、验收

- 12 条命令每条跑出真实输出
- 三条硬约束各测一次违反，必须被拒绝
- 幂等 7 条各跑两遍，断言一致
- 三张 PNG 生成并目视确认
- md / json 与 png 同源同数
- doctor 对人为写坏的记录能报出来
- capture.jsonl 在 add 成功前后行数变化可验证

## 十五、库与环境

GTD 已上线，`data/gtd.db` 里是真实数据。开发/测试与正式数据**物理隔离**，靠三层：

### 15.1 三种库

| 库 | 位置 | 用途 | journal_mode |
|---|---|---|---|
| 内存库 | `:memory:` | 单元测试（store / render / cli） | `memory`（SQLite 不支持内存 WAL，这是正确行为） |
| 测试库 | `data/test/gtd.db` | 集成 / 端到端测试（需真实文件、跨进程） | `wal` |
| 正式库 | `data/gtd.db` | 真实使用 | `wal` |

- 内存库每次 `Store(":memory:")` 都是全新的空库，零残留。`capture_path` 仍必须指 `tmp_path`，否则测试原话会写进真实 `capture.jsonl`。
- 测试库文件名固定、可随时清空，已在 `.gitignore` 忽略；夹具在每个用例开始前清表。测试库的 capture 落 `data/test/capture.jsonl`，与正式 `capture.jsonl` 分开。
- 正式库只由真实使用写入；测试永不连接（见 15.3）。

### 15.2 库路径优先级

`--db <path>` > 环境变量 `GTD_DB` > 默认正式库 `data/gtd.db`。

- `prod_db_path()`：正式库位置，**固定**，不受 `GTD_DB` 影响（守卫比的就是它）。
- `default_db_path()`：`GTD_DB` 或 `prod_db_path()`。
- `integration_db_path()`：`GTD_TEST_DB` 或 `data/test/gtd.db`（名字不叫 `test_*`，免得被 import 进测试模块后 pytest 当用例收集）。

### 15.3 正式库硬守卫

`Store.__init__` 末尾检查：只要满足三条，直接 `RuntimeError`，不建连接、不建目录、不写任何东西。

1. 环境变量 `PYTEST_CURRENT_TEST` 存在（即处于 pytest 用例中）
2. 不是内存库
3. 解析后的库路径 == `prod_db_path()`

比的是**固定**的正式库路径而非 `default_db_path()`，所以：`GTD_DB` 指到测试库不会误判，`GTD_DB` 指到正式库也照样拦下 —— 这道闸关不掉。

### 15.4 库身份 `db_kind`

`kind_of(path)` / `Store.db_kind()` 返回 `prod` / `test` / `memory`，`gtd doctor` 输出里带 `db`（绝对路径）与 `db_kind`：

```json
{"db": "/Users/Shared/workspace/gtd/data/gtd.db", "db_kind": "prod", "journal_mode": "wal", ...}
```

### 15.5 测试分层约定

- 单测 → `Store(":memory:", tmp_path / "capture.jsonl")`。
- 集成测试 → `data/test/gtd.db`（`GTD_TEST_DB` + `GTD_DB` 同时指向它，capture 落同目录）。
- 命令层测试用 `tmp_path` 下的文件库：一个用例里要跨多次 `cli.main()` 调用共享状态，内存库做不到。
- WAL 断言按库类型分支：内存库断言 `memory`，文件库断言 `wal`。
- 任何测试都**不许**把 `GTD_CAPTURE` 留空 —— 留空就落到真实 `capture.jsonl`，等于污染原话。

## 十六、周期任务

### 16.1 规则：从「今天」起算

拼多多领券这类事，商家按固定节奏刷新，跟你什么时候点完成无关。所以：

**下一个到期日 = 点完成的那天（今天）+ N 天**，不是「原到期日 + N 天」。

- 例：原 due `2026-11-20`、周期 5 天，今天 `2026-11-22` 点完成 → 下一个 due = `2026-11-27`（不是 `11-25`）。
- 只做「每 N 天」一种节奏，不做每周/每月/cron 表达式，也不建任何 cron。

### 16.2 数据模型（schema_version 2）

`tasks` 与 `tasks_archive` 各加两列：

| 列 | 类型 | 含义 |
|---|---|---|
| `repeat_days` | INTEGER，可空 | NULL = 一次性；有效范围 1..365 |
| `done_count` | INTEGER NOT NULL DEFAULT 0 | 累计完成次数；一次性任务恒为 0 |

迁移写在 `Store.init()` 的 `_migrate()` 里：先 `pragma table_info` 查列，缺哪列才 `alter table ... add column`，所以重复跑安全；`meta.schema_version` 从 `INSERT OR IGNORE` 改成 upsert，旧库从 `1` 升到 `2`，数据原样保留。任何 gtd 命令都走 `init()`，所以旧库被任意命令碰到的第一次就自动升级。

### 16.3 `done` 的分叉

| | 一次性任务 | 周期任务（`repeat_days` 非空） |
|---|---|---|
| status | `done` | 保持 `next` |
| due | 不动 | `today + repeat_days` |
| defer | 不动 | 清空 |
| completed_at | 首次完成时刻（重复点不动） | 每次都写本次完成时刻 |
| done_count | 0 | +1 |

- 同一天对同一条周期任务连点 `done`：`completed_at` 已是今天 → 视为重复，`duplicate: true`，不滚 due、不加 `done_count`（防手滑把 5 天滚成 10 天）。
- 完成后 JSON 多一段（一次性任务没有这个字段）：

```json
{"ok": true, "id": 12, "repeat": {"days": 5, "next_due": "2026-11-27", "done_count": 3}}
```

批量 `done` 里有多条周期任务时字段叫 `repeats`（数组）。

### 16.4 `add` / `edit`

- `gtd add "领券" --due 2026-11-22 --repeat-days 5`：周期任务**必须带 due**（第一次到期日），否则 `ParamError`。
- `gtd edit 12 --repeat-days 5`；`--repeat-days 0` 或 `--repeat-days ""` 表示取消周期。
- 1..365 之外（0 / 负数 / 366 / 非整数）一律 `ParamError`。
- `inbox` 条目不能直接变周期：inbox 不许带 due，而周期必须带 due —— 先 clarify。
- 指纹不变：`fingerprint()` 仍只算 `title|due|slot|category|status`，不含 `repeat_days` —— 同一件事改了周期仍算同一条。

### 16.5 `undo`

撤销一次「周期任务的 done」必须同时回滚三样，少一样就是脏数据：`due` 回原值、`done_count` 回原值、`completed_at` 回原值。

实现：`undo_log.payload` 存整行快照 `before`，另附 `prev_due` / `prev_done_count` / `prev_status` / `prev_completed_at` 四个字段便于人工核对；`_apply_undo` 按快照整行还原。

### 16.6 显示

- md：新增 `周期` 列，值 `每 N 天`；一次性任务留空。
- json：条目流带 `repeat_days` 与 `done_count`（`board` 与 `show` 都有）。
- png：周期任务的标签在时间后追加「· 每 N 天」，例 `今天 · 每 5 天`、`逾期 2 天 · 每 5 天`。
- 三视图骨架、折叠口径、序号规则都不动。

### 16.7 体检与已知口径

- `doctor` 新增两条检查：`repeat_without_due`（周期任务缺 due）、`bad_repeat_days`（超出 1..365）。
- 周期任务完成也计入顶部「今日完成」统计（`completed_at` 是今天），但**不会**出现在「✓ N 号已完成」那一行 —— 那行只列 `status='done'` 的条目（周期任务仍停在 `next`）。

## 十七、到点提醒

### 17.1 一句话

`gtd notify` 每 5 分钟被 launchd 叫一次：把该提醒的 `next` 任务按提醒时刻合成一条消息，用 `hermes send` 推到微信；**发成功了才写 `notified_at`**，所以同一条只提醒一次。

### 17.2 时刻表（slot → 提醒时刻）

| slot | 提醒时刻 | 消息标题行 |
|---|---|---|
| `am` | 12:00 | `⏰ 12:00 · 上午结束` |
| `pm` | 18:00 | `⏰ 18:00 · 下午结束` |
| `all` | 22:30 | `⏰ 22:30 · 今天最后一下` |
| `at:HH:MM` / `before:HH:MM` | 当天 HH:MM | `⏰ HH:MM`（无后缀） |

- 没有 `due`、或 slot 解析不出来 → 不产生提醒。
- 只在 `due == 今天` 且 `status='next'` 时提醒；迟到补发窗口 `NOTIFY_GRACE = 2 小时`，超过就不补（半夜不会把白天欠的消息一次轰出来）。

### 17.3 去重

- 一次发送 = 一个提醒时刻下的一组任务；发成功后 `store.mark_notified(ids, at=now)` 写 `tasks.notified_at`。
- 下一轮 `pending_notifications()` 只捞 `notified_at is null` 的，所以「等 5 分钟不重复发」靠的是这个字段，不是日志。
- 改期会作废旧提醒：`edit` 改 due/slot 时 `notified_at` 复位成 null，重新等下一轮。
- 发送失败**不标记**，下一轮自动重试，同时 `notify.log` 记 `FAIL`。

### 17.4 消息长什么样

```
⏰ 12:00 · 上午结束

还没完成 2 件：
1. 交物业费（家务）
2. 给车做保养
```

- 序号是当日序号（`daily_seq.seq`），跟看板一致；分类为「其他」时不显示括号。
- 超过 5 条只列前 5 条 + `…还有 N 件`。
- 没有到点任务就静默：`{"ok": true, "sent": 0}`，不发消息、不写日志。

### 17.5 投递

`hermes send --profile default --to weixin --json <消息>`。`--profile default` 不能省：heli profile 的 home 里没有 `channel_directory.json`，会报「没有平台」。目标可用环境变量 `GTD_NOTIFY_TARGET` 覆盖（如 `feishu:...`）。

### 17.6 launchd

- plist：`~/Library/LaunchAgents/com.yapex.gtd.notify.plist`
- `StartInterval` 300 秒；`RunAtLoad` false —— 加载后 5 分钟才第一次跑，避免一装就骚扰。
- 输出：`data/notify.out` / `data/notify.err`。
- `EnvironmentVariables.PATH` 必须含 `/Users/yapex/.local/bin`（`gtd`、`hermes`）与 `/opt/homebrew/bin`（`uv`，`bin/gtd` 靠它）；launchd 默认 PATH 里两个都没有，缺了就是 `uv: not found`。

安装 / 重装：

```bash
plutil -lint ~/Library/LaunchAgents/com.yapex.gtd.notify.plist
launchctl bootout gui/$(id -u)/com.yapex.gtd.notify 2>/dev/null   # 已加载时先卸
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yapex.gtd.notify.plist
launchctl print gui/$(id -u)/com.yapex.gtd.notify | head -20
```

### 17.7 日志

`data/notify.log`（追加写，不是数据库表；`data/` 已进 `.gitignore`），管道分隔：

```
2026-11-23T12:00:05+08:00 | 12:00 | ids=12,15 | ok | message_id=hermes-weixin-xxxx
2026-11-23T18:00:03+08:00 | 18:00 | ids=18 | FAIL | exit=1 | No profile
```

失败行最后一段是 `stderr` 首行（截断 200 字符）；hermes 把错误塞在 stdout 的 JSON 里时取它的 `error` 字段。

### 17.8 体检

`gtd doctor` 多一项 `notify_24h`：读 `data/notify.log` 统计最近 24 小时的 `{"ok", "fail", "last_run"}`，`last_run` 是最后一次**成功**发送的时刻。

### 17.9 手工验法

```bash
gtd notify --dry-run        # 只打印将要发什么：不发送、不标记、不写日志
gtd notify                  # 真发；没有到点任务则 {"ok": true, "sent": 0}
gtd --db <临时库> notify --dry-run --now 2026-11-22T12:05   # --now 仅测试用，钉死时刻
```

## 十八、提醒事项单向同步

### 18.1 一句话

`next` + 有 `due` 的任务在入库时**尽力**复制一份到「提醒事项」的 `GTD` 列表（`gtd/reminder_bridge.py` 调 `remindctl`），`reminder_id` 非空 = 已交付；同步失败只写 `data/reminder.log`，**绝不**让写库失败。

### 18.2 四个同步点

| 动作 | 条件 | 做什么 |
|---|---|---|
| `add` | 落成 `next` 且 `due` 非空 | 建 Reminder，回写 `reminder_id` / `reminder_synced_at` |
| `clarify`（inbox → next） | 同上 | 同上 |
| `done` | `reminder_id` 非空 | 完成该 Reminder；周期任务滚期后再按新 `due` 建一条 |
| `drop` | `reminder_id` 非空 | 删除该 Reminder |

- **幂等**：`reminder_id` 非空时绝不再建第二条。
- **去重**：`pending_notifications()` 排除 `reminder_id` 非空的记录 —— 交给提醒事项的，`gtd notify` 不再发，否则双重提醒。
- Reminder 的时刻 = `notify_at(due, slot)` 的**本地时刻**（分钟级），`--due` 与 `--alarm` 都设成它。
- 专用列表名 `REMINDER_LIST = "GTD"`，不污染用户已有列表；`ensure_list()` 负责不存在时创建。
- 硬超时 `REMINDCTL_TIMEOUT = 20` 秒；pytest 环境下 bridge 一律不真调（`PYTEST_CURRENT_TEST` 守卫），要放行须显式 `GTD_REMINDER_ALLOW_TESTS=1`。
- 日志：`data/reminder.log`（`GTD_REMINDER_LOG` 可覆盖），`时刻 | level | message`。

