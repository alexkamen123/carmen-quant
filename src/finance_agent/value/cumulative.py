# src/finance_agent/value/cumulative.py
"""累计「你 vs 躺平·截至今天」头条——回答北极星最朴素的问题：
听卡门建议的真实持仓 vs 同期把同样的钱躺平买指数，到今天我多赚还是少赚。

口径 = 真实账户（basis='real'）：
  - 仓位取 portfolio.yaml 的实际 shares × cost_basis（本金）、× 今日最新价（市值）；
  - 「躺平」= 同一笔本金、在该仓位入场日买入对应市场基准(SPY/恒指/沪深300)持有至今；
  - 入场日取 user_actions 里该票最早 BUY；缺失则退回组合 inception（最早 BUY 日）。
  - 跨币种按 CLAUDE.md 折美元（HKD÷7.8、CNY÷7.2）后汇总。
  - 全部实时算、不冻结；数据不足的仓位排除出对比并如实标注（partial）。

与 metrics.py 的 7 天定格记分牌是两件事：这里是「你账户到今天」，那里是「信号短期命中」。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from finance_agent.db.tracker import (
    _resolve_db, _conn, _fetch_current_price, _fetch_benchmark_window,
)

# 货币→美元（仅用于跨币种汇总，与单仓盈亏显示无关；口径同集中度计算）
_CCY_USD = {"us": 1.0, "hk": 1.0 / 7.8, "cn": 1.0 / 7.2}

_DEFAULT_PF = Path(__file__).resolve().parents[2].parent / "config" / "portfolio.yaml"


def compute_live_rec_returns(db_path=None, today: str | None = None, min_days: int = 5,
                             price_fn=_fetch_current_price,
                             bench_fn=_fetch_benchmark_window) -> dict[int, tuple]:
    """每条推荐「到今天」的真实涨跌 + 同期基准（终局视角，替代第7天定格判对错）。
    只纳入满 min_days 天的推荐（太新的没到评估期、噪声大）；entry=price_at_rec，今日价/基准实时拉。
    返回 {rec_id: (live_ret_pct, live_bench_pct | None)}。无 entry / 拉价失败 / 太新的不计入。
    用法：作为 compute_value_metrics 的 live_overrides——把每行 return_7d/benchmark_return_7d
    在计算前替换成至今值，下游命中率/alpha 计算一行不改即切到终局口径。"""
    from datetime import datetime, timezone, timedelta
    p = _resolve_db(db_path)
    if today is None:
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    t_dt = datetime.strptime(today, "%Y-%m-%d")
    with _conn(p) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ticker, date, price_at_rec, COALESCE(market,'us') AS market "
            "FROM recommendations WHERE price_at_rec IS NOT NULL"
        ).fetchall()]
    # 按 (ticker,市场) 缓存今日价、按 (市场,入场日) 缓存至今基准——几百条推荐多为重复标的，
    # 否则会对每条逐个拉价(数百次 yfinance、数分钟)。缓存后只拉约标的数次。
    px_cache: dict = {}
    bench_cache: dict = {}

    def _px(tk, mkt):
        if (tk, mkt) not in px_cache:
            try:
                px_cache[(tk, mkt)] = price_fn(tk, mkt)
            except Exception:
                px_cache[(tk, mkt)] = None
        return px_cache[(tk, mkt)]

    def _bench(mkt, d):
        if (mkt, d) not in bench_cache:
            try:
                bench_cache[(mkt, d)] = bench_fn(mkt, d, today)
            except Exception:
                bench_cache[(mkt, d)] = None
        return bench_cache[(mkt, d)]

    out: dict[int, tuple] = {}
    for r in rows:
        try:
            held = (t_dt - datetime.strptime(r["date"], "%Y-%m-%d")).days
        except (ValueError, TypeError):
            continue
        if held < min_days:
            continue                      # 太新：还没到评估期，排除
        entry = r["price_at_rec"]
        if not entry or entry <= 0:
            continue
        px = _px(r["ticker"], r["market"])
        if px is None:
            continue                      # 拉价失败：该条不纳入（下游回落或排除）
        live_ret = round((px - entry) / entry * 100, 2)
        bench = _bench(r["market"], r["date"])
        out[r["id"]] = (live_ret, round(bench, 2) if bench is not None else None)
    return out


def compute_live_action_returns(db_path=None, price_fn=_fetch_current_price) -> dict[int, float]:
    """逐笔操作「到今天」的真实涨跌（动态实时拉价）——回答"这笔操作现在赚还是亏"。
    用与 7 日定格同源的 entry price（user_actions.price）+ 今日价，尺度一致、可并排。
    返回 {action_id: live_pct}；无 entry / 拉价失败的笔不计入（渲染层 fallback 7 日定格）。
    港股用 isdigit 判市场（与 backfill_action_returns 一致）。任何单笔失败不影响其余。"""
    p = _resolve_db(db_path)
    with _conn(p) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ticker, price FROM user_actions "
            "WHERE action IN ('BUY','SELL','TRIM') AND price IS NOT NULL AND price > 0"
        ).fetchall()]
    out: dict[int, float] = {}
    for r in rows:
        mkt = "hk" if str(r["ticker"]).isdigit() else "us"
        try:
            px = price_fn(r["ticker"], mkt)
        except Exception:
            px = None
        if px is None:
            continue
        out[r["id"]] = round((px - r["price"]) / r["price"] * 100, 2)
    return out


def aggregate_cumulative(positions: list[dict], as_of: str,
                         basis: str = "real") -> dict | None:
    """纯聚合：positions 每条须含 principal_usd / current_value_usd / passive_value_usd
    （均为同一可比集合、已折美元）。返回头条 dict；无可比仓位返回 None。"""
    usable = [p for p in positions if p.get("principal_usd")]
    if not usable:
        return None
    principal = sum(p["principal_usd"] for p in usable)
    strat_val = sum(p["current_value_usd"] for p in usable)
    passive_val = sum(p["passive_value_usd"] for p in usable)
    if principal <= 0:
        return None
    strat_pct = (strat_val - principal) / principal * 100
    passive_pct = (passive_val - principal) / principal * 100
    return {
        "basis": basis,
        "as_of": as_of,
        "n_positions": len(usable),
        "principal_usd": round(principal, 2),
        "strategy_cum_pct": round(strat_pct, 1),
        "passive_cum_pct": round(passive_pct, 1),
        "excess_pct": round(strat_pct - passive_pct, 1),
        "excess_amount_usd": round(strat_val - passive_val, 2),
    }


def _load_positions(portfolio_path) -> list[dict]:
    with open(portfolio_path) as f:
        pf = yaml.safe_load(f) or {}
    out = []
    for h in pf.get("holdings", []):
        out.append({
            "ticker": str(h["ticker"]),
            "market": h.get("market", "us"),
            "shares": float(h.get("shares", 0) or 0),
            "cost_basis": h.get("cost_basis"),
        })
    return out


def _entry_dates(db_path) -> tuple[dict, str | None]:
    """每票最早 BUY 日 + 组合 inception（最早 BUY 日，缺失时的兜底入场日）。"""
    p = _resolve_db(db_path)
    with _conn(p) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT ticker, MIN(date) AS d FROM user_actions "
            "WHERE action = 'BUY' GROUP BY ticker"
        ).fetchall()]
    per_ticker = {r["ticker"].upper(): r["d"] for r in rows if r["d"]}
    inception = min(per_ticker.values()) if per_ticker else None
    return per_ticker, inception


def compute_cumulative_value(db_path=None, portfolio_path=None, today: str | None = None,
                             price_fn=_fetch_current_price,
                             bench_fn=_fetch_benchmark_window) -> dict | None:
    """编排：读真实持仓 → 拉今日价/基准 → 折美元 → 聚合成头条。
    price_fn / bench_fn 可注入便于测试。任何单仓失败只排除该仓、绝不整体崩。
    返回头条 dict（含 partial 排除清单），无数据返回 None。"""
    portfolio_path = portfolio_path or _DEFAULT_PF
    if today is None:
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        holdings = _load_positions(portfolio_path)
    except Exception:
        return None
    if not holdings:
        return None

    per_ticker, inception = _entry_dates(db_path)
    positions, partial = [], []
    for h in holdings:
        tk, mkt, sh, cb = h["ticker"], h["market"], h["shares"], h["cost_basis"]
        ccy = _CCY_USD.get(mkt, 1.0)
        if not cb or sh <= 0:
            partial.append({"ticker": tk, "reason": "无成本/股数"})
            continue
        px = price_fn(tk, mkt)
        if px is None:
            partial.append({"ticker": tk, "reason": "现价拉取失败"})
            continue
        entry = per_ticker.get(tk.upper()) or inception
        bench_ret = bench_fn(mkt, entry, today) if entry else None
        if bench_ret is None:
            partial.append({"ticker": tk, "reason": "基准对比缺失"})
            continue
        principal_usd = sh * cb * ccy
        current_usd = sh * px * ccy
        passive_usd = principal_usd * (1 + bench_ret / 100.0)
        positions.append({
            "ticker": tk, "market": mkt,
            "principal_usd": principal_usd,
            "current_value_usd": current_usd,
            "passive_value_usd": passive_usd,
        })

    agg = aggregate_cumulative(positions, as_of=today, basis="real")
    if agg is None:
        return None
    agg["partial"] = partial
    agg["inception"] = inception
    return agg
