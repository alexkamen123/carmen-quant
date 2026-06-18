# tests/test_value/test_evolution_log.py
"""evolution_log 组件测试（零网络、零外部依赖）。"""
import csv

from finance_agent.value.evolution_log import HEADER, log_evolution


def _rows(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.reader(f))


def test_creates_header_on_first_write(tmp_path):
    p = tmp_path / "logs" / "evolution.csv"
    rid = log_evolution(str(p), "finance-agent", "sharpe", 1.42, param_version="v3")
    rows = _rows(p)
    assert rows[0] == HEADER
    assert rows[1][1] == "finance-agent"
    assert rows[1][3] == "sharpe"
    assert rows[1][4] == "1.42"
    assert rows[1][5] == "v3"
    # run_id 留空 → 回退为 ts（行内第 3 列与返回值一致）
    assert rows[1][2] == rid


def test_append_does_not_duplicate_header(tmp_path):
    p = tmp_path / "evolution.csv"
    rid = log_evolution(str(p), "finance-agent", "sharpe", 1.0, param_version="v1")
    log_evolution(str(p), "finance-agent", "pnl_pct", 3.7, param_version="v1",
                  run_id=rid, note="dip分级")
    rows = _rows(p)
    assert rows[0] == HEADER
    assert len([r for r in rows if r == HEADER]) == 1
    assert len(rows) == 3  # header + 2 metrics
    # 同 run_id 串起两行
    assert rows[1][2] == rows[2][2] == rid


def test_csv_escaping_for_comma_quote_newline(tmp_path):
    p = tmp_path / "evolution.csv"
    log_evolution(str(p), "finance-agent", "note_metric", "v,1",
                  note='含"引号"和\n换行')
    rows = _rows(p)  # csv.reader 正确还原转义
    assert rows[1][4] == "v,1"
    assert rows[1][6] == '含"引号"和\n换行'


def test_makes_parent_dir(tmp_path):
    p = tmp_path / "a" / "b" / "c" / "evolution.csv"
    log_evolution(str(p), "finance-agent", "x", 1)
    assert p.exists()


def test_none_value_serializes_empty_via_str(tmp_path):
    p = tmp_path / "evolution.csv"
    log_evolution(str(p), "finance-agent", "win_rate", None, param_version="v0")
    rows = _rows(p)
    # None → csv 写成空串（compute_value_metrics 无样本时 win_rate=None 的真实场景）
    assert rows[1][4] == ""
