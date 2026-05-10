# src/finance_agent/graph/workflow.py
import asyncio
from datetime import datetime, date as date_type
import yaml
from pathlib import Path
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from finance_agent.graph.state import AgentState, StockAnalysis, NewsItem, EarningsSummary
from finance_agent.data.router import DataRouter
from finance_agent.signals.technical import calculate_signals
from finance_agent.agents.bull_agent import run_bull_analysis
from finance_agent.agents.bear_agent import run_bear_analysis
from finance_agent.agents.portfolio_manager import run_portfolio_manager_batch, _sector_summary
from finance_agent.agents.fundamental_analyst import run_fundamental_analysis
from finance_agent.data.macro import fetch_macro_context

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
            return StockAnalysis(
                ticker=ticker, market=market, signals=signals, news=news, earnings=earnings,
                shares=item.get("shares", 0.0),
                sector=item.get("sector", ""),
            )
        except Exception as e:
            state.errors.append(f"{ticker}: {e}")
            return None

    stock_results, macro = await asyncio.gather(
        asyncio.gather(*[fetch_one(h) for h in all_holdings]),
        fetch_macro_context(),
    )
    stocks = [r for r in stock_results if r is not None]
    print(f"[Macro] {macro.to_prompt_str()}")
    return state.model_copy(update={
        "stocks": stocks,
        "date": datetime.today().strftime("%Y-%m-%d"),
        "macro_summary": macro.to_prompt_str(),
    })


async def fundamentals_node(state: AgentState) -> AgentState:
    """用 Claude 分析每只股票基本面（串行，Claude CLI 判断能力更强）"""
    etf_tickers = {"QQQM", "VOO"}
    updated = []
    for analysis in state.stocks:
        if analysis.ticker in etf_tickers:
            updated.append(analysis.model_copy(update={
                "earnings": analysis.earnings.model_copy(
                    update={"fundamental_view": "宽基 ETF，按定投计划执行"}
                )
            }))
        else:
            updated.append(await run_fundamental_analysis(analysis))
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
    """Portfolio Manager 批量裁决：1 次 Claude 调用处理所有股票"""
    updated_stocks = await run_portfolio_manager_batch(
        state.stocks, macro_summary=state.macro_summary
    )
    return state.model_copy(update={"stocks": updated_stocks})


async def format_report_node(state: AgentState) -> AgentState:
    """生成飞书卡片（含辩论过程）和控制台文本"""
    EMOJI      = {"买入": "🟢", "持有": "🟡", "观望": "🟡",
                  "减仓": "🟠", "卖出": "🔴", "按计划定投": "⬜"}
    CONF       = {"高": "★★★", "中": "★★☆", "低": "★☆☆"}
    # 飞书卡片 header 配色
    TEMPLATE   = {"买入": "green", "持有": "blue", "观望": "blue",
                  "减仓": "orange", "卖出": "red", "按计划定投": "grey"}

    # ── 控制台文本（保留，供 --skip-notify 调试用）──────────────
    lines = [f"📊 卡门智投日报 · {state.date}", "", "━━━━ 今日操作建议 ━━━━"]
    for s in state.stocks:
        emoji = EMOJI.get(s.recommendation, "⬜")
        conf  = CONF.get(s.confidence, "")
        lines.append(f"\n{emoji} {s.ticker}  {s.recommendation} {conf}")
        lines.append(f"   {s.one_line}")
        if s.entry_hint:
            lines.append(f"   📌 {s.entry_hint}")
        if s.key_risk:
            lines.append(f"   ⚠️  {s.key_risk}")
        if s.bull_thesis and s.ticker not in ("QQQM", "VOO"):
            if s.earnings.fundamental_view and "宽基" not in s.earnings.fundamental_view:
                lines.append(f"   📈 基本面：{s.earnings.fundamental_view}")
            lines.append(f"   🐂 多方：{s.bull_thesis}")
            lines.append(f"   🐻 空方：{s.bear_thesis}")
    if state.errors:
        lines.append(f"\n⚙️ 获取失败：{', '.join(state.errors)}")
    lines.append("\n以上仅供参考，操作前请自行判断")
    report_text = "\n".join(lines)

    # ── 飞书卡片 JSON ────────────────────────────────────────────
    # 决定 header 配色：取第一个非定投标的的建议色，否则用蓝色
    dominant = next(
        (s.recommendation for s in state.stocks if s.recommendation not in ("按计划定投", "")),
        "持有"
    )
    elements: list[dict] = []

    for i, s in enumerate(state.stocks):
        emoji = EMOJI.get(s.recommendation, "⬜")
        conf  = CONF.get(s.confidence, "")
        is_etf = s.ticker in ("QQQM", "VOO")

        # 财报预警（距今 ≤7 天）
        earnings_alert = ""
        ned = s.earnings.next_earnings_date
        if ned:
            try:
                days_to_earnings = (datetime.strptime(ned, "%Y-%m-%d").date() - datetime.today().date()).days
                if 0 <= days_to_earnings <= 3:
                    earnings_alert = f"🔔 **财报预警**：{ned}（{days_to_earnings}天后）"
                elif days_to_earnings <= 7:
                    earnings_alert = f"📅 财报临近：{ned}（{days_to_earnings}天后）"
            except ValueError:
                pass

        # 仓位建议标签
        POS_EMOJI = {"减仓": "🔽", "维持": "➡️", "小加": "🔼", "大加": "⏫"}
        pos_icon = next((v for k, v in POS_EMOJI.items() if k in (s.position_change or "")), "➡️")

        # 主推荐块
        main_md_lines = [
            f"**{emoji} {s.ticker}**　{s.recommendation}　{conf}　{pos_icon} {s.position_change or '维持'}",
            f"{s.one_line}",
        ]
        if earnings_alert:
            main_md_lines.append(earnings_alert)
        if s.entry_hint:
            main_md_lines.append(f"📌 {s.entry_hint}")
        if s.key_risk:
            main_md_lines.append(f"⚠️ {s.key_risk}")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(main_md_lines)},
        })

        # 辩论 + 基本面块（非 ETF）
        if s.bull_thesis and not is_etf:
            debate_lines = []
            fv = s.earnings.fundamental_view
            if fv and "宽基" not in fv and "暂无" not in fv:
                debate_lines.append(f"📈 **基本面**：{fv}")
            debate_lines.append(f"🐂 **多方**：{s.bull_thesis}")
            debate_lines.append(f"🐻 **空方**：{s.bear_thesis}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(debate_lines)},
            })

        # 分隔线（最后一只不加）
        if i < len(state.stocks) - 1:
            elements.append({"tag": "hr"})

    # 底部提示
    if state.errors:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": f"⚙️ 数据获取失败：{', '.join(state.errors)}"}],
        })
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "以上仅供参考，操作前请自行判断"}],
    })

    # 宏观 + 集中度放在卡片最顶部
    macro_elements: list[dict] = []
    header_lines = []
    if state.macro_summary:
        header_lines.append(f"🌍 **宏观**｜{state.macro_summary}")
    sector_str = _sector_summary(state.stocks)
    if sector_str and sector_str != "暂无持仓市值数据":
        header_lines.append(f"📂 **集中度**｜{sector_str}")
    if header_lines:
        macro_elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(header_lines)}},
            {"tag": "hr"},
        ]

    report_card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 卡门智投日报 · {state.date}"},
            "template": TEMPLATE.get(dominant, "blue"),
        },
        "elements": macro_elements + elements,
    }

    return state.model_copy(update={"report_text": report_text, "report_card": report_card})


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
