# tests/test_agents/test_pm_fallback.py
"""task#28③ 回归：PM DeepSeek 降级路径必须保留 thesis + strategy_evidence。
（06-10 深夜 A/B 实验因降级丢实盘反馈而整体无效——此处锁死不再复发。）"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from finance_agent.agents import portfolio_manager as pm
from finance_agent.graph.state import StockAnalysis
from finance_agent.signals.models import TechnicalSignals


def _mk_stock(**kw) -> StockAnalysis:
    signals = TechnicalSignals(
        ticker="MU", close=100.0, change_pct=0.0,
        rsi=50.0, rsi_signal="neutral",
        ma5=99.0, ma20=98.0, ma60=95.0,
        ma_signal="neutral", price_vs_ma20=2.0,
        macd=0.1, macd_signal=0.1, macd_hist=0.0, macd_trend="neutral",
        bb_upper=110.0, bb_lower=90.0, bb_position=0.5,
        volume_ratio=1.0, composite_score=0.0,
    )
    return StockAnalysis(ticker="MU", market="us", signals=signals, **kw)


@pytest.mark.asyncio
async def test_fallback_prompt_keeps_thesis_and_evidence(monkeypatch):
    stock = _mk_stock(
        thesis="HBM 供需紧张是核心逻辑",
        shares=2.0,   # 真实持仓（shares=0 会触发观察池语义闸把持有转观望）
        strategy_evidence="【实盘反馈】我们近期对 MU 建议的实际结果（7日口径）：\n"
                          "- 减仓/卖出6次：后续7日平均+23.5%（系统性卖早，减仓宜慎重；不构成加仓依据）",
    )
    # 强制走降级路径 + 关护栏（避免读真实 config/DB）
    monkeypatch.setattr(pm, "has_claude_cli", lambda: False)
    monkeypatch.setattr(pm, "guard_enabled", lambda: False)
    captured = {}

    async def fake_deepseek(system, user):
        captured["system"] = system
        captured["user"] = user
        return json.dumps({"recommendation": "持有", "confidence": "中",
                           "position_change": "维持", "one_line": "ok"})

    with patch.object(pm, "deepseek_chat", new=AsyncMock(side_effect=fake_deepseek)):
        out = await pm.run_portfolio_manager_batch([stock])

    assert "HBM 供需紧张" in captured["user"]            # thesis 进了降级 prompt
    assert "系统性卖早" in captured["user"]              # 实盘反馈进了降级 prompt
    assert "只能用于让建议更保守" in captured["user"]     # 保守约束同步存在
    assert out[0].recommendation == "持有"


@pytest.mark.asyncio
async def test_watch_stock_semantic_gate(monkeypatch):
    """06-11 复盘 D2 回归：未持有标的（shares=0）被 LLM 判'持有/减仓/卖出'
    → 代码层语义闸归一为 观望/维持（不信 prompt 自觉）；降级 prompt 须含'未持有'。"""
    stock = _mk_stock(thesis="影子观察")
    stock = stock.model_copy(update={"shares": 0.0})
    monkeypatch.setattr(pm, "has_claude_cli", lambda: False)
    monkeypatch.setattr(pm, "guard_enabled", lambda: False)
    captured = {}

    async def fake_deepseek(system, user):
        captured["user"] = user
        return json.dumps({"recommendation": "持有", "confidence": "中",
                           "position_change": "维持", "one_line": "拿着不动"})

    with patch.object(pm, "deepseek_chat", new=AsyncMock(side_effect=fake_deepseek)):
        out = await pm.run_portfolio_manager_batch([stock])

    assert "未持有（观察标的" in captured["user"]
    assert out[0].recommendation == "观望"          # 语义闸生效


@pytest.mark.asyncio
async def test_held_stock_keeps_hold(monkeypatch):
    """语义闸只管未持有标的：真实持仓的'持有'原样保留。"""
    stock = _mk_stock(thesis="真实持仓")
    stock = stock.model_copy(update={"shares": 3.0})
    monkeypatch.setattr(pm, "has_claude_cli", lambda: False)
    monkeypatch.setattr(pm, "guard_enabled", lambda: False)

    async def fake_deepseek(system, user):
        return json.dumps({"recommendation": "持有", "confidence": "中",
                           "position_change": "维持", "one_line": "ok"})

    with patch.object(pm, "deepseek_chat", new=AsyncMock(side_effect=fake_deepseek)):
        out = await pm.run_portfolio_manager_batch([stock])
    assert out[0].recommendation == "持有"


@pytest.mark.asyncio
async def test_fallback_empty_evidence_no_placeholder(monkeypatch):
    """无证据时不留空洞占位行（prompt 整洁，不给 LLM 幻觉素材）。"""
    stock = _mk_stock(thesis="", strategy_evidence="")
    monkeypatch.setattr(pm, "has_claude_cli", lambda: False)
    monkeypatch.setattr(pm, "guard_enabled", lambda: False)
    captured = {}

    async def fake_deepseek(system, user):
        captured["user"] = user
        return json.dumps({"recommendation": "观望", "confidence": "低",
                           "position_change": "维持", "one_line": "ok"})

    with patch.object(pm, "deepseek_chat", new=AsyncMock(side_effect=fake_deepseek)):
        await pm.run_portfolio_manager_batch([stock])

    assert "暂未记录持仓逻辑" in captured["user"]
    assert "实盘反馈" not in captured["user"].split("若材料中含")[0]   # 正文无凭空的反馈区块
