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

# 各市场对应的基准指数（用于计算超额收益）
_BENCHMARK_TICKER = {"us": "SPY", "hk": "^HSI", "cn": "000300.SS"}


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


def _migrate_recommendations_table(con: sqlite3.Connection) -> None:
    """为已存在的 recommendations 表补充 market / benchmark_return_7d 列（向后兼容）"""
    existing = {row[1] for row in con.execute("PRAGMA table_info(recommendations)").fetchall()}
    for col, typedef in [("market", "TEXT"), ("benchmark_return_7d", "REAL")]:
        if col not in existing:
            try:
                con.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass


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
        con.executescript(_CREATE_ACTIONS_SQL)
        con.executescript(_CREATE_SNAPSHOT_SQL)
        con.executescript(_CREATE_GUIDANCE_SQL)
        _migrate_actions_table(con)
        _migrate_recommendations_table(con)


# ── 写入当日推荐 ──────────────────────────────────────────────

def save_recommendations(date: str, records: list[dict],
                         db_path: str | Path | None = None) -> None:
    """
    records 每项：{ticker, recommendation, confidence, position_change, price_at_rec, market}
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
             r.get("position_change"), r.get("price_at_rec"), r.get("market", "us"))
            for r in records
            if r["ticker"] not in existing
        ]
        if rows:
            con.executemany(
                "INSERT INTO recommendations(date,ticker,recommendation,confidence,"
                "position_change,price_at_rec,market) VALUES(?,?,?,?,?,?,?)",
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


def _fetch_benchmark_return(market: str, rec_date: str) -> float | None:
    """
    获取从推荐日起约 7 个交易日的基准指数涨跌幅（%）。
    用 yfinance 拉历史数据，取第 0 和第 6（最多）个收盘价计算。
    失败返回 None（不阻塞主流程）。
    """
    import time
    time.sleep(0.3)
    benchmark = _BENCHMARK_TICKER.get(market, "SPY")
    try:
        start_dt = datetime.strptime(rec_date, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=15)
        df = yf.download(benchmark, start=rec_date, end=end_dt.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 2:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        closes = df["close"].dropna()
        if len(closes) < 2:
            return None
        p0 = float(closes.iloc[0])
        p7 = float(closes.iloc[min(6, len(closes) - 1)])
        return round((p7 - p0) / p0 * 100, 2) if p0 > 0 else None
    except Exception:
        return None


def _yf_ticker(ticker: str, market: str) -> str:
    return f"{int(ticker):04d}.HK" if (market == "hk" and str(ticker).isdigit()) else ticker


def _lower_cols(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return df


def _fetch_paired_window(ticker: str, market: str, rec_date: str, fwd_td: int = 7):
    """
    个股腿：rec_date 起第 0 个交易日收盘 → 第 min(fwd_td, len-1) 个交易日收盘。
    返回 (p0, p_exit, exit_date, n_td)；产出 exit_date 供基准腿对齐到【同一窗口】。失败 None。
    """
    import time
    time.sleep(0.4)
    try:
        start_dt = datetime.strptime(rec_date, "%Y-%m-%d")
        end_dt = start_dt + timedelta(days=fwd_td * 2 + 14)   # 留足交易日余量（含长假）
        df = yf.download(_yf_ticker(ticker, market), start=rec_date,
                         end=end_dt.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if df.empty:
            return None
        closes = _lower_cols(df)["close"].dropna()
        if len(closes) < 2:
            return None
        p0 = float(closes.iloc[0])
        exit_idx = min(fwd_td, len(closes) - 1)
        return (p0, float(closes.iloc[exit_idx]),
                closes.index[exit_idx].strftime("%Y-%m-%d"), exit_idx)
    except Exception:
        return None


def _fetch_benchmark_window(market: str, rec_date: str, exit_date: str) -> float | None:
    """基准腿：与个股腿【同起点(rec_date 首个交易日) 同终点(exit_date)】，消除两腿窗口错位。"""
    import time
    time.sleep(0.3)
    benchmark = _BENCHMARK_TICKER.get(market, "SPY")
    try:
        end_dt = datetime.strptime(exit_date, "%Y-%m-%d") + timedelta(days=4)
        df = yf.download(benchmark, start=rec_date, end=end_dt.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        closes = _lower_cols(df)["close"].dropna()
        if len(closes) < 2:
            return None
        b0 = float(closes.iloc[0])
        le = closes[closes.index <= pd.Timestamp(exit_date)]   # asof exit_date
        b_exit = float(le.iloc[-1]) if not le.empty else float(closes.iloc[-1])
        return round((b_exit - b0) / b0 * 100, 2) if b0 > 0 else None
    except Exception:
        return None


async def fill_7d_returns(db_path: str | Path | None = None) -> int:
    """
    找出 7 个交易日前（日历日 ~10 天）还没有 price_7d 的记录，回填。
    用【配对窗口】：个股与基准同起点(rec日)同终点(rec+7交易日)，消除旧版"个股用回填时
    最新价 vs 基准取固定第6交易日"的窗口错位伪 alpha（L1b 核心修复）。
    """
    p = _resolve_db(db_path)
    init_db(p)
    cutoff = (datetime.today() - timedelta(days=10)).strftime("%Y-%m-%d")

    with _conn(p) as con:
        pending = con.execute(
            "SELECT id, ticker, recommendation, position_change, price_at_rec, date, "
            "COALESCE(market, 'us') as market "
            "FROM recommendations WHERE date <= ? AND price_7d IS NULL",
            (cutoff,),
        ).fetchall()

    if not pending:
        return 0

    loop = asyncio.get_event_loop()
    filled = 0
    for row in pending:
        win = await loop.run_in_executor(
            None, lambda t=row["ticker"], m=row["market"], d=row["date"]: _fetch_paired_window(t, m, d)
        )
        if win is None:
            continue
        p0, p_exit, exit_date, _n = win
        if p0 <= 0:
            continue
        ret = (p_exit - p0) / p0 * 100
        outcome = _determine_outcome(
            row["recommendation"] or "", row["position_change"] or "", ret
        )
        # 基准腿与个股腿同起点同终点（exit_date），消除窗口错位
        benchmark_ret = await loop.run_in_executor(
            None, lambda m=row["market"], d=row["date"], e=exit_date: _fetch_benchmark_window(m, d, e)
        )
        with _conn(p) as con:
            con.execute(
                "UPDATE recommendations SET price_7d=?, return_7d=?, outcome=?, benchmark_return_7d=? WHERE id=?",
                (round(p_exit, 4), round(ret, 2), outcome,
                 round(benchmark_ret, 2) if benchmark_ret is not None else None,
                 row["id"]),
            )
        filled += 1

    if filled:
        print(f"[Tracker] 回填 {filled} 条 7 日涨跌记录（含基准对比）")
    return filled


async def realign_alpha(db_path: str | Path | None = None, dry_run: bool = True,
                        limit: int | None = None) -> dict:
    """
    一次性重算历史 alpha（修旧版两腿窗口错位的伪 alpha）：对所有 price_7d 或 benchmark
    已填的行，用配对窗口重算 return_7d/price_7d/benchmark_return_7d/outcome。
    dry_run=True 只返回 old→new 对照不写库；幂等可重跑。返回 {checked, changed, samples}。
    """
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            "SELECT id, ticker, recommendation, position_change, date, return_7d, "
            "benchmark_return_7d, COALESCE(market,'us') AS market FROM recommendations "
            "WHERE price_7d IS NOT NULL OR benchmark_return_7d IS NOT NULL ORDER BY date"
        ).fetchall()
    if limit:
        rows = rows[:limit]

    loop = asyncio.get_event_loop()
    changed = 0
    samples: list[dict] = []
    for row in rows:
        win = await loop.run_in_executor(
            None, lambda t=row["ticker"], m=row["market"], d=row["date"]: _fetch_paired_window(t, m, d)
        )
        if win is None or win[0] <= 0:
            continue
        p0, p_exit, exit_date, _n = win
        new_ret = round((p_exit - p0) / p0 * 100, 2)
        new_bm = await loop.run_in_executor(
            None, lambda m=row["market"], d=row["date"], e=exit_date: _fetch_benchmark_window(m, d, e)
        )
        new_bm = round(new_bm, 2) if new_bm is not None else None
        new_outcome = _determine_outcome(row["recommendation"] or "", row["position_change"] or "", new_ret)
        old_ret, old_bm = row["return_7d"], row["benchmark_return_7d"]
        if old_ret == new_ret and old_bm == new_bm:
            continue
        changed += 1
        if len(samples) < 40:
            oa = (old_ret - old_bm) if (old_ret is not None and old_bm is not None) else None
            na = (new_ret - new_bm) if new_bm is not None else None
            samples.append({
                "ticker": row["ticker"], "date": row["date"],
                "old_ret": old_ret, "new_ret": new_ret,
                "old_alpha": round(oa, 2) if oa is not None else None,
                "new_alpha": round(na, 2) if na is not None else None,
            })
        if not dry_run:
            with _conn(p) as con:
                con.execute(
                    "UPDATE recommendations SET price_7d=?, return_7d=?, outcome=?, "
                    "benchmark_return_7d=? WHERE id=?",
                    (round(p_exit, 4), new_ret, new_outcome, new_bm, row["id"]),
                )
    return {"checked": len(rows), "changed": changed, "dry_run": dry_run, "samples": samples}


def backfill_market(db_path: str | Path | None = None) -> int:
    """
    为 market 为 NULL 的历史推荐按 ticker 推断补齐（纯数字→hk，其余→us）。
    纯元数据修正，不重算 return_7d / benchmark_return_7d（历史基准值仍为旧口径，
    重算留 L1b）。返回补齐条数。
    """
    p = _resolve_db(db_path)
    init_db(p)
    n = 0
    with _conn(p) as con:
        rows = con.execute(
            "SELECT id, ticker FROM recommendations WHERE market IS NULL"
        ).fetchall()
        for r in rows:
            mkt = "hk" if str(r["ticker"]).isdigit() else "us"
            con.execute("UPDATE recommendations SET market = ? WHERE id = ?", (mkt, r["id"]))
            n += 1
    if n:
        print(f"[Tracker] 回填 {n} 条 market 标注（按 ticker 推断）")
    return n


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
    actual_return REAL,            -- 操作后 7 天实际涨跌幅（%），BUY/SELL 均自动回填
    source        TEXT DEFAULT 'manual',  -- manual=log-action 手动；auto=portfolio.yaml 自动检测
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_actions_ticker ON user_actions(ticker);
CREATE INDEX IF NOT EXISTS idx_actions_date   ON user_actions(date);
"""

