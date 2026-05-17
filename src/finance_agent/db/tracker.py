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

import pandas as pd
import yfinance as yf

_DEFAULT_DB = Path("data/agent.db")


def _resolve_db(db_path: str | Path | None = None) -> Path:
    """优先使用传入路径，其次环境变量 AGENT_DB_PATH，最后默认值"""
    if db_path:
        return Path(db_path)
    env = __import__("os").environ.get("AGENT_DB_PATH")
    return Path(env) if env else _DEFAULT_DB


# 模块级默认路径（向后兼容）—— 调用方可在 init_db() 时传入自定义路径
DB_PATH = _resolve_db()

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

CREATE TABLE IF NOT EXISTS theses (
    ticker          TEXT PRIMARY KEY,
    market          TEXT NOT NULL DEFAULT 'us',
    thesis_text     TEXT NOT NULL,          -- Claude 生成的完整持仓逻辑（Markdown）
    pillars         TEXT,                   -- JSON 数组：[{"pillar": "...", "status": "intact|weakening|broken"}]
    stop_conditions TEXT,                   -- 什么情况下应考虑出场（一段文字）
    generated_at    TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dip_alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    market            TEXT NOT NULL DEFAULT 'us',
    alerted_at        TEXT DEFAULT (datetime('now')),
    drop_pct          REAL,
    price_at_alert    REAL,
    opportunity       TEXT,
    thesis_intact     INTEGER,
    drop_reason       TEXT,
    price_24h         REAL,
    return_24h        REAL,
    price_7d          REAL,
    return_7d         REAL
);
CREATE INDEX IF NOT EXISTS idx_dip_ticker ON dip_alerts(ticker);
CREATE INDEX IF NOT EXISTS idx_dip_at     ON dip_alerts(alerted_at);
"""


@contextmanager
def _conn(db_path: Path | None = None):
    p = db_path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(db_path: str | Path | None = None) -> None:
    p = _resolve_db(db_path)
    with _conn(p) as con:
        con.executescript(_CREATE_SQL)


# ── 写入当日推荐 ──────────────────────────────────────────────

def save_recommendations(date: str, records: list[dict],
                         db_path: str | Path | None = None) -> None:
    """
    records 每项：{ticker, recommendation, confidence, position_change, price_at_rec}
    若当天已有记录则跳过（幂等）。
    """
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
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
    """用 yfinance 拉最新收盘价，带 2s 间隔避免 crumb 竞争。"""
    import time
    time.sleep(0.5)  # backfill 是串行循环，0.5s 间隔足够避免 crumb 踩踏
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


async def fill_7d_returns(db_path: str | Path | None = None) -> int:
    """
    找出 7 个交易日前（日历日 ~10 天）还没有 price_7d 的记录，
    拉当前价格回填，返回回填条数。
    """
    p = _resolve_db(db_path)
    init_db(p)
    cutoff = (datetime.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    with _conn(p) as con:
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
        with _conn(p) as con:
            con.execute(
                "UPDATE recommendations SET price_7d=?, return_7d=?, outcome=? WHERE id=?",
                (round(price_now, 4), round(ret, 2), outcome, row["id"]),
            )
        filled += 1

    if filled:
        print(f"[Tracker] 回填 {filled} 条 7 日涨跌记录")
    return filled


# ── Thesis CRUD ──────────────────────────────────────────────

def save_thesis(ticker: str, market: str, thesis_text: str,
                pillars: list[dict] | None = None,
                stop_conditions: str = "",
                db_path: str | Path | None = None) -> None:
    """写入或更新持仓逻辑（upsert by ticker）"""
    import json
    p = _resolve_db(db_path)
    init_db(p)
    pillars_json = json.dumps(pillars, ensure_ascii=False) if pillars else None
    with _conn(p) as con:
        con.execute("""
            INSERT INTO theses(ticker, market, thesis_text, pillars, stop_conditions, updated_at)
            VALUES(?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                market          = excluded.market,
                thesis_text     = excluded.thesis_text,
                pillars         = excluded.pillars,
                stop_conditions = excluded.stop_conditions,
                updated_at      = excluded.updated_at
        """, (ticker, market, thesis_text, pillars_json, stop_conditions))
    print(f"[Thesis] 已保存 {ticker} 持仓逻辑")


def load_thesis(ticker: str, db_path: str | Path | None = None) -> str:
    """加载某只股票的持仓逻辑，不存在则返回空字符串"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        row = con.execute(
            "SELECT thesis_text FROM theses WHERE ticker = ?", (ticker,)
        ).fetchone()
    return row["thesis_text"] if row else ""


