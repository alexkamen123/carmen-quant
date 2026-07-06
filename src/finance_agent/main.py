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
    get_dip_stats, backfill_dip_outcomes, detect_portfolio_changes, realign_alpha,
    save_recommendations, _fetch_current_price, _conn, _resolve_db,
)
from finance_agent.alerts.earnings_trigger import check_and_alert_earnings
from finance_agent.alerts.cooldown_monitor import run_cooldown_check
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


def _evolve_after_backfill(win_rates: dict) -> None:
    """L2c 闭环：backfill 后按真实策略 edge 更新自适应权重，并把本轮关键指标
    append 进 evolution_log。全程兜底，任何异常只打印不抛（绝不阻断日报）。"""
    param_version = "v0"
    try:
        from finance_agent.value.strategy_scorecard import compute_strategy_edge
        from finance_agent.backtest.strategy_weights import update_weights_from_edge
        edge = compute_strategy_edge()
        res = update_weights_from_edge(edge)
        param_version = res.get("param_version", "v0")
        if res.get("changed"):
            console.print(f"🔁 策略权重自适应：{param_version} 调整 {res['deltas']}")
        else:
            console.print(f"🔁 策略权重自适应：无变化（{param_version}）")
    except Exception as e:
        console.print(f"⚠️ 策略权重自适应跳过（不阻断）: {e}")

    try:
        from finance_agent.value.evolution_log import (
            log_evolution, DEFAULT_CSV_PATH, PROJECT,
        )
        from finance_agent.value.metrics import compute_value_metrics
        vm = compute_value_metrics(DB_PATH)
        hit = vm.get("hit_rate", {}) or {}
        ca = vm.get("combined_alpha", {}) or {}
        rid = log_evolution(DEFAULT_CSV_PATH, PROJECT, "win_rate",
                            hit.get("win_rate"), param_version=param_version,
                            note="日报 backfill 后")
        log_evolution(DEFAULT_CSV_PATH, PROJECT, "combined_alpha", ca.get("avg"),
                      param_version=param_version, run_id=rid)
        log_evolution(DEFAULT_CSV_PATH, PROJECT, "backfilled_tickers", len(win_rates),
                      param_version=param_version, run_id=rid)
        console.print(f"🧬 evolution_log 已记录（run_id={rid}）")
    except Exception as e:
        console.print(f"⚠️ evolution_log 跳过（不阻断）: {e}")


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

        # Step 1b: L2c 评估→反哺参数（自适应策略族权重）+ evolution_log 留痕。
        # 全程 try 兜底：闭环失败绝不阻断日报主流程（与 Step 3 入库同纪律）。
        _evolve_after_backfill(win_rates)

    # Step 2: 运行今日分析
    console.print("🤖 开始今日分析...")
    state = await run_workflow(db_path=DB_PATH)

    # Step 3: 保存到 SQLite（失败不阻断推送——分析已完成，卡片必须发出去；
    # 06-12 自检 P1：并发锁库时这里抛异常会让整份日报白跑且静默无推送）
    try:
        await save_daily_signals(state, DB_PATH)
        console.print(f"💾 已保存 {len(state.stocks)} 只股票信号")
    except Exception as e:
        console.print(f"⚠️ 信号入库失败（不阻断推送）: {e}")

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
    pushed = asyncio.run(_price_scan(threshold_pct=threshold))
    console.print(f"{'✅' if pushed else '⚪'} 价格扫描完成，推送 {pushed} 条异动")


async def _price_scan(threshold_pct: float) -> int:
    pushed = await run_price_scan(threshold_pct=threshold_pct)
    # P2c 失效触发器：价格向失效扫描（guarded·失败不拖垮 price-scan）
    from finance_agent.signals.thesis_invalidation import thesis_invalidation_enabled
    if thesis_invalidation_enabled():
        try:
            from finance_agent.alerts.thesis_invalidation_trigger import scan_price_invalidation
            await scan_price_invalidation()
        except Exception as e:
            console.print(f"⚠️ 失效扫描(price)失败，跳过：{e}")
    return pushed


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


@app.command("backfill-realign")
def backfill_realign(
    apply: bool = typer.Option(False, "--apply", help="真正写库（默认 dry-run 只看 diff）"),
):
    """一次性重算历史 alpha（修两腿窗口错位的伪 alpha）。默认 dry-run；--apply 前自动备份 DB"""
    asyncio.run(_backfill_realign(apply=apply))