# 持仓快照：存每只票最新状态，用于和 portfolio.yaml 对比推断买卖操作
_CREATE_SNAPSHOT_SQL = """
CREATE TABLE IF NOT EXISTS holdings_snapshot (
    ticker      TEXT PRIMARY KEY,
    market      TEXT NOT NULL DEFAULT 'us',
    shares      REAL NOT NULL,
    cost_basis  REAL,
    updated_at  TEXT DEFAULT (datetime('now'))
);
"""

# 指导台账：周报/月报生成的纠偏建议，下期比对 user_actions 检验是否执行
_CREATE_GUIDANCE_SQL = """
CREATE TABLE IF NOT EXISTS guidance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_date  TEXT NOT NULL,
    source        TEXT NOT NULL,          -- weekly / monthly
    ticker        TEXT,                   -- 相关标的，可逗号分隔（IAU,SGOV）
    action        TEXT,                   -- 建仓/补仓/减仓/清仓
    target        TEXT,                   -- 目标描述
    rationale     TEXT,
    due_by        TEXT,
    status        TEXT DEFAULT 'open',    -- open / followed / expired
    resolved_date TEXT,
    note          TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_guidance_status ON guidance(status);
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
    if "source" not in existing_cols:
        try:
            con.execute("ALTER TABLE user_actions ADD COLUMN source TEXT DEFAULT 'manual'")
        except sqlite3.OperationalError:
            pass


def log_user_action(ticker: str, action: str,
                    shares: float | None = None,
                    price: float | None = None,
                    note: str = "",
                    rec_date: str = "",
                    source: str = "manual",
                    db_path: str | Path | None = None) -> None:
    """记录用户的实际操作（BUY/SELL/TRIM/HOLD/SKIP）。source: manual=手动 / auto=自动检测"""
    p = _resolve_db(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _conn(p) as con:
        con.executescript(_CREATE_ACTIONS_SQL)
        _migrate_actions_table(con)
        today = datetime.today().strftime("%Y-%m-%d")
        con.execute(
            "INSERT INTO user_actions(date, ticker, action, shares, price, note, rec_date, source) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (today, ticker.upper(), action.upper(), shares, price, note, rec_date or today, source),
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


def get_period_actions(since: str, until: str,
                       db_path: str | Path | None = None) -> list[dict]:
    """返回 [since, until] 区间内、actual_return 已回填的操作（逐笔复盘用）。"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            "SELECT date, ticker, action, shares, price, actual_return "
            "FROM user_actions "
            "WHERE date >= ? AND date <= ? AND actual_return IS NOT NULL "
            "ORDER BY date",
            (since, until),
        ).fetchall()
    return [dict(r) for r in rows]


