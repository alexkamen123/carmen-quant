# src/finance_agent/agents/portfolio_manager.py
import json
import os
import anthropic
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import (
    PM_SYSTEM, PM_USER,
    PM_BATCH_SYSTEM, PM_BATCH_USER, PM_BATCH_STOCK_TEMPLATE,
)
from finance_agent.agents.bull_agent import deepseek_chat

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


async def _claude_chat(system: str, user: str, max_tokens: int = 1200) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if api_key:
        client = anthropic.AsyncAnthropic(api_key=api_key)
    else:
        client = anthropic.AsyncAnthropic(auth_token=oauth_token)
    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


def _parse_decision(d: dict) -> dict:
    return {
        "recommendation": d.get("recommendation", "观望"),
        "confidence":     d.get("confidence", "低"),
        "entry_hint":     d.get("entry_hint", ""),
        "key_risk":       d.get("key_risk", ""),
        "one_line":       d.get("one_line", ""),
    }


async def run_portfolio_manager_batch(stocks: list[StockAnalysis]) -> list[StockAnalysis]:
    """
    一次 Claude 调用处理所有非 ETF 股票的 PM 裁决。
    失败时逐只降级到 DeepSeek。
    """
    # 分离需要 PM 决策的股票
    etf_tickers = {"QQQM", "VOO"}
    needs_pm = [s for s in stocks if s.ticker not in etf_tickers]
    etf_stocks = [s for s in stocks if s.ticker in etf_tickers]

    # ETF 直接标记
    result_map: dict[str, StockAnalysis] = {}
    for s in etf_stocks:
        result_map[s.ticker] = s.model_copy(update={
            "recommendation": "按计划定投",
            "confidence": "高",
            "one_line": f"{s.ticker} 按月定投计划执行，无需额外操作",
        })

    if not needs_pm:
        return [result_map[s.ticker] for s in stocks]

    # 构建批量 prompt
    blocks = []
    for s in needs_pm:
        blocks.append(PM_BATCH_STOCK_TEMPLATE.format(
            ticker=s.ticker,
            market=MARKET_LABEL.get(s.market, s.market),
            signals_str=s.signals.to_prompt_str(),
            fundamental_view=s.earnings.fundamental_view or "暂无基本面数据",
            bull_thesis=s.bull_thesis or "无",
            bear_thesis=s.bear_thesis or "无",
        ))
    user_msg = PM_BATCH_USER.format(
        n=len(needs_pm),
        stocks_block="\n\n".join(blocks),
    )

    has_claude = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    decisions: list[dict] = []

    if has_claude:
        try:
            raw = await _claude_chat(PM_BATCH_SYSTEM, user_msg, max_tokens=200 * len(needs_pm))
            start, end = raw.find("["), raw.rfind("]") + 1
            decisions = json.loads(raw[start:end])
        except Exception as e:
            print(f"[PM] Claude 批量调用失败，逐只降级到 DeepSeek: {e}")

    # Claude 失败或未配置 → DeepSeek 逐只处理
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

    # 将决策写回 StockAnalysis
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
