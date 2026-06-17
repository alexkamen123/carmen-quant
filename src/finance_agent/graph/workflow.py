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
    save_recommendations, fill_7d_returns, fill_long_returns, accuracy_summary,
    load_all_theses, get_thesis_ages, ticker_signal_stats,
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
                signal_weight=item.get("signal_weight", "normal"),
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
        "exposure_posture": macro.exposure_posture,
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


async def memory_node(state: AgentState) -> AgentState:
    """从 mempal 拉取各股历史决策，注入 memory_context 供 PM 参考"""
    from finance_agent.memory.mempal_client import search_history, _mempal_available
    if not _mempal_available():
        return state
    updated = []
    for s in state.stocks:
        if s.is_etf:
            updated.append(s)
            continue
        ctx = search_history(s.ticker)
        updated.append(s.model_copy(update={"memory_context": ctx}) if ctx else s)
        if ctx:
            print(f"[Memory] {s.ticker} 历史决策上下文已加载（{len(ctx)} 字符）")
    return state.model_copy(update={"stocks": updated})


async def strategy_node(state: AgentState) -> AgentState:
    """注入量化策略信号（仅美股）+ L2b 实盘反馈（全市场，settings 一键关）"""
    from finance_agent.backtest.signal_lookup import format_strategy_evidence
    from finance_agent.db.tracker import format_live_feedback, get_today_dip_conclusion
    updated = []
    for s in state.stocks:
        if s.is_etf:
            updated.append(s)
            continue
        evidence = ""
        if s.market == "us":
            try:
                evidence = format_strategy_evidence(s.ticker)
            except Exception as e:
                print(f"[Strategy] {s.ticker} 信号查找失败（跳过）: {e}")
        live = ""
        try:
            live = format_live_feedback(s.ticker)
        except Exception as e:
            print(f"[Strategy] {s.ticker} 实盘反馈查找失败（跳过）: {e}")
        if evidence:
            print(f"[Strategy] {s.ticker} 量化信号触发 ↓\n{evidence}")
        if live:
            print(f"[Strategy] {s.ticker} 实盘反馈注入 ↓\n{live}")
        dip_note = ""
        try:
            dip_note = get_today_dip_conclusion(s.ticker)
        except Exception:
            pass
        if dip_note:
            print(f"[Strategy] {s.ticker} 一致性桥：注入今日盘中卡结论")
        combined = "\n".join(x for x in (evidence, live, dip_note) if x)
        updated.append(s.model_copy(update={"strategy_evidence": combined}))
    return state.model_copy(update={"stocks": updated})


async def opportunity_node(state: AgentState) -> AgentState:
    """今日机会扫描：因子库宇宙里触发中的高信赖信号（排除已持仓，标注观察池）。
    扫描失败绝不拖垮日报主流程。"""
    from finance_agent.signals.opportunities import scan_opportunities
    try:
        held = {s.ticker for s in state.stocks if s.shares}
        watch = {s.ticker for s in state.stocks if not s.shares}
        opps = scan_opportunities(exclude_tickers=held, watch_tickers=watch)
        if opps:
            tks = "、".join(o["ticker"] for o in opps[:8])
            print(f"[Opportunity] {len(opps)} 只非持仓票触发高信赖信号：{tks}")
        return state.model_copy(update={"opportunities": opps})
    except Exception as e:
        print(f"[Opportunity] 机会扫描失败（跳过，不影响日报）: {e}")
        return state


async def decision_node(state: AgentState) -> AgentState:
    """Portfolio Manager 批量裁决：1 次 Claude 调用处理所有股票"""
    updated_stocks = await run_portfolio_manager_batch(
        state.stocks,
        macro_summary=state.macro_summary,
        exposure_posture=state.exposure_posture,
    )
    return state.model_copy(update={"stocks": updated_stocks})


