# src/finance_agent/signals/sentiment_factor.py
"""P1b 新闻情绪因子化——把 DeepSeek 新闻评分从「点状用完即丢」变成可回测、可 RankIC 检验的
滚动情绪因子：归一 [-1,1] → 全量落库 news_sentiment_scores → 7 日均值 + 趋势 →
仅「情绪偏负且下滑 + 技术动量转负」双确认成立才给减仓提示（降噪，避免单条噪音误导仓位）。

双 flag 分层：
  news_sentiment_factor.enabled（默认 on）   纯观测：news-scan 评分落库 + 快照埋点 recommendations
  sentiment_double_confirm_alert.enabled（默认 off） 注入 PM（改核心建议）——enable 须用户点头

⚠️ 诚实边界：
  - normalize 是启发式非统计量（sign×impact/10，非拟合）；阈值为占位 heuristic，
    在 recommendations 积累 ≥60 条带 outcome 样本、经月度 RankIC(P1c) 校准前，
    禁止当作已验证 alpha 对外宣传、禁止用于加大仓位。
  - 窗口内 n < MIN_ITEMS → 因子主动闭嘴返 None（早期恒 insufficient 是预期而非 bug）。
  - 「动量转负」借 macd_trend/composite_score 是代理，上线独立动量因子后应替换避免自我印证。
  - 提示措辞只单向收紧（复核减仓/止损纪律），绝不反向构成加仓/买入依据。
"""
from __future__ import annotations

import sqlite3
from datetime import date as _date, datetime, timedelta
from pathlib import Path

WINDOW_DAYS = 7        # 滚动窗口（日历日）
MIN_ITEMS = 3          # 窗口内新闻数低于此 → 因子闭嘴（样本不足不下结论）
TREND_RECENT_DAYS = 3  # 趋势近段=最近3天，前段=窗口其余天；任一为空 → trend=None
NEG_SCORE_THRESHOLD = -0.2   # 情绪「偏负」门槛（占位 heuristic·待 RankIC 校准）：
                             # 均值须至少相当于「一条利空 impact≥6 + 两条中性」，防微负噪声触发减仓提示

