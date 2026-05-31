# src/finance_agent/memory/mempal_client.py
"""
mempal 集成：把日报/周报/月度回顾持久化到 mempal 记忆库。
mempal 未安装时静默跳过，不影响主流程。

安装：curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
      cargo install mempal

Wing 结构：
  finance-agent / reports/daily   — 每日持仓日报
  finance-agent / reports/weekly  — 周度配置建议
  finance-agent / reports/monthly — 月度回顾

不存储：新闻预警、财报预警、每日轻量跟进（内容散、量大、价值低）
"""
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path


WING = "finance-agent"


def _mempal_available() -> bool:
    return shutil.which("mempal") is not None


def _ingest_text(content: str, room: str) -> bool:
    """把一段文字写进临时目录，然后用 mempal ingest 写入指定 wing/room。"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = room.replace("/", "_") + f"_{date.today().isoformat()}.txt"
            (Path(tmpdir) / fname).write_text(content, encoding="utf-8")
            result = subprocess.run(
                ["mempal", "ingest", "--wing", WING, "--room", room, tmpdir],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                print(f"[Memory] mempal ingest 失败: {result.stderr.strip()}")
            return result.returncode == 0
    except Exception as e:
        print(f"[Memory] mempal 调用失败: {e}")
        return False


def ingest_daily_report(report_text: str) -> None:
    """每个工作日分析完成后调用，存入完整日报文本。"""
    if not _mempal_available():
        print("[Memory] mempal 未安装，跳过日报记忆存储")
        return
    if not report_text or not report_text.strip():
        return

    today = date.today().isoformat()
    content = f"日期：{today}\n\n{report_text}"
    ok = _ingest_text(content, "reports/daily")
    print(f"[Memory] {'✅' if ok else '❌'} 日报已{'存入' if ok else '存储失败'} mempal（finance-agent/reports/daily）")


def ingest_weekly_report(result: dict) -> None:
    """每周一周报生成后调用，存入周度配置建议摘要。"""
    if not _mempal_available():
        print("[Memory] mempal 未安装，跳过周报记忆存储")
        return
    if not result:
        return

    today = date.today().isoformat()
    diagnosis = result.get("diagnosis", {})
    hedge = result.get("hedge_instruments", [])
    opportunities = result.get("opportunities", [])

    hedge_lines = []
    for b in hedge:
        direction = b.get("direction", "")
        instruments = ", ".join(i.get("ticker", "") for i in b.get("instruments", []))
        if instruments:
            hedge_lines.append(f"  {direction}：{instruments}")

    opp_lines = [
        f"  {o.get('ticker','')}（{o.get('sector','')}）— {o.get('reason','')}"
        for o in opportunities
    ]

    content = (
        f"日期：{today}\n"
        f"集中度风险：{diagnosis.get('concentration_risk', '无')}\n"
        f"宏观风险：{diagnosis.get('macro_risk', '无')}\n"
        f"对冲方向（{len(diagnosis.get('hedge_directions', []))} 个）：\n"
        + "\n".join(f"  {d}" for d in diagnosis.get("hedge_directions", [])) + "\n"
        f"对冲品种：\n" + ("\n".join(hedge_lines) or "  无") + "\n"
        f"机会标的（{len(opportunities)} 只）：\n"
        + ("\n".join(opp_lines) or "  无")
    )
    ok = _ingest_text(content, "reports/weekly")
    print(f"[Memory] {'✅' if ok else '❌'} 周报已{'存入' if ok else '存储失败'} mempal（finance-agent/reports/weekly）")


def ingest_monthly_review(summary_text: str, stats: dict) -> None:
    """每月 1 日月度回顾生成后调用，存入复盘摘要 + 准确率数据。"""
    if not _mempal_available():
        print("[Memory] mempal 未安装，跳过月度回顾记忆存储")
        return
    if not summary_text:
        return

    month = stats.get("month", date.today().strftime("%Y-%m"))
    content = (
        f"月份：{month}\n"
        f"整体准确率：{stats.get('overall_acc', '无数据')}\n"
        f"各股票准确率：\n{stats.get('ticker_acc', '无数据')}\n\n"
        f"AI 月度复盘：\n{summary_text.strip()}"
    )
    ok = _ingest_text(content, "reports/monthly")
    print(f"[Memory] {'✅' if ok else '❌'} 月度回顾已{'存入' if ok else '存储失败'} mempal（finance-agent/reports/monthly）")


def search_history(ticker: str, query: str = "") -> str:
    """
    查询某只股票的历史决策，返回原始文本（供注入 prompt 使用）。
    未安装时返回空字符串。
    """
    if not _mempal_available():
        return ""

    q = query or f"{ticker} 决策 recommendation"
    result = subprocess.run(
        ["mempal", "search", q, "--wing", "finance-agent", "--top-k", "3"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