async def _backfill_realign(apply: bool):
    import shutil
    from datetime import datetime as _dt
    from finance_agent.db.tracker import _resolve_db
    db = _resolve_db(None)   # 与日报流水线同源（AGENT_DB_PATH 优先），避免双库分歧
    if apply:
        bak = f"{db}.bak-prealign-{_dt.now():%Y%m%d-%H%M%S}"   # 带时间戳，绝不覆盖旧备份
        shutil.copy2(db, bak)
        console.print(f"💾 已备份 DB → {bak}")
    console.print("🔧 重算历史 alpha（联网拉配对窗口，较慢）...")
    res = await realign_alpha(db_path=db, dry_run=not apply)
    if res["checked"] == 0:
        console.print(f"[yellow]⚠️ 0 行待重算——确认 DB 路径是否正确（{db}）[/yellow]")
        return
    console.print(f"\n检查 {res['checked']} 行，{'已改写' if apply else '将改写'} {res['changed']} 行：")
    for s in res["samples"][:25]:
        console.print(f"  {s['date']} {s['ticker']:6s} ret {s['old_ret']}→{s['new_ret']}"
                      f"  alpha {s['old_alpha']}→{s['new_alpha']}")
    if res["failed"]:
        console.print(f"\n[yellow]⚠️ {res['failed']} 行取数失败未迁移（仍为旧口径，与新口径混算会污染记分牌）"
                      f"——联网恢复后重跑直至 failed=0[/yellow]")
        for s in res["failed_samples"][:10]:
            console.print(f"  [dim]{s['date']} {s['ticker']} ({s['reason']})[/dim]")
    if not apply and res["changed"]:
        console.print("\n[dim]确认无误后加 --apply 写库（会先自动备份）[/dim]")


@app.command("shadow-ab")
def shadow_ab_cmd(
    backfill: bool = typer.Option(True, "--backfill/--no-backfill", help="先回填到期样本的真实超额"),
):
    """方案A 影子 A/B 体温计：回填到期样本超额 + 出裁决（护栏到底帮没帮）。

    口径：每天日报记一行"开护栏 vs 关护栏"两套机会篮子，7日后回填各自 vs SPY 超额，
    攒够分歧样本(≥10)对比 on/off 篮子均值。纯观测，不改任何线上建议。"""
    from finance_agent.value.shadow_ab import LOG_PATH, report_shadow_ab, sweep

    if not LOG_PATH.exists():
        console.print(f"⚪ 暂无影子记录（{LOG_PATH}）——日报跑过且护栏开启后才开始累积")
        return
    if backfill:
        console.print("⏳ 回填到期样本真实超额（联网取数）...")
        rep = sweep(log_path=LOG_PATH)
        console.print(f"   新回填 {rep['backfilled']} 行")
    else:
        rep = report_shadow_ab(LOG_PATH)
    label = {"insufficient": "样本不足·暂不下结论", "guardrail_helps": "✅ 护栏帮了忙",
             "guardrail_hurts": "❌ 护栏帮倒忙", "neutral": "⚪ 中性·无显著差异"}
    console.print(f"\n🌡️ 方案A 影子 A/B 体温计")
    console.print(f"   分歧样本 n={rep['n']}（需 ≥10 才下结论）")
    if rep["on_mean"] is not None:
        console.print(f"   开护栏篮子均超额 on={rep['on_mean']:+.2f}%  vs  关护栏 off={rep['off_mean']:+.2f}%")
        console.print(f"   净增量 edge={rep['edge']:+.2f}%")
    console.print(f"   裁决：{label.get(rep['verdict'], rep['verdict'])}")