# ── 持仓快照与自动采集 ─────────────────────────────────────────

def _load_snapshot(con: sqlite3.Connection) -> dict[str, dict]:
    """读 holdings_snapshot，返回 {ticker: {market, shares, cost_basis}}"""
    rows = con.execute(
        "SELECT ticker, market, shares, cost_basis FROM holdings_snapshot"
    ).fetchall()
    return {
        r["ticker"]: {"market": r["market"], "shares": r["shares"],
                      "cost_basis": r["cost_basis"]}
        for r in rows
    }


def _save_snapshot(con: sqlite3.Connection, holdings: list[dict]) -> None:
    """用当前 holdings 覆盖快照（先清空，以正确处理已清仓消失的票）"""
    con.execute("DELETE FROM holdings_snapshot")
    con.executemany(
        "INSERT INTO holdings_snapshot(ticker, market, shares, cost_basis, updated_at) "
        "VALUES(?, ?, ?, ?, datetime('now'))",
        [
            (str(h["ticker"]), h.get("market", "us"),
             float(h.get("shares", 0) or 0),
             (float(h.get("cost_basis") or 0) or None))
            for h in holdings
        ],
    )


def _infer_action_price(cur_shares: float, cur_cost: float | None,
                        prev_shares: float, prev_cost: float | None) -> float | None:
    """
    加仓时从加权均价变化反推「本次边际成交价」：
      (今shares×今cost − 昨shares×昨cost) / Δshares
    任一 cost 缺失或非加仓则返回 None。
    """
    delta = cur_shares - prev_shares
    if delta <= 0 or cur_cost is None or prev_cost is None:
        return None
    price = (cur_shares * cur_cost - prev_shares * prev_cost) / delta
    return round(price, 4) if price > 0 else None


