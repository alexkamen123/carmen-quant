# tests/test_agents/test_arbiter.py
"""P0a 辩论仲裁层：memory→arbiter→decision，把多空辩论逼成明确五档评级(买入/增持/持有/减持/卖出)
注入 PM 材料(advisory·不硬改建议)。改核心建议→flag debate_arbiter.enabled 默认 off。
铁律：flag off 时 arbiter_node 纯透传、零 LLM 调用、PM 模板逐字节不变。"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from finance_agent.agents import arbiter
from finance_agent.agents.prompts import PM_BATCH_STOCK_TEMPLATE
from finance_agent.graph.state import StockAnalysis
from finance_agent.signals.models import TechnicalSignals


def _mk(ticker="NVDA", **kw) -> StockAnalysis:
    sig = TechnicalSignals(
        ticker=ticker, close=100.0, change_pct=0.0, rsi=50.0, rsi_signal="neutral",
        ma5=99.0, ma20=98.0, ma60=95.0, ma_signal="neutral", price_vs_ma20=2.0,
        macd=0.1, macd_signal=0.1, macd_hist=0.0, macd_trend="neutral",
        bb_upper=110.0, bb_lower=90.0, bb_position=0.5, volume_ratio=1.0, composite_score=0.0,
    )
    return StockAnalysis(ticker=ticker, market="us", signals=sig,
                         bull_thesis="多头：AI需求强", bear_thesis="空头：估值贵", **kw)


# ── 纯函数 ──────────────────────────────────────────────
def test_arbiter_block_empty_when_no_rating():
    """无评级 → 空块（保证 PM 模板逐字节不变）。"""
    assert arbiter._arbiter_block("", None, []) == ""


def test_shadow_suppresses_injection(monkeypatch):
    """影子观测档：arbiter 照常算评级，但 pm_arbiter_block 返空 → 不注入 PM、建议逐字节不变。"""
    monkeypatch.setattr(arbiter, "arbiter_shadow", lambda: True)
    s = _mk("NVDA", shares=2).model_copy(update={
        "arbiter_rating": "减持", "arbiter_balanced": False,
        "arbiter_rationale": ["采纳空头第2条：估值透支"]})
    assert arbiter.pm_arbiter_block(s) == ""   # shadow 下有评级也不注入


def test_record_arbiter_shadow(tmp_path):
    """影子留痕：把算出的五档评级落盘供日后比对(arbiter vs PM vs outcome)。"""
    log = tmp_path / "arb.jsonl"
    s = _mk("NVDA", shares=2).model_copy(update={
        "arbiter_rating": "减持", "arbiter_balanced": False,
        "arbiter_rationale": ["采纳空头第2条"]})
    s2 = _mk("AAPL", shares=1)   # 无评级 → 不落
    arbiter.record_arbiter_shadow([s, s2], "2026-07-02", log_path=log)
    import json
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["ticker"] == "NVDA" and rows[0]["rating"] == "减持"


def test_arbiter_block_balanced_label_only_when_true():
    """诚实(审查Important)：'（多空势均力敌）'只在 balanced=True 才贴，绝不凭 rating=持有 硬贴。"""
    assert "势均力敌" in arbiter._arbiter_block("持有", True, ["采纳多头第1条"])
    assert "势均力敌" not in arbiter._arbiter_block("持有", False, ["采纳空头第1条"])
    assert "势均力敌" not in arbiter._arbiter_block("观望", None, ["采纳空头第1条"])


def test_pm_template_byte_identical_when_arbiter_off():
    """PM 模板加 {arbiter_block} 后，空串时结尾仍是财报日、不多换行。"""
    rendered = PM_BATCH_STOCK_TEMPLATE.format(
        ticker="NVDA", market="美股", position_pct=5.0, shares=2, current_price=100,
        cost_basis_str="x", thesis="t", signals_str="s", strategy_evidence="",
        fundamental_view="f", bull_thesis="b", bear_thesis="be",
        next_earnings_date="2026-08-01", arbiter_block="")
    assert rendered.endswith("【下次财报】2026-08-01")   # 空块不加任何字符


def test_parse_arbiter_valid():
    """解析 JSON 数组 → 按 ticker 出评级/balanced/rationale。"""
    raw = json.dumps([{"ticker": "NVDA", "rating": "减持", "balanced": False,
                       "rationale": ["采纳空头第2条：估值透支"]}])
    r = arbiter._parse_arbiter(raw, [_mk("NVDA", shares=2)])
    assert r["NVDA"]["rating"] == "减持"
    assert r["NVDA"]["rationale"] == ["采纳空头第2条：估值透支"]


def test_hold_requires_balanced():
    """持有须 balanced=true，否则视为回避、置空(不出评级)。"""
    raw = json.dumps([{"ticker": "NVDA", "rating": "持有", "balanced": False, "rationale": ["采纳多头"]}])
    assert arbiter._parse_arbiter(raw, [_mk("NVDA", shares=2)]) == {}
    raw2 = json.dumps([{"ticker": "NVDA", "rating": "持有", "balanced": True, "rationale": ["采纳多头第1条"]}])
    assert arbiter._parse_arbiter(raw2, [_mk("NVDA", shares=2)])["NVDA"]["rating"] == "持有"


def test_rationale_needs_citation():
    """rationale 不引用具体证据(无采纳/驳回/多头/空头等)→ 丢弃该条。"""
    raw = json.dumps([{"ticker": "NVDA", "rating": "买入", "balanced": None,
                       "rationale": ["感觉不错", "采纳多头第1条：AI需求"]}])
    r = arbiter._parse_arbiter(raw, [_mk("NVDA", shares=2)])
    assert r["NVDA"]["rationale"] == ["采纳多头第1条：AI需求"]   # 无引用的被丢


def test_invalid_rating_dropped():
    """非五档评级 → 丢弃。"""
    raw = json.dumps([{"ticker": "NVDA", "rating": "抄底", "balanced": None, "rationale": []}])
    assert arbiter._parse_arbiter(raw, [_mk("NVDA", shares=2)]) == {}


# ── run_arbiter_batch（异步，mock LLM）────────────────────
@pytest.mark.asyncio
async def test_disabled_passthrough_no_llm(monkeypatch):
    """flag off → 纯透传、零 LLM 调用、arbiter_rating 空。"""
    monkeypatch.setattr(arbiter, "arbiter_enabled", lambda: False)
    claude = AsyncMock()
    monkeypatch.setattr(arbiter, "claude_cli_chat", claude)
    monkeypatch.setattr(arbiter, "deepseek_chat", claude)
    out = await arbiter.run_arbiter_batch([_mk("NVDA", shares=2)])
    assert out[0].arbiter_rating == ""
    claude.assert_not_called()


@pytest.mark.asyncio
async def test_watch_pool_normalize(monkeypatch):
    """观察池(shares=0)：只映射买入/观望——增持→买入(看多建仓)，减持/卖出/持有→观望(不建仓)。
    偏空方向由 rationale 保留，绝不洗成'持有（多空势均力敌）'(审查Important)。"""
    monkeypatch.setattr(arbiter, "arbiter_enabled", lambda: True)
    monkeypatch.setattr(arbiter, "has_claude_cli", lambda: False)

    async def fake_ds(system, user):
        return json.dumps([{"ticker": "NVDA", "rating": "卖出", "balanced": None,
                            "rationale": ["采纳空头第1条：估值透支"]}])
    with patch.object(arbiter, "deepseek_chat", new=AsyncMock(side_effect=fake_ds)):
        out = await arbiter.run_arbiter_batch([_mk("NVDA", shares=0.0)])
    assert out[0].arbiter_rating == "观望"   # 卖出→观望（未持有=不建仓，不洗成持有）
    block = arbiter.pm_arbiter_block(out[0])
    assert "势均力敌" not in block          # 偏空归一后绝无虚假中性标签


@pytest.mark.asyncio
async def test_watch_pool_bullish_to_buy(monkeypatch):
    """观察池看多(增持)→买入。"""
    monkeypatch.setattr(arbiter, "arbiter_enabled", lambda: True)
    monkeypatch.setattr(arbiter, "has_claude_cli", lambda: False)

    async def fake_ds(system, user):
        return json.dumps([{"ticker": "NVDA", "rating": "增持", "balanced": None,
                            "rationale": ["采纳多头第1条：AI需求"]}])
    with patch.object(arbiter, "deepseek_chat", new=AsyncMock(side_effect=fake_ds)):
        out = await arbiter.run_arbiter_batch([_mk("NVDA", shares=0.0)])
    assert out[0].arbiter_rating == "买入"


@pytest.mark.asyncio
async def test_etf_skipped(monkeypatch):
    """ETF 不参与仲裁。"""
    monkeypatch.setattr(arbiter, "arbiter_enabled", lambda: True)
    monkeypatch.setattr(arbiter, "has_claude_cli", lambda: False)
    called = {"n": 0}

    async def fake_ds(system, user):
        called["n"] += 1
        return "[]"
    etf = _mk("SPY", shares=5.0)
    etf = etf.model_copy(update={"is_etf": True})
    with patch.object(arbiter, "deepseek_chat", new=AsyncMock(side_effect=fake_ds)):
        out = await arbiter.run_arbiter_batch([etf])
    assert out[0].arbiter_rating == "" and called["n"] == 0   # 全ETF→不调LLM


@pytest.mark.asyncio
async def test_claude_fallback_to_deepseek(monkeypatch):
    """claude 失败 → 降级 deepseek。"""
    monkeypatch.setattr(arbiter, "arbiter_enabled", lambda: True)
    monkeypatch.setattr(arbiter, "has_claude_cli", lambda: True)

    async def boom(*a, **k):
        raise RuntimeError("claude down")

    async def fake_ds(system, user):
        return json.dumps([{"ticker": "NVDA", "rating": "增持", "balanced": None,
                            "rationale": ["采纳多头第1条：AI需求"]}])
    with patch.object(arbiter, "claude_cli_chat", new=AsyncMock(side_effect=boom)), \
         patch.object(arbiter, "deepseek_chat", new=AsyncMock(side_effect=fake_ds)):
        out = await arbiter.run_arbiter_batch([_mk("NVDA", shares=2)])
    assert out[0].arbiter_rating == "增持"


@pytest.mark.asyncio
async def test_both_fail_noop(monkeypatch):
    """两路 LLM 全失败 → 原样返回、不崩、arbiter_rating 空。"""
    monkeypatch.setattr(arbiter, "arbiter_enabled", lambda: True)
    monkeypatch.setattr(arbiter, "has_claude_cli", lambda: True)

    async def boom(*a, **k):
        raise RuntimeError("down")
    with patch.object(arbiter, "claude_cli_chat", new=AsyncMock(side_effect=boom)), \
         patch.object(arbiter, "deepseek_chat", new=AsyncMock(side_effect=boom)):
        out = await arbiter.run_arbiter_batch([_mk("NVDA", shares=2)])
    assert out[0].arbiter_rating == ""