@app.command("oos-monitor")
def oos_monitor_cmd(
    trailing_days: int = typer.Option(504, "--trailing-days", help="滚动窗口交易日数(默认2年)；0=全历史"),
):
    """方案A walk-forward 主裁判：每月在最新数据重跑样本外验证，追踪 regime-vs-static
    增量是否衰减。诚实闸门：跌市样本不足→insufficient(不误报)，连续衰减才告警(不自动关flag)。"""
    from finance_agent.backtest.oos_monitor import run_oos_monitor
    console.print("🔬 walk-forward OOS 复验（联网取宇宙×~3年，稍候）...")
    out = run_oos_monitor(trailing_days=(trailing_days or None))
    if out.get("error"):
        console.print(f"❌ 取数失败：{out['error']}")
        return
    if out.get("skipped"):
        console.print(f"⏭️ 本月已复验过（{out['skipped']}），只出裁决")
    for r in out.get("rows", []):
        console.print(f"   h={r['horizon']}d：regime={r['regime_alpha']}% vs static={r['static_alpha']}%"
                      f" · 增量edge={r['regime_edge']} · 跌市{r['n_down']}笔 · {r['verdict']}")
    label = {"healthy": "✅ 方案A仍有效", "decaying": "🚨 增量衰减·建议复审是否关flag",
             "insufficient_regime_data": "⚪ 跌市样本不足·暂不下结论(涨市常态)"}
    for h, d in out.get("decay", {}).items():
        console.print(f"🌡️ h={h}d 衰减裁决：{label.get(d['status'], d['status'])}"
                      f"（可测月 {d['n_testable']}/{d['k_needed']}）")


@app.command("rankic-monitor")
def rankic_monitor_cmd(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="decaying 时不发飞书，只打印"),
):
    """P1c 月度 RankIC 自检：建议方向序 vs 后续超额序的秩相关有没有排序力。
    诚实闸门：样本不足→insufficient不下结论；连续2可测月IC<0.03才推卡(纯观测·不改建议)。"""
    from finance_agent.value.rankic_monitor import (IC_THRESHOLD, notify_if_decaying,
                                                    run_rankic_monitor)
    rk = run_rankic_monitor(db_path=DB_PATH)
    if rk.get("skipped"):
        console.print(f"⏭️ 本月已自检过（{rk['skipped']}），只出裁决")
    snap = rk.get("snapshot")
    if snap:
        console.print(f"   样本 n={snap['n']}（方向性 {snap['n_directional']}）· "
                      f"RankIC={snap['ic']} · {snap['verdict']}")
    d = rk.get("decay", {})
    label = {"healthy": "✅ 方向仍有排序力", "decaying": f"🚨 连续衰减(IC<{IC_THRESHOLD})·建议复核策略",
             "insufficient_history": "⚪ 可测月不足·暂不下结论"}
    console.print(f"📏 衰减裁决：{label.get(d.get('status'), d.get('status'))}"
                  f"（可测月 {d.get('n_measured', 0)}/{d.get('k_needed', 2)}，当前 IC {d.get('current_ic')}）")
    # 同月幂等：本月已记录(skipped)则不重复推卡，防手动重跑刷屏（审查 nit）
    if not rk.get("skipped"):
        asyncio.run(notify_if_decaying(d, skip_notify=skip_notify))


@app.command("sue-edge")
def sue_edge_cmd():
    """P2b 扩展：SUE 漂移 edge 影子测量——反事实测『大超预期后做多/爆雷后避开』到底赚不赚。
    纯观测·不发建议·不花钱；诚实：样本<60 不下结论，in-sample 读数、OOS 校准前禁据此加仓。"""
    from finance_agent.value.sue_edge import sue_edge_reading
    rd = sue_edge_reading(db_path=DB_PATH)
    vlabel = {"insufficient": "⚪ 样本不足·暂不下结论", "edge_present": "✅ 漂移按方向兑现",
              "no_edge": "❌ 无边际"}
    for side, name in (("beat", "大超预期→做多"), ("miss", "爆雷→避开/做空")):
        s = rd[side]
        console.print(f"📈 {name}：n={s['n']} · 命中率={s['hit_rate']} · 均超额={s['mean_excess']} · "
                      f"RankIC={s['rankic']} → {vlabel.get(s['verdict'], s['verdict'])}")
    console.print(f"   整体 RankIC(SUE vs 30日超额)={rd['overall_rankic']} · 总样本 {rd['n_total']}")
    console.print(f"   ⚠️ {rd['caveat']}")


