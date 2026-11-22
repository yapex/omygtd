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

## 命令

```
gtd add "<标题>" [--due D] [--slot S] [--category C] [--waiting-on W] [--defer D] [--note T] [--dup-ok]
gtd clarify <id> --action "<具体动作>" [--due] [--slot] [--category]
gtd done <id...>          # 支持 gtd done 1 3 4
gtd drop <id...>
gtd edit <id> [--title] [--due] [--slot] [--category] [--note]
gtd show <id>
gtd board --view <today|inbox|categories> --format <png|md|json> [--out PATH]
gtd undo / doctor / backup / init / sync
```

默认输出 JSON。退出码：0 成功（含幂等命中）/ 1 参数错 / 2 找不到记录。

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

## 记的时候必须落实的三件事

1. **时间**：日期 + 时段。说得出就给，说不出就留在 `inbox`（不要硬凑日期）
2. **分类**：按下表判断，判断不出落「其他」
3. **是否等别人**：等别人就必须带日期（那是该催的日子）

**不要问**：要不要记、id 是多少、内部字段是什么。他说记就记。

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
