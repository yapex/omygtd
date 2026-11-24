---
name: gtd
description: 记待办、看今日、勾完成。触发「记一下」「今天做什么」「看收件箱」。
version: 2.2.0
metadata:
  hermes:
    tags: [gtd, todo, personal]
---

# GTD

本地 GTD 壳 + macOS 提醒事项。**Reminder 是主数据源**（提醒、iCloud 同步、多设备通知全交给系统），本地 SQLite 只存三样可重建的东西：收件箱、图上编号、只读镜像。

**分类走列表**：我自己建的列表一律英文（`GTD` / `Waiting`），老板自己建的保持中文（`提醒事项`）——英文名是我建的标记，别把两边命名混成一套。**旧的四状态 / 九分类 / SQLite 账本设计已废**（2026-11-23 转向），别照旧笔记复活它。

唯一权威设计见 `~/workspace/gtd/DESIGN.md`。仓库 `github.com/yapex/omygtd`：测试绿即直接 commit + push。
`~/.hermes/skills/gtd` 是指向仓库 `skill/` 的软链——**改 skill 就是改代码，要 commit**。

## 唯一入口（硬规则）

**所有 Reminder 操作唯一入口 = `gtd` CLI。任何会话、任何子代理都不许直接调 `remindctl`。**

| 要做的事 | 用 |
|---|---|
| 完成某条 | `gtd done <图上编号>` |
| 改期 | `gtd defer <图上编号> --due 2026-12-08` |
| 新增 | `gtd add "<标题>" --due 2026-12-01` |
| 查看 | `gtd view` |
| 体检 | `gtd doctor` |

直调会绕过 `board_index`（图上编号）、幂等检查、镜像更新，事后无法追溯「2 号是哪条」。
真实事故：子代理收到「二已经确认了」，直接 `remindctl complete`，结果对但整条链路无据可查。

secretary-gate 已加拦截：`remindctl add/edit/complete/delete` 一律拒绝，只读（`list`/`show`/`status`）放行。

## 入参口径（2026-11-24 老板拍板，v2.2）

**归一由调用方（LLM）负责，CLI 不接受口语。** 你说「明天下午三点提醒我」时，
先把话翻译成标准旗标再调 CLI：

```
「明天下午三点」→ --due 2026-11-26 --at 15:00
「下周一」       → --due 2026-11-30
「每周一例会」   → --due 2026-11-30 --repeat weekly
```

`--due` 只收 `YYYY-MM-DD`，`--at` 只收 24 小时制 `HH:MM`。传「明天」「9点」这类口语
直接**退出码 2**（一条都不写），并回一句带正确示例的错误——不猜、不兜底、不做兼容。
理由：用程序文法枚举口语永远追不完，接口只吃标准格式，换算放在调用方。

## 命令（v2.2 实测接口）

```
gtd [--db PATH] [--now WHEN] [--human] <subcommand>

gtd add "<标题>" [--due YYYY-MM-DD] [--at HH:MM | --slot am|pm]
                 [--repeat daily|weekly|monthly] [--repeat-interval N]
                 [--priority none|low|medium|high] [--note T]
gtd inbox                                        # 收件箱里还没迁出的念头
gtd clarify <N> --due YYYY-MM-DD [--at HH:MM | --slot am|pm]
gtd view [--format {md,json}]                    # 全库视图，md 是默认
gtd board [--out PATH]                           # 出 PNG：board-YYYYMMDD-HHMMSS.png + today-YYYYMMDD.png
gtd done <N...>                                  # 按图上编号完成，可多个
gtd defer <N...> --due YYYY-MM-DD [--at HH:MM | --slot am|pm]
gtd priority <N> <none|low|medium|high>
gtd list                                         # Reminder 各列表条数
gtd doctor                                       # 体检：收件箱积压 / Reminder 可用 / 镜像漂移
```

- 全局参数 `--human` 必须在子命令**前**：`gtd --human list`
- 默认输出 JSON；退出码 **0 成功 / 1 业务失败（号不对、已完成、写失败）/ 2 参数错（日期或钟点不合标准格式、旗标组合不合法）**
- `--now "2026-11-23 09:00"` 钉住时刻（测试用）
- `add` 幂等：内容指纹 + **2 分钟**窗口，不要传 key