@app.command("check-invalidation")
def check_invalidation_cmd(skip_notify: bool = typer.Option(False, "--skip-notify", help="不推飞书只跑")):
    """手动跑一次持仓财报维度失效扫描（命中声明的失效条件→止损复核告警）。"""
    from finance_agent.alerts.thesis_invalidation_trigger import scan_earnings_invalidation
    asyncio.run(scan_earnings_invalidation(push=not skip_notify))


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
    # 方案A 影子 A/B：搭周六价值体检的车顺手回填到期样本（静默·不进飞书卡·失败不拖垮）
    try:
        from finance_agent.value.shadow_ab import sweep
        rep = sweep()
        console.print(
            f"🌡️ 影子A/B：回填 {rep['backfilled']} 行 · 分歧样本 n={rep['n']} · 裁决 {rep['verdict']}")
    except Exception as e:
        console.print(f"[ShadowAB] 周回填跳过（不影响体检）: {e}")
    # P2b SUE 因子：观测档漂移回填（30天到期样本，guarded·失败不拖垮体检）
    from finance_agent.signals.sue_factor import sue_factor_enabled
    if sue_factor_enabled():
        try:
            from finance_agent.db.tracker import backfill_sue_outcomes
            res = await backfill_sue_outcomes()
            console.print(f"📊 SUE 漂移回填：{res}")
        except Exception as e:
            console.print(f"[SUE] 回填跳过（不影响体检）: {e}")


