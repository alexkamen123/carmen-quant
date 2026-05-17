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
from finance_agent.db.tracker import (
    save_recommendations, fill_7d_returns, accuracy_summary,
    load_all_theses, get_thesis_ages,
)
from finance_agent.notifications.glossary import build_glossary_element
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
        sector = item.get("sector", "")
        is_etf = "ETF" in sector or item.get("is_dca", False)
        try:
            df = await router.fetch_ohlcv(ticker, market, days=60)
            signals = calculate_signals(df, ticker=ticker)
            news_raw = await router.fetch_news(ticker, market, limit=3)
            news = [NewsItem(**n) for n in news_raw]
            # ETF 没有 PE/营收等基本面数据，跳过 API 调用避免 404 噪音
            if is_etf:
                earnings_raw = {}
            else:
                earnings_raw = await router.fetch_earnings(ticker, market)
            earnings = EarningsSummary(**{k: v for k, v in earnings_raw.items() if v is not None})
            cost_basis = float(item.get("cost_basis") or 0.0)
            current_price = float(signals.close) if signals else 0.0
            unrealized_pnl_pct = (
                round((current_price / cost_basis - 1) * 100, 1)
                if cost_basis > 0 and current_price > 0 else None
            )
            # 竞争对手新闻（取各 peer 最新 2 条，最多 3 个 peer）
            peer_news: list[NewsItem] = []
            for peer in item.get("peers", [])[:3]:
                try:
                    peer_raw = await router.fetch_news(peer, market, limit=2)
                    for n in peer_raw:
                        peer_news.append(NewsItem(
                            title=f"[{peer}] {n['title']}",
                            summary=n.get("summary", ""),
                            published=n.get("published", ""),
                        ))
                except Exception:
                    pass
            return StockAnalysis(
                ticker=ticker, market=market, signals=signals,
                news=news, peer_news=peer_news, earnings=earnings,
                shares=item.get("shares", 0.0),
                sector=sector,
                is_etf=is_etf,
                cost_basis=cost_basis,
                unrealized_pnl_pct=unrealized_pnl_pct,
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


_THESIS_STALE_DAYS = 30


async def thesis_node(state: AgentState) -> AgentState:
    """从 DB 加载每只股票的持仓逻辑，超过 30 天自动触发重生成。"""
    from finance_agent.db.thesis_generator import generate_thesis_for
    import yaml as _yaml
    from pathlib import Path as _Path

    all_theses = load_all_theses()
    ages = get_thesis_ages()

    # 读取 portfolio.yaml 供自动重生成时使用
    _config = _Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(_config) as _f:
        _port = _yaml.safe_load(_f)
    _holding_map = {h["ticker"]: h for h in _port.get("holdings", [])}

    updated = []
    for s in state.stocks:
        thesis = all_theses.get(s.ticker, "")
        age = ages.get(s.ticker, 9999)
        stale = age >= _THESIS_STALE_DAYS and not s.is_etf

        if stale:
            print(f"[Thesis] {s.ticker} 持仓逻辑已 {age} 天未更新，自动重生成...")
            h = _holding_map.get(s.ticker, {})
            try:
                thesis = await generate_thesis_for(
                    ticker=s.ticker,
                    market=h.get("market", s.market or "us"),
                    cost_basis=float(h.get("cost_basis") or 0),
                    shares=float(h.get("shares", 0)),
                    notes=h.get("notes", ""),
                    force=True,
                )
            except Exception as e:
                print(f"[Thesis] 自动重生成失败 {s.ticker}: {e}")
                print(f"[Thesis] ⚠️ WARNING: {s.ticker} 持仓逻辑已 {age} 天未更新，重生成失败，继续使用旧版本")
                stale = False  # 保留旧 thesis，不标为 stale

        updated.append(s.model_copy(update={"thesis": thesis, "thesis_stale": stale}))

    loaded = sum(1 for s in updated if s.thesis)
    stale_tickers = [s.ticker for s in updated if s.thesis_stale]
    if stale_tickers:
        print(f"[Thesis] 自动重生成：{', '.join(stale_tickers)}")
    elif loaded:
        print(f"[Thesis] 加载 {loaded} 只股票的持仓逻辑（均在新鲜期内）")
    return state.model_copy(update={"stocks": updated})


async def fundamentals_node(state: AgentState) -> AgentState:
    """用 Claude 分析每只股票基本面（串行，Claude CLI 判断能力更强）"""
    updated = []
    for analysis in state.stocks:
        if analysis.is_etf:
            updated.append(analysis.model_copy(update={
                "earnings": analysis.earnings.model_copy(
                    update={"fundamental_view": "宽基/股息 ETF，按定投计划执行"}
                )
            }))
        else:
            updated.append(await run_fundamental_analysis(analysis))
    return state.model_copy(update={"stocks": updated})


async def debate_node(state: AgentState) -> AgentState:
    """Bull/Bear 辩论：ETF 直接标记，个股两层并行（先并行所有 Bull，再并行所有 Bear）"""
    etf_stocks = [s for s in state.stocks if s.is_etf]
    non_etf = [s for s in state.stocks if not s.is_etf]

    etf_updated = [
        s.model_copy(update={
            "bull_thesis": "定投标的，按月计划执行",
            "bear_thesis": "定投标的，不做短期判断",
        })
        for s in etf_stocks
    ]

    if non_etf:
        # 限制并发为 3，避免 DeepSeek API 限速（原串行改并行后的保护）
        sem = asyncio.Semaphore(3)

        async def _bull(s):
            async with sem:
                return await run_bull_analysis(s)

        async def _bear(s):
            async with sem:
                return await run_bear_analysis(s)

        bull_results = list(await asyncio.gather(*[_bull(s) for s in non_etf]))
        bear_results = list(await asyncio.gather(*[_bear(s) for s in bull_results]))
    else:
        bear_results = []

    # 按原顺序合并
    result_map = {s.ticker: s for s in etf_updated + bear_results}
    return state.model_copy(update={"stocks": [result_map[s.ticker] for s in state.stocks]})


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
        if s.short_term_action and s.short_term_action != "立即执行":
            lines.append(f"   ⏱️  本周：{s.short_term_action}")
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
        # 短期执行建议（只在与长期不一致时显示）
        if s.short_term_action and s.short_term_action != "立即执行":
            main_md_lines.append(f"⏱️ **本周操作：{s.short_term_action}**（长期建议仍为{s.recommendation}）")
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

        # 辩论 + 基本面块（非 ETF，精简为 1-2 行）
        if s.bull_thesis and not is_etf:
            debate_lines = []
            # 基本面：只取第一句（截到第一个句号/换行）
            fv = s.earnings.fundamental_view
            if fv and "宽基" not in fv and "暂无" not in fv:
                fv_short = fv.split("。")[0].split("\n")[0][:60]
                debate_lines.append(f"📈 {fv_short}")
            # 多空：各取第一条论点，合并一行
            def _first_point(text: str) -> str:
                """提取第一条编号论点或第一句，截到 30 字"""
                import re
                m = re.search(r"(?:^|\n)\s*[1１]\s*[\.．、:：]?\s*(.+)", text)
                s_ = m.group(1).strip() if m else text.split("\n")[0]
                return s_[:30]
            bull_short = _first_point(s.bull_thesis)
            bear_short = _first_point(s.bear_thesis or "")
            if bull_short or bear_short:
                debate_lines.append(f"🐂 {bull_short}　｜　🐻 {bear_short}")
            if debate_lines:
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(debate_lines)},
                })

        # 分隔线（最后一只不加）
        if i < len(state.stocks) - 1:
            elements.append({"tag": "hr"})

    # 底部：历史准确率 + 错误提示 + 名词解释 + 免责声明
    acc = accuracy_summary(days=30)
    if acc:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"📊 {acc}"}],
        })
    if state.errors:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": f"⚙️ 数据获取失败：{', '.join(state.errors)}"}],
        })
    # 名词解释：把整张卡片的文字拼在一起，检测出现了哪些术语
    full_text = " ".join(
        s.one_line + " " + (s.bull_thesis or "") + " " + (s.bear_thesis or "") +
        " " + (s.entry_hint or "") + " " + (s.key_risk or "")
        for s in state.stocks
    ) + " " + (state.macro_summary or "")
    glossary_el = build_glossary_element(full_text, max_terms=5)
    if glossary_el:
        elements.append({"tag": "hr"})
        elements.append(glossary_el)
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                       "content": "以上仅供参考，操作前请自行判断。"
                                  "如实际操作，可运行 finance-agent log-action TICKER BUY/SELL 记录"}],
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


