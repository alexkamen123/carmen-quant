# src/finance_agent/graph/workflow.py
import asyncio
from datetime import datetime
import yaml
from pathlib import Path
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from finance_agent.graph.state import AgentState, StockAnalysis, NewsItem, EarningsSummary
from finance_agent.data.router import DataRouter
from finance_agent.signals.technical import calculate_signals
from finance_agent.agents.bull_agent import run_bull_analysis
from finance_agent.agents.bear_agent import run_bear_analysis
from finance_agent.agents.portfolio_manager import run_portfolio_manager
from finance_agent.agents.fundamental_analyst import run_fundamental_analysis

router = DataRouter()

# ─── 节点函数 ────────────────────────────────────────────

async def fetch_data_node(state: AgentState) -> AgentState:
    """并行拉取所有持仓的行情数据"""
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    all_holdings = portfolio.get("holdings", []) + portfolio.get("watchlist", [])

    async def fetch_one(item: dict) -> StockAnalysis | None:
        ticker = item["ticker"]
        market = item["market"]
        try:
            df = await router.fetch_ohlcv(ticker, market, days=60)
            signals = calculate_signals(df, ticker=ticker)
            news_raw = await router.fetch_news(ticker, market, limit=3)
            news = [NewsItem(**n) for n in news_raw]
            earnings_raw = await router.fetch_earnings(ticker, market)
            earnings = EarningsSummary(**{k: v for k, v in earnings_raw.items() if v is not None})
            return StockAnalysis(ticker=ticker, market=market, signals=signals, news=news, earnings=earnings)
        except Exception as e:
            state.errors.append(f"{ticker}: {e}")
            return None

    results = await asyncio.gather(*[fetch_one(h) for h in all_holdings])
    stocks = [r for r in results if r is not None]
    return state.model_copy(update={"stocks": stocks, "date": datetime.today().strftime("%Y-%m-%d")})


async def fundamentals_node(state: AgentState) -> AgentState:
    """用 Claude 分析每只股票基本面（串行 + 间隔，避免 Pro 限速）"""
    updated = []
    needs_claude = [s for s in state.stocks if s.ticker not in ("QQQM", "VOO")]
    for analysis in state.stocks:
        if analysis.ticker in ("QQQM", "VOO"):
            updated.append(analysis.model_copy(update={
                "earnings": analysis.earnings.model_copy(
                    update={"fundamental_view": "宽基 ETF，按定投计划执行"}
                )
            }))
        else:
            updated.append(await run_fundamental_analysis(analysis))
            # 串行调用，防止同一会话并发触发限速
            if analysis != needs_claude[-1]:
                await asyncio.sleep(1)
    return state.model_copy(update={"stocks": updated})


async def debate_node(state: AgentState) -> AgentState:
    """对每只股票依次运行 Bull/Bear 辩论（串行避免 API 限速）"""
    updated_stocks = []
    for analysis in state.stocks:
        # 定投标的跳过辩论，直接标记
        if analysis.ticker in ("QQQM", "VOO"):
            updated = analysis.model_copy(update={
                "bull_thesis": "定投标的，按月计划执行",
                "bear_thesis": "定投标的，不做短期判断",
            })
        else:
            bull_result = await run_bull_analysis(analysis)
            updated = await run_bear_analysis(bull_result)
        updated_stocks.append(updated)
    return state.model_copy(update={"stocks": updated_stocks})


async def decision_node(state: AgentState) -> AgentState:
    """Portfolio Manager 对每只股票做最终裁决"""
    updated_stocks = []
    needs_pm = [s for s in state.stocks if s.ticker not in ("QQQM", "VOO")]
    for analysis in state.stocks:
        if analysis.ticker in ("QQQM", "VOO"):
            updated = analysis.model_copy(update={
                "recommendation": "按计划定投",
                "confidence": "高",
                "one_line": f"{analysis.ticker} 按月定投计划执行，无需额外操作",
            })
        else:
            updated = await run_portfolio_manager(analysis)
            # 串行调用，防止同一会话并发触发限速
            if analysis != needs_pm[-1]:
                await asyncio.sleep(1)
        updated_stocks.append(updated)
    return state.model_copy(update={"stocks": updated_stocks})


async def format_report_node(state: AgentState) -> AgentState:
    """生成飞书消息文本（含辩论过程）"""
    EMOJI = {"买入": "🟢", "持有": "🟡", "观望": "🟡",
              "减仓": "🟠", "卖出": "🔴", "按计划定投": "⬜"}
    CONF  = {"高": "★★★", "中": "★★☆", "低": "★☆☆"}

    lines = [
        f"📊 卡门智投日报 · {state.date}",
        "",
        "━━━━ 今日操作建议 ━━━━",
    ]
    for s in state.stocks:
        emoji = EMOJI.get(s.recommendation, "⬜")
        conf  = CONF.get(s.confidence, "")
        lines.append(f"\n{emoji} {s.ticker}  {s.recommendation} {conf}")
        lines.append(f"   {s.one_line}")
        if s.entry_hint:
            lines.append(f"   📌 {s.entry_hint}")
        if s.key_risk:
            lines.append(f"   ⚠️  {s.key_risk}")

        # 展示辩论过程（定投标的跳过）
        if s.bull_thesis and s.ticker not in ("QQQM", "VOO"):
            if s.earnings.fundamental_view and "宽基" not in s.earnings.fundamental_view:
                lines.append(f"   📈 基本面：{s.earnings.fundamental_view}")
            lines.append(f"   🐂 多方：{s.bull_thesis}")
            lines.append(f"   🐻 空方：{s.bear_thesis}")

    if state.errors:
        lines.append(f"\n⚙️ 获取失败：{', '.join(state.errors)}")

    lines.append("\n以上仅供参考，操作前请自行判断")
    return state.model_copy(update={"report_text": "\n".join(lines)})


# ─── 构建图 ──────────────────────────────────────────────

def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("fetch_data",   fetch_data_node)
    builder.add_node("fundamentals", fundamentals_node)
    builder.add_node("debate",       debate_node)
    builder.add_node("decision",     decision_node)
    builder.add_node("format",       format_report_node)

    builder.set_entry_point("fetch_data")
    builder.add_edge("fetch_data",   "fundamentals")
    builder.add_edge("fundamentals", "debate")
    builder.add_edge("debate",       "decision")
    builder.add_edge("decision",     "format")
    builder.add_edge("format",        END)

    return builder.compile(checkpointer=checkpointer)


async def run_workflow(db_path: str = "data/agent.db") -> AgentState:
    """带 checkpoint 的完整运行（使用内存 checkpointer）"""
    checkpointer = MemorySaver()
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "daily_run"}}
    raw = await graph.ainvoke(AgentState(), config=config)
    # LangGraph ainvoke 返回 dict，转回 AgentState
    return AgentState(**raw) if isinstance(raw, dict) else raw
