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
