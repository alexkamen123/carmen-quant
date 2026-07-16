# tests/test_daily_stock_fold.py
"""日报卡视觉改版（路线A·飞书原生组件）：持仓股逐股块重组为
「主屏可见 + 展开理由折叠」，纯展示重构、不改任何建议逻辑/数字。

主屏可见：column_set 首行(标的/建议/信心) + 结论 + 本周操作 + 财报预警
          + 要盯的风险 + 卖飞/该减没减守门警示（安全相关不折叠）
折叠详情：入场点 / 假设 / 止损 / 基本面 / 多空辩论 / 信号历史
"""
from types import SimpleNamespace

from finance_agent.graph.workflow import _build_held_stock_elements, _stock_head_columns


def _sig(**kw):
    base = dict(rsi=50.0, close=100.0, ma20=98.0, ma60=95.0, macd=0.1)
    base.update(kw)
    return SimpleNamespace(**base)


def _earn(fundamental_view="", next_earnings_date=None):
    return SimpleNamespace(fundamental_view=fundamental_view,
                           next_earnings_date=next_earnings_date)


def _stk(ticker="NVDA", rec="买入", conf="高", shares=10.0, **kw):
    base = dict(
        ticker=ticker, recommendation=rec, confidence=conf, shares=shares,
        one_line="增长与利润俱强，回调可分批", position_change="小加",
        short_term_action="逢回调分批", entry_hint="回踩20日线",
        key_risk="板块短期情绪弱", key_assumption="AI资本开支未见拐点",
        stop_loss_hint="跌破前低8%减半", bull_thesis="1. 护城河扎实",
        bear_thesis="1. 估值透支需消化", signals=_sig(),
        earnings=_earn(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _texts(els):
    """收集所有 div/column 里的 markdown 文本（含 column_set 嵌套）。"""
    out = []
    for e in els:
        if e.get("tag") == "div":
            out.append(e["text"]["content"])
        elif e.get("tag") == "column_set":
            for col in e["columns"]:
                for ce in col["elements"]:
                    out.append(ce["text"]["content"])
    return "\n".join(out)


def test_head_row_is_column_set():
    els = _build_held_stock_elements(_stk())
    assert els[0]["tag"] == "column_set"
    assert len(els[0]["columns"]) == 2


def test_confidence_is_plain_words_not_stars():
    """★★★ → 「信心 高」说人话。"""
    head = _texts(_build_held_stock_elements(_stk(conf="高")))
    assert "信心 高" in head
    assert "★" not in head


def test_visible_block_keeps_conclusion_action_and_guards():
    els = _build_held_stock_elements(_stk())
    txt = _texts(els)  # 只看主屏可见（column + div），不含折叠面板正文
    assert "增长与利润俱强" in txt          # 结论
    assert "本周操作：逢回调分批" in txt      # 本周操作
    assert "板块短期情绪弱" in txt           # 要盯的风险


def test_heavy_details_go_into_collapsible_panel():
    els = _build_held_stock_elements(_stk())
    panels = [e for e in els if e.get("tag") == "collapsible_panel"]
    assert len(panels) == 1
    assert panels[0]["expanded"] is False          # 默认折叠
    body = panels[0]["elements"][0]["content"]
    # 重型细节下沉到折叠里
    assert "入场：回踩20日线" in body
    assert "假设：AI资本开支未见拐点" in body
    assert "止损：跌破前低8%减半" in body
    assert "多方" in body and "空方" in body
    # 主屏可见区不再平铺这些重型字段
    vis = _texts(els)
    assert "回踩20日线" not in vis
    assert "跌破前低8%减半" not in vis


def test_earnings_alert_visible_when_near():
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=2)).strftime("%Y-%m-%d")
    els = _build_held_stock_elements(_stk(earnings=_earn(next_earnings_date=soon)))
    assert "财报预警" in _texts(els)


def test_no_panel_when_no_heavy_details():
    """无入场/假设/止损/多空 → 不生成空折叠面板。"""
    s = _stk(entry_hint="", key_assumption="", stop_loss_hint="", bull_thesis="")
    els = _build_held_stock_elements(s)
    assert not any(e.get("tag") == "collapsible_panel" for e in els)


def test_head_columns_helper_shape():
    c = _stock_head_columns("**🟢 NVDA**", "信心 高")
    assert c["tag"] == "column_set"
    assert c["columns"][0]["elements"][0]["text"]["content"] == "**🟢 NVDA**"
    # 右栏空 meta 时用占位空格，避免飞书空 column 渲染异常
    assert _stock_head_columns("x", "")["columns"][1]["elements"][0]["text"]["content"] == " "
