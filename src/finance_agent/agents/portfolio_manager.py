# src/finance_agent/agents/portfolio_manager.py
import asyncio
import json
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import (
    PM_SYSTEM, PM_USER,
    PM_BATCH_SYSTEM, PM_BATCH_USER, PM_BATCH_STOCK_TEMPLATE,
)
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli, strip_markdown

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


def _sector_summary(stocks: list[StockAnalysis]) -> str:
    """
    按行业计算持仓市值占比，返回一行文字供 PM prompt 使用。
    例如："半导体/AI算力 52% | 互联网/AI 30% | 宽基ETF 14% | 股息ETF 4%"
    """
    sector_value: dict[str, float] = {}
    total = 0.0
    for s in stocks:
        if not s.sector or s.shares <= 0:
            continue
        price = s.signals.close if s.signals else 0.0
        value = s.shares * price
        sector_value[s.sector] = sector_value.get(s.sector, 0.0) + value
        total += value

    if total <= 0:
        return "暂无持仓市值数据"

    parts = sorted(sector_value.items(), key=lambda x: x[1], reverse=True)
    return " | ".join(f"{sec} {val/total*100:.0f}%" for sec, val in parts)


def _parse_decision(d: dict) -> dict:
    return {
        "recommendation": d.get("recommendation", "观望"),
        "confidence":     d.get("confidence", "低"),
        "entry_hint":     d.get("entry_hint", ""),
        "key_risk":       d.get("key_risk", ""),
        "one_line":       d.get("one_line", ""),
    }


async def run_portfolio_manager_batch(
    stocks: list[StockAnalysis],
    macro_summary: str = "",
) -> list[StockAnalysis]:
    """
    一次 Claude CLI 调用处理所有非 ETF 股票的 PM 裁决。
    失败时逐只降级到 DeepSeek。
    """
    etf_tickers = {"QQQM", "VOO"}
    needs_pm = [s for s in stocks if s.ticker not in etf_tickers]
    result_map: dict[str, StockAnalysis] = {}

    # ETF 直接标记
    for s in stocks:
        if s.ticker in etf_tickers:
            result_map[s.ticker] = s.model_copy(update={
                "recommendation": "按计划定投",
                "confidence": "高",
                "one_line": f"{s.ticker} 按月定投计划执行，无需额外操作",
            })

    if not needs_pm:
        return [result_map[s.ticker] for s in stocks]

    # 构建批量 prompt
    blocks = [
        PM_BATCH_STOCK_TEMPLATE.format(
            ticker=s.ticker,
            market=MARKET_LABEL.get(s.market, s.market),
            signals_str=s.signals.to_prompt_str(),
            fundamental_view=s.earnings.fundamental_view or "暂无基本面数据",
            bull_thesis=s.bull_thesis or "无",
            bear_thesis=s.bear_thesis or "无",
        )
        for s in needs_pm
    ]
    sector_str = _sector_summary(stocks)
    print(f"[PM] 持仓集中度：{sector_str}")

    user_msg = PM_BATCH_USER.format(
        macro_summary=macro_summary or "暂无宏观数据",
        sector_summary=sector_str,
        n=len(needs_pm),
        stocks_block="\n\n".join(blocks),
    )

    decisions: list[dict] = []

    # 优先 claude CLI（Pro 订阅路径，无 API 限速）
    if has_claude_cli():
        try:
            raw = await claude_cli_chat(PM_BATCH_SYSTEM, user_msg)
            raw = strip_markdown(raw)
            start, end = raw.find("["), raw.rfind("]") + 1
            decisions = json.loads(raw[start:end])
            print(f"[PM] Claude CLI 批量裁决成功（{len(decisions)} 只）")
        except Exception as e:
            print(f"[PM] Claude CLI 失败，降级到 DeepSeek: {e}")

    # 降级：DeepSeek 逐只处理
    if not decisions:
        for s in needs_pm:
            user_msg_single = PM_USER.format(
                ticker=s.ticker,
                market=MARKET_LABEL.get(s.market, s.market),
                signals_str=s.signals.to_prompt_str(),
                fundamental_view=s.earnings.fundamental_view or "暂无基本面数据",
                bull_thesis=s.bull_thesis or "无",
                bear_thesis=s.bear_thesis or "无",
            )
            try:
                raw = await deepseek_chat(PM_SYSTEM, user_msg_single)
                start, end = raw.find("{"), raw.rfind("}") + 1
                decisions.append({"ticker": s.ticker, **json.loads(raw[start:end])})
            except Exception as e2:
                print(f"[PM] DeepSeek 也失败 {s.ticker}: {e2}")
                decisions.append({"ticker": s.ticker})

    decision_by_ticker = {d.get("ticker", ""): d for d in decisions}
    for s in needs_pm:
        d = decision_by_ticker.get(s.ticker, {})
        result_map[s.ticker] = s.model_copy(update=_parse_decision(d))

    return [result_map[s.ticker] for s in stocks]


async def run_portfolio_manager(analysis: StockAnalysis) -> StockAnalysis:
    """单只股票 PM（保留向后兼容，内部用 DeepSeek）"""
    user_msg = PM_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        signals_str=analysis.signals.to_prompt_str(),
        fundamental_view=analysis.earnings.fundamental_view or "暂无基本面数据",
        bull_thesis=analysis.bull_thesis or "无",
        bear_thesis=analysis.bear_thesis or "无",
    )
    try:
        raw = await deepseek_chat(PM_SYSTEM, user_msg)
        start, end = raw.find("{"), raw.rfind("}") + 1
        decision = json.loads(raw[start:end])
    except Exception:
        decision = {}
    return analysis.model_copy(update=_parse_decision(decision))
