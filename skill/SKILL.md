---
name: gtd
description: 记待办、看今日、勾完成。触发「记一下」「今天做什么」「看收件箱」。
version: 2.0.0
metadata:
  hermes:
    tags: [gtd, todo, personal]
---

# GTD

本地 GTD 壳 + macOS 提醒事项。**Reminder 是主数据源**（提醒、iCloud 同步、多设备通知全交给系统），本地 SQLite 只存三样可重建的东西：收件箱、图上编号、只读镜像。

唯一权威设计见 `~/workspace/gtd/DESIGN.md`。仓库 `github.com/yapex/omygtd`：测试绿即直接 commit + push。
`~/.hermes/skills/gtd` 是指向仓库 `skill/` 的软链——**改 skill 就是改代码，要 commit**。

## 唯一入口（硬规则）

**所有 Reminder 操作唯一入口 = `gtd` CLI。任何会话、任何子代理都不许直接调 `remindctl`。**

| 要做的事 | 用 |
|---|---|
| 完成某条 | `gtd done <图上编号>` |
| 新增 | `gtd add "<原话>"` |
| 查看 | `gtd view` |
| 体检 | `gtd doctor` |

直调会绕过 `board_index`（图上编号）、幂等检查、镜像更新，事后无法追溯「2 号是哪条」。
真实事故：子代理收到「二已经确认了」，直接 `remindctl complete`，结果对但整条链路无据可查。

secretary-gate 已加拦截：`remindctl add/edit/complete/delete` 一律拒绝，只读（`list`/`show`/`status`）放行。

## 命令（v2 实测接口）

```
gtd [--db PATH] [--now WHEN] [--human] <subcommand>

gtd add "<标题>" [--note T]      # 有时间进 Reminder，没时间进收件箱
gtd inbox                        # 收件箱里还没迁出的念头
gtd clarify <N> "<文字>"          # 给收件箱念头补时间；N 是 view / inbox 上的号
gtd view [--format {md,json}]    # 全库视图，md 是默认
gtd board [--out PATH]           # 出 PNG：board-YYYYMMDD-HHMMSS.png + today-YYYYMMDD.png
gtd done <N...>                  # 按图上编号完成，可多个
gtd list                         # Reminder 各列表条数
gtd doctor                       # 体检：收件箱积压 / Reminder 可用 / 镜像漂移
```

- 全局参数 `--human` 必须在子命令**前**：`gtd --human list`
- 默认输出 JSON；退出码 0 成功 / 1 参数错 / 2 找不到记录
- `--now "2026-11-23 09:00"` 钉住时刻（测试用）
- `add` 幂等：内容指纹 + **2 分钟**窗口，不要传 key

**v1 写法已全部失效**：`--due` / `--slot` / `--category` / `--waiting-on` / `--defer` / `board --view` / `board --format` / `edit` / `show` / `undo` / `backup` / `sync`。传了直接 `unrecognized arguments`（退出码 2）。

## 时间怎么落

由 `parser` 从原话自动解析，**不需要也不接受手动指定**：

| 你说 | 存成 | 手机上 |
|---|---|---|
| 「明天 买菜」 | due 全天，无闹钟 | 整天挂锁屏，不响 |
| 「明天上午 交材料」 | due 全天 + 12:00 闹钟 | 整天挂着，中午响一次 |
| 「明天下午 开会」 | due 全天 + 18:00 闹钟 | 整天挂着，18:00 响 |
| 「明天中午前 交材料」 | 精确 12:00 | 到点响 |
| 「今晚 10 点半 卖股票」 | 精确 22:30 | 到点响 |
| 「买鸡蛋」（无时间词） | 进收件箱 | 不提醒，等澄清 |

差别在「什么时候办都行」（全天）还是「有个截止点」（精确时刻）。

**解析器细节**：时间词之间可以有空格（「今天晚上 10 点半」和「今天晚上10点半」都对）；只剥离**句首连续的时间前缀**，句中出现的钟点不会抢时刻（「明天中午前交材料，顺便看看 3 点的会」→ 12:00，不是 15:00）。

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

## 失败时怎么说

必须说清三件事：

1. **没记上**（不要含糊说「有点问题」）
2. **失败原因**（解析不出时间 / Reminder 不可用 / 库写失败）
3. **原话复述**，并问要不要再试

**不许**在没确认写入成功的情况下说「已记录」。

## 三个已验证的坑

**Reminder 存 UTC**：`dueDate` 是 `2026-11-23T14:30:00Z` 这种，读的时候必须转本地。犯过——图上把 22:30 显示成 14:30，老板会以为还早。写侧送本地墙钟是对的，错只在读侧。

**紧急响铃 EventKit 不暴露**：Reminders.app 界面有 Urgent 开关，但 EventKit 没这个属性。要开只能手动进 app。

**子任务未验证**：Reminders.app 界面支持子任务，但 EventKit 是否暴露父子关系**没查清**（调研被取消了）。目前一条主任务带多个子项时，记成独立的多条提醒。
