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
    assert classify_dip(None, "大盘恐慌") == DIP_BUCKET_WATCH  # 史前行/模型未判


def test_classify_dip_negation_regressions():
    """对抗审查回归：重叠关键词屏蔽 + 复合否定词。"""
    # 长词'业绩暴雷'被'并非'否定后整段屏蔽，内含短词'暴雷'不得独立误命中
    assert classify_dip(1, "大盘恐慌，并非业绩暴雷") == DIP_BUCKET_OPPORTUNITY
    assert classify_dip(1, "不存在业绩暴雷") == DIP_BUCKET_OPPORTUNITY
    assert classify_dip(1, "排除业绩爆雷可能") == DIP_BUCKET_OPPORTUNITY
    # 复合否定（4 字 endswith）
    assert classify_dip(1, "未见明显基本面恶化") == DIP_BUCKET_OPPORTUNITY
    assert classify_dip(1, "没有明显业绩恶化") == DIP_BUCKET_OPPORTUNITY
    assert classify_dip(1, "看不到指引下调") == DIP_BUCKET_OPPORTUNITY
    # 关键词真实未被否定时仍须命中（屏蔽逻辑不许放跑真矛盾）
    assert classify_dip(1, "业绩暴雷，并非情绪问题") == DIP_BUCKET_WATCH
    assert classify_dip(1, "既有板块联动也有业绩恶化") == DIP_BUCKET_WATCH


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
    assert DIP_BUCKET_OPPORTUNITY in out and "还没满7天" in out
    assert "—机会" not in out and "机会，" not in out

    # n≥5：每桶一行、空桶不显示、旧聚合措辞消失
    m_big = {"dip": {"n": 6, "cases": [], "buckets": {
        DIP_BUCKET_OPPORTUNITY: {"n": 4, "filled": 3, "up": 2, "avg_ret7": 5.0},
        DIP_BUCKET_BROKEN: {"n": 2, "filled": 2, "up": 0, "avg_ret7": -8.0},
    }}}
    out = _dip_section(m_big)
    assert f"{DIP_BUCKET_OPPORTUNITY}（跌了但逻辑没破，可能是机会） 4 次" in out
    assert f"{DIP_BUCKET_BROKEN}（公司基本面真出问题了） 2 次" in out and "继续跌=我们预警对了" in out
    assert DIP_BUCKET_WATCH not in out                  # 空桶无行
    assert "抄底机会事后上涨" not in out                 # 旧措辞替换非叠加

    # 桶 filled==0 → "暂都没满 7 天"
    m_unfilled = {"dip": {"n": 5, "cases": [], "buckets": {
        DIP_BUCKET_OPPORTUNITY: {"n": 5, "filled": 0, "up": 0, "avg_ret7": None},
    }}}
    assert "暂都没满 7 天" in _dip_section(m_unfilled)
