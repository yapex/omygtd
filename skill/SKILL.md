---
name: gtd
description: 记待办、看今日、归类、勾完成。触发「记一下」「今天做什么」「看收件箱」。
version: 1.0.0
metadata:
  hermes:
    tags: [gtd, todo, personal]
---

# GTD

唯一权威设计见 `~/workspace/gtd/DESIGN.md`。本文件只写「Agent 怎么用」。

## 项目底座

- 目录 `~/workspace/gtd/`；`DESIGN.md` 是唯一权威设计源，与本文件冲突时以它为准。
- 代码仓 `github.com/yapex/omygtd`：测试绿即直接 commit + push，不等老板批。
- 数据、`out/`、`RUNLOG` 永不入库。
- 状态固定四态 `inbox / next / done / dropped`；分类九个（见下表）；**不设 someday / waiting 区**——「等别人」用带日期的条目表达。
- 二期接 EventKit 写日历/提醒：表已预留 `external_*` / `sync_*` 字段。

## EventKit 坑（二期）

EventKit 经 osascript / python 调用时 TCC 不弹窗（responsible 进程是终端），必须让老板在终端手工授权一次；生产用途要打成签名 .app。

## 命令（v2 实际接口，2026-11-23 用 `gtd add --help` / `gtd board --help` 实测）

```
gtd [--db PATH] [--now WHEN] [--human] <subcommand>

gtd add "<标题>" [--note T]        # 只有 text + --note；没有 --due/--slot/--category
                                  # 有时间进 Reminder、没时间进收件箱（按文本判）
gtd inbox                          # 列收件箱里还没迁出的念头
gtd clarify <id> ...               # 给收件箱念头补时间
gtd board [--out PATH]             # 只出 today 视图 PNG；没有 --view / --format
gtd done <id...>
gtd list                           # Reminder 各列表条数
gtd doctor                         # 体检：收件箱积压 / Reminder 可用 / 镜像漂移
```

**v1 写法已失效**：`--category`/`--due`/`--slot`/`--waiting-on`/`--defer`/`--dup-ok`、`board --view … --format …`
以及 `edit`/`show`/`undo`/`backup`/`sync` 在 v2 里都不存在，传了直接 `error: unrecognized arguments`（退出码 2）。
分类由内部词表自动判（「财报」→ 投资），不需要也不接受手动指定。

全局参数 `--human` 必须写在子命令**前**：`gtd --human add "..."`。

默认输出 JSON。退出码：0 成功（含幂等命中）/ 1 参数错 / 2 找不到记录或参数不认。

## secretary-gate 配额（本机）

执行类工具受 `~/.hermes/agent-hooks/secretary-gate` 管：judge 豁免按动作类扣配额（read 2 / verify 3 / card 不限，TTL 300s），**层 2 每会话累计**用尽后只能 `kanban_create` 派卡重置。教训：一次判态内把所有命令合成**一个** terminal 调用；别用 `--help` 探接口，那会把配额烧在探路上。

## 触发映射

| 用户说 | 做什么 |
|---|---|
| 「记一下 X」「提醒我 X」 | `gtd add` |
| 「今天做什么」「今天有啥」 | `gtd board --view today --format md`，需要看图才加 `--format png` |
| 「看收件箱」 | `gtd board --view inbox --format md` |
| 「看分类」 | `gtd board --view categories --format md` |
| 「1、3、4 完成了」 | `gtd done 1 3 4` |
| 「5 号改到周五」 | `gtd edit 5 --due YYYY-MM-DD` |
| 「刚才记错了」 | `gtd undo` |

## 记的时候必须落实的四件事

1. **时间**：日期 + 时段。说得出就给，说不出就留在 `inbox`（不要硬凑日期）
2. **分类**：按下表判断，判断不出落「其他」
3. **是否等别人**：等别人就必须带日期（那是该催的日子）
4. **记完回图**：写完成功后立刻出**更新后的图片视图**发给老板，不只回一行「记好了」——见下节

**不要问**：要不要记、id 是多少、内部字段是什么。他说记就记。

## 记完回文字（2026-11-23 老板改，原「记完必回图」作废）

每次 `gtd add` / `gtd clarify` / `gtd done` 成功后，**回文字，不出图**。

老板原话：「当我们修改 GTD 之后会返回一个图片，这个动作有点慢，不如直接返回 markdown 文字」

出图要起 Chrome 渲染，2–3 秒；文字秒出，手机上 Markdown 表格也看得清。

**当前做法**（`gtd view` 尚未落地时的过渡）：
- 成功 → 回一行确认：图上编号 / 标题 / 时间 / 所在列表，再补一句全局状态（今天几件、收件箱几条）
- 失败 → 按下面「失败时怎么说」三条讲清

**待落地**：`gtd view` 命令（输出全库 Markdown，编号与 board 图上一致）正在开发。落地后改为跑 `gtd view` 拿全文，直接附在回复里。

**只有老板明说「看图」「看今天的」时才出图**（`gtd board`）。

## 分类判断

| 关键词 | 分类 |
|---|---|
| 买 / 购 / 下单 / 补货 | 购物 |
| 股票 / 财报 / 估值 / 基金 | 投资 |
| 孩子 / 老婆 / 父母 / 学校 / 家长 | 家庭 |
| 医院 / 体检 / 药 / 复查 | 健康 |
| 打扫 / 修 / 装 / 家电 / 水电 | 家务 |
| 读书 / 课程 / 学 / 练习 | 学习 |
| 报告 / 会议 / 客户 / 同事 / 汇报 | 工作 |
| 订位 / 挂号 / 预约 / 机票 / 酒店 | 预约 |
| 判断不出 | 其他 |

词表封闭，不许造新词。

## 时间表达

| slot | 含义 | 显示 |
|---|---|---|
| `all` | 整天 | 今天 / 12/6 |
| `am` | 12:00 前 | 上午 |
| `pm` | 18:00 前 | 下午 |
| `before:HH:MM` | 该时刻前开始都行 | 16:00 前 |
| `at:HH:MM` | 准点开始 | 14:00 |

「前」表示时间窗口，不带「前」表示准点。

## 序号

today 图的序号是**当日**的，只在当天有效。说「1、3、4」时按当天映射解析。跨天要用户明说「昨天 3 号」。

序号只负责指认，不是排序依据；断号不补。

## 出图还是文字

- **看**：`--format png`（折叠，一屏能看完）
- **改**：`--format md` 或 `json`（不折叠，全量）
- **单条**：`gtd show <id>`

## 幂等

CLI 自动按内容指纹 + 120 秒窗口去重，**不要传 key**。返回 `duplicate: true` 说明是重放，正常，不要当失败。

## 失败时怎么说

CLI 内部已重试两次。仍失败时必须说清三件事：

1. 没记上
2. 失败原因
3. 原话已留在 `capture.jsonl`，并复述原话

例：`没记上。重试两次都失败（database is locked）。你的原话我留着：「买鸡蛋」。要我过会儿再存一遍吗？`

**不许**在没确认写入成功的情况下说「已记录」。
