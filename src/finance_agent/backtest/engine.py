# src/finance_agent/backtest/engine.py
"""
每天运行完毕后，此脚本回填昨天信号的实际结果，并更新胜率。
在 GitHub Actions 中，在每日分析之前先运行此脚本。
"""
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from finance_agent.data.router import DataRouter

router = DataRouter()


async def backfill_yesterday(db_path: str) -> dict[str, float]:
    """
    找出昨天的信号，获取今天收盘价，回填 next_day_close 和 signal_correct。
    返回：{ticker: win_rate} 字典
    """
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # 找出昨天未回填的信号
        async with db.execute("""
            SELECT id, ticker, market, close_price, recommendation
            FROM daily_signals
            WHERE date = ? AND next_day_close IS NULL
        """, (yesterday,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        for row in rows:
            try:
                df = await router.fetch_ohlcv(row["ticker"], row["market"], days=3)
                today_close = float(df["close"].iloc[-1])
                prev_close  = row["close_price"]
                change_pct  = (today_close - prev_close) / prev_close * 100

                # 判断信号是否正确
                rec = row["recommendation"]
                if rec in ("买入",) and change_pct > 1.0:
                    correct = 1
                elif rec in ("减仓", "卖出") and change_pct < -1.0:
                    correct = 1
                elif rec in ("持有", "观望", "按计划定投"):
                    correct = None  # 不纳入胜率统计
                else:
                    correct = 0

                await db.execute("""
                    UPDATE daily_signals
                    SET next_day_close = ?, next_day_change_pct = ?, signal_correct = ?
                    WHERE id = ?
                """, (today_close, change_pct, correct, row["id"]))
            except Exception:
                continue

        await db.commit()

        # 重新计算胜率
        async with db.execute("""
            SELECT ticker,
                   COUNT(*) FILTER (WHERE signal_correct IS NOT NULL) as total,
                   SUM(signal_correct) as correct
            FROM daily_signals
            GROUP BY ticker
        """) as cur:
            stats = [dict(r) for r in await cur.fetchall()]

        for s in stats:
            total   = s["total"] or 0
            correct = int(s["correct"] or 0)
            rate    = correct / total if total > 0 else 0.0
            await db.execute("""
                INSERT INTO win_rate_stats (ticker, total_signals, correct_signals, win_rate, last_updated)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(ticker) DO UPDATE SET
                    total_signals   = excluded.total_signals,
                    correct_signals = excluded.correct_signals,
                    win_rate        = excluded.win_rate,
                    last_updated    = excluded.last_updated
            """, (s["ticker"], total, correct, rate))

        await db.commit()

    return {s["ticker"]: (int(s["correct"] or 0) / s["total"] if s["total"] else 0.0)
            for s in stats}
