"""减仓冷却提醒：冷却判定 + 卡片构建。"""
from finance_agent.alerts.cooldown_monitor import is_cooled, build_cooldown_card


# ── 冷却判定：涨势停 且 RSI 退超买，两条件同时满足才算冷却 ──
def test_still_spiking_not_cooled():
    # DRAM 当下：单日 +6%（>3 阈值）即便 RSI 已退超买，也算仍过热
    assert is_cooled(6.0, 65, max_day_change=3.0, rsi_ceiling=70) is False


def test_rsi_still_overbought_not_cooled():
    # 涨幅收窄到 2% 但 RSI 仍 72 超买 → 未冷却
    assert is_cooled(2.0, 72, max_day_change=3.0, rsi_ceiling=70) is False


def test_both_conditions_met_cooled():
    assert is_cooled(1.5, 58, max_day_change=3.0, rsi_ceiling=70) is True


def test_big_drop_also_not_cooled():
    # 单日 -5% 暴跌同样属"情绪未平"，绝对值超阈值 → 不算冷却
    assert is_cooled(-5.0, 45, max_day_change=3.0, rsi_ceiling=70) is False


# ── 卡片构建：结构正确、动作/实测值进卡 ──
def test_card_structure():
    items = [{
        "ticker": "DRAM", "close": 70.5, "change_pct": 1.2, "rsi": 60.0,
        "action": "挂单卖 1 股，留 2 股", "reason": "锁利",
    }]
    card = build_cooldown_card(items)
    assert card["header"]["title"]["content"] == "🔔 卡门智投 · 减仓冷却提醒"
    blob = str(card["elements"])
    assert "DRAM" in blob and "挂单卖 1 股" in blob and "RSI 60" in blob
