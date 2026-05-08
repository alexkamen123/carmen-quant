# tests/test_agents/test_debate.py
import pytest
from unittest.mock import AsyncMock, patch
from finance_agent.agents.bull_agent import run_bull_analysis
from finance_agent.agents.bear_agent import run_bear_analysis
from finance_agent.graph.state import StockAnalysis, NewsItem
from finance_agent.signals.models import TechnicalSignals


def make_mock_analysis() -> StockAnalysis:
    signals = TechnicalSignals(
        ticker="NVDA", close=850.0, change_pct=1.5,
        rsi=55.0, rsi_signal="neutral",
        ma5=840.0, ma20=820.0, ma60=780.0,
        ma_signal="neutral", price_vs_ma20=3.7,
        macd=5.2, macd_signal=4.1, macd_hist=1.1,
        macd_trend="bullish",
        bb_upper=900.0, bb_lower=760.0, bb_position=0.6,
        volume_ratio=1.2,
        composite_score=0.3,
    )
    return StockAnalysis(
        ticker="NVDA", market="us",
        signals=signals,
        news=[NewsItem(title="NVDA beats earnings", summary="Strong Q2", published="2026-05-01")],
    )


@pytest.mark.asyncio
async def test_bull_returns_non_empty_thesis():
    analysis = make_mock_analysis()
    with patch("finance_agent.agents.bull_agent.deepseek_chat", new_callable=AsyncMock) as mock:
        mock.return_value = "1. AI需求强劲 2. 技术面向好 3. 财报超预期"
        result = await run_bull_analysis(analysis)
    assert len(result.bull_thesis) > 10


@pytest.mark.asyncio
async def test_bear_returns_non_empty_thesis():
    analysis = make_mock_analysis()
    with patch("finance_agent.agents.bear_agent.deepseek_chat", new_callable=AsyncMock) as mock:
        mock.return_value = "1. 估值偏高 2. RSI接近超买 3. 竞争加剧"
        result = await run_bear_analysis(analysis)
    assert len(result.bear_thesis) > 10