def load_all_theses(db_path: str | Path | None = None) -> dict[str, str]:
    """加载所有持仓逻辑，返回 {ticker: thesis_text}"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute("SELECT ticker, thesis_text FROM theses").fetchall()
    return {r["ticker"]: r["thesis_text"] for r in rows}


def get_thesis_ages(db_path: str | Path | None = None) -> dict[str, int]:
    """返回 {ticker: days_since_updated} 字典，供新鲜度检查使用。"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute("SELECT ticker, updated_at FROM theses").fetchall()
    now = datetime.utcnow()
    result = {}
    for r in rows:
        try:
            updated = datetime.fromisoformat(r["updated_at"])
            result[r["ticker"]] = (now - updated).days
        except Exception:
            result[r["ticker"]] = 9999
    return result


def list_theses(db_path: str | Path | None = None) -> list[dict]:
    """列出所有持仓逻辑摘要（供 CLI 展示）"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            "SELECT ticker, market, updated_at, "
            "SUBSTR(thesis_text, 1, 80) AS preview FROM theses ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


# ── 用户操作记录 ─────────────────────────────────────────────

_CREATE_ACTIONS_SQL = """
CREATE TABLE IF NOT EXISTS user_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    action        TEXT NOT NULL,   -- BUY / SELL / TRIM / HOLD / SKIP
    shares        REAL,            -- 操作股数（可选）
    price         REAL,            -- 操作价格（可选）
    note          TEXT,            -- 备注
    rec_date      TEXT,            -- 对应哪天的推荐（空=当天）
    actual_return REAL,            -- BUY 操作 7 天后的实际涨跌幅（%），自动回填
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_actions_ticker ON user_actions(ticker);
CREATE INDEX IF NOT EXISTS idx_actions_date   ON user_actions(date);
"""

_MIGRATE_ACTIONS_SQL = """
ALTER TABLE user_actions ADD COLUMN actual_return REAL;
"""


def _migrate_actions_table(con: sqlite3.Connection) -> None:
    """为已存在的 user_actions 表补充新列（向后兼容）"""
    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(user_actions)").fetchall()}
    if "actual_return" not in existing_cols:
        try:
            con.execute("ALTER TABLE user_actions ADD COLUMN actual_return REAL")
        except sqlite3.OperationalError:
            pass  # 并发写入时可能已存在，忽略


def log_user_action(ticker: str, action: str,
                    shares: float | None = None,
                    price: float | None = None,
                    note: str = "",
                    rec_date: str = "",
                    db_path: str | Path | None = None) -> None:
    """记录用户的实际操作（BUY/SELL/TRIM/HOLD/SKIP）"""
    p = _resolve_db(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _conn(p) as con:
        con.executescript(_CREATE_ACTIONS_SQL)
        _migrate_actions_table(con)
        today = datetime.today().strftime("%Y-%m-%d")
        con.execute(
            "INSERT INTO user_actions(date, ticker, action, shares, price, note, rec_date) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (today, ticker.upper(), action.upper(), shares, price, note, rec_date or today),
        )
    print(f"[UserAction] 已记录：{ticker} {action}" +
          (f" {shares}股" if shares else "") +
          (f" @{price}" if price else ""))


def get_action_history(ticker: str | None = None,
                       days: int = 30,
                       db_path: str | Path | None = None) -> list[dict]:
    """获取操作历史，ticker=None 则返回全部"""
    p = _resolve_db(db_path)
    init_db(p)
    since = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn(p) as con:
        con.executescript(_CREATE_ACTIONS_SQL)
        if ticker:
            rows = con.execute(
                "SELECT * FROM user_actions WHERE ticker=? AND date>=? ORDER BY date DESC",
                (ticker.upper(), since),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM user_actions WHERE date>=? ORDER BY date DESC",
                (since,),
            ).fetchall()
    return [dict(r) for r in rows]


# ── 准确率统计摘要 ────────────────────────────────────────────

def accuracy_summary(days: int = 30, db_path: str | Path | None = None) -> str:
    """
    返回最近 N 天内已回填记录的准确率摘要文字，供注入飞书卡片。
    """
    p = _resolve_db(db_path)
    init_db(p)
    since = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn(p) as con:
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


# ── 周度准确率统计（方案 B）─────────────────────────────────────

def weekly_accuracy_summary(db_path: str | Path | None = None) -> dict:
    """
    查询最近 14 天内已回填 return_7d 的推荐记录，计算方案 B 胜率。

    方案 B 判定规则：
      买入 / 加仓 → return_7d > 0 为正确
      减仓 / 卖出 → return_7d < 0 为正确
      持有 / 观望 → return_7d >= -2.0% 为正确（持有期间没显著亏损）

    返回 dict：
      available: bool
      period: "MM-DD ~ MM-DD"
      total, correct, wrong, win_rate
      best:  {"ticker", "rec", "ret"}  — 正确中涨跌最大的
      worst: {"ticker", "rec", "ret"}  — 错误中涨跌最大的
    """
    p = _resolve_db(db_path)
    init_db(p)
    since = (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d")

    with _conn(p) as con:
        rows = con.execute(
            "SELECT ticker, recommendation, position_change, return_7d, date "
            "FROM recommendations "
            "WHERE date >= ? AND return_7d IS NOT NULL "
            "ORDER BY date",
            (since,),
        ).fetchall()

    if not rows:
        return {"available": False}

    results = []
    for r in rows:
        rec = r["recommendation"] or ""
        pc  = r["position_change"] or ""
        ret = r["return_7d"]

        bullish = rec == "买入" or pc.startswith(("大加", "小加"))
        bearish = rec in ("减仓", "卖出") or pc.startswith("减仓")

        if bullish:
            outcome = "正确" if ret > 0 else "错误"
        elif bearish:
            outcome = "正确" if ret < 0 else "错误"
        else:  # 持有 / 观望 / ETF定投
            outcome = "正确" if ret >= -2.0 else "错误"

        results.append({
            "ticker":  r["ticker"],
            "rec":     rec,
            "ret":     ret,
            "outcome": outcome,
            "date":    r["date"],
        })

    total   = len(results)
    correct = sum(1 for r in results if r["outcome"] == "正确")
    wrong   = sum(1 for r in results if r["outcome"] == "错误")
    win_rate = round(correct / total * 100) if total > 0 else 0

    correct_list = [r for r in results if r["outcome"] == "正确"]
    wrong_list   = [r for r in results if r["outcome"] == "错误"]
    best  = max(correct_list, key=lambda r: abs(r["ret"]), default=None)
    worst = max(wrong_list,   key=lambda r: abs(r["ret"]), default=None)

    dates  = sorted({r["date"] for r in results})
    period = f"{dates[0][5:]} ~ {dates[-1][5:]}" if dates else ""

    return {
        "available": True,
        "period":    period,
        "total":     total,
        "correct":   correct,
        "wrong":     wrong,
        "win_rate":  win_rate,
        "best":      best,
        "worst":     worst,
    }


# ── 用户反馈闭环 ──────────────────────────────────────────────

async def backfill_action_returns(db_path: str | Path | None = None) -> int:
    """
    回填 BUY 操作 7 天后的实际涨跌幅（actual_return）。
    - 找出 7+ 天前、actual_return 为空的 BUY 记录
    - 用 yfinance 拉取操作当天收盘价 → 当前价，计算涨跌幅
    - 只计算到操作日后第 7 个交易日（近似用 period='10d'）
    """
    p = _resolve_db(db_path)
    init_db(p)
    cutoff = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")

    with _conn(p) as con:
        _migrate_actions_table(con)
        pending = con.execute(
            "SELECT id, ticker, date, price FROM user_actions "
            "WHERE action = 'BUY' AND actual_return IS NULL AND date <= ?",
            (cutoff,),
        ).fetchall()

    if not pending:
        return 0

    loop = asyncio.get_event_loop()
    filled = 0

    for row in pending:
        row_id, ticker, buy_date, entry_price = row["id"], row["ticker"], row["date"], row["price"]
        try:
            # 用 yfinance 拉 buy_date 之后的价格序列
            df = await loop.run_in_executor(
                None,
                lambda t=ticker, d=buy_date: yf.download(
                    t, start=d, period="15d", progress=False, auto_adjust=True
                )
            )
            if df.empty:
                continue

            if hasattr(df.columns, "levels"):  # MultiIndex
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]

            # 以操作日收盘价为基准（若用户记录了 price 则优先用）
            base = float(entry_price) if entry_price else float(df["close"].iloc[0])
            # 取第 7 个交易日（或最后一天）收盘价
            target_idx = min(6, len(df) - 1)
            target_price = float(df["close"].iloc[target_idx])
            ret = round((target_price - base) / base * 100, 2)

            with _conn(p) as con:
                _migrate_actions_table(con)
                con.execute(
                    "UPDATE user_actions SET actual_return = ? WHERE id = ?",
                    (ret, row_id),
                )
            filled += 1
            print(f"[FeedbackLoop] {ticker} BUY@{base:.2f} → 7d {ret:+.1f}%")
        except Exception as e:
            print(f"[FeedbackLoop] {ticker} 回填失败: {e}")

    if filled:
        print(f"[FeedbackLoop] 回填 {filled} 条用户操作涨跌记录")
    return filled


def get_feedback_accuracy(db_path: str | Path | None = None) -> dict:
    """
    计算用户操作准确率，返回结构化统计：
      bought  — 实际执行买入的操作，有多少盈利
      skipped — 标记 SKIP/HOLD 但模型推荐买入的，实际涨了多少（错过机会）
    """
    p = _resolve_db(db_path)
    init_db(p)

    def _stats(rows: list) -> dict:
        total = len(rows)
        if total == 0:
            return {"total": 0, "wins": 0, "win_rate": 0.0, "avg_return": 0.0}
        wins = sum(1 for r in rows if (r["actual_return"] or 0) > 0)
        avg = sum((r["actual_return"] or 0) for r in rows) / total
        return {
            "total": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1),
            "avg_return": round(avg, 2),
        }

    with _conn(p) as con:
        _migrate_actions_table(con)
        bought = con.execute(
            "SELECT actual_return FROM user_actions "
            "WHERE action = 'BUY' AND actual_return IS NOT NULL"
        ).fetchall()
        skipped = con.execute(
            "SELECT actual_return FROM user_actions "
            "WHERE action IN ('SKIP', 'HOLD') AND actual_return IS NOT NULL"
        ).fetchall()

    return {
        "bought": _stats(bought),
        "skipped": _stats(skipped),
    }


def feedback_summary(db_path: str | Path | None = None) -> str:
    """
    返回一行反馈闭环摘要文字，供注入飞书卡片或月度回顾。
    示例：「实际买入 8 次：胜率 75% · 均盈 +3.2%；跳过 5 次：其中 3 次事后涨了」
    """
    s = get_feedback_accuracy(db_path)
    b, k = s["bought"], s["skipped"]

    parts = []
    if b["total"] > 0:
        parts.append(
            f"实际买入 {b['total']} 次：胜率 {b['win_rate']}% · 均{'+' if b['avg_return'] >= 0 else ''}{b['avg_return']}%"
        )
    if k["total"] > 0:
        missed = k["wins"]
        parts.append(
            f"跳过/观望 {k['total']} 次：其中 {missed} 次事后上涨（错过机会）"
        )

    return "；".join(parts) if parts else ""


# ── 暴跌警报追踪 ────────────────────────────────────────────────

def save_dip_alert(ticker: str, market: str, drop_pct: float,
                   price_at_alert: float, analysis: dict,
                   db_path: str | Path | None = None) -> int:
    """
    保存一条暴跌警报记录，返回新记录 id。
    analysis 来自 _analyze_dip() 的返回值。
    """
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        cur = con.execute(
            """INSERT INTO dip_alerts
               (ticker, market, drop_pct, price_at_alert, opportunity, thesis_intact, drop_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker, market, round(drop_pct, 2), round(price_at_alert, 4),
                analysis.get("opportunity", ""),
                1 if analysis.get("thesis_intact") else 0,
                analysis.get("drop_reason", ""),
            ),
        )
        return cur.lastrowid


