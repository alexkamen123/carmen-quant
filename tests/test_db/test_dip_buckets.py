# tests/test_db/test_dip_buckets.py
"""order6：dip 规则分级（classify_dip）+ metrics 分桶统计 + 报告渲染测试（零网络）。"""
from finance_agent.db import tracker
from finance_agent.value.metrics import (
    classify_dip, compute_value_metrics,
    DIP_BUCKET_OPPORTUNITY, DIP_BUCKET_BROKEN, DIP_BUCKET_WATCH,
)
from finance_agent.value.report import _dip_section


def test_classify_dip_matrix():
    """判定表穷举（含否定词定位检查回归用例）。"""
    assert classify_dip(0, "随便什么") == DIP_BUCKET_BROKEN
    # 生产唯一行原文：否定前缀紧邻关键词 → 不算命中 → 机会
    assert classify_dip(1, "大盘恐慌，非基本面恶化") == DIP_BUCKET_OPPORTUNITY
    # intact=1 却归因基本面 → 证据矛盾，保守挂起
    assert classify_dip(1, "业绩暴雷") == DIP_BUCKET_WATCH
    # 否定词在句子别处，关键词本身未被否定 → 仍矛盾（定位检查，防全句扫描误放行）
    assert classify_dip(1, "业绩暴雷，并非情绪问题") == DIP_BUCKET_WATCH
    assert classify_dip(1, "大盘恐慌板块联动") == DIP_BUCKET_OPPORTUNITY
    assert classify_dip(1, "") == DIP_BUCKET_OPPORTUNITY      # 空归因，intact 主轴默认机会
    assert classify_dip(None, "大盘恐慌") == DIP_BUCKET_WATCH  # 史前行


def test_dip_buckets_in_metrics(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    rows = [
        ("AAA", 1, "大盘情绪驱动", 19.45),
        ("BBB", 0, "业绩暴雷", -5.0),
        ("CCC", 1, "板块联动", None),     # 未回填行：进 n 不进 filled
    ]
    with tracker._conn(db) as con:
        for t, intact, reason, r7 in rows:
            con.execute(
                "INSERT INTO dip_alerts(ticker, market, drop_pct, price_at_alert, "
                "thesis_intact, drop_reason, return_7d) VALUES(?, 'us', -8.0, 100.0, ?, ?, ?)",
                (t, intact, reason, r7),
            )
    m = compute_value_metrics(db)
    b = m["dip"]["buckets"]
    assert b[DIP_BUCKET_OPPORTUNITY] == {"n": 2, "filled": 1, "up": 1, "avg_ret7": 19.45}
    assert b[DIP_BUCKET_BROKEN] == {"n": 1, "filled": 1, "up": 0, "avg_ret7": -5.0}
    assert DIP_BUCKET_WATCH not in b
    assert all("bucket" in c for c in m["dip"]["cases"])


def test_dip_section_render():
    # n<5：个案列表用 bucket 标签，旧 "—机会" 字样消失
    m_small = {"dip": {"n": 2, "buckets": {}, "cases": [
        {"ticker": "AAA", "bucket": DIP_BUCKET_OPPORTUNITY, "return_7d": 19.45},
        {"ticker": "BBB", "bucket": DIP_BUCKET_BROKEN, "return_7d": None},
    ]}}
    out = _dip_section(m_small)
    assert DIP_BUCKET_OPPORTUNITY in out and "待回填" in out
    assert "—机会" not in out and "机会，" not in out

    # n≥5：每桶一行、空桶不显示、旧聚合措辞消失
    m_big = {"dip": {"n": 6, "cases": [], "buckets": {
        DIP_BUCKET_OPPORTUNITY: {"n": 4, "filled": 3, "up": 2, "avg_ret7": 5.0},
        DIP_BUCKET_BROKEN: {"n": 2, "filled": 2, "up": 0, "avg_ret7": -8.0},
    }}}
    out = _dip_section(m_big)
    assert f"{DIP_BUCKET_OPPORTUNITY} 4 条" in out
    assert f"{DIP_BUCKET_BROKEN} 2 条" in out and "续跌=预警对" in out
    assert DIP_BUCKET_WATCH not in out                  # 空桶无行
    assert "抄底机会事后上涨" not in out                 # 旧措辞替换非叠加

    # 桶 filled==0 → "均待回填"
    m_unfilled = {"dip": {"n": 5, "cases": [], "buckets": {
        DIP_BUCKET_OPPORTUNITY: {"n": 5, "filled": 0, "up": 0, "avg_ret7": None},
    }}}
    assert "均待回填" in _dip_section(m_unfilled)