async def track_node(state: AgentState) -> AgentState:
    """保存当日推荐到 SQLite，并回填 10 天前的历史记录"""
    # 先回填历史（7 个交易日 ≈ 10 日历日）
    await fill_7d_returns()

    # 保存今日推荐
    records = [
        {
            "ticker":          s.ticker,
            "recommendation":  s.recommendation,
            "confidence":      s.confidence,
            "position_change": s.position_change,
            "price_at_rec":    s.signals.close if s.signals else None,
        }
        for s in state.stocks
        if s.recommendation  # 跳过没有裁决的股票
    ]
    save_recommendations(state.date, records)
    return state


# ─── 构建图 ──────────────────────────────────────────────

def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("fetch_data",   fetch_data_node)
    builder.add_node("thesis",       thesis_node)
    builder.add_node("fundamentals", fundamentals_node)
    builder.add_node("debate",       debate_node)
    builder.add_node("decision",     decision_node)
    builder.add_node("format",       format_report_node)
    builder.add_node("track",        track_node)

    builder.set_entry_point("fetch_data")
    builder.add_edge("fetch_data",   "thesis")
    builder.add_edge("thesis",       "fundamentals")
    builder.add_edge("fundamentals", "debate")
    builder.add_edge("debate",       "decision")
    builder.add_edge("decision",     "format")
    builder.add_edge("format",       "track")
    builder.add_edge("track",         END)

    return builder.compile(checkpointer=checkpointer)


async def run_workflow(db_path: str = "data/agent.db") -> AgentState:
    """带 checkpoint 的完整运行（使用内存 checkpointer）"""
    checkpointer = MemorySaver()
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "daily_run"}}
    raw = await graph.ainvoke(AgentState(), config=config)
    # LangGraph ainvoke 返回 dict，转回 AgentState
    return AgentState(**raw) if isinstance(raw, dict) else raw
