# tests/test_db/test_dip_honesty.py
"""暴跌预警诚实认账：不许用「不算分」给自己开脱。

用户反馈（北极星「不许粉饰」）：「待观察」桶 7/7 事后上涨 +33%，却标「不算分」=避重就轻。
诚实标准：
1) 没敢判却事后多数上涨 = 我们**漏喊的机会**（该买没敢买），必须认账、拆清来源；
2) 没敢判却事后多数下跌 = 观望躲过了，不冤枉自己；
3) 没喊「快跑」的几类若已满7天全涨 → 可能只是普涨行情，**别给预警邀功**；
4) 真正考验预警的「基本面破裂·快跑」没满7天 → 诚实说待揭晓。
"""
from finance_agent.value.report import _dip_section
from finance_agent.value.metrics import (
    DIP_BUCKET_OPPORTUNITY, DIP_BUCKET_BROKEN, DIP_BUCKET_WATCH,
)


def test_watch_majority_up_owned_as_missed_opportunity():
    m = {"dip": {"n": 20, "cases": [
        *[{"ticker": f"W{i}", "bucket": DIP_BUCKET_WATCH, "thesis_intact": None,
           "return_7d": 30.0} for i in range(5)],     # 早期没上线判断
        *[{"ticker": f"C{i}", "bucket": DIP_BUCKET_WATCH, "thesis_intact": 1,
           "return_7d": 30.0} for i in range(2)],      # 证据打架
    ], "buckets": {
        DIP_BUCKET_OPPORTUNITY: {"n": 11, "filled": 11, "up": 11, "avg_ret7": 35.2},
        DIP_BUCKET_BROKEN: {"n": 2, "filled": 0, "up": 0, "avg_ret7": None},
        DIP_BUCKET_WATCH: {"n": 7, "filled": 7, "up": 7, "avg_ret7": 33.0},
    }}}
    out = _dip_section(m)
    assert "不算分" not in out                                   # 禁止洗白
    assert "漏喊的机会" in out                                   # 认账
    assert "早期没上线判断 5 次" in out and "证据打架没敢判 2 次" in out  # 拆来源
    assert "别急着邀功" in out and "18/18" in out                # 自我泼冷水


def test_watch_majority_down_is_good_avoidance():
    m = {"dip": {"n": 6, "cases": [
        {"ticker": "W1", "bucket": DIP_BUCKET_WATCH, "thesis_intact": None, "return_7d": -10.0},
    ], "buckets": {
        DIP_BUCKET_WATCH: {"n": 5, "filled": 4, "up": 1, "avg_ret7": -8.0},
    }}}
    out = _dip_section(m)
    assert "漏喊的机会" not in out
    assert "躲过" in out or "没踩雷" in out


def test_broken_unfilled_says_pending():
    m = {"dip": {"n": 6, "cases": [], "buckets": {
        DIP_BUCKET_BROKEN: {"n": 2, "filled": 0, "up": 0, "avg_ret7": None},
        DIP_BUCKET_OPPORTUNITY: {"n": 4, "filled": 4, "up": 4, "avg_ret7": 5.0},
    }}}
    out = _dip_section(m)
    assert "待揭晓" in out
