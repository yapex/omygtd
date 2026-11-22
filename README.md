# omygtd

个人 GTD 系统的 CLI 实现。SQLite 作真相源，三种输出格式（PNG / Markdown / JSON），为「用对话管理待办」而设计。

## 设计文档

完整接口契约与设计决策见 [`DESIGN.md`](DESIGN.md)。它不是事后补的文档，是先定契约再写代码的产物。

## 安装

```bash
git clone https://github.com/yapex/omygtd.git ~/workspace/gtd
cd ~/workspace/gtd
./install.sh
```

`install.sh` 做三件事：软链 CLI 到 `~/.local/bin/gtd`、软链 skill 到 `~/.hermes/skills/gtd`、初始化数据库。

容器化环境用 `./install.sh --copy`（容器不认软链）。

## 用法

```bash
gtd add "买鸡蛋" --due 2026-11-23 --slot am --category 购物
gtd done 1 3 4                 # 按今日序号批量完成
gtd board --view today --format md
gtd board --view today --format png --out board.png
```

默认输出 JSON。详见 [`DESIGN.md`](DESIGN.md) 第六节。

## 架构

```
gtd/store.py      数据层：SQLite、约束校验、幂等、序号
gtd/render.py     渲染层：条目流 → png / md / json
gtd/cli.py        命令层：参数解析与调度
```

三层单向依赖：`cli → store`、`cli → render`，`render` 不碰数据库。

## 测试

```bash
uv run pytest -q     # 单测走内存库 :memory:，集成走专用测试库
```

## 状态

MVP-1 完成。二期计划接 macOS 日历与提醒（EventKit）。
