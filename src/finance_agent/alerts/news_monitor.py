# src/finance_agent/alerts/news_monitor.py
"""
盘中新闻扫描：每小时检查持仓股票的最新新闻，
对"高影响"新闻（DeepSeek 评分 >= 7）立即推送飞书提醒。

轻量设计：
- 只用 yfinance 拉新闻（不跑完整 pipeline）
- 只用 DeepSeek 快速分类（便宜且快，无限速）
- 2小时内发布的新闻才处理（避免重复告警）
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
import yfinance as yf

from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.notifications.feishu import send_feishu_card

# ── 已推送记录（防重，内存级，每次 CI run 重置是可接受的）─────────
_alerted: set[str] = set()   # key = f"{ticker}:{news_url_or_title_hash}"

ALERT_SYSTEM = """你是一个股票新闻影响评估助手。
根据提供的新闻标题，评估该新闻对指定股票的影响。

严格按以下 JSON 格式输出（不要有其他内容）：
{
  "impact": 1-10,
  "sentiment": "利好" | "利空" | "中性",
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（财报超预期/暴雷、重大并购、监管处罚、CEO 离职）
5-7:  中等影响（行业政策变化、竞争对手动态、分析师调级）
1-4:  低影响（一般行业新闻、重申评级、常规发布会）"""

ALERT_USER = """股票：{ticker}
新闻标题：{title}
发布时间：{published}

请评估这条新闻对 {ticker} 股票的影响程度。"""


async def _classify_news(ticker: str, title: str, published: str) -> dict:
    """用 DeepSeek 快速评估新闻影响，失败时返回低影响默认值"""
    try:
        raw = await deepseek_chat(
            ALERT_SYSTEM,
            ALERT_USER.format(ticker=ticker, title=title, published=published),
        )
        start, end = raw.find("{"), raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"impact": 0, "sentiment": "中性", "reason": "解析失败"}


def _get_fresh_news(ticker: str, market: str, hours: int = 2) -> list[dict]:
    """
    从 yfinance 拉最新新闻，只返回 hours 小时内发布的条目。
    港股代码自动转换格式。
    """
    try:
        if market == "hk" and ticker.isdigit():
            yf_ticker = f"{int(ticker):04d}.HK"
        else:
            yf_ticker = ticker

        stock = yf.Ticker(yf_ticker)
        news_raw = getattr(stock, "news", None) or []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        fresh = []
        for item in news_raw:
            ts = item.get("providerPublishTime", 0)
            if not ts:
                continue
            pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            if pub_dt >= cutoff:
                fresh.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "published": pub_dt.strftime("%H:%M UTC"),
                    "key": f"{ticker}:{hash(item.get('title', ''))}"
                })
        return fresh
    except Exception:
        return []


async def _send_alert(ticker: str, market: str, title: str, published: str,
                      impact: int, sentiment: str, reason: str) -> None:
    """发送飞书紧急提醒卡片"""
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"⚡ 持仓快讯 · {ticker}（{market_label}）"},
            "template": "red" if sentiment == "利空" else (
                "green" if sentiment == "利好" else "blue"
            ),
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"{SENT_EMOJI.get(sentiment, '⚪')} **{sentiment}** {IMPACT_BAR}  影响度 {impact}/10\n"
                        f"**{title}**\n"
                        f"🕐 {published}"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"💡 {reason}"},
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text",
                              "content": "以上为 AI 快速判断，仅供参考，请自行核实原文"}],
            },
        ],
    }
    await send_feishu_card(card)
    print(f"[Alert] 已推送 {ticker} 快讯：{title[:40]}... (影响度={impact}, {sentiment})")


async def run_news_scan(impact_threshold: int = 7) -> int:
    """
    扫描所有持仓（及其竞争对手）的最新新闻，高影响立即推送飞书。
    peers 新闻以"持仓 X 的竞对 Y"形式标注，影响度阈值提高 1 分（减少噪音）。
    返回推送条数。
    """
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    holdings = portfolio.get("holdings", []) + portfolio.get("watchlist", [])
    # ETF 跳过（指数 ETF 一般没有个股突发新闻）
    etf_skip = {"QQQM", "VOO", "SCHD", "DRAM"}
    holdings = [h for h in holdings if h["ticker"] not in etf_skip]

    # 构建扫描队列：(scan_ticker, scan_market, holding_ticker, is_peer)
    scan_queue: list[tuple[str, str, str, bool]] = []
    seen_tickers: set[str] = set()
    for item in holdings:
        ticker = item["ticker"]
        market = item["market"]
        if ticker not in seen_tickers:
            scan_queue.append((ticker, market, ticker, False))
            seen_tickers.add(ticker)
        for peer in item.get("peers", []):
            if peer not in seen_tickers:
                scan_queue.append((peer, market, ticker, True))
                seen_tickers.add(peer)

    pushed = 0
    for scan_ticker, market, holding_ticker, is_peer in scan_queue:
        fresh_news = _get_fresh_news(scan_ticker, market, hours=2)
        if not fresh_news:
            continue

        # peers 新闻阈值 +1，减少间接噪音
        effective_threshold = impact_threshold + (1 if is_peer else 0)

        for news in fresh_news:
            key = news["key"]
            if key in _alerted:
                continue

            result = await _classify_news(scan_ticker, news["title"], news["published"])
            impact = result.get("impact", 0)

            if impact >= effective_threshold:
                # peers 新闻标注来源，附注影响持仓
                title_display = news["title"]
                if is_peer:
                    title_display = f"[竞对 {scan_ticker}] {title_display}"
                await _send_alert(
                    ticker=holding_ticker if is_peer else scan_ticker,
                    market=market,
                    title=title_display,
                    published=news["published"],
                    impact=impact,
                    sentiment=result.get("sentiment", "中性"),
                    reason=result.get("reason", "")
                    + (f"（竞对 {scan_ticker} 动态，间接影响 {holding_ticker}）" if is_peer else ""),
                )
                _alerted.add(key)
                pushed += 1
            else:
                src = f"{scan_ticker}（竞对）" if is_peer else scan_ticker
                print(f"[Alert] {src} 影响度={impact}，低于阈值，跳过：{news['title'][:50]}")

    if pushed == 0:
        print(f"[Alert] 本次扫描无高影响新闻（阈值={impact_threshold}）")
    return pushed
