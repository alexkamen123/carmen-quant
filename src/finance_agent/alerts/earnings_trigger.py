# src/finance_agent/alerts/earnings_trigger.py
"""
财报季事件驱动预警：每日检查持仓股的近期财报日期，
7 天内触发飞书预警卡片，供提前做深度分析。

港股财报日 yfinance 支持不稳定，暂只覆盖美股。
"""
import asyncio
import datetime
import statistics
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
import yfinance as yf

from finance_agent.data.yf_utils import _ticker_earnings_dates_with_retry, _extract_surprises
from finance_agent.db.tracker import save_sue_alert
from finance_agent.notifications.feishu import send_feishu_card
from finance_agent.signals.sue_factor import compute_sue, _sue_pead_cfg

EARNINGS_WINDOW_DAYS = 7  # 提前几天触发预警
SUE_LOOKBACK_TD = 3  # 近 N 个交易日内的财报判为"刚出炉"


def get_next_earnings(ticker: str) -> Optional[datetime.date]:
    """
    通过 yfinance 获取美股下次财报日期。
    calendar API 在不同版本返回格式不一，做兼容处理。
    """
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar

        if cal is None:
            return None

        # yfinance ≥0.2: calendar 是 dict，key 为 "Earnings Date"
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
        # 旧版：DataFrame，index 含 "Earnings Date"
        elif hasattr(cal, "loc"):
            try:
                dates = cal.loc["Earnings Date"]
            except KeyError:
                return None
        else:
            return None

        if not hasattr(dates, "__iter__"):
            dates = [dates]

        today = datetime.date.today()
        for d in dates:
            # 统一转为 date 对象
            if hasattr(d, "date"):          # Timestamp / datetime
                d = d.date()
            if isinstance(d, datetime.date) and d >= today:
                return d

        return None
    except Exception:
        return None


async def check_and_alert_earnings(push: bool = True) -> list[dict]:
    """
    扫描所有美股持仓，返回 7 天内有财报的标的列表。
    push=True 时推送飞书预警。
    """
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    holdings = [h for h in portfolio.get("holdings", []) if h.get("market", "us") == "us"]
    today = datetime.date.today()
    upcoming: list[dict] = []

    loop = asyncio.get_event_loop()
    for h in holdings:
        ticker = h["ticker"]
        # is_dca ETF 也扫一下，财报周期不重要但成分股财报可能影响
        earnings_date = await loop.run_in_executor(None, get_next_earnings, ticker)
        if earnings_date is None:
            continue

        days_until = (earnings_date - today).days
        if 0 <= days_until <= EARNINGS_WINDOW_DAYS:
            upcoming.append({
                "ticker": ticker,
                "earnings_date": earnings_date.isoformat(),
                "days_until": days_until,
                "sector": h.get("sector", ""),
                "shares": h.get("shares", 0),
                "cost_basis": h.get("cost_basis", 0),
                "notes": h.get("notes", ""),
            })
            print(f"[EarningsTrigger] {ticker} 财报在 {days_until} 天后 ({earnings_date})")

    if not upcoming:
        print("[EarningsTrigger] 未来 7 天内无持仓股财报")
        return []

    # 按紧迫程度排序
    upcoming.sort(key=lambda x: x["days_until"])

    if push:
        # 去重（06-12 UX 扫描 P0）：每个 (ticker, 财报日) 只在首次发现 + 财报前 1 天
        # 各提醒一次——原零去重逻辑会让同一财报连推 7 天（每天只差一个倒计时数字）
        from finance_agent.alerts.news_monitor import _load_alerted, _save_alerted
        today_str = today.isoformat()
        alerted = _load_alerted(today_str)   # 表是全量 key 查询的（key 含财报日，跨天有效）
        fresh = []
        for u in upcoming:
            key = f"earnings:{u['ticker']}:{u['earnings_date']}"
            key_eve = f"{key}:eve"
            if key not in alerted and not _key_exists(key):
                fresh.append(u)
                _save_alerted(key, today_str)
            elif u["days_until"] <= 1 and not _key_exists(key_eve):
                fresh.append(u)          # 财报前夜再提醒一次（有信息增量）
                _save_alerted(key_eve, today_str)
        if fresh:
            await _push_earnings_alert(fresh)
        else:
            print(f"[EarningsTrigger] {len(upcoming)} 只均已提醒过（首次+前夜各一次），不重复推送")

    return upcoming


