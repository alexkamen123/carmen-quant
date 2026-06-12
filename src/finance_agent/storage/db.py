# src/finance_agent/storage/db.py
import aiosqlite
from pathlib import Path
from finance_agent.graph.state import AgentState

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path, timeout=15) as db:
        await db.executescript(SCHEMA_PATH.read_text())
        await db.commit()


async def save_daily_signals(state: AgentState, db_path: str) -> None:
    async with aiosqlite.connect(db_path, timeout=15) as db:
        for s in state.stocks:
            await db.execute("""
                INSERT INTO daily_signals
                    (date, ticker, market, close_price, composite_score,
                     recommendation, confidence, one_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.date, s.ticker, s.market,
                s.signals.close, s.signals.composite_score,
                s.recommendation, s.confidence, s.one_line,
            ))
        await db.commit()


async def get_signal_history(ticker: str, db_path: str, days: int = 30) -> list[dict]:
    async with aiosqlite.connect(db_path, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM daily_signals
            WHERE ticker = ?
            ORDER BY date DESC LIMIT ?
        """, (ticker, days)) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]
