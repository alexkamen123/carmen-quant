# src/finance_agent/agents/fundamental_analyst.py
import os
import anthropic
from finance_agent.graph.state import StockAnalysis, EarningsSummary
from finance_agent.agents.prompts import FUNDAMENTAL_SYSTEM, FUNDAMENTAL_USER

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
    """用 Claude 分析基本面，填充 earnings.fundamental_view"""

    # 如果没有财务数据（港股/A股），返回空判断
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

    # 优先 API key，其次 OAuth token
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

    try:
        if api_key:
            client = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            client = anthropic.AsyncAnthropic(auth_token=oauth_token)

        message = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=400,
            system=FUNDAMENTAL_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        view = message.content[0].text.strip()
    except Exception as e:
        # 降级：直接用财务数字，不做 AI 解读
        view = f"财务数据：{financials_str}（Claude 调用失败: {e}）"

    return analysis.model_copy(update={
        "earnings": analysis.earnings.model_copy(update={"fundamental_view": view})
    })
