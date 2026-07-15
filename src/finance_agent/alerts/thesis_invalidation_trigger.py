"""P2c 失效扫描器：挂已有 earnings/price/news 车，命中声明的失效条件→落库+飞书告警。

受 thesis_invalidation_enabled() 门控（off→调用方不调，扫描器逐字节不变）。
单票 try/except continue 降级；去重复用 news_alerted。纯提醒·不改建议/仓位。
"""
import asyncio
import datetime

from finance_agent.data.yfinance_provider import fetch_quarterly_rev_margin
from finance_agent.db.tracker import (get_sue_alerts, load_thesis_triggers,
                                      save_invalidation_event)
from finance_agent.signals.thesis_invalidation import (_ti_cfg, check_price_break,
                                                       format_invalidation_alert, match_triggers)
from finance_agent.alerts.earnings_trigger import _load_us_holdings


def _today_str():
    return datetime.date.today().isoformat()


def _latest_sue(ticker, db_path):
    rows = get_sue_alerts(ticker, db_path=db_path)
    return rows[0]["sue_score"] if rows else None


def _fetch_rev_margin(ticker):
    return fetch_quarterly_rev_margin(ticker)


def _latest_close(ticker):
    """取最新收盘价·失败 None。仿现成取数（无既有函数可复用·用 yfinance fast_info）。"""
    try:
        import yfinance as yf
        px = yf.Ticker(ticker).fast_info.get("last_price")
        return float(px) if px is not None else None
    except Exception:
        return None


def _detail_for(pillar, measured):
    tt = pillar.get("trigger_type")
    if tt == "earnings_miss":
        return f"财报大幅低于预期（SUE={measured.get('earnings_miss'):+.1f}σ）"
    if tt == "revenue_decline":
        ys = measured.get("revenue_decline") or []
        s = "、".join(f"{y:+.1f}%" for y in ys[-2:])
        return f"营收连续两季同比转负（{s}）"
    if tt == "margin_break":
        return f"毛利率跌破 {pillar.get('threshold')}%（当前 {measured.get('margin_break'):.1f}%）"
    if tt == "price_break":
        return f"最新价(盘中)跌破止损价 {pillar.get('threshold')}"
    if tt == "news_negative":
        return "出现重大利空新闻"
    return "触发失效条件"


async def _push_invalidation(ticker, pillar, trigger_type, detail):
    """飞书告警（复用 news_monitor 卡片）+ 去重。失败不抛。"""
    from finance_agent.alerts.news_monitor import _send_stock_alert, _save_alerted, _load_alerted
    key = f"invalidation:{ticker}:{trigger_type}:{_today_str()}"
    if key in _load_alerted(_today_str()):
        return
    text = format_invalidation_alert(ticker, pillar, trigger_type, detail)
    try:
        await _send_stock_alert(ticker, "us", "论点失效提醒", _today_str(),
                                impact=8, sentiment="利空", reason=text)
        _save_alerted(key, _today_str())
    except Exception:
        pass


async def _record_and_alert(ticker, hits, measured, db_path, push=True):
    for p in hits:
        detail = _detail_for(p, measured)
        save_invalidation_event(ticker, "us", p["trigger_type"], p.get("pillar", ""),
                                _today_str(), detail, db_path=db_path)
        if push:
            await _push_invalidation(ticker, p.get("pillar", ""), p["trigger_type"], detail)


async def scan_earnings_invalidation(db_path=None, push=True):
    """命中 earnings_miss / revenue_decline / margin_break → 落库+告警。挂 earnings-check。"""
    cfg = _ti_cfg()
    loop = asyncio.get_event_loop()
    try:
        holdings = _load_us_holdings()
    except Exception:
        return
    for h in holdings:
        ticker = h["ticker"]
        try:
            trig = load_thesis_triggers(ticker, db_path=db_path)
            if not trig:
                continue
            types = {p.get("trigger_type") for p in trig["pillars"]}
            measured = {}
            if "earnings_miss" in types:
                sue = _latest_sue(ticker, db_path)
                if sue is not None:
                    measured["earnings_miss"] = sue
            if {"revenue_decline", "margin_break"} & types:
                rm = await loop.run_in_executor(None, lambda t=ticker: _fetch_rev_margin(t))
                if rm:
                    if "revenue_decline" in types:
                        measured["revenue_decline"] = rm.get("yoy_growths", [])
                    if "margin_break" in types and rm.get("gross_margin") is not None:
                        measured["margin_break"] = rm["gross_margin"]
            hits = match_triggers(trig["pillars"], measured,
                                  sigma=float(cfg["sigma"]), impact_min=int(cfg["impact_min"]))
            await _record_and_alert(ticker, hits, measured, db_path, push=push)
        except Exception:
            continue


async def scan_price_invalidation(db_path=None, push=True):
    """命中 price_break（收盘跌破声明止损价）→ 落库+告警。挂 price-scan。"""
    loop = asyncio.get_event_loop()
    try:
        holdings = _load_us_holdings()
    except Exception:
        return
    for h in holdings:
        ticker = h["ticker"]
        try:
            trig = load_thesis_triggers(ticker, db_path=db_path)
            if not trig:
                continue
            pbs = [p for p in trig["pillars"] if p.get("trigger_type") == "price_break"]
            if not pbs:
                continue
            close = await loop.run_in_executor(None, lambda t=ticker: _latest_close(t))
            hits = [p for p in pbs if check_price_break(close, p.get("threshold"))]
            await _record_and_alert(ticker, hits, {"price_break": close}, db_path, push=push)
        except Exception:
            continue


async def scan_news_invalidation(ticker, impact, sentiment, db_path=None, push=True):
    """news-scan 分类出一条新闻时回调：命中该票声明的 news_negative → 落库+告警。"""
    cfg = _ti_cfg()
    try:
        trig = load_thesis_triggers(ticker, db_path=db_path)
        if not trig:
            return
        measured = {"news_negative": (impact, sentiment)}
        hits = match_triggers(trig["pillars"], measured,
                              sigma=float(cfg["sigma"]), impact_min=int(cfg["impact_min"]))
        await _record_and_alert(ticker, hits, measured, db_path, push=push)
    except Exception:
        return
