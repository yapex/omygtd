"""阶段 3 · 命令层测试（先写测试，后写实现）。

默认输出 JSON；退出码 0/1/2；done 支持多 id 按当日序号解析。
这里允许出现 sqlite3 —— 用来独立复算统计口径，和被测算的代码无关。
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys

import pytest

from gtd import cli


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def payload(out: str) -> dict:
    return json.loads(out)


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """命令层用临时文件库（不是内存库）：同一个用例里要跨多次 cli.main() 调用共享状态。

    库在 tmp_path 下、每个用例一份，仍然与正式库完全隔离；正式库本身有守卫拦着。
    """
    monkeypatch.setenv("GTD_DB", str(tmp_path / "gtd.db"))
    monkeypatch.setenv("GTD_CAPTURE", str(tmp_path / "capture.jsonl"))
    monkeypatch.setenv("GTD_OUT", str(tmp_path / "out"))
    return tmp_path


def test_add_prints_json_with_contract(db_env, capsys):
    code, out, _ = run(capsys, "add", "买鸡蛋", "--due", "2026-11-23", "--category", "购物")
    data = payload(out)
    assert code == 0 and data["ok"] is True and data["duplicate"] is False
    assert set(data) >= {"ok", "id", "duplicate", "changed", "stats"}
    assert data["changed"]["title"] == "买鸡蛋" and data["changed"]["category"] == "购物"
    assert set(data["stats"]) == {"overdue", "done_today", "inbox", "inbox_oldest_days",
                                  "uncategorized_ratio", "write_failures"}


def test_duplicate_add_returns_same_id(db_env, capsys):
    _, first, _ = run(capsys, "add", "买鸡蛋", "--due", "2026-11-23")
    _, second, _ = run(capsys, "add", "买鸡蛋", "--due", "2026-11-23")
    a, b = payload(first), payload(second)
    assert b["duplicate"] is True and b["id"] == a["id"]


def test_clarify_promotes_to_next(db_env, capsys):
    _, out, _ = run(capsys, "add", "那个东西")
    tid = payload(out)["id"]
    code, out, _ = run(capsys, "clarify", str(tid), "--action", "查一下发票在哪")
    data = payload(out)
    assert code == 0 and data["changed"]["status"] == "next"
    assert data["changed"]["title"] == "查一下发票在哪"
    assert data["changed"]["due"]


def test_done_accepts_multiple_daily_seq(db_env, capsys):
    ids = [payload(run(capsys, "add", f"事{i}", "--due", "2026-11-22")[1])["id"] for i in range(4)]
    code, out, _ = run(capsys, "done", "1", "3", "4")
    data = payload(out)
    assert code == 0 and data["ids"] == [ids[0], ids[2], ids[3]]
    assert data["stats"]["done_today"] == 3


def test_done_twice_is_idempotent(db_env, capsys):
    run(capsys, "add", "只做一次", "--due", "2026-11-22")
    first = payload(run(capsys, "done", "1")[1])
    second = payload(run(capsys, "done", "1")[1])
    assert first["duplicate"] is False and second["duplicate"] is True


def test_drop(db_env, capsys):
    run(capsys, "add", "算了", "--due", "2026-11-22")
    code, out, _ = run(capsys, "drop", "1")
    assert code == 0 and payload(out)["changed"]["status"] == "dropped"


def test_edit_changes_field(db_env, capsys):
    run(capsys, "add", "旧标题", "--due", "2026-11-22")
    code, out, _ = run(capsys, "edit", "1", "--title", "新标题", "--category", "家务")
    assert code == 0 and payload(out)["changed"]["title"] == "新标题"


def test_edit_rejects_status(db_env, capsys):
    run(capsys, "add", "不许改状态", "--due", "2026-11-22")
    code, out, _ = run(capsys, "edit", "1", "--status", "done")
    assert code == 1 and payload(out)["ok"] is False


def test_show_by_seq(db_env, capsys):
    run(capsys, "add", "看看我", "--due", "2026-11-22")
    code, out, _ = run(capsys, "show", "1")
    assert code == 0 and payload(out)["task"]["title"] == "看看我"


def test_missing_record_exit_2(db_env, capsys):
    code, out, _ = run(capsys, "show", "99999")
    assert code == 2 and payload(out)["ok"] is False


def test_bad_argument_exit_1(db_env, capsys):
    code, out, _ = run(capsys, "add", "x", "--category", "生活")
    assert code == 1 and payload(out)["ok"] is False


def test_board_json_count_matches_sql(db_env, capsys):
    run(capsys, "add", "今天到期", "--due", "2026-11-22")
    run(capsys, "add", "逾期项", "--due", "2026-11-01")
    run(capsys, "add", "未来项", "--due", "2026-12-31")
    run(capsys, "add", "随手记")
    code, out, _ = run(capsys, "board", "--view", "today", "--format", "json",
                       "--day", "2026-11-22")
    data = payload(out)
    conn = sqlite3.connect(db_env / "gtd.db")
    sql_n = conn.execute(
        "select count(*) from tasks where status not in ('done','dropped')"
        " and due <= date('2026-11-22')").fetchone()[0]
    conn.close()
    assert code == 0 and data["count"] == sql_n == 2


def test_board_md_not_folded(db_env, capsys):
    for i in range(5):
        run(capsys, "add", f"投资事{i}", "--due", "2026-12-01", "--category", "投资")
    code, out, _ = run(capsys, "board", "--view", "all", "--format", "md", "--day", "2026-11-22")
    rows = [l for l in out.splitlines() if l.startswith("| ") and not l.startswith("| # ")]
    assert code == 0 and len(rows) == 5
    assert "| # | id |" in out and "共 5 条" not in out


def test_board_png_writes_file(db_env, capsys):
    run(capsys, "add", "出图", "--due", "2026-11-22")
    code, out, _ = run(capsys, "board", "--view", "today", "--format", "png")
    data = payload(out)
    assert code == 0 and data["format"] == "png"
    assert data["path"].endswith(".png")


def test_board_bad_view_exit_1(db_env, capsys):
    code, out, _ = run(capsys, "board", "--view", "nope", "--format", "json")
    assert code == 1 and payload(out)["ok"] is False


def test_undo(db_env, capsys):
    run(capsys, "add", "手滑了", "--due", "2026-11-22")
    code, out, _ = run(capsys, "undo")
    assert code == 0 and payload(out)["action"] == "add"
    assert run(capsys, "show", "1")[0] == 2


def test_doctor_reports_health(db_env, capsys):
    run(capsys, "add", "一条", "--due", "2026-11-22")
    code, out, _ = run(capsys, "doctor")
    data = payload(out)
    assert code == 0 and data["ok"] is True and data["problems"] == []


def test_doctor_reports_db_kind(db_env, capsys):
    """doctor 必须报出库身份：prod / test / memory。"""
    code, out, _ = run(capsys, "doctor")
    data = payload(out)
    assert code == 0 and data["db_kind"] == "test"
    assert data["db"] == str(db_env / "gtd.db")


def test_global_db_flag_beats_env(tmp_path, monkeypatch, capsys):
    """优先级：--db > GTD_DB > 默认正式库。"""
    monkeypatch.setenv("GTD_DB", str(tmp_path / "env.db"))
    monkeypatch.setenv("GTD_CAPTURE", str(tmp_path / "capture.jsonl"))
    target = tmp_path / "flag.db"
    code, out, _ = run(capsys, "--db", str(target), "add", "flag 库", "--due", "2026-11-23")
    assert code == 0 and payload(out)["ok"] is True
    assert target.exists()
    assert not (tmp_path / "env.db").exists()


def test_cli_can_use_memory_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GTD_CAPTURE", str(tmp_path / "capture.jsonl"))
    code, out, _ = run(capsys, "--db", ":memory:", "add", "内存库一条")
    data = payload(out)
    assert code == 0 and data["ok"] is True and data["stats"]["inbox"] == 1


def test_backup_and_idempotent_second_run(db_env, capsys):
    run(capsys, "add", "备份我", "--due", "2026-11-22")
    first = payload(run(capsys, "backup")[1])
    second = payload(run(capsys, "backup")[1])
    assert first["performed"] is True
    assert second["performed"] is False


def test_init_is_safe_to_repeat(db_env, capsys):
    assert payload(run(capsys, "init")[1])["ok"] is True
    assert payload(run(capsys, "init")[1])["ok"] is True


def test_sync_is_stub(db_env, capsys):
    code, out, _ = run(capsys, "sync")
    data = payload(out)
    assert code == 0 and "二期未实现" in out


def test_module_entrypoint_works(db_env):
    proc = subprocess.run([sys.executable, "-m", "gtd", "doctor"],
                          capture_output=True, text=True, timeout=120,
                          cwd=str(db_env.parent))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] in (True, False)
