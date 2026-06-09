# src/finance_agent/main.py
import asyncio
from pathlib import Path
import typer
from rich.console import Console
from dotenv import load_dotenv

from finance_agent.graph.workflow import run_workflow
from finance_agent.storage.db import init_db, save_daily_signals
from finance_agent.notifications.feishu import send_feishu_card, send_feishu_message
from finance_agent.backtest.engine import backfill_yesterday
from finance_agent.backtest.historical import backtest_ticker, backtest_portfolio
from finance_agent.alerts.news_monitor import run_news_scan, run_price_scan
from finance_agent.db.thesis_generator import generate_all_theses, generate_thesis_for
from finance_agent.db.tracker import (
    list_theses, log_user_action, get_action_history,
    backfill_action_returns, get_feedback_accuracy, feedback_summary,
    get_dip_stats, backfill_dip_outcomes, detect_portfolio_changes,
)
from finance_agent.alerts.earnings_trigger import check_and_alert_earnings
from finance_agent.weekly.allocation_advisor import run_allocation_advisor
from finance_agent.weekly.report_card import build_weekly_card
from finance_agent.weekly.daily_followup import run_daily_followup
from finance_agent.monthly.review import run_monthly_review
from finance_agent.morning.note import run_morning_note
from finance_agent.value.report import run_value_report
from finance_agent.memory.mempal_client import (
    ingest_daily_report,
    ingest_weekly_report,
    ingest_monthly_review,
)

load_dotenv()

app = typer.Typer(help="卡门家庭量化交易助手")
console = Console()
DB_PATH = "data/agent.db"

# 让 tracker.py / thesis_generator.py 使用与 main 一致的 DB 路径
import os as _os
_os.environ.setdefault("AGENT_DB_PATH", DB_PATH)


@app.command()
def run(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
    backfill:    bool = typer.Option(True,  "--backfill/--no-backfill", help="运行前回填昨日胜率"),
):
    """运行每日分析并推送飞书"""
    asyncio.run(_run(skip_notify=skip_notify, backfill=backfill))


async def _run(skip_notify: bool, backfill: bool):
    Path("data").mkdir(exist_ok=True)
    await init_db(DB_PATH)

    # Step 0: 检测 portfolio.yaml 持仓变更，自动记录买卖操作（喂行为闭环）
    try:
        changes = detect_portfolio_changes(db_path=DB_PATH)
        for c in changes:
            price = f"@{c['price']}" if c.get("price") else ""
            console.print(f"   🔄 自动记录 {c['ticker']} {c['action']} {c['shares']}股{price}")
    except Exception as e:
        console.print(f"[yellow]⚠️ 持仓变更检测跳过：{e}[/yellow]")

    # Step 1: 回填昨日胜率 + 暴跌告警回测
    if backfill:
        console.print("📊 回填昨日信号胜率...")
        win_rates = await backfill_yesterday(DB_PATH)
        for ticker, rate in win_rates.items():
            console.print(f"   {ticker}: {rate:.0%}")
        filled = backfill_dip_outcomes(db_path=DB_PATH)
        if filled:
            console.print(f"📉 回填 {filled} 条暴跌告警回测价格")

    # Step 2: 运行今日分析
    console.print("🤖 开始今日分析...")
    state = await run_workflow(db_path=DB_PATH)

    # Step 3: 保存到 SQLite
    await save_daily_signals(state, DB_PATH)
    console.print(f"💾 已保存 {len(state.stocks)} 只股票信号")

    # Step 4: 打印报告
    console.print("\n" + state.report_text)

    # Step 5: 存入 mempal 决策记忆库（仅日报全文，新闻预警/轻量跟进不存）
    ingest_daily_report(state.report_text)

    # Step 6: 推送飞书（优先卡片，降级纯文本）
    if not skip_notify:
        if state.report_card:
            ok = await send_feishu_card(state.report_card, fallback_text=state.report_text)
        else:
            ok = await send_feishu_message(state.report_text, fallback_subject="⚠️ 飞书推送失败 | 卡门智投日报")
        console.print("✅ 飞书推送成功" if ok else "❌ 飞书推送失败")