def _key_exists(key: str) -> bool:
    """news_alerted 全表查 key（财报 key 跨天有效，不能只查当天分区）。"""
    from finance_agent.db.tracker import _conn, _resolve_db
    try:
        with _conn(_resolve_db(None)) as con:
            return con.execute(
                "SELECT 1 FROM news_alerted WHERE key = ? LIMIT 1", (key,)
            ).fetchone() is not None
    except Exception:
        return False   # 查不到去重表宁可多推不可漏推（财报是高价值提醒）


async def _push_earnings_alert(upcoming: list[dict]) -> None:
    """构建并推送财报预警飞书卡片"""
    elements: list[dict] = []

    for item in upcoming:
        days = item["days_until"]
        if days == 0:
            urgency, day_str = "🔴", "今天"
        elif days == 1:
            urgency, day_str = "🔴", "明天"
        elif days <= 3:
            urgency, day_str = "🟡", f"{days} 天后"
        else:
            urgency, day_str = "🟢", f"{days} 天后"

        content_lines = [
            f"{urgency} **{item['ticker']}**　{item['sector']}",
            f"　　财报日期：**{item['earnings_date']}**（{day_str}）",
            f"　　持仓：{item['shares']} 股 · 均价 ${item['cost_basis']}",
        ]
        if item["notes"]:
            content_lines.append(f"　　_{item['notes']}_")

        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(content_lines)},
        })
        elements.append({"tag": "hr"})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "财报前后波动通常较大，注意仓位管理。"
                                 "建议提前用 equity-research:earnings 做深度预分析。"}],
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"📅 卡门智投 · 财报预警（{len(upcoming)} 只）"},
            "template": "orange",
        },
        "elements": elements,
    }

    fallback = "财报预警：" + "；".join(
        f"{u['ticker']} {u['earnings_date']}（{u['days_until']}天后）" for u in upcoming)
    ok = await send_feishu_card(card, fallback_text=fallback)
    print(f"[EarningsTrigger] 飞书推送{'✅' if ok else '❌'}")


def _today_date():
    return datetime.date.today()


def _load_us_holdings():
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)
    return [h for h in portfolio.get("holdings", []) if h.get("market", "us") == "us"]


def _td_since(d, today):
    """d 到 today 的交易日跨度（bdate 近似，含跨周末）。d>today → 0。"""
    if d > today:
        return 0
    return max(0, len(pd.bdate_range(d, today)) - 1)


async def record_sue_events(db_path=None):
    """观测线：对每只美股持仓，若最近一次已发布财报落在近 SUE_LOOKBACK_TD 交易日内 → 算 SUE 落库。

    调用方须在 sue_factor_enabled() 为真时才调；off 时根本不调，earnings-check 逐字节不变。
    单票失败降级（try/except continue）；幂等靠 save_sue_alert 的 (ticker,earnings_date)。
    """
    cfg = _sue_pead_cfg()
    min_q = int(cfg.get("min_quarters", 8))
    today = _today_date()
    loop = asyncio.get_event_loop()
    recorded = []
    for h in _load_us_holdings():
        ticker = h["ticker"]
        try:
            df = await loop.run_in_executor(
                None, lambda t=ticker: _ticker_earnings_dates_with_retry(t, limit=20))
            surprises = _extract_surprises(df)
            if not surprises:
                continue
            last_date, last_rep, last_est = surprises[-1]
            if _td_since(last_date, today) > SUE_LOOKBACK_TD:
                continue
            hist = [rep - est for (_, rep, est) in surprises[:-1]]
            sue = compute_sue(last_rep, last_est, hist, min_quarters=min_q)
            if sue is None:
                continue
            sigma = statistics.stdev(hist) if len(hist) >= 2 else None
            save_sue_alert(ticker=ticker, market="us", earnings_date=last_date.isoformat(),
                           sue_score=sue, eps_reported=last_rep, eps_estimate=last_est,
                           surprise_std=sigma, db_path=db_path)
            recorded.append((ticker, sue))
        except Exception:
            continue
    return recorded
