"""减仓时机提醒：情绪平复判定（双向）+ 卡片构建。"""
from finance_agent.alerts.cooldown_monitor import (
    is_settled, is_rebound_ready, build_cooldown_card,
)


# ── rebound（借反弹减到 MA20 阻力）──
def test_rebound_fires_near_ma20_with_stalled_momentum():
    # 现价 580 逼近 MA20 600（在 0.04 带内：600*0.96=576）且单日 +1.5% 动能停 → 触发
    assert is_rebound_ready(580.0, 600.0, 1.5, 4.0, 0.04) is True


def test_rebound_not_yet_far_below_ma20():
    # 现价 540 离 MA20 600 还有 10%（<576 阈值）→ 未到阻力
    assert is_rebound_ready(540.0, 600.0, 1.0, 4.0, 0.04) is False


def test_rebound_not_while_surging():
    # 已到阻力位但单日 +8% 仍在猛冲（>4 阈值）→ 别在冲刺中提醒，等动能停
    assert is_rebound_ready(595.0, 600.0, 8.0, 4.0, 0.04) is False


def test_rebound_guards_zero_ma20():
    assert is_rebound_ready(100.0, 0.0, 0.0, 4.0) is False


# ── spike（涨势冷却）：涨幅收窄 且 RSI 退超买，两条件同时满足才算平复 ──
def test_spike_still_hot_not_settled():
    # DRAM 当下：单日 +6%（>3 阈值）即便 RSI 已退超买，也算仍过热
    assert is_settled(6.0, 65, 3.0, "spike", 70) is False


def test_spike_rsi_still_overbought_not_settled():
    # 涨幅收窄到 2% 但 RSI 仍 72 超买 → 未平复
    assert is_settled(2.0, 72, 3.0, "spike", 70) is False


def test_spike_both_met_settled():
    assert is_settled(1.5, 58, 3.0, "spike", 70) is True


# ── crash（跌势企稳）：跌幅收窄 且 RSI 退出超卖（脱离恐慌） ──
def test_crash_still_plunging_not_settled():
    # QBTS 当下：单日 -9.5% 暴跌，动能未停 → 未企稳（绝不能砍在恐慌底）
    assert is_settled(-9.5, 48, 3.0, "crash", 30) is False


def test_crash_rsi_still_oversold_not_settled():
    # 跌幅收窄到 -2% 但 RSI 仍 25 深陷超卖 → 未企稳
    assert is_settled(-2.0, 25, 3.0, "crash", 30) is False


def test_crash_both_met_settled():
    # 止跌（-1.2%）且 RSI 回到 42 脱离恐慌 → 企稳
    assert is_settled(-1.2, 42, 3.0, "crash", 30) is True


def test_direction_gates_are_opposite():
    # 同一组数据，spike 看 RSI 上限、crash 看 RSI 下限，方向相反
    assert is_settled(1.0, 50, 3.0, "spike", 70) is True   # 50<70 退超买
    assert is_settled(1.0, 50, 3.0, "crash", 30) is True   # 50>30 退超卖
    assert is_settled(1.0, 80, 3.0, "spike", 70) is False  # 80 仍超买
    assert is_settled(1.0, 20, 3.0, "crash", 30) is False  # 20 仍超卖


# ── 卡片构建：方向标签正确（已冷却/已企稳）、动作进卡 ──
def test_card_spike_label():
    items = [{
        "ticker": "DRAM", "direction": "spike", "close": 70.5,
        "change_pct": 1.2, "rsi": 60.0, "action": "挂单卖 1 股，留 2 股", "reason": "锁利",
    }]
    blob = str(build_cooldown_card(items)["elements"])
    assert "DRAM" in blob and "已冷却" in blob and "挂单卖 1 股" in blob


def test_card_crash_label():
    items = [{
        "ticker": "QBTS", "direction": "crash", "close": 24.0,
        "change_pct": -0.8, "rsi": 41.0, "action": "减 1 股（先减一半，留 1 股）", "reason": "止跌",
    }]
    blob = str(build_cooldown_card(items)["elements"])
    assert "QBTS" in blob and "已企稳" in blob and "减 1 股" in blob
