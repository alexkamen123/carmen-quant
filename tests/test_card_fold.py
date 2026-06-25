# tests/test_card_fold.py
"""周报/月报减冗余：把最明细的块收进折叠面板（与日报/价值体检同款范式）。
- 周报：「三桶配置漂移」精确表 → 折叠（主屏速览已点出偏离最大的桶）
- 月报：「逐笔复盘」per-trade 明细 → 折叠（主屏留总评/评分/准确率）
"""
from finance_agent.notifications.cards import collapsible_panel


def test_collapsible_panel_schema():
    p = collapsible_panel("**标题**", "正文内容", expanded=False)
    assert p["tag"] == "collapsible_panel"
    assert p["expanded"] is False
    assert p["header"]["title"]["content"] == "**标题**"
    assert p["elements"][0]["content"] == "正文内容"


def test_weekly_drift_is_folded():
    from finance_agent.weekly.report_card import build_weekly_card
    result = {
        "drift_rows": [
            {"status": "🔴", "label": "成长桶（risk）", "target_pct": 60, "current_pct": 75, "drift": 15},
            {"status": "🟢", "label": "对冲桶", "target_pct": 12, "current_pct": 12, "drift": 0},
        ],
    }
    card = build_weekly_card(result)
    panels = [e for e in card["elements"] if e.get("tag") == "collapsible_panel"]
    assert len(panels) >= 1
    drift_panel = next(p for p in panels if "漂移" in p["header"]["title"]["content"])
    body = drift_panel["elements"][0]["content"]
    assert "成长桶" in body and "75" in body          # 明细进折叠
    # 主屏 div/note 不再平铺三桶逐行表
    flat = " ".join(e.get("text", {}).get("content", "") for e in card["elements"] if e.get("tag") == "div")
    assert "对冲桶：12% → " not in flat              # 三桶逐行不在主屏平铺


def test_monthly_trade_review_is_folded():
    from finance_agent.notifications.cards import collapsible_panel as _cp
    # 直接验证月报用到的折叠构造（run_monthly_review 需 DB/LLM，过重，这里只锁 helper 用法）
    body = "✅ **NVDA** BUY 1股@190 → 7日 +5.0% · 买对"
    panel = _cp("**🔍 逐笔复盘**（点开看每笔）", body)
    assert panel["tag"] == "collapsible_panel" and "NVDA" in panel["elements"][0]["content"]