def _clip_md(text: str, limit: int = 45) -> str:
    """截到限长，硬切处补省略号，并修复被砍断的 Markdown 加粗。

    06-11 复盘 D3：断头句看着像 bug；
    06-15：砍在 ** 中间会漏出裸星号，需补回闭合符。
    """
    if len(text) <= limit:
        return text
    clipped = text[:limit].rstrip("*")
    if clipped.count("**") % 2:  # 砍断了一对加粗，补回闭合 **
        clipped += "**"
    return clipped + "…"


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
        if s.key_assumption:
            lines.append(f"   🔑 假设：{s.key_assumption}")
        if s.stop_loss_hint:
            lines.append(f"   🛡️ 止损：{s.stop_loss_hint}")
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
        ]
        if s.one_line:
            main_md_lines.append(f"{s.one_line}")
        # PM 降级兜底：核心字段全空（DeepSeek 降级且单票也失败）时给个交代，不留白卡
        elif not (s.entry_hint or s.key_risk or s.key_assumption):
            main_md_lines.append("_（本股本次为降级分析，详情不全，建议以持仓逻辑为准）_")
        # 短期执行建议（只在与长期不一致时显示）
        if s.short_term_action and s.short_term_action != "立即执行":
            main_md_lines.append(f"⏱️ **本周操作：{s.short_term_action}**（长期建议仍为{s.recommendation}）")
        if earnings_alert:
            main_md_lines.append(earnings_alert)
        if s.entry_hint:
            main_md_lines.append(f"📌 {s.entry_hint}")
        if s.key_risk:
            main_md_lines.append(f"⚠️ {s.key_risk}")
        if s.key_assumption:
            main_md_lines.append(f"🔑 **假设**：{s.key_assumption}")
        if s.stop_loss_hint:
            main_md_lines.append(f"🛡️ **止损**：{s.stop_loss_hint}")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(main_md_lines)},
        })

        # 辩论 + 基本面块（非 ETF，精简为 1-2 行）
        if s.bull_thesis and not is_etf:
            debate_lines = []
            fv = s.earnings.fundamental_view
            if fv and "宽基" not in fv and "暂无" not in fv:
                # 📈 基本面摘要句较长，放宽到 80 字避免拦腰断句
                fv_short = _clip_md(fv.split("。")[0].split("\n")[0], limit=80)
                debate_lines.append(f"📈 {fv_short}")
            # 多空：各取第一条论点，分两行展示
            def _first_point(text: str) -> str:
                """提取第一条编号论点或第一句，截到 45 字"""
                import re
                m = re.search(r"(?:^|\n)\s*[1１]\s*[\.．、:：]?\s*(.+)", text)
                s_ = m.group(1).strip() if m else text.split("\n")[0]
                return _clip_md(s_)
            bull_short = _first_point(s.bull_thesis)
            bear_short = _first_point(s.bear_thesis or "")
            if bull_short:
                debate_lines.append(f"🐂 {bull_short}")
            if bear_short:
                debate_lines.append(f"🐻 {bear_short}")
            # 信号历史面板：只对买入/加仓显示，样本不足时加警示
            if s.recommendation in ("买入",) or "加" in (s.position_change or ""):
                hist = ticker_signal_stats(s.ticker)
                if hist:
                    debate_lines.append(hist)
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
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                       "content": "以上仅供参考，操作前请自行判断。"
                                  "如实际操作，可运行 finance-agent log-action TICKER BUY/SELL 记录"}],
    })

    # 宏观 + 集中度 + L1战略红线状态 放在卡片最顶部
    macro_elements: list[dict] = []
    header_lines = []
    if state.macro_summary:
        header_lines.append(f"🌍 **宏观（L2战术层）**｜{state.macro_summary}")
    sector_str = _sector_summary(state.stocks)
    if sector_str and sector_str != "暂无持仓市值数据":
        header_lines.append(f"📂 **集中度**｜{sector_str}")

    # 读取战略红线状态，生成红绿灯行
    try:
        _cfg = Path(__file__).parents[3] / "config"   # .../finance-agent/config（原多一级 parent，红绿灯一直没显示）
        _settings = yaml.safe_load(open(_cfg / "settings.yaml")) or {}
        _inj = _settings.get("strategy_injection", {})
        if _inj.get("enabled", True) and _inj.get("show_limit_status_in_daily", True):
            _port = yaml.safe_load(open(_cfg / "portfolio.yaml")) or {}
            _strat = _port.get("strategy", {})
            _limits = _strat.get("risk_limits", [])
            if _limits:
                limit_icons = []
                for lim in _limits:
                    # 简单解析 rule 中的数值与当前值做红绿灯判断
                    rule_str = lim.get("rule", "")
                    current_str = lim.get("current", "")
                    name = lim.get("name", "")
                    try:
                        import re
                        threshold = float(re.search(r"[\d.]+", rule_str.split("<=")[-1] if "<=" in rule_str else rule_str.split(">=")[-1]).group())
                        current_val = float(re.search(r"[\d.]+", current_str).group()) if current_str else None
                        if current_val is not None:
                            if "<=" in rule_str:
                                icon = "🟢" if current_val <= threshold * 0.9 else ("🟡" if current_val <= threshold else "🔴")
                            else:
                                icon = "🟢" if current_val >= threshold * 1.1 else ("🟡" if current_val >= threshold else "🔴")
                            limit_icons.append(f"{icon} {name}：{current_str}/{threshold}%")
                    except Exception:
                        limit_icons.append(f"⚪ {name}")
                if limit_icons:
                    header_lines.append(f"🎯 **L1战略红线**｜{' | '.join(limit_icons)}")
    except Exception:
        pass  # 红线状态读取失败不影响主报告

    if header_lines:
        macro_elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(header_lines)}},
            {"tag": "hr"},
        ]

    # ── 今日速览 TL;DR（置顶：把"该干啥"从逐股列表里拎到最前）──
    buy_t = [s.ticker for s in state.stocks
             if s.recommendation == "买入" or (s.position_change or "").startswith(("大加", "小加"))]
    sell_t = [s.ticker for s in state.stocks
              if s.recommendation in ("减仓", "卖出") or (s.position_change or "").startswith("减仓")]
    tldr_parts = []
    if buy_t:
        tldr_parts.append(f"🟢 加/买：{'、'.join(buy_t)}")
    if sell_t:
        tldr_parts.append(f"🔴 减/卖：{'、'.join(sell_t)}")
    tldr_body = "　·　".join(tldr_parts) if tldr_parts else "今日无买卖建议，持有/定投为主"
    tldr_elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**📋 今日速览**\n{tldr_body}"}},
        {"tag": "hr"},
    ]

    # ── 今日机会区（非持仓票的触发中高信赖信号，紧跟速览）──
    from finance_agent.signals.opportunities import format_opportunity_section
    opp_text = format_opportunity_section(
        state.opportunities,
        semi_room_note="⚠️ 半导体类标的受 L1 集中度红线约束，加仓前看顶部红线余量",
    )
    if opp_text:
        tldr_elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": opp_text}},
            {"tag": "hr"},
        ]

    # 名词解释（P4：就近放在宏观块之后，不再沉到卡片最底部要用户滚到底）。
    # 只检测"宏观行 + 机会区"——这两处黑话最密（VIX/出货日/动量突破/超额…）；逐股已有
    # 大白话 one_line + 多空翻译，不缺解释。若把逐股长文也并进来，RSI/MACD/布林等基础词
    # 会按字典序挤占名额，反而把用户点名的"动量突破/超额"截断掉。
    glossary_src = (state.macro_summary or "") + " " + opp_text
    glossary_el = build_glossary_element(glossary_src, max_terms=10)
    glossary_block = [glossary_el, {"tag": "hr"}] if glossary_el else []

    report_card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📊 卡门智投日报 · {state.date}"},
            "template": TEMPLATE.get(dominant, "blue"),
        },
        "elements": tldr_elements + macro_elements + glossary_block + elements,
    }

    return state.model_copy(update={"report_text": report_text, "report_card": report_card})