@app.command("news-scan")
def news_scan(
    threshold: int = typer.Option(7, "--threshold", "-t", help="影响度阈值（>= 此值才推送）"),
):
    """扫描持仓新闻，高影响立即推送飞书提醒"""
    pushed = asyncio.run(run_news_scan(impact_threshold=threshold))
    console.print(f"{'✅' if pushed else '⚪'} 扫描完成，推送 {pushed} 条高影响新闻")


@app.command("price-scan")
def price_scan(
    threshold: float = typer.Option(3.0, "--threshold", "-t", help="1小时跌幅阈值（%），默认 3.0"),
):
    """轻量价格异动扫描，跳过新闻，约 30 秒，适合每 5 分钟触发"""
    pushed = asyncio.run(run_price_scan(threshold_pct=threshold))
    console.print(f"{'✅' if pushed else '⚪'} 价格扫描完成，推送 {pushed} 条异动")


@app.command("weekly-report")
def weekly_report(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
    force: bool = typer.Option(False, "--force", "-f", help="强制重新生成（忽略本周缓存）"),
):
    """运行周度配置建议（配置诊断 → 对冲选品 → 机会筛选）"""
    asyncio.run(_weekly_report(skip_notify=skip_notify, force=force))


async def _weekly_report(skip_notify: bool, force: bool = False):
    console.print("📊 开始周度配置建议分析...")
    result = await run_allocation_advisor(force=force)

    diagnosis = result.get("diagnosis", {})
    console.print(f"[诊断] {diagnosis.get('concentration_risk', '')}")
    console.print(f"[宏观] {diagnosis.get('macro_risk', '')}")
    console.print(f"[对冲方向] {len(diagnosis.get('hedge_directions', []))} 个")
    console.print(f"[对冲品种] {sum(len(b.get('instruments', [])) for b in result.get('hedge_instruments', []))} 个")
    console.print(f"[机会筛选] {len(result.get('opportunities', []))} 只（初筛 {result.get('candidates_screened', 0)} 只）")

    ingest_weekly_report(result)

    if not skip_notify:
        card = build_weekly_card(result)
        weekly_text = (
            f"卡门智投周报\n"
            f"集中度风险：{diagnosis.get('concentration_risk', '')}\n"
            f"宏观风险：{diagnosis.get('macro_risk', '')}\n"
            f"对冲方向：{len(diagnosis.get('hedge_directions', []))} 个\n"
            f"机会：{len(result.get('opportunities', []))} 只"
        )
        ok = await send_feishu_card(card, fallback_text=weekly_text)
        console.print("✅ 周度报告推送成功" if ok else "❌ 周度报告推送失败")