**旧写法已全部失效**：口语时间词（`gtd add "明天 买菜"` 想让 CLI 自己听懂）、`defer 9 明天`、
`clarify 3 "明天下午"`、`--category` / `--waiting-on` / 旧旗标 `--defer`、`board --view` /
`board --format`、`edit` / `show` / `undo` / `backup` / `sync`。传了直接 `unrecognized arguments`
或退出码 2。改期现在是**子命令** `gtd defer`，不是旗标。

## 时间怎么落（四种形态）

| 传什么 | Reminder 存成 | 手机上 |
|---|---|---|
| 不给 `--due` | 进收件箱，不排期 | 不提醒，等澄清 |
| `--due 2026-12-01` | due 全天，无闹钟 | 整天挂锁屏，不响 |
| `--due 2026-12-01 --slot am` | due 全天 + 12:00 闹钟 | 整天挂着，中午响一次 |
| `--due 2026-12-01 --slot pm` | due 全天 + 18:00 闹钟 | 整天挂着，18:00 响 |
| `--due 2026-12-01 --at 09:30` | due 就是 09:30，到点响 | 到点响 |

`--at` 与 `--slot` **互斥**（同时给 → 退出码 2）；`--at` / `--slot` 必须跟 `--due` 一起给。
差别在「什么时候办都行」（全天）还是「有个截止点」（精确时刻）。

`gtd defer` / `gtd clarify` 用**同一套旗标**，语义完全一致：

```
gtd defer 9 12 --due 2026-12-08 --slot am     # 9、12 号改到 12-08 上午
gtd clarify 3 --due 2026-12-01 --at 09:30     # 收件箱 3 号落成 12-01 09:30
```

`defer` 的退出码：全改成功 → 0；号不对 / 那条已完成 / 收件箱号 / **重复任务** / 写失败 → 1
（`results[].ok=false`，逐条给 `error`）；`--due` 缺失或格式不对 → 2（一条都不改）。

**不说时间就留收件箱**，不要硬凑日期。

## 编号

**图上的号 = `board_index` 表里的号，当天不变。**（老板 2026-11-23 定）

- 当天首次 `view` / `board` 时从 1 分配
- 之后**老号不动**，新条目追加 `max+1`
- 删掉的号**不回收、不填补**
- 跨天重新从 1 分配

**为什么要幂等**：老板看的是那张图/那段文字，他说「2 号完成」时指的就是当时的 2。数据没动，反复调用号一个字都不能变。

`done N` 只认这张表：
- 今天没出过图 → 拒绝，让他先看
- 图上没这个号 → 拒绝
- 那条已完成 → 拒绝

`defer N` 同规则；**改期后号不变**——引用没变，只是日子变了，不用重新看图。
收件箱里的号 `defer` 也拒（念头还没进 Reminder，没有到期时间），改用 `gtd clarify <N> --due ...`
——**单一职责：`defer` 只改已有到期时间，不给念头补时间**。
带重复规则（`recurrenceRule`）的条目 `defer` 也拒：EventKit 的周期以 due 为锚点，改这一条会把整条链的节奏一起挪走，要改节奏去 Reminders.app 改频率。

## 记完回文字（2026-11-23 老板定）

每次 `add` / `clarify` / `done` 成功后：

```bash
gtd view
```

**把 Markdown 直接发给老板，不出图。** 出图要起 Chrome 渲染 2–3 秒；文字秒出。

**只有他明说「看图」「看今天的」时才 `gtd board`。**

## 三个区

`view` 和 `board` 内容一致：

1. **今天** — 今天到期的未完成条目
2. **接下来** — 未来 7 天，最多 5 条，超出显示「还有 N 条」
3. **收件箱** — 还没澄清的念头

空区整区不渲染。逾期条目在标题后标「（逾期 N 天）」。
全天显示 `全天`、上午 `上午`、下午 `下午`、精确时刻 `HH:MM`。

