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

退出码：0 成功（含幂等命中）/ 1 参数错 / 2 找不到记录。

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

CLI 内部对写库失败**重试两次**（间隔 200ms / 500ms），两次都失败则返回 `{"ok": false, "error": "...", "capture_id": "..."}`，退出码 2。

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
