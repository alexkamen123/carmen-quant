# tests/test_alerts/test_atr_adaptive.py
"""order8：dip 预警 ATR 自适应阈值（两阶段判定）测试（mock yfinance/信号）。"""
from types import SimpleNamespace

import pandas as pd
import pytest

from finance_agent.alerts import news_monitor as nm

_CFG = {"enabled": True, "base_pct": 3.0, "k": 0.8, "cap_pct": 7.0}


def test_effective_threshold_formula():
    """公式：max(base, min(k*atr, cap))，只抬升不下调；
    atr 不可得（新股/信号失败）→ 取 cap 保守降噪（06-12 00100 七连击复盘）。"""
    assert nm._effective_drop_threshold(0.02, 0.0, _CFG) == 3.0    # 极低波动 → base
    assert nm._effective_drop_threshold(4.14, 0.0, _CFG) == 3.31   # 0.8*4.14
    assert nm._effective_drop_threshold(11.22, 0.0, _CFG) == 7.0   # cap 封顶
    assert nm._effective_drop_threshold(0.0, 0.0, _CFG) == 7.0     # ATR 未知 → cap
    assert nm._effective_drop_threshold(None, 0.0, _CFG) == 7.0


def test_escalation_decision_matrix():
    """台阶去重判定表：首报 full / 同档 skip / 加深 light / 剧烈恶化 full / 日上限 skip。"""
    f = nm._escalation_decision
    assert f([], 1) == "full"            # 当日首报
    assert f([1], 1) == "skip"           # 同档重复=零增量
    assert f([2], 1) == "skip"           # 回升后再跌回浅档=已报过更深，不重复
    assert f([1], 2) == "full"           # 2 >= 2*1：剧烈恶化重新分析
    assert f([2], 3) == "light"          # 加深一档：增量卡
    assert f([2], 4) == "full"           # 4 >= 2*2：翻倍重分析
    assert f([1, 2, 3], 4) == "skip"     # 每日硬上限 3 张
    assert nm._alert_step(0.0) == 2.0 and nm._alert_step(8.0) == 4.0


def test_gap_up_compounds_on_atr_result():
    """gap_up 抬升叠加在 ATR-adaptive 结果上，而非写死 3.0% 上（原 bug 修正）。"""
    assert nm._effective_drop_threshold(11.22, 15.0, _CFG) == 12.25   # 7.0 * 1.75
    assert nm._effective_drop_threshold(1.2, 12.0, _CFG) == 4.8       # 3.0 * 1.6
    assert nm._effective_drop_threshold(4.14, 10.0, _CFG) == 3.31     # gap<=10 不叠加


def test_disabled_falls_back_to_base():
    cfg = {**_CFG, "enabled": False}
    assert nm._effective_drop_threshold(11.22, 0.0, cfg) == 3.0
    # 关闭时 gap_up 抬升仍生效（改动前已有的行为，开关只关 ATR 部分）
    assert nm._effective_drop_threshold(11.22, 15.0, cfg) == 5.25     # 3.0 * 1.75


def _wire_check_drop(monkeypatch, intraday_closes, atr_pct):
    """搭桥：市场开盘 + 5m K 线 + 2d 日线（无跳空）+ 3mo 日线 → 指定 atr_pct 信号。"""
    monkeypatch.setattr(nm, "_is_market_open", lambda m: True)
    idx = pd.date_range("2026-06-10 09:30", periods=len(intraday_closes), freq="5min")
    df_5m = pd.DataFrame({"close": intraday_closes}, index=idx)
    df_2d = pd.DataFrame({"close": [intraday_closes[0], intraday_closes[0]]})
    df_daily = pd.DataFrame({"close": [100.0] * 30})

    def fake_dl(tkr, period=None, interval=None, **k):
        if interval == "5m":
            return df_5m
        if period == "2d":
            return df_2d
        return df_daily

    monkeypatch.setattr(nm.yf, "download", fake_dl)
    import finance_agent.signals.technical as tech
    monkeypatch.setattr(
        tech, "calculate_signals",
        lambda df, ticker=None: SimpleNamespace(atr_pct=atr_pct, atr=1.0,
                                                bb_lower=90.0, ma20=100.0, rsi=50.0),
    )


def test_stage1_loose_pass_stage2_strict_reject(monkeypatch):
    """跌 4%：过 base=3% 预筛，但高波动票（ATR 8.75 → 阈值 7%）Stage 2 拦下 → None。"""
    closes = [100.0] * 10 + [96.0, 96.0]   # 1h 内 -4%
    _wire_check_drop(monkeypatch, closes, atr_pct=8.75)
    assert nm._check_price_drop("HIVOL", "us", 3.0) is None


def test_stage2_pass_low_vol(monkeypatch):
    """同样跌 4%：低波动票（ATR 2% → 阈值=base 3%）正常触发，带 effective_threshold。"""
    closes = [100.0] * 10 + [96.0, 96.0]
    _wire_check_drop(monkeypatch, closes, atr_pct=2.0)
    info = nm._check_price_drop("LOVOL", "us", 3.0)
    assert info is not None and info["effective_threshold"] == 3.0
    assert info["open_triggered"] is False   # 1h 已触发，开盘标记不抢