async def track_node(state: AgentState) -> AgentState:
    """保存当日推荐到 SQLite，并回填 10 天前的历史记录"""
    # 先回填历史（7 个交易日 ≈ 10 日历日）
    await fill_7d_returns()
    # 30/90 日窗口回填（pending 为空时仅一次 SQL 零网络）；不许炸掉日报主链路
    try:
        await fill_long_returns()
    except Exception as e:
        print(f"[Tracker] 30/90日回填跳过：{e}")

    # 保存今日推荐
    records = [
        {
            "ticker":          s.ticker,
            "market":          s.market,
            "recommendation":  s.recommendation,
            "confidence":      s.confidence,
            "position_change": s.position_change,
            "price_at_rec":    s.signals.close if s.signals else None,
            # shares=0 = watchlist 影子推荐（纯测量、无真实仓位），记分牌分栏统计
            "is_watch":        0 if s.shares else 1,
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
    builder.add_node("strategy",     strategy_node)
    builder.add_node("memory",       memory_node)
    builder.add_node("decision",     decision_node)
    builder.add_node("opportunity",  opportunity_node)
    builder.add_node("format",       format_report_node)
    builder.add_node("track",        track_node)

    builder.set_entry_point("fetch_data")
    builder.add_edge("fetch_data",   "thesis")
    builder.add_edge("thesis",       "fundamentals")
    builder.add_edge("fundamentals", "debate")
    builder.add_edge("debate",       "strategy")
    builder.add_edge("strategy",     "memory")
    builder.add_edge("memory",       "decision")
    builder.add_edge("decision",     "opportunity")
    builder.add_edge("opportunity",  "format")
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