def backfill_dip_outcomes(db_path: str | Path | None = None) -> int:
    """
    对已有 24h 但未回填 price_24h 的记录，以及 7d 未回填 price_7d 的记录，
    拉取当前价格计算实际收益。返回回填条数。
    """
    p = _resolve_db(db_path)
    init_db(p)
    filled = 0
    with _conn(p) as con:
        rows = con.execute(
            """SELECT id, ticker, market, alerted_at, price_at_alert, price_24h, price_7d
               FROM dip_alerts
               WHERE (price_24h IS NULL OR price_7d IS NULL)
                 AND alerted_at < datetime('now', '-23 hours')"""
        ).fetchall()

    for row in rows:
        yf_ticker = (f"{int(row['ticker']):04d}.HK"
                     if row["market"] == "hk" else row["ticker"])
        try:
            hist = yf.download(yf_ticker, period="10d", interval="1d",
                               progress=False, auto_adjust=True)
            if hist.empty:
                continue
            if isinstance(hist.columns, pd.MultiIndex):
                hist.columns = [c[0].lower() for c in hist.columns]
            else:
                hist.columns = [c.lower() for c in hist.columns]
            closes = hist["close"].dropna()
            if len(closes) < 1:
                continue
            alerted_dt = datetime.fromisoformat(row["alerted_at"])
            base = row["price_at_alert"]
            updates: dict[str, float] = {}

            def _ret(price: float) -> float:
                return round((price - base) / base * 100, 2) if base else 0.0

            # 24h price: first trading close after alerted_at + 1 day
            if row["price_24h"] is None:
                target_1d = alerted_dt + timedelta(days=1)
                later = closes[closes.index >= pd.Timestamp(target_1d.date())]
                if not later.empty:
                    p24 = round(float(later.iloc[0]), 4)
                    updates["price_24h"] = p24
                    updates["return_24h"] = _ret(p24)

            # 7d price
            if row["price_7d"] is None:
                target_7d = alerted_dt + timedelta(days=7)
                if datetime.utcnow() >= target_7d:
                    later = closes[closes.index >= pd.Timestamp(target_7d.date())]
                    if not later.empty:
                        p7 = round(float(later.iloc[0]), 4)
                        updates["price_7d"] = p7
                        updates["return_7d"] = _ret(p7)

            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                with _conn(p) as con:
                    con.execute(
                        f"UPDATE dip_alerts SET {set_clause} WHERE id = ?",
                        (*updates.values(), row["id"]),
                    )
                filled += 1
        except Exception as e:
            print(f"[DipTracker] 回填失败 {row['ticker']}: {e}")

    if filled:
        print(f"[DipTracker] 回填 {filled} 条暴跌警报")
    return filled


def get_dip_stats(days: int = 30, db_path: str | Path | None = None) -> list[dict]:
    """返回最近 N 天的暴跌警报统计，按 alerted_at 降序。"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            """SELECT ticker, market, alerted_at, drop_pct, price_at_alert,
                      opportunity, thesis_intact, drop_reason,
                      return_24h, return_7d
               FROM dip_alerts
               WHERE alerted_at >= datetime('now', ?)
               ORDER BY alerted_at DESC""",
            (f"-{days} days",),
        ).fetchall()
    return [dict(r) for r in rows]