def test_open_triggered_uses_effective(monkeypatch):
    """open_triggered 用 effective*1.5 判定（原代码用 base 的 bug 已修复）：
    较开盘跌 5%（过 base*1.5=4.5%）但 ATR 阈值 4%→open 线 6%，不触发 → None。"""
    # 开盘后立刻跌到 95.2 后横盘 20 根：1h 窗口（后 12 根）跌 0%，较开盘 -4.8%
    closes = [100.0] + [95.2] * 20
    _wire_check_drop(monkeypatch, closes, atr_pct=5.0)   # eff=4.0，open 线=6.0
    assert nm._check_price_drop("GAPDN", "us", 3.0) is None


def test_disabled_open_trigger_matches_pre_change(monkeypatch):
    """对抗审查 P1 回归：一键关后 open 触发必须回退裸 base*1.5（不受 gap/ATR 影响）。
    gap_up=15%、较开盘 -5%：改动前会发卡（-5<=-4.5），关掉开关后也必须发卡。"""
    closes = [100.0] + [95.0] * 20   # 1h 横盘，较开盘 -5%
    _wire_check_drop(monkeypatch, closes, atr_pct=11.0)
    monkeypatch.setattr(nm, "_load_dip_atr_cfg", lambda: {**_CFG, "enabled": False})
    # 注入 gap_up=15%：让 2d 日线前收远低于开盘
    idx = pd.date_range("2026-06-09", periods=2, freq="D")
    df_2d = pd.DataFrame({"close": [100.0 / 1.15, 100.0]}, index=idx)
    orig_dl = nm.yf.download

    def dl(tkr, period=None, interval=None, **k):
        if period == "2d":
            return df_2d
        return orig_dl(tkr, period=period, interval=interval, **k)

    monkeypatch.setattr(nm.yf, "download", dl)
    info = nm._check_price_drop("OFFSW", "us", 3.0)
    assert info is not None and info["open_triggered"] is True


def test_surge_two_stage(monkeypatch):
    """D8：大涨检测镜像两阶段——低波动票 +4% 触发；高波动票（阈值7%）同涨幅拦下。"""
    closes = [100.0] * 10 + [104.0, 104.0]   # 1h 内 +4%
    _wire_check_drop(monkeypatch, closes, atr_pct=2.0)
    info = nm._check_price_surge("LOVOL", "us", 3.0)
    assert info is not None and info["rise_1h"] == 4.0
    assert info["effective_threshold"] == 3.0

    _wire_check_drop(monkeypatch, closes, atr_pct=8.75)   # eff=7.0
    assert nm._check_price_surge("HIVOL", "us", 3.0) is None


def test_surge_stage1_no_daily_fetch(monkeypatch):
    """大涨预筛不过 → 不触日线网络（与 drop 同款省流结构）。"""
    closes = [100.0] * 12
    monkeypatch.setattr(nm, "_is_market_open", lambda m: True)
    idx = pd.date_range("2026-06-12 09:30", periods=len(closes), freq="5min")
    df_5m = pd.DataFrame({"close": closes}, index=idx)

    def fake_dl(tkr, period=None, interval=None, **k):
        if interval == "5m":
            return df_5m
        raise AssertionError("预筛未过不该触日线")

    monkeypatch.setattr(nm.yf, "download", fake_dl)
    assert nm._check_price_surge("FLAT", "us", 3.0) is None


@pytest.mark.asyncio
async def test_surge_card_no_sell_instruction(monkeypatch):
    """D8 安全不变量：大涨卡片绝不出现卖出指令；卖飞签名上下文正确注入。"""
    sent = []

    async def fake_send(card):
        sent.append(card)

    monkeypatch.setattr(nm, "send_feishu_card", fake_send)
    from finance_agent.db import tracker
    monkeypatch.setattr(tracker, "get_live_feedback",
                        lambda t, db_path=None, asof=None: {
                            "ticker": t, "n_total": 6, "buy": None,
                            "sell": {"n": 6, "avg_next": 23.5}})
    monkeypatch.setattr(tracker, "get_behavior_hint_stats",
                        lambda db_path=None: {"n_buy": 11, "avg_buy": -6.1,
                                              "n_sell_regret": 3, "sell_regret_pct": 5.0,
                                              "small_sample": True})
    await nm._send_price_surge_alert("MU", "us", rise_1h=5.6, rise_from_open=4.0,
                                     price_now=941.0, cost_basis=800.0)
    texts = []
    for el in sent[0]["elements"]:
        if el.get("tag") == "div":
            texts.append(el["text"]["content"])
        if el.get("tag") == "note":
            texts += [x.get("content", "") for x in el.get("elements", [])]
    blob = "\n".join(texts)
    assert "+5.6%" in blob and "浮盈 **+17.6%**" in blob
    assert "卖早" in blob and "3 次卖出后涨了 5%+" in blob
    assert "不构成卖出指令" in blob
    assert "建议卖出" not in blob and "止盈离场" not in blob   # 绝不催卖


def test_stage1_early_return_skips_daily_fetch(monkeypatch):
    """两条件都不过 base 预筛 → 立即 None，绝不触日线/信号网络请求。"""
    closes = [100.0] * 12   # 无跌幅
    monkeypatch.setattr(nm, "_is_market_open", lambda m: True)
    idx = pd.date_range("2026-06-10 09:30", periods=len(closes), freq="5min")
    df_5m = pd.DataFrame({"close": closes}, index=idx)
    calls = []

    def fake_dl(tkr, period=None, interval=None, **k):
        calls.append(period)
        if interval == "5m":
            return df_5m
        raise AssertionError("预筛未过不该再触网")

    monkeypatch.setattr(nm.yf, "download", fake_dl)
    assert nm._check_price_drop("FLAT", "us", 3.0) is None
    assert calls == ["1d"]
