# tests/test_daily_stock_fold.py
"""日报卡视觉改版（路线A·飞书原生组件）：持仓股逐股块重组为
「主屏可见 + 展开理由折叠」，纯展示重构、不改任何建议逻辑/数字。

首行单行紧凑：标的 · 建议 · 信心 · 仓位（不用 column_set——手机端布局散乱）
主屏可见：结论 + 本周操作 + 财报预警 + 入场 + 止损 + 要盯的风险
          + 卖飞/该减没减守门警示（今天要看的动作/安全线，不折叠）
折叠详情：假设 / 基本面 / 多空辩论 / 信号历史（讨论性内容下沉）
"""
from types import SimpleNamespace

from finance_agent.graph.workflow import _build_held_stock_elements


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


def _vis(els):
    """主屏可见文本 = 第一个 div（首行+结论+动作+止损/入场+风险+守门）。"""
    return els[0]["text"]["content"]


def _fold(els):
    """折叠面板正文（无则空串）。"""
    for e in els:
        if e.get("tag") == "collapsible_panel":
            return e["elements"][0]["content"]
    return ""


def test_head_row_single_line_compact():
    els = _build_held_stock_elements(_stk())
    head = _vis(els).splitlines()[0]
    assert "NVDA" in head and "买入" in head and "信心 高" in head  # 标的·建议·信心同一行
    assert els[0]["tag"] == "div"                                  # 不再用 column_set


def test_confidence_is_plain_words_not_stars():
    """★★★ → 「信心 高」说人话。"""
    head = _vis(_build_held_stock_elements(_stk(conf="高")))
    assert "信心 高" in head
    assert "★" not in head


def test_visible_block_keeps_action_stop_entry_and_guards():
    """止损/入场/结论/本周操作/风险都露在主屏（用户要求止损入场露出）。"""
    vis = _vis(_build_held_stock_elements(_stk()))
    assert "增长与利润俱强" in vis            # 结论
    assert "本周操作：逢回调分批" in vis        # 本周操作
    assert "入场：回踩20日线" in vis           # 入场露出
    assert "止损：跌破前低8%减半" in vis        # 止损露出
    assert "板块短期情绪弱" in vis             # 要盯的风险


def test_discussion_goes_into_collapsible_panel():
    els = _build_held_stock_elements(_stk())
    panels = [e for e in els if e.get("tag") == "collapsible_panel"]
    assert len(panels) == 1
    assert panels[0]["expanded"] is False          # 默认折叠
    body = _fold(els)
    assert "假设：AI资本开支未见拐点" in body       # 讨论性内容下沉
    assert "多方" in body and "空方" in body
    # 主屏可见区不含多空辩论
    assert "多方" not in _vis(els)


def test_earnings_alert_visible_when_near():
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=2)).strftime("%Y-%m-%d")
    els = _build_held_stock_elements(_stk(earnings=_earn(next_earnings_date=soon)))
    assert "财报预警" in _vis(els)


def test_no_panel_when_no_discussion():
    """无假设/多空 → 不生成空折叠面板（止损/入场在主屏、不算折叠内容）。"""
    s = _stk(key_assumption="", bull_thesis="")
    els = _build_held_stock_elements(s)
    assert not any(e.get("tag") == "collapsible_panel" for e in els)
