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
from finance_agent.alerts.news_monitor import run_news_scan, run_price_scan
from finance_agent.db.thesis_generator import generate_all_theses, generate_thesis_for
from finance_agent.db.tracker import (
    list_theses, log_user_action, get_action_history,
    backfill_action_returns, get_feedback_accuracy, feedback_summary,
    get_dip_stats, backfill_dip_outcomes,
)
from finance_agent.alerts.earnings_trigger import check_and_alert_earnings
from finance_agent.weekly.allocation_advisor import run_allocation_advisor
from finance_agent.weekly.report_card import build_weekly_card
from finance_agent.weekly.daily_followup import run_daily_followup
from finance_agent.monthly.review import run_monthly_review
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
    card, summary, stats = result
    console.print("✅ 月度回顾已生成")
    ingest_monthly_review(summary, stats)
    if not skip_notify:
        ok = await send_feishu_card(card)
        console.print("✅ 月度回顾推送成功" if ok else "❌ 月度回顾推送失败")


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


if __name__ == "__main__":
    app()
