# tests/test_shadow_fold.py
"""日报减冗余：非持仓/影子股(shares=0)收进折叠面板，只给一句话简评，
不再像真实持仓那样渲染入场点/止损/多空辩论等重型字段（用户反馈：影子股太详细、冗余）。
持仓股(shares>0)保留完整明细——本测试只锁影子折叠面板的纯函数。
"""
from types import SimpleNamespace
from finance_agent.graph.workflow import _build_shadow_panel, _panel


def _stk(ticker, rec="观望", conf="中", one_line="触发动量突破", key_risk="", **kw):
    base = dict(ticker=ticker, recommendation=rec, confidence=conf,
                one_line=one_line, key_risk=key_risk, shares=0.0,
                entry_hint="", stop_loss_hint="", bull_thesis="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_shadow_panel_folds_non_held():
    panel = _build_shadow_panel([_stk("PLTR"), _stk("KLAC")])
    assert panel["tag"] == "collapsible_panel"
    assert panel["expanded"] is False                       # 默认折叠
    content = panel["elements"][0]["content"]
    assert "PLTR" in content and "KLAC" in content
    title = panel["header"]["title"]["content"]
    assert "2" in title and "影子" in title                  # 标题带数量


def test_shadow_panel_none_when_empty():
    assert _build_shadow_panel([]) is None


def test_shadow_brief_not_full_detail():
    """影子股只给一句话简评+风险，不渲染入场点/止损/多空辩论等重型字段。"""
    s = _stk("PLTR", one_line="触发突破", key_risk="估值贵",
             entry_hint="回踩20日线", stop_loss_hint="跌破即止损", bull_thesis="长期AI叙事...")
    content = _build_shadow_panel([s])["elements"][0]["content"]
    assert "触发突破" in content and "估值贵" in content       # 简评+风险保留
    assert "止损" not in content and "多方" not in content     # 重型字段不进折叠
    assert "回踩20日线" not in content                        # 入场点也不进


def test_panel_schema_shape():
    p = _panel("**标题**", "正文", expanded=False)
    assert p["tag"] == "collapsible_panel" and p["expanded"] is False
    assert p["elements"][0]["content"] == "正文"
