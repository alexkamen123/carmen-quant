# src/finance_agent/agents/fundamental_analyst.py
from finance_agent.graph.state import StockAnalysis, EarningsSummary
from finance_agent.agents.prompts import FUNDAMENTAL_SYSTEM, FUNDAMENTAL_USER
from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli
from finance_agent.agents.bull_agent import deepseek_chat

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}


def _fmt_financials(e: EarningsSummary) -> str:
    lines = []
    if e.revenue_growth_yoy is not None:
        lines.append(f"营收同比增速：{e.revenue_growth_yoy:+.1f}%")
    if e.gross_margin is not None:
        lines.append(f"毛利率：{e.gross_margin:.1f}%")
    if e.pe_ratio is not None:
        lines.append(f"PE（TTM）：{e.pe_ratio:.1f}x")
    if e.ps_ratio is not None:
        lines.append(f"PS（TTM）：{e.ps_ratio:.1f}x")
    if e.debt_to_equity is not None:
        lines.append(f"资产负债率：{e.debt_to_equity:.1f}%")
    return "\n".join(lines) if lines else "暂无财务数据"


async def run_fundamental_analysis(analysis: StockAnalysis) -> StockAnalysis:
    """用 Claude 分析基本面（判断能力要求高），填充 earnings.fundamental_view"""

    financials_str = _fmt_financials(analysis.earnings)
    if financials_str == "暂无财务数据" and not analysis.news:
        return analysis.model_copy(update={
            "earnings": analysis.earnings.model_copy(
                update={"fundamental_view": "暂无财务数据，仅参考技术面"}
            )
        })

    news_str = "\n".join(f"- {n.title}" for n in analysis.news) or "暂无新闻"

    user_msg = FUNDAMENTAL_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        financials_str=financials_str,
        news_str=news_str,
    )

    try:
        # 优先用 Claude（基本面判断能力更强）
        if has_claude_cli():
            view = await claude_cli_chat(FUNDAMENTAL_SYSTEM, user_msg, timeout=90)
            print(f"[Fundamental] Claude 分析 {analysis.ticker} 完成")
        else:
            raise RuntimeError("无 Claude CLI")
    except Exception as e:
        print(f"[Fundamental] Claude 失败，降级 DeepSeek ({analysis.ticker}): {e}")
        try:
            view = await deepseek_chat(FUNDAMENTAL_SYSTEM, user_msg)
        except Exception:
            view = financials_str  # 最终降级：展示原始财务数字

    return analysis.model_copy(update={
        "earnings": analysis.earnings.model_copy(update={"fundamental_view": view})
    })