@app.command("daily-followup")
def daily_followup(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """周二到周五：基于周一周报做轻量跟进（价格变化 + 是否还有机会）"""
    asyncio.run(_daily_followup(skip_notify=skip_notify))


async def _daily_followup(skip_notify: bool):
    console.print("📌 开始每日配置跟进...")
    result = await run_daily_followup()
    if result is None:
        console.print("⚪ 无周报数据，跳过今日跟进")
        return
    console.print(result["text"])
    if not skip_notify:
        card = result.get("card")
        text = result["text"]
        if card:
            ok = await send_feishu_card(card, fallback_text=text)
        else:
            ok = await send_feishu_message(text, fallback_subject="⚠️ 飞书推送失败 | 卡门智投每日跟进")
        console.print("✅ 跟进推送成功" if ok else "❌ 跟进推送失败")


@app.command("generate-theses")
def generate_theses(
    force: bool = typer.Option(False, "--force", "-f", help="强制重新生成（覆盖已有记录）"),
    ticker: str = typer.Option("", "--ticker", "-t", help="只生成指定股票，留空则全部"),
):
    """为持仓股票生成/更新持仓逻辑（Thesis），写入 SQLite"""
    asyncio.run(_generate_theses(force=force, ticker=ticker))


async def _generate_theses(force: bool, ticker: str):
    import yaml
    from pathlib import Path
    if ticker:
        config_path = Path("config/portfolio.yaml")
        with open(config_path) as f:
            portfolio = yaml.safe_load(f)
        holding = next(
            (h for h in portfolio.get("holdings", []) if h["ticker"] == ticker), None
        )
        if not holding:
            console.print(f"[red]未找到 {ticker} 的持仓记录[/red]")
            return
        thesis = await generate_thesis_for(
            ticker=ticker, market=holding.get("market", "us"),
            cost_basis=float(holding.get("cost_basis") or 0),
            shares=float(holding.get("shares", 0)),
            notes=holding.get("notes", ""), force=force,
        )
        console.print(f"\n[bold]{ticker} 持仓逻辑：[/bold]\n{thesis}")
    else:
        results = await generate_all_theses(force=force)
        console.print(f"✅ 共生成 {len(results)} 只股票的持仓逻辑")
        for t, th in results.items():
            console.print(f"\n[bold]{t}：[/bold]\n{th[:120]}...")


@app.command("show-theses")
def show_theses():
    """展示所有已保存的持仓逻辑摘要"""
    rows = list_theses()
    if not rows:
        console.print("暂无持仓逻辑，请先运行 finance-agent generate-theses")
        return
    for r in rows:
        console.print(f"\n[bold]{r['ticker']}[/bold]（{r['market']}，更新：{r['updated_at'][:10]}）")
        console.print(f"  {r['preview']}...")


@app.command("morning-note")
def morning_note(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """盘前晨报：联网搜索隔夜全球市场动态，生成晨报并推送飞书"""
    asyncio.run(_morning_note(skip_notify=skip_notify))


async def _morning_note(skip_notify: bool):
    console.print("🌅 开始生成盘前晨报（联网搜索中，约需 1-3 分钟）...")
    card, text, note = await run_morning_note()
    console.print(f"\n[bold]核心判断：[/bold]{note.get('headline', '')}")
    console.print(text)
    if not skip_notify:
        ok = await send_feishu_card(card, fallback_text=text)
        console.print("✅ 晨报推送成功" if ok else "❌ 晨报推送失败")


@app.command("value-report")
def value_report(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """价值体检：诚实量化我们的建议/用户行为/风险预警到底有没有创造投资价值"""
    asyncio.run(_value_report(skip_notify=skip_notify))


async def _value_report(skip_notify: bool):
    console.print("🏆 生成价值体检报告（回填最新数据中）...")
    card, text, m = await run_value_report(db_path=DB_PATH)
    console.print("\n" + text)
    if not skip_notify:
        ok = await send_feishu_card(card, fallback_text=text)
        console.print("✅ 价值体检推送成功" if ok else "❌ 价值体检推送失败")


@app.command("monthly-review")
def monthly_review(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """生成上月投资回顾（准确率统计 + Claude 总结），推送飞书"""
    asyncio.run(_monthly_review(skip_notify=skip_notify))


async def _monthly_review(skip_notify: bool):
    console.print("📆 开始月度投资回顾...")
    result = await run_monthly_review(db_path_str=DB_PATH)
    if result is None:
        console.print("⚪ 上月无已回填数据，跳过月度回顾")
        return
    card, summary, stats, scorecard = result
    console.print("✅ 月度回顾已生成")
    for d in scorecard.get("dimensions", []):
        console.print(f"   {d['name']}: {d['score']}/10 — {d.get('reason', '')}")
    ingest_monthly_review(summary, stats)
    if not skip_notify:
        ok = await send_feishu_card(card)
        console.print("✅ 月度回顾推送成功" if ok else "❌ 月度回顾推送失败")


@app.command("sync-actions")
def sync_actions(
    dry_run: bool = typer.Option(False, "--dry-run", help="只检测不写库，预览将记录的操作"),
):
    """检测 portfolio.yaml 持仓变更，自动记录为买卖操作（喂给行为闭环）"""
    changes = detect_portfolio_changes(db_path=DB_PATH, dry_run=dry_run)
    if not changes:
        console.print("⚪ 未检测到持仓变更（或首次建立基线）")
        return
    tag = "（dry-run，未写库）" if dry_run else ""
    console.print(f"✅ 检测到 {len(changes)} 笔操作{tag}：")
    for c in changes:
        price = f"@{c['price']}" if c.get("price") else "（价格未知）"
        console.print(f"   {c['ticker']} {c['action']} {c['shares']}股 {price}  [{c['note']}]")


@app.command("log-action")
def log_action(
    ticker: str = typer.Argument(..., help="股票代码，如 NVDA"),
    action: str = typer.Argument(..., help="操作类型：BUY / SELL / TRIM / HOLD / SKIP"),
    shares: float = typer.Option(None, "--shares", "-s", help="操作股数"),
    price: float  = typer.Option(None, "--price",  "-p", help="操作价格"),
    note: str     = typer.Option("",  "--note",   "-n", help="备注"),
):
    """记录实际操作（BUY/SELL/TRIM/HOLD/SKIP），与当日推荐关联"""
    log_user_action(ticker=ticker, action=action, shares=shares,
                    price=price, note=note, db_path=DB_PATH)
    console.print(f"✅ 已记录：{ticker.upper()} {action.upper()}")


@app.command("show-actions")
def show_actions(
    ticker: str = typer.Option("", "--ticker", "-t", help="只看某只股票，留空看全部"),
    days:   int = typer.Option(30, "--days",   "-d", help="最近 N 天"),
):
    """展示最近的操作记录"""
    rows = get_action_history(
        ticker=ticker if ticker else None, days=days, db_path=DB_PATH
    )
    if not rows:
        console.print(f"近 {days} 天无操作记录")
        return
    for r in rows:
        parts = [f"[bold]{r['date']}[/bold]", r["ticker"], r["action"]]
        if r["shares"]:
            parts.append(f"{r['shares']}股")
        if r["price"]:
            parts.append(f"@{r['price']}")
        if r["note"]:
            parts.append(f"（{r['note']}）")
        console.print("  ".join(parts))


@app.command()
def backfill_only():
    """仅回填昨日胜率，不运行新分析"""
    asyncio.run(backfill_yesterday(DB_PATH))
    console.print("✅ 胜率回填完成")


@app.command("earnings-check")
def earnings_check(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """检查持仓股近 7 天内是否有财报，有则推送飞书预警"""
    asyncio.run(_earnings_check(skip_notify=skip_notify))


async def _earnings_check(skip_notify: bool):
    console.print("📅 检查持仓股财报日期...")
    upcoming = await check_and_alert_earnings(push=not skip_notify)
    if not upcoming:
        console.print("✅ 未来 7 天内无持仓股财报")
    else:
        for item in upcoming:
            console.print(
                f"  🔔 [bold]{item['ticker']}[/bold] 财报：{item['earnings_date']} "
                f"（{item['days_until']} 天后）"
            )


@app.command("feedback-stats")
def feedback_stats():
    """查看用户实际操作的胜率反馈（BUY 操作 7 日后盈亏）"""
    asyncio.run(_feedback_stats())


async def _feedback_stats():
    console.print("🔁 回填操作涨跌数据...")
    filled = await backfill_action_returns(db_path=DB_PATH)
    if filled:
        console.print(f"  回填了 {filled} 条记录")

    s = get_feedback_accuracy(db_path=DB_PATH)
    b, k = s["bought"], s["skipped"]

    console.print("\n[bold]📊 操作反馈统计[/bold]")
    if b["total"] > 0:
        console.print(
            f"  买入 {b['total']} 次 → 胜率 {b['win_rate']}%，平均 {b['avg_return']:+.2f}%"
        )
    else:
        console.print("  暂无已回填的 BUY 操作")

    if k["total"] > 0:
        console.print(
            f"  跳过/观望 {k['total']} 次 → 其中 {k['wins']} 次事后上涨（错过机会）"
        )

    summary = feedback_summary(db_path=DB_PATH)
    if summary:
        console.print(f"\n  {summary}")


@app.command("kg-init")
def kg_init(
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印三元组，不写入 mempal"),
):
    """从 portfolio.yaml 的 peers 字段初始化供应链/竞争关系知识图谱"""
    import yaml
    from pathlib import Path
    import subprocess

    config_path = Path("config/portfolio.yaml")
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    triples: list[tuple[str, str, str]] = []
    for h in portfolio.get("holdings", []):
        ticker = h["ticker"]
        for peer in h.get("peers", []):
            triples.append((ticker, "competes_with", peer))
            triples.append((peer, "competes_with", ticker))

    # 去重
    triples = list(dict.fromkeys(triples))

    console.print(f"[bold]共 {len(triples)} 条三元组待写入[/bold]")
    ok, fail = 0, 0
    for subj, pred, obj in triples:
        if dry_run:
            console.print(f"  [dim]{subj} --{pred}--> {obj}[/dim]")
            continue
        result = subprocess.run(
            ["mempal", "kg", "add", subj, pred, obj],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            ok += 1
        else:
            console.print(f"  [red]❌ {subj} --{pred}--> {obj}：{result.stderr.strip()}[/red]")
            fail += 1

    if not dry_run:
        console.print(f"✅ 写入 {ok} 条，失败 {fail} 条")


@app.command("dip-stats")
def dip_stats(
    days: int = typer.Option(30, "--days", "-d", help="最近 N 天"),
):
    """展示暴跌警报历史及实际涨跌结果（含24h/7d回测）"""
    backfill_dip_outcomes(db_path=DB_PATH)
    rows = get_dip_stats(days=days, db_path=DB_PATH)
    if not rows:
        console.print(f"近 {days} 天无暴跌警报记录")
        return
    console.print(f"\n[bold]📉 暴跌警报追踪（最近 {days} 天，共 {len(rows)} 条）[/bold]")
    for r in rows:
        opp = r.get("opportunity") or "—"
        intact = "✅" if r.get("thesis_intact") else "❌"
        r24 = f"{r['return_24h']:+.2f}%" if r.get("return_24h") is not None else "待回填"
        r7d = f"{r['return_7d']:+.2f}%" if r.get("return_7d") is not None else "待回填"
        console.print(
            f"  {r['alerted_at'][:16]}  [bold]{r['ticker']}[/bold]  "
            f"跌{abs(r['drop_pct']):.2f}%  机会={opp}  逻辑{intact}  "
            f"24h={r24}  7d={r7d}"
        )


@app.command("backtest")
def backtest(
    ticker: str = typer.Option("", "--ticker", "-t", help="指定股票代码，留空则跑全部持仓（跳过定投ETF）"),
    days:   int = typer.Option(730, "--days", "-d", help="回测天数，默认730（约2年）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="展示每一条信号记录"),
):
    """历史回测：验证 composite_score 信号的有效性（不看总收益，看信号质量）"""
    asyncio.run(_backtest(ticker=ticker, days=days, verbose=verbose))


async def _backtest(ticker: str, days: int, verbose: bool):
    import yaml
    from pathlib import Path
    from rich.table import Table

    config_path = Path("config/portfolio.yaml")
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)
    holdings = portfolio.get("holdings", [])

    if ticker:
        holding = next((h for h in holdings if h["ticker"] == ticker), None)
        market = holding.get("market", "us") if holding else "us"
        console.print(f"[bold]回测 {ticker}（{days} 天）...[/bold]")
        results = [await backtest_ticker(ticker, market, days)]
    else:
        targets = [h for h in holdings if not h.get("is_dca")]
        console.print(f"[bold]回测全部持仓 {len(targets)} 只（{days} 天，跳过定投ETF）...[/bold]")
        results = await backtest_portfolio(holdings, days)

    # ── 汇总表 ─────────────────────────────────────────────────────────────
    table = Table(title=f"历史回测结果（过去 {days} 天）", show_lines=True)
    table.add_column("股票", style="bold")
    table.add_column("市场")
    table.add_column("买入\n信号数", justify="right")
    table.add_column("卖出\n信号数", justify="right")
    table.add_column("买入后10日\n均收益%", justify="right")
    table.add_column("基准10日\n均收益%", justify="right")
    table.add_column("买入超额\n收益%", justify="right")
    table.add_column("低位买入\n准确率", justify="right")
    table.add_column("卖出预警\n准确率", justify="right")
    table.add_column("备注")

    for r in results:
        if r.error:
            table.add_row(r.ticker, r.market, "—", "—", "—", "—", "—", "—", "—", f"[red]{r.error}[/red]")
            continue

        # 买入超额收益
        if r.buy_avg_fwd10 is not None:
            excess = r.buy_avg_fwd10 - r.baseline_fwd10
            buy_str = f"{r.buy_avg_fwd10:+.2f}%"
            excess_str = f"[green]{excess:+.2f}%[/green]" if excess > 0 else f"[red]{excess:+.2f}%[/red]"
        else:
            buy_str = "—"
            excess_str = "—"

        baseline_str = f"{r.baseline_fwd10:+.2f}%"

        # 卖出后10日收益（负值=预警有效）
        sell_str = f"{r.sell_avg_fwd10:+.2f}%" if r.sell_avg_fwd10 is not None else "—"

        bottom_str = f"{r.buy_bottom_pct:.0%}" if r.buy_bottom_pct is not None else "—"
        drop_str   = f"{r.sell_before_drop_pct:.0%}" if r.sell_before_drop_pct is not None else "—"

        # 综合评价
        notes = []
        if r.buy_avg_fwd10 is not None and excess > 0.5:
            notes.append("[green]买入有效[/green]")
        elif r.buy_avg_fwd10 is not None and excess < -0.5:
            notes.append("[red]买入无效[/red]")
        if r.sell_before_drop_pct is not None and r.sell_before_drop_pct >= 0.4:
            notes.append("[green]卖出有效[/green]")
        if r.buy_bottom_pct is not None and r.buy_bottom_pct >= 0.5:
            notes.append("[green]抄底准[/green]")

        table.add_row(
            r.ticker, r.market,
            str(r.buy_count), str(r.sell_count),
            buy_str, baseline_str, excess_str,
            bottom_str, drop_str,
            " ".join(notes) if notes else "—",
        )

    console.print(table)

    # ── 说明 ────────────────────────────────────────────────────────────────
    console.print(
        "\n[dim]指标说明：\n"
        "  买入超额收益 = 买入信号后10日均收益 − 随机入场基准，正值=信号有价值\n"
        "  低位买入准确率 = 买入信号中，入场价处于60日区间低40%的比例，越高越好\n"
        "  卖出预警准确率 = 卖出信号后20日内出现≥5%跌幅的比例，越高越好[/dim]"
    )

    # ── verbose 模式：逐条信号 ───────────────────────────────────────────────
    if verbose:
        for r in results:
            if r.error or not r.signals:
                continue
            console.print(f"\n[bold]{r.ticker} 全部信号（{len(r.signals)} 条）[/bold]")
            sig_table = Table(show_lines=False, header_style="dim")
            sig_table.add_column("日期")
            sig_table.add_column("方向")
            sig_table.add_column("分数", justify="right")
            sig_table.add_column("收盘价", justify="right")
            sig_table.add_column("5日%", justify="right")
            sig_table.add_column("10日%", justify="right")
            sig_table.add_column("20日%", justify="right")
            sig_table.add_column("入场位", justify="right")
            for s in r.signals:
                color = "green" if s.signal == "buy" else "red"
                direction = f"[{color}]{'▲买入' if s.signal == 'buy' else '▼卖出'}[/{color}]"
                sig_table.add_row(
                    s.date, direction, f"{s.score:+.3f}", f"{s.close_price}",
                    f"{s.fwd_5d:+.2f}%" if s.fwd_5d is not None else "—",
                    f"{s.fwd_10d:+.2f}%" if s.fwd_10d is not None else "—",
                    f"{s.fwd_20d:+.2f}%" if s.fwd_20d is not None else "—",
                    f"{s.entry_percentile:.0%}" if s.entry_percentile is not None else "—",
                )
            console.print(sig_table)


if __name__ == "__main__":
    app()