@app.command("monthly-review")
def monthly_review(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """生成上月投资回顾（准确率统计 + Claude 总结），推送飞书"""
    asyncio.run(_monthly_review(skip_notify=skip_notify))


async def _monthly_review(skip_notify: bool):
    console.print("📆 开始月度投资回顾...")
    # 方案A walk-forward 主裁判：搭月报的车每月复验一次(独立于月报数据·guarded·失败不拖垮)
    try:
        from finance_agent.backtest.oos_monitor import run_oos_monitor
        out = run_oos_monitor()
        for h, d in out.get("decay", {}).items():
            if d["status"] == "decaying":
                console.print(f"🚨 方案A OOS 衰减(h={h}d)：regime 已连续 {d['n_testable']} 个可测月"
                              f"不再赢 static，建议复审是否关 regime_aware_guardrail")
            else:
                console.print(f"🔬 方案A OOS(h={h}d)：{d['status']}（可测月 {d['n_testable']}）")
    except Exception as e:
        console.print(f"[OOS] 月度复验跳过（不影响月报）: {e}")
    # P1c 月度 RankIC 自检：建议方向有没有排序力(纯观测·guarded·仅 decaying 推卡)
    try:
        from finance_agent.value.rankic_monitor import notify_if_decaying, run_rankic_monitor
        rk = run_rankic_monitor(db_path=DB_PATH)
        d = rk.get("decay", {})
        console.print(f"📏 RankIC 自检：{d.get('status')}（可测月 {d.get('n_measured', 0)}"
                      f"，当前 IC {d.get('current_ic')}）")
        # 同月幂等：本月已记录(skipped)则不重复推卡，防手动重跑刷屏（审查 nit）
        if not rk.get("skipped"):
            await notify_if_decaying(d, skip_notify=skip_notify)
    except Exception as e:
        console.print(f"[RankIC] 月度自检跳过（不影响月报）: {e}")
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
        ok = await send_feishu_card(card, fallback_text=f"月度回顾（卡片渲染失败，详见本地日志）：{summary[:300]}")
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
    if action.upper() == "BUY":
        _try_print_behavior_hint()


_WATCH_ALLOWED = {"买入", "减仓", "卖出", "持有", "观望"}


@app.command("log-watch")
def log_watch(
    ticker: str = typer.Argument(..., help="股票代码，如 GS / 00700"),
    rec: str    = typer.Argument(..., help="方向裁决：买入/减仓/卖出/持有/观望"),
    price: float = typer.Option(None, "--price",  "-p", help="裁决时价格，留空自动拉现价"),
    market: str  = typer.Option("us", "--market", "-m", help="市场：us/hk/cn"),
    date: str    = typer.Option(None, "--date",   "-d", help="裁决日期 YYYY-MM-DD，留空取今天"),
):
    """记录一条影子选股裁决（is_watch=1，不花真钱，纯考选股眼光）。

    可证伪性闸门：只接受离散方向裁决；模糊估值判断（如"偏贵待回调"）请落为'观望'——
    它不进命中率，只留痕。影子轨与真实账户口径物理隔离，绝不并入「你 vs 躺平」头条。
    """
    from datetime import date as _date

    t, r = ticker.upper().strip(), rec.strip()
    if r not in _WATCH_ALLOWED:
        console.print(f"❌ rec 必须是 {'/'.join(_WATCH_ALLOWED)} 之一；"
                      f"模糊估值判断（偏贵待回调等）请落为'观望'，不会进命中率")
        raise typer.Exit(1)

    d = date or _date.today().isoformat()

    # CLI 层去重保护（不碰 save_recommendations 核心写入路径、不松动 (date,ticker) 唯一性不变式）：
    # 当天该票已有任意记录则跳过，避免与日报 is_watch=0 行静默冲突
    p = _resolve_db(DB_PATH)
    with _conn(p) as con:
        clash = con.execute(
            "SELECT is_watch, recommendation FROM recommendations WHERE date=? AND ticker=?",
            (d, t),
        ).fetchone()
    if clash:
        console.print(f"⚠️ {d} 已有 {t} 记录（is_watch={clash['is_watch']}，{clash['recommendation']}）；"
                      f"为避免唯一性冲突跳过，如需另记请用 --date 指定其它日期")
        raise typer.Exit(0)

    if price is None:
        price = _fetch_current_price(t, market)
        if price is None:
            console.print(f"❌ 无法自动获取 {t} 现价，请用 --price 显式指定")
            raise typer.Exit(1)

    save_recommendations(d, [{
        "ticker": t, "recommendation": r, "position_change": None,
        "price_at_rec": price, "market": market, "is_watch": 1,
    }], db_path=DB_PATH)
    tag = "方向性·可考眼光" if r in ("买入", "减仓", "卖出") else "非方向性·仅留痕"
    console.print(f"✅ 影子选股已记：{d} {t} {r} @ {price:.2f}（{market}）· {tag}")


def _try_print_behavior_hint() -> None:
    """order9 触点 B：手动记 BUY 时打印历史买入行为统计（描述性，失败静默）。
    auto detect（detect_portfolio_changes→log_user_action）不经过此 handler，不会触发。"""
    try:
        from finance_agent.db.tracker import (format_behavior_hint,
                                              get_behavior_hint_stats)
        stats = get_behavior_hint_stats(db_path=DB_PATH)
        if stats:
            console.print(f"[dim]{format_behavior_hint(stats, style='cli')}[/dim]")
    except Exception:
        pass


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
    win_rates = asyncio.run(backfill_yesterday(DB_PATH))
    _evolve_after_backfill(win_rates or {})
    console.print("✅ 胜率回填完成")


@app.command("health-check")
def health_check(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
):
    """调度心跳自检：launchd 任务加载状态 + 日志新鲜度，异常才推飞书（健康静默）"""
    from finance_agent.ops.health import run_health_check
    problems = asyncio.run(run_health_check(skip_notify=skip_notify))
    if problems:
        raise typer.Exit(code=1)


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
    # P2b SUE 因子：观测档回看落库（guarded·失败不拖垮 earnings-check）
    from finance_agent.signals.sue_factor import sue_factor_enabled
    if sue_factor_enabled():
        try:
            from finance_agent.alerts.earnings_trigger import record_sue_events
            rec = await record_sue_events()
            if rec:
                console.print(f"📊 SUE 观测：落库 {len(rec)} 条盈余惊喜事件 {rec}")
        except Exception as e:
            console.print(f"[SUE] 回看落库跳过（不影响 earnings-check）: {e}")

    # P2c 失效触发器：财报向失效扫描（guarded·失败不拖垮 earnings-check）
    from finance_agent.signals.thesis_invalidation import thesis_invalidation_enabled
    if thesis_invalidation_enabled():
        try:
            from finance_agent.alerts.thesis_invalidation_trigger import scan_earnings_invalidation
            await scan_earnings_invalidation()
        except Exception as e:
            console.print(f"⚠️ 失效扫描(earnings)失败，跳过：{e}")


@app.command("cooldown-check")
def cooldown_check(
    force: bool = typer.Option(False, "--force", help="跳过冷却判定强制推（仅测试跑通流程用）"),
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印卡"),
):
    """检查"长期想减但当下过热"的持仓是否已冷却，到点推减仓提醒卡"""
    pushed = asyncio.run(run_cooldown_check(force=force, skip_notify=skip_notify))
    console.print(f"{'✅' if pushed else '⚪'} 冷却检查完成，推送 {pushed} 条减仓提醒")


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