def detect_portfolio_changes(
    portfolio_path: str | Path = "config/portfolio.yaml",
    db_path: str | Path | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """
    对比 portfolio.yaml 与上次快照，推断买卖操作并写入 user_actions（source='auto'）。
    返回检测到的操作列表 [{ticker, market, action, shares, price, note}]。

    规则：
      新增 ticker  → BUY 全部，价=cost_basis
      shares ↑     → BUY Δ，价=反推边际成交价
      shares ↓     → SELL |Δ|，价=检测日收盘价（近似）
      ticker 消失  → SELL 全部，价=检测日收盘价
      shares 不变  → 忽略（cost 微调视为手填修正）
    首次运行（快照为空）只建基线、返回 []。
    dry_run=True 只检测不写库、不更新快照。
    """
    import yaml
    p = _resolve_db(db_path)
    init_db(p)

    with open(Path(portfolio_path)) as f:
        portfolio = yaml.safe_load(f) or {}
    holdings = portfolio.get("holdings", [])
    current = {
        str(h["ticker"]): {
            "market": h.get("market", "us"),
            "shares": float(h.get("shares", 0) or 0),
            "cost_basis": (float(h.get("cost_basis") or 0) or None),
        }
        for h in holdings
    }

    with _conn(p) as con:
        prev = _load_snapshot(con)

    # ── 首次运行：只建基线，不生成操作 ──
    if not prev:
        if not dry_run:
            with _conn(p) as con:
                _save_snapshot(con, holdings)
        print(f"[AutoDetect] 首次运行，建立持仓基线（{len(current)} 只），不生成操作")
        return []

    SHARE_EPS = 1e-4
    detected: list[dict] = []
    for ticker in sorted(set(prev) | set(current)):
        c, pv = current.get(ticker), prev.get(ticker)
        if c and not pv:                                  # 新建仓
            detected.append({
                "ticker": ticker, "market": c["market"], "action": "BUY",
                "shares": round(c["shares"], 4), "price": c["cost_basis"],
                "note": "自动检测：新建仓",
            })
        elif pv and not c:                                # 清仓
            price = _fetch_current_price(ticker, pv["market"])
            detected.append({
                "ticker": ticker, "market": pv["market"], "action": "SELL",
                "shares": round(pv["shares"], 4),
                "price": round(price, 4) if price else None,
                "note": "自动检测：清仓（portfolio.yaml 已移除）",
            })
        elif c and pv:
            delta = c["shares"] - pv["shares"]
            if delta > SHARE_EPS:                         # 加仓
                detected.append({
                    "ticker": ticker, "market": c["market"], "action": "BUY",
                    "shares": round(delta, 4),
                    "price": _infer_action_price(c["shares"], c["cost_basis"],
                                                 pv["shares"], pv["cost_basis"]),
                    "note": "自动检测：加仓",
                })
            elif delta < -SHARE_EPS:                      # 减仓
                price = _fetch_current_price(ticker, c["market"])
                detected.append({
                    "ticker": ticker, "market": c["market"], "action": "SELL",
                    "shares": round(-delta, 4),
                    "price": round(price, 4) if price else None,
                    "note": "自动检测：减仓",
                })
            # shares 不变（含 cost 微调）→ 忽略

    if dry_run:
        return detected

    for a in detected:
        log_user_action(
            ticker=a["ticker"], action=a["action"], shares=a["shares"],
            price=a["price"], note=a["note"], source="auto", db_path=p,
        )
    with _conn(p) as con:
        _save_snapshot(con, holdings)

    if detected:
        print(f"[AutoDetect] 检测到 {len(detected)} 笔操作，已记入 user_actions（source=auto）")
    else:
        print("[AutoDetect] 持仓无变化，未生成操作")
    return detected


# ── 指导台账（guidance ledger）─────────────────────────────────

_BUY_GUIDANCE = {"建仓", "补仓", "买入", "加仓"}
_SELL_GUIDANCE = {"减仓", "清仓", "卖出"}


def save_guidance(items: list[dict], source: str = "weekly",
                  db_path: str | Path | None = None) -> int:
    """
    批量写入指导项，返回新增条数。
    去重：同 (source, ticker, action) 在近 7 天内已有 open 记录则跳过（避免每周重复）。
    items 每项：{ticker, action, target, rationale, due_by?}
    """
    p = _resolve_db(db_path)
    init_db(p)
    today = datetime.today().strftime("%Y-%m-%d")
    recent = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    added = 0
    with _conn(p) as con:
        for it in items:
            ticker = (it.get("ticker") or "").upper()
            action = it.get("action") or ""
            dup = con.execute(
                "SELECT 1 FROM guidance WHERE source=? AND ticker=? AND action=? "
                "AND status='open' AND created_date >= ? LIMIT 1",
                (source, ticker, action, recent),
            ).fetchone()
            if dup:
                continue
            due_by = it.get("due_by") or (
                datetime.today() + timedelta(days=14)).strftime("%Y-%m-%d")
            con.execute(
                "INSERT INTO guidance(created_date, source, ticker, action, target, "
                "rationale, due_by, status) VALUES(?,?,?,?,?,?,?,'open')",
                (today, source, ticker, action, it.get("target", ""),
                 it.get("rationale", ""), due_by),
            )
            added += 1
    if added:
        print(f"[Guidance] 写入 {added} 条新指导（source={source}）")
    return added


def check_guidance_adherence(db_path: str | Path | None = None) -> dict:
    """
    遍历所有 open 指导项，比对 created_date 之后的 user_actions：
      建仓/补仓/买入/加仓 → 找 BUY；减仓/清仓/卖出 → 找 SELL/TRIM
      ticker 逗号分隔，任一匹配即算执行
    命中 → followed + resolved_date；已过 due_by 未命中 → expired
    返回 {followed:[...], expired:[...], open:[...]}，每项含 ticker/action/target/due_by。
    """
    p = _resolve_db(db_path)
    init_db(p)
    today = datetime.today().strftime("%Y-%m-%d")
    # followed/expired/open 用于正向指导（该做的做没做）；
    # guardrail_* 用于护栏（反向语义：照做=忍住没买）；分开避免污染正向指导计数。
    result: dict[str, list] = {"followed": [], "expired": [], "open": [],
                               "guardrail_resisted": [], "guardrail_ignored": []}

    with _conn(p) as con:
        rows = con.execute("SELECT * FROM guidance WHERE status='open'").fetchall()
        for g in rows:
            tickers = [t.strip().upper() for t in (g["ticker"] or "").split(",") if t.strip()]
            action = g["action"] or ""
            item = {"id": g["id"], "ticker": g["ticker"], "action": action,
                    "target": g["target"], "due_by": g["due_by"]}

            # ── 护栏(反向语义)：买了=没忍住(ignored)；到期没买=忍住了(resisted)──
            if g["source"] == "guardrail":
                bought = None
                if tickers:
                    tk_ph = ",".join("?" * len(tickers))
                    bought = con.execute(
                        f"SELECT date FROM user_actions WHERE ticker IN ({tk_ph}) "
                        f"AND action = 'BUY' AND date >= ? ORDER BY date LIMIT 1",
                        (*tickers, g["created_date"]),
                    ).fetchone()
                if bought:
                    con.execute("UPDATE guidance SET status='gr_ignored', resolved_date=? WHERE id=?",
                                (bought["date"], g["id"]))
                    item["resolved_date"] = bought["date"]
                    result["guardrail_ignored"].append(item)
                elif g["due_by"] and today > g["due_by"]:
                    con.execute("UPDATE guidance SET status='gr_resisted', resolved_date=? WHERE id=?",
                                (today, g["id"]))
                    result["guardrail_resisted"].append(item)
                else:
                    result["open"].append(item)
                continue

            if action in _BUY_GUIDANCE:
                want = ("BUY",)
            elif action in _SELL_GUIDANCE:
                want = ("SELL", "TRIM")
            else:
                want = ()

            matched = None
            if tickers and want:
                tk_ph = ",".join("?" * len(tickers))
                act_ph = ",".join("?" * len(want))
                matched = con.execute(
                    f"SELECT date FROM user_actions "
                    f"WHERE ticker IN ({tk_ph}) AND action IN ({act_ph}) AND date >= ? "
                    f"ORDER BY date LIMIT 1",
                    (*tickers, *want, g["created_date"]),
                ).fetchone()

            if matched:
                con.execute(
                    "UPDATE guidance SET status='followed', resolved_date=? WHERE id=?",
                    (matched["date"], g["id"]),
                )
                item["resolved_date"] = matched["date"]
                result["followed"].append(item)
            elif g["due_by"] and today > g["due_by"]:
                con.execute(
                    "UPDATE guidance SET status='expired', resolved_date=? WHERE id=?",
                    (today, g["id"]),
                )
                result["expired"].append(item)
            else:
                result["open"].append(item)

    return result


def get_open_guidance(db_path: str | Path | None = None) -> list[dict]:
    """列出所有未结（open）指导项，按创建日期倒序。"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            "SELECT * FROM guidance WHERE status='open' ORDER BY created_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def guidance_month_summary(since: str, until: str,
                           db_path: str | Path | None = None) -> dict:
    """返回 [since, until] 内创建的 guidance 按状态计数（行为打分输入用）。"""
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM guidance "
            "WHERE created_date >= ? AND created_date <= ? GROUP BY status",
            (since, until),
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "followed": counts.get("followed", 0),
        "expired": counts.get("expired", 0),
        "open": counts.get("open", 0),
    }


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
    回填 BUY/SELL/TRIM 操作 7 天后的远期涨跌幅（actual_return）。
    - 找出 7+ 天前、actual_return 为空的记录
    - 用 yfinance 拉操作当天收盘价 → 第 7 个交易日收盘价，计算涨跌幅
    - 符号约定：BUY 远期为正=买对了；SELL/TRIM 远期为负=卖对了（躲过下跌）
    - 港股代码（纯数字）自动转 yfinance 的 NNNN.HK 格式
    """
    p = _resolve_db(db_path)
    init_db(p)
    cutoff = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")

    with _conn(p) as con:
        _migrate_actions_table(con)
        pending = con.execute(
            "SELECT id, ticker, action, date, price FROM user_actions "
            "WHERE action IN ('BUY','SELL','TRIM') AND actual_return IS NULL AND date <= ?",
            (cutoff,),
        ).fetchall()

    if not pending:
        return 0

    loop = asyncio.get_event_loop()
    filled = 0

    for row in pending:
        row_id, ticker, act = row["id"], row["ticker"], row["action"]
        act_date, entry_price = row["date"], row["price"]
        # 港股纯数字代码 → NNNN.HK
        yf_ticker = f"{int(ticker):04d}.HK" if ticker.isdigit() else ticker
        try:
            df = await loop.run_in_executor(
                None,
                lambda t=yf_ticker, d=act_date: yf.download(
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
            print(f"[FeedbackLoop] {ticker} {act}@{base:.2f} → 7d {ret:+.1f}%")
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

    def _stats(rows: list, bullish: bool = True) -> dict:
        total = len(rows)
        if total == 0:
            return {"total": 0, "wins": 0, "win_rate": 0.0, "avg_return": 0.0}
        # BUY：远期上涨=买对了；SELL：远期下跌=卖对了（躲过下跌）
        if bullish:
            wins = sum(1 for r in rows if (r["actual_return"] or 0) > 0)
        else:
            wins = sum(1 for r in rows if (r["actual_return"] or 0) < 0)
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
        sold = con.execute(
            "SELECT actual_return FROM user_actions "
            "WHERE action IN ('SELL', 'TRIM') AND actual_return IS NOT NULL"
        ).fetchall()
        skipped = con.execute(
            "SELECT actual_return FROM user_actions "
            "WHERE action IN ('SKIP', 'HOLD') AND actual_return IS NOT NULL"
        ).fetchall()

    return {
        "bought": _stats(bought),
        "sold": _stats(sold, bullish=False),
        "skipped": _stats(skipped),
    }


def ticker_signal_stats(ticker: str, db_path: str | Path | None = None) -> str:
    """
    返回某只股票的历史买入信号统计行，供飞书卡片展示信服力数据。
    样本 <5 时返回空字符串；5-19 条加置信警示；≥20 条正常展示。
    有基准数据时追加超额收益（alpha）维度：跑赢大盘比例 + 平均超额。
    格式：📊 NVDA历史买入信号 | 胜率67%（±23%CI）· 超额+2.1%/跑赢大盘58%· 均盈+3.2% · 均亏-1.8% · 盈亏比1.2（12次）
    """
    import math
    p = _resolve_db(db_path)
    init_db(p)
    with _conn(p) as con:
        rows = con.execute(
            """SELECT recommendation, position_change, return_7d, benchmark_return_7d
               FROM recommendations
               WHERE ticker = ? AND return_7d IS NOT NULL
                 AND (recommendation IN ('买入') OR position_change LIKE '大加%' OR position_change LIKE '小加%')
               ORDER BY date DESC LIMIT 60""",
            (ticker.upper(),),
        ).fetchall()

    if len(rows) < 5:
        return ""

    wins = [r["return_7d"] for r in rows if r["return_7d"] > 0]
    losses = [r["return_7d"] for r in rows if r["return_7d"] <= 0]
    n = len(rows)
    win_count = len(wins)
    win_rate = win_count / n

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    rr = round(avg_win / avg_loss, 1) if avg_loss > 0 else 0.0

    # Wilson 置信区间（95%）
    z = 1.96
    ci = z * math.sqrt(win_rate * (1 - win_rate) / n)
    ci_pct = round(ci * 100)
    win_pct = round(win_rate * 100)

    # 超额收益：仅对有基准数据的行计算
    alpha_str = ""
    rows_with_bm = [r for r in rows if r["benchmark_return_7d"] is not None]
    if rows_with_bm:
        alphas = [r["return_7d"] - r["benchmark_return_7d"] for r in rows_with_bm]
        avg_alpha = sum(alphas) / len(alphas)
        beat_count = sum(1 for a in alphas if a > 0)
        beat_rate = round(beat_count / len(rows_with_bm) * 100)
        sign = "+" if avg_alpha >= 0 else ""
        alpha_str = f" · 超额{sign}{avg_alpha:.1f}%/跑赢大盘{beat_rate}%"

    suffix = "（样本偏少，仅参考）" if n < 20 else ""
    return (
        f"📊 {ticker}历史买入信号{suffix}｜"
        f"胜率{win_pct}%（±{ci_pct}%CI）"
        f"{alpha_str}"
        f" · 均盈+{avg_win:.1f}% · 均亏-{avg_loss:.1f}% · 盈亏比{rr}（{n}次）"
    )


def feedback_summary(db_path: str | Path | None = None) -> str:
    """
    返回一行反馈闭环摘要文字，供注入飞书卡片或月度回顾。
    示例：「实际买入 8 次：胜率 75% · 均盈 +3.2%；跳过 5 次：其中 3 次事后涨了」
    """
    s = get_feedback_accuracy(db_path)
    b, k = s["bought"], s["skipped"]
    sold = s.get("sold", {"total": 0, "wins": 0})

    parts = []
    if b["total"] > 0:
        parts.append(
            f"实际买入 {b['total']} 次：胜率 {b['win_rate']}% · 均{'+' if b['avg_return'] >= 0 else ''}{b['avg_return']}%"
        )
    if sold["total"] > 0:
        parts.append(
            f"卖出 {sold['total']} 次：其中 {sold['wins']} 次成功躲跌（卖后下跌）"
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
