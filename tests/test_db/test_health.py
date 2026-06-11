# tests/test_db/test_health.py
"""task#33：调度心跳自检测试（零网络零 launchctl 依赖）。"""
import os
from datetime import datetime, timedelta

from finance_agent.ops.health import build_health_card, check_scheduler_health


def _touch(path, hours_ago, now):
    path.write_text("log")
    ts = (now - timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (ts, ts))


def test_all_healthy_silent(tmp_path):
    now = datetime(2026, 6, 12, 9, 0)
    log = tmp_path / "a.log"
    _touch(log, 0.5, now)
    rules = {"carmen-pricescan": (str(log), 1)}
    assert check_scheduler_health(rules, loaded={"carmen-pricescan"}, now=now) == []


def test_stale_missing_unloaded_detected(tmp_path):
    now = datetime(2026, 6, 12, 9, 0)
    stale = tmp_path / "stale.log"
    _touch(stale, 5, now)                      # 5h > 1h 上限
    rules = {
        "carmen-pricescan": (str(stale), 1),                       # 心跳超时
        "carmen-evening": (str(tmp_path / "nope.log"), 76),        # 无日志
        "carmen-value": (str(stale), 999),                         # 未加载（不看日志）
    }
    probs = check_scheduler_health(
        rules, loaded={"carmen-pricescan", "carmen-evening"}, now=now)
    kinds = {p["job"]: p["kind"] for p in probs}
    assert kinds == {"carmen-pricescan": "心跳超时", "carmen-evening": "无日志",
                     "carmen-value": "未加载"}


def test_weekend_gap_within_limit(tmp_path):
    """周五跑、周一查（~64h）不许误报（上限 76h 就是为周末空窗留的）。"""
    now = datetime(2026, 6, 15, 9, 0)   # 周一
    log = tmp_path / "evening.log"
    _touch(log, 64, now)
    rules = {"carmen-evening": (str(log), 76)}
    assert check_scheduler_health(rules, loaded={"carmen-evening"}, now=now) == []


def test_card_render():
    card = build_health_card([{"job": "carmen-pricescan", "kind": "心跳超时",
                               "detail": "日志 5.0h 未更新（上限 1h）"}])
    assert "调度心跳异常 · 1 项" in card["header"]["title"]["content"]
    blob = str(card["elements"])
    assert "carmen-pricescan" in blob and "launchctl list" in blob