## 优先级（四档，与 Reminders.app 一致）

存 `none | low | medium | high`，视图显示 `!!!` / `!!` / `!` / 无。

**两个入口**：

1. **记的时候传**：`gtd add "做分析" --priority high`（reminder 与收件箱都认）。
   「很重要 / 急」这类说法**不再自动识别**——标题会原样入库，档位只从旗标来。
2. **看视图后改**：`gtd priority <N> <level>`
   - 收件箱条目 → 改本地 `inbox.priority`
   - 今天区 / 接下来区 → 改 Reminder 那条（`remindctl edit --priority`），手机同步变

收件箱按 `priority DESC, created_at ASC` 排；今天区和接下来区按日期排，**优先级只影响标记，不影响那两区的顺序**。

**感叹号方向（老板实测）**：high `!!!`、medium `!!`、low `!`。越重要越多，不要反过来。

## 重复（交给 Reminder 原生，不自研）

`--repeat daily|weekly|monthly` + `--repeat-interval N`（默认 1）→ 传 `remindctl --repeat`。
`--due` 那天就是**锚点**：

```
gtd add "例会" --due 2026-11-30 --repeat weekly                  # 每周一（11-30 是周一）
gtd add "领券" --due 2026-11-28 --repeat daily --repeat-interval 5   # 每 5 天
```

映射：`weekly + 2` → `every 2 weeks` → EventKit `{frequency: weekly, interval: 2}`；
`daily + 5` → `{daily, 5}`。`--repeat` 必须跟 `--due`（锚点不能猜）。

**语义（实测，2026-11-23）**：
- **完成时**才往前滚 +N 天，同时另生成一条已完成快照
- **到期不自动滚** → 某次没打勾，那条就停在那儿，不会补

老板拍板：**先用 Reminder 原生能力，MVP 优先，不过度设计**。以后真发现差异（比如需要「不管做没做都提醒」）再补。

## 失败时怎么说

必须说清三件事：

1. **没记上**（不要含糊说「有点问题」）
2. **失败原因**（日期格式不对 / Reminder 不可用 / 库写失败）
3. **原话复述**，并问要不要再试

**不许**在没确认写入成功的情况下说「已记录」。

## 已验证的坑

**Reminder 存 UTC**：`dueDate` 是 `2026-11-23T14:30:00Z` 这种，读的时候必须转本地。犯过——图上把 22:30 显示成 14:30，老板会以为还早。写侧送本地墙钟是对的，错只在读侧。

**紧急响铃 EventKit 不暴露**：Reminders.app 界面有 Urgent 开关，但 EventKit 没这个属性。要开只能手动进 app。

**子任务未验证**：Reminders.app 界面支持子任务，但 EventKit 是否暴露父子关系**没查清**（调研被取消了）。目前一条主任务带多个子项时，记成独立的多条提醒。

**重复任务不能 defer（2026-11-24 实测）**：`weekly / interval=1` 那条 `gtd defer` 之后 `recurrenceRule` 本身没变（频率、interval 都在），但 `complete` 往前滚的是**改后的 due**——周一 11-30 的任务 defer 到周三 11-25，滚出来是 12-02（周三），原周一锚点丢了（滚动后的 `alarmDate` 还被写成当天 00:00）。所以 `defer` 见到 `recurrenceRule` 直接拒，返回 `ok:false` + 「重复任务暂不支持 defer…请改 Reminder 里的频率」。

**改期必须显式清闹钟**：`remindctl edit --due` 只改 dueDate，**旧闹钟不会自己消失**——把带闹钟的条目改回全天，`alarmDate` 仍停在旧时刻，手机上会在旧时间响。所以 `defer` 不传 `--alarm` 时就发 `--clear-alarm`（2026-11-24 实测：删掉这步会留旧闹钟）。

**`remindctl list` 的计数只算未完成**：要看全量条数（含已完成）用 `--json` 读 `total`，否则「库里到底多少条」会系统性少报（2026-11-23 实测）。`gtd list` 同源，报数前先想清楚问的是哪一种。
