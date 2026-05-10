# src/finance_agent/db/tracker.py
"""
历史建议追踪：每次运行后写入推荐记录，7 天后回填实际涨跌，计算准确率。

表结构：
  recommendations(id, date, ticker, recommendation, confidence, position_change,
                  price_at_rec, price_7d, return_7d, outcome, created_at)

outcome 取值：
  正确  — 买入/大加/小加 且 7d 涨幅 > 0；或 减仓/卖出 且 7d 跌幅 < 0
  错误  — 方向相反
  中性  — 持有/观望，或涨跌幅在 ±1% 以内
"""
import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

DB_PATH = Path("data/agent.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS recommendations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    recommendation  TEXT,
    confidence      TEXT,
    position_change TEXT,
    price_at_rec    REAL,
    price_7d        REAL,
    return_7d       REAL,
    outcome         TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rec_date   ON recommendations(date);
CREATE INDEX IF NOT EXISTS idx_rec_ticker ON recommendations(ticker);
"""


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(_CREATE_SQL)


# ── 写入当日推荐 ──────────────────────────────────────────────

def save_recommendations(date: str, records: list[dict]) -> None:
    """
    records 每项：{ticker, recommendation, confidence, position_change, price_at_rec}
    若当天已有记录则跳过（幂等）。
    """
    init_db()
    with _conn() as con:
        existing = {r["ticker"] for r in con.execute(
            "SELECT ticker FROM recommendations WHERE date = ?", (date,)
        ).fetchall()}
        rows = [
            (date, r["ticker"], r.get("recommendation"), r.get("confidence"),
             r.get("position_change"), r.get("price_at_rec"))
            for r in records
            if r["ticker"] not in existing
        ]
        if rows:
            con.executemany(
                "INSERT INTO recommendations(date,ticker,recommendation,confidence,"
                "position_change,price_at_rec) VALUES(?,?,?,?,?,?)",
                rows,
            )
            print(f"[Tracker] 保存 {len(rows)} 条推荐记录（{date}）")


# ── 回填 7 日实际涨跌 ─────────────────────────────────────────

def _determine_outcome(recommendation: str, position_change: str, ret: float) -> str:
    """根据方向与实际涨跌判断推荐是否正确"""
    bullish = recommendation in ("买入",) or (position_change or "").startswith(("大加", "小加"))
    bearish = recommendation in ("减仓", "卖出") or (position_change or "").startswith("减仓")
    if abs(ret) < 1.0:
        return "中性"
    if bullish:
        return "正确" if ret > 0 else "错误"
    if bearish:
        return "正确" if ret < 0 else "错误"
    return "中性"   # 持有 / 观望


def _fetch_current_price(ticker: str, market: str = "us") -> float | None:
    """用 yfinance 拉最新收盘价"""
    try:
        if market == "hk" and ticker.isdigit():
            yf_ticker = f"{int(ticker):04d}.HK"
        else:
            yf_ticker = ticker
        hist = yf.Ticker(yf_ticker).history(period="2d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


async def fill_7d_returns() -> int:
    """
    找出 7 个交易日前（日历日 ~10 天）还没有 price_7d 的记录，
    拉当前价格回填，返回回填条数。
    """
    init_db()
    cutoff = (datetime.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    with _conn() as con:
        pending = con.execute(
            "SELECT id, ticker, recommendation, position_change, price_at_rec "
            "FROM recommendations WHERE date <= ? AND price_7d IS NULL",
            (cutoff,),
        ).fetchall()

    if not pending:
        return 0

    loop = asyncio.get_event_loop()
    filled = 0
    for row in pending:
        price_now = await loop.run_in_executor(
            None, lambda t=row["ticker"]: _fetch_current_price(t)
        )
        if price_now is None or not row["price_at_rec"]:
            continue
        ret = (price_now - row["price_at_rec"]) / row["price_at_rec"] * 100
        outcome = _determine_outcome(
            row["recommendation"] or "", row["position_change"] or "", ret
        )
        with _conn() as con:
            con.execute(
                "UPDATE recommendations SET price_7d=?, return_7d=?, outcome=? WHERE id=?",
                (round(price_now, 4), round(ret, 2), outcome, row["id"]),
            )
        filled += 1

    if filled:
        print(f"[Tracker] 回填 {filled} 条 7 日涨跌记录")
    return filled


# ── 准确率统计摘要 ────────────────────────────────────────────

def accuracy_summary(days: int = 30) -> str:
    """
    返回最近 N 天内已回填记录的准确率摘要文字，供注入飞书卡片。
    """
    init_db()
    since = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as con:
        rows = con.execute(
            "SELECT outcome FROM recommendations "
            "WHERE date >= ? AND outcome IS NOT NULL",
            (since,),
        ).fetchall()

    if not rows:
        return ""

    total = len(rows)
    correct = sum(1 for r in rows if r["outcome"] == "正确")
    wrong   = sum(1 for r in rows if r["outcome"] == "错误")
    pct = round(correct / (correct + wrong) * 100) if (correct + wrong) > 0 else 0
    return f"近{days}天推荐准确率：{pct}%（{correct}✅ {wrong}❌ {total - correct - wrong}➖，共{total}条）"
