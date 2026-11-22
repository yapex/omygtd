#!/bin/bash
# MVP-1 端到端证据采集：12 条命令各跑一次，输出原文落盘。
# 用法：bash scripts/evidence_mvp1.sh
set -u
cd /Users/Shared/workspace/gtd || exit 1

OUT="${GTD_EVIDENCE:-$HOME/.hermes/profiles/heli/cache/scratch/evidence_mvp1.txt}"
: > "$OUT"
export GTD_DB="$PWD/data/gtd.db"
export GTD_CAPTURE="$PWD/capture.jsonl"
export GTD_OUT="$PWD/out"

say() { printf '\n$ %s\n' "$*" | tee -a "$OUT"; }

gtd() { uv run python -m gtd "$@"; }

run() {
  say "gtd $*"
  gtd "$@" > "$OUT.cmd" 2>&1
  local code=$?
  cat "$OUT.cmd" | tee -a "$OUT"
  printf '[exit=%s]\n' "$code" | tee -a "$OUT"
}

shell() {
  say "$*"
  bash -c "$*" > "$OUT.cmd" 2>&1
  local code=$?
  cat "$OUT.cmd" | tee -a "$OUT"
  printf '[exit=%s]\n' "$code" | tee -a "$OUT"
}

# 演示数据整体重建（旧 schema 已在 data/gtd.old-20261122.db 备份）
rm -f data/gtd.db data/gtd.db-wal data/gtd.db-shm capture.jsonl "$OUT.cmd"
rm -f gtd/__pycache__/*.pyc tests/__pycache__/*.pyc

echo "=== 0. 环境 ===" | tee -a "$OUT"
shell 'date "+%Y-%m-%d %H:%M:%S %Z"; python3 -V; uv --version'

echo "=== 1. init ===" | tee -a "$OUT"
run init

echo "=== 2. add ×13（含逾期/今天/未来/收件箱/等待/defer）===" | tee -a "$OUT"
run add "回王总邮件" --due 2026-11-21 --category 工作 --slot am
run add "交房租" --due 2026-11-22 --category 家务 --slot before:16:00
run add "给医生打电话" --due 2026-11-22 --category 预约 --slot at:14:00
run add "买鸡蛋" --due 2026-11-23 --category 购物 --slot am
run add "看盘" --due 2026-11-30 --category 投资
run add "读财报" --due 2026-11-30 --category 投资
run add "调仓" --due 2026-11-30 --category 投资
run add "算估值" --due 2026-11-30 --category 投资
run add "等中介回信" --due 2026-11-24 --category 家庭 --waiting-on 中介
run add "修水龙头" --due 2026-11-25 --category 家务
run add "跑 5 公里" --due 2026-11-28 --category 健康
run add "学 Rust" --defer 2026-12-15 --category 学习
run add "随手记的那个东西"

echo "=== 3. clarify / edit ===" | tee -a "$OUT"
run add "想想年会的事"
run clarify 14 --action "写年会流程草稿" --category 工作
run add "记得问物业门禁卡"
run add "研究下那个新软件"
run edit 4 --note "买两斤，别买散装的"
run edit 3 --status done

echo "=== 4. 幂等实测（同一条 add 连跑两次）===" | tee -a "$OUT"
run add "幂等测试：买牛奶" --due 2026-11-24 --category 购物
run add "幂等测试：买牛奶" --due 2026-11-24 --category 购物

echo "=== 5. done 多 id / drop / undo / show ===" | tee -a "$OUT"
run done 2 10
run drop 11
run undo
run show 3

echo "=== 6. board md（today / inbox / categories / all）===" | tee -a "$OUT"
shell "uv run python -m gtd board --view today --format md | head -30"
run board --view inbox --format md
run board --view categories --format md
run board --view all --format md

echo "=== 7. board json（today，前 20 行）===" | tee -a "$OUT"
shell "uv run python -m gtd board --view today --format json | head -20"
run board --view today --format json

echo "=== 8. 三张 PNG ===" | tee -a "$OUT"
run board --view today --format png
run board --view inbox --format png
run board --view categories --format png
shell 'file out/today-*.png out/inbox-*.png out/categories-*.png'

echo "=== 9. 统计口径核对 ===" | tee -a "$OUT"
shell 'uv run python -m gtd board --view today --format json | grep -m1 "\"count\""'
shell "sqlite3 data/gtd.db \"select count(*) as sql_today_count from tasks where status not in ('done','dropped') and due <= date('now','localtime')\""

echo "=== 10. doctor / backup / sync ===" | tee -a "$OUT"
run doctor
run backup
run backup
run sync

echo "=== 11. schema 全文 ===" | tee -a "$OUT"
shell 'sqlite3 data/gtd.db .schema'

echo "=== 12. 全量 pytest ===" | tee -a "$OUT"
shell 'uv run pytest -q 2>&1 | tail -4'

echo "=== 13. 卫生自查 grep（老板点名的四条反例）===" | tee -a "$OUT"
shell 'rm -f gtd/__pycache__/*.pyc tests/__pycache__/*.pyc; grep -rn "gtd_ref\|due_time\|project" gtd/ tests/ --include="*.py" || echo "0 命中（旧字段已清零）"'
shell 'grep -n "print(" gtd/store.py | grep -v "fingerprint(" || echo "0 命中（store.py 无 print）"'
shell 'grep -n "sqlite3" gtd/cli.py gtd/render.py | grep -v "不 import sqlite3" || echo "0 命中（cli/render 不碰 sqlite3）"'
shell 'grep -n "SELECT\|select " gtd/render.py || echo "0 命中（render 无 SQL）"'
shell 'grep -n "if .*format.*==\|if .*fmt.*==" gtd/cli.py || echo "0 命中（cli 无格式分支）"'
shell 'grep -c "" capture.jsonl'

echo "=== 14. 收尾状态 ===" | tee -a "$OUT"
shell 'sqlite3 data/gtd.db "select id,status,category,due,slot,waiting_on,defer,capture_id from tasks order by id"'
shell 'sqlite3 data/gtd.db "select day,count(*) from daily_seq group by day"'
shell 'ls -l out/*.png data/gtd.db data/backup/ capture.jsonl data/snapshots/'
echo "证据已写入 $OUT"
