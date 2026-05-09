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

load_dotenv()

app = typer.Typer(help="卡门家庭量化交易助手")
console = Console()
DB_PATH = "data/agent.db"


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

    # Step 1: 回填昨日胜率
    if backfill:
        console.print("📊 回填昨日信号胜率...")
        win_rates = await backfill_yesterday(DB_PATH)
        for ticker, rate in win_rates.items():
            console.print(f"   {ticker}: {rate:.0%}")

    # Step 2: 运行今日分析
    console.print("🤖 开始今日分析...")
    state = await run_workflow(db_path=DB_PATH)

    # Step 3: 保存到 SQLite
    await save_daily_signals(state, DB_PATH)
    console.print(f"💾 已保存 {len(state.stocks)} 只股票信号")

    # Step 4: 打印报告
    console.print("\n" + state.report_text)

    # Step 5: 推送飞书（优先卡片，降级纯文本）
    if not skip_notify:
        if state.report_card:
            ok = await send_feishu_card(state.report_card)
        else:
            ok = await send_feishu_message(state.report_text)
        console.print("✅ 飞书推送成功" if ok else "❌ 飞书推送失败")


@app.command()
def backfill_only():
    """仅回填昨日胜率，不运行新分析"""
    asyncio.run(backfill_yesterday(DB_PATH))
    console.print("✅ 胜率回填完成")


if __name__ == "__main__":
    app()