_CONFIG_DIR = Path(__file__).parents[3] / "config"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS news_sentiment_scores (
    ticker     TEXT NOT NULL,
    key        TEXT NOT NULL,           -- news_alerted 同源去重 key（低分新闻同日重评不产生重复行）
    date       TEXT NOT NULL,           -- 扫描日 YYYY-MM-DD（北京时间）
    title      TEXT,
    impact     INTEGER,
    sentiment  TEXT,                    -- 利好/利空/中性
    score      REAL,                    -- normalize 后 [-1,1]
    is_peer    INTEGER DEFAULT 0,       -- 1=该票只作为竞对被扫描（评分仍是对该票本身的，仅记来源）
    market     TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    -- date 必须进主键（对抗审查 CONFIRMED）：dedup key 只含 MM-DD 无年份，
    -- 只用 (ticker,key) 会把跨年同月日同 seed 的真实新闻误当重复静默丢弃；
    -- 含 date 后同日多轮重评仍去重（与 news_alerted (key,date) 同语义）
    PRIMARY KEY (ticker, key, date)
);
CREATE INDEX IF NOT EXISTS idx_nss_ticker_date ON news_sentiment_scores(ticker, date);
"""


def _flag_enabled(block: str, default: bool) -> bool:
    """settings.yaml 顶层块 enabled 开关，缺失/解析失败回落 default。"""
    try:
        import yaml
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            return bool((s.get(block) or {}).get("enabled", default))
    except Exception:
        pass
    return default


def sentiment_factor_enabled() -> bool:
    """纯观测档（落库+埋点快照，不注入任何 prompt）——默认 on，与 oos_monitor 同治理级别。"""
    return _flag_enabled("news_sentiment_factor", True)


def double_confirm_enabled() -> bool:
    """注入 PM 档（改核心建议）——默认 off，enable 须用户点头。"""
    return _flag_enabled("sentiment_double_confirm_alert", False)


def normalize_sentiment_score(sentiment: str, impact) -> float:
    """sign × clamp(impact,0,10) / 10 → [-1,1]。未知 sentiment 按中性 0（不猜方向）。"""
    try:
        magnitude = max(0.0, min(10.0, float(impact))) / 10.0
    except (TypeError, ValueError):
        return 0.0
    sign = {"利好": 1.0, "利空": -1.0}.get(sentiment, 0.0)
    return round(sign * magnitude, 3)


def _resolve_db(db_path=None) -> Path:
    from finance_agent.db.tracker import _resolve_db as _r
    return _r(db_path)


def record_news_sentiment(ticker: str, key: str, date: str, sentiment: str,
                          impact, title: str = "", market: str = "",
                          is_peer: int = 0, db_path=None) -> bool:
    """news-scan 评分全量落库。返回是否真的写入了新行。
    不落库的情形：观测 flag 关 / 分类失败(impact<1，写假中性只会稀释因子) / (ticker,key) 已存在。"""
    if not sentiment_factor_enabled():
        return False
    try:
        if float(impact) < 1:
            return False
    except (TypeError, ValueError):
        return False
    score = normalize_sentiment_score(sentiment, impact)
    p = _resolve_db(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=15)
    try:
        con.executescript(_CREATE_SQL)
        cur = con.execute(
            "INSERT OR IGNORE INTO news_sentiment_scores"
            "(ticker, key, date, title, impact, sentiment, score, is_peer, market) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, key, date, title, int(float(impact)), sentiment, score, int(is_peer), market),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def compute_sentiment_factor(ticker: str, db_path=None, asof: str | None = None,
                             window_days: int = WINDOW_DAYS,
                             min_items: int = MIN_ITEMS) -> dict | None:
    """滚动情绪因子：窗口内该票新闻 score 的均值 + 近段/前段趋势差。
    n < min_items → None（闭嘴）。趋势任一半窗为空 → trend=None（没有基线就不编趋势）。"""
    asof = asof or datetime.now().strftime("%Y-%m-%d")
    asof_d = _date.fromisoformat(asof)
    win_start = (asof_d - timedelta(days=window_days - 1)).isoformat()
    recent_start = (asof_d - timedelta(days=TREND_RECENT_DAYS - 1)).isoformat()

    p = _resolve_db(db_path)
    if not p.exists():
        return None
    con = sqlite3.connect(p, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(_CREATE_SQL)
        rows = con.execute(
            "SELECT date, score FROM news_sentiment_scores "
            "WHERE ticker = ? AND date >= ? AND date <= ?",
            (ticker, win_start, asof),
        ).fetchall()
    finally:
        con.close()

    if len(rows) < min_items:
        return None
    scores = [r["score"] for r in rows]
    recent = [r["score"] for r in rows if r["date"] >= recent_start]
    prior = [r["score"] for r in rows if r["date"] < recent_start]
    trend = (round(sum(recent) / len(recent) - sum(prior) / len(prior), 3)
             if recent and prior else None)
    return {"score_7d": round(sum(scores) / len(scores), 3),
            "trend": trend, "n": len(rows)}


def detect_double_confirm(factor: dict | None, macd_trend: str,
                          composite_score: float | None) -> bool:
    """双确认：情绪明确偏负(均值≤阈值)且仍在下滑(trend<0) AND 技术动量转负
    (macd bearish 且 composite<0)。条件缺一不发——宁可漏提示也不拿噪声催人减仓。"""
    if not factor:
        return False
    score, trend = factor.get("score_7d"), factor.get("trend")
    if score is None or score > NEG_SCORE_THRESHOLD:
        return False
    if trend is None or trend >= 0:
        return False
    if macd_trend != "bearish":
        return False
    if composite_score is None or composite_score >= 0:
        return False
    return True


def format_sentiment_note(ticker: str, macd_trend: str, composite_score: float | None,
                          db_path=None, asof: str | None = None) -> str:
    """双确认成立时的减仓提示（注入 strategy_evidence 供 PM）。
    flag 默认 off → 恒 ""（线上逐字节零变化）。措辞只单向收紧，绝不构成加仓依据。"""
    if not double_confirm_enabled():
        return ""
    factor = compute_sentiment_factor(ticker, db_path=db_path, asof=asof)
    if not detect_double_confirm(factor, macd_trend, composite_score):
        return ""
    return (f"【情绪双确认】{ticker} 近{WINDOW_DAYS}日新闻情绪偏负"
            f"（均值{factor['score_7d']:+.2f}·{factor['n']}条·趋势{factor['trend']:+.2f}）"
            f"且技术动量转弱——宜复核减仓/止损纪律。"
            f"（情绪归一为占位启发式·待RankIC校准，本提示仅单向收紧、不构成反向操作依据）")


def sentiment_snapshot(ticker: str, db_path=None, asof: str | None = None) -> dict:
    """recommendations 埋点快照（供 P1c RankIC 校验因子排序力）。
    观测 flag 关 / 因子闭嘴 → 全 None（列恒 NULL，与改动前一致）。"""
    empty = {"sentiment_7d": None, "sentiment_trend": None, "sentiment_n": None}
    if not sentiment_factor_enabled():
        return empty
    factor = compute_sentiment_factor(ticker, db_path=db_path, asof=asof)
    if not factor:
        return empty
    return {"sentiment_7d": factor["score_7d"],
            "sentiment_trend": factor["trend"],
            "sentiment_n": factor["n"]}
