# src/finance_agent/alerts/news_monitor.py
"""
盘中新闻扫描：每 2 小时检查持仓股票的最新新闻，
对"高影响"新闻（DeepSeek 评分 >= 7）立即推送飞书提醒。

新闻源：
- 美股个股：yfinance（Yahoo Finance）
- 美股宏观：CNBC RSS（Markets + Tech，分钟级实时，需 User-Agent header）
- 港股/A 股：AkShare 东方财富（中文新闻，覆盖更全）
- 港股宏观：AkShare 全球财经快讯（东方财富 np-weblist），需 NO_PROXY 绕过代理
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import feedparser
import httpx
import yaml
import yfinance as yf

from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.notifications.feishu import send_feishu_card
from finance_agent.db.tracker import _resolve_db, _conn, init_db

# ── 东方财富域名需直连，不走本地代理 ─────────────────────────────────────
_EASTMONEY_HOSTS = "eastmoney.com,push2his.eastmoney.com,datacenter-web.eastmoney.com,np-weblist.eastmoney.com"
_current_no_proxy = os.environ.get("NO_PROXY", "")
if "eastmoney.com" not in _current_no_proxy:
    os.environ["NO_PROXY"] = f"{_current_no_proxy},{_EASTMONEY_HOSTS}".strip(",")

# ── 北京时区 ──────────────────────────────────────────────────────────────
_BJT = timezone(timedelta(hours=8))


_CREATE_ALERTED_SQL = """
CREATE TABLE IF NOT EXISTS news_alerted (
    key   TEXT NOT NULL,
    date  TEXT NOT NULL,
    PRIMARY KEY (key, date)
);
CREATE INDEX IF NOT EXISTS idx_alerted_date ON news_alerted(date);
"""


def _load_alerted(date_str: str, db_path=None) -> set[str]:
    p = _resolve_db(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _conn(p) as con:
        con.executescript(_CREATE_ALERTED_SQL)
        rows = con.execute(
            "SELECT key FROM news_alerted WHERE date = ?", (date_str,)
        ).fetchall()
    return {r["key"] for r in rows}


def _save_alerted(key: str, date_str: str, db_path=None) -> None:
    p = _resolve_db(db_path)
    with _conn(p) as con:
        con.execute(
            "INSERT OR IGNORE INTO news_alerted (key, date) VALUES (?, ?)",
            (key, date_str),
        )


# ─────────────────────────────────────────────────────────────────────────────
# 新闻拉取
# ─────────────────────────────────────────────────────────────────────────────

def _get_fresh_news_us(ticker: str, hours: int = 2) -> list[dict]:
    """美股：yfinance Yahoo Finance 新闻"""
    try:
        stock = yf.Ticker(ticker)
        news_raw = stock.news or []
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
                    "published": pub_dt.astimezone(_BJT).strftime("%m-%d %H:%M"),
                    "key": f"{ticker}:{hash(item.get('title', ''))}",
                })
        return fresh
    except Exception as e:
        print(f"[Alert] yfinance 新闻拉取失败 {ticker}: {e}")
        return []


def _get_fresh_news_hk_cn(ticker: str, hours: int = 2) -> list[dict]:
    """港股/A股：AkShare 东方财富个股新闻（中文，覆盖更全）"""
    try:
        df = ak.stock_news_em(symbol=ticker)
        if df.empty:
            return []
        cutoff_bjt = datetime.now(_BJT) - timedelta(hours=hours)
        fresh = []
        for _, row in df.iterrows():
            try:
                pub_str = str(row.get("发布时间", ""))
                if not pub_str or pub_str == "nan":
                    continue
                pub_dt = datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BJT)
                if pub_dt >= cutoff_bjt:
                    title = str(row.get("新闻标题", ""))
                    fresh.append({
                        "title": title,
                        "url": str(row.get("新闻链接", "")),
                        "published": pub_dt.strftime("%m-%d %H:%M"),
                        "key": f"{ticker}:{hash(title)}",
                    })
            except Exception:
                continue
        return fresh
    except Exception as e:
        print(f"[Alert] AkShare 新闻拉取失败 {ticker}: {e}")
        return []


def _get_fresh_news(ticker: str, market: str, hours: int = 2) -> list[dict]:
    if market in ("hk", "cn"):
        return _get_fresh_news_hk_cn(ticker, hours)
    return _get_fresh_news_us(ticker, hours)


def _get_macro_news(hours: int = 2) -> list[dict]:
    """
    全球财经快讯（东方财富）。
    包含美联储、贸易政策、地缘政治等宏观事件，
    是补捉"特朗普访华"类宏观消息的主要来源。
    """
    try:
        df = ak.stock_info_global_em()
        if df.empty:
            return []

        # 探测列名（东方财富接口偶尔调整列名）
        col_time = next((c for c in df.columns if "时间" in c or "date" in c.lower()), None)
        col_title = next((c for c in df.columns if "标题" in c or "title" in c.lower()), None)
        col_summary = next((c for c in df.columns if "摘要" in c or "content" in c.lower() or "内容" in c), None)
        if not col_title:
            print(f"[Alert] 全球快讯列名未识别: {df.columns.tolist()}")
            return []

        cutoff_bjt = datetime.now(_BJT) - timedelta(hours=hours)
        fresh = []
        for _, row in df.iterrows():
            try:
                title = str(row.get(col_title, ""))
                if not title or title == "nan":
                    continue
                time_str = str(row.get(col_time, "")) if col_time else ""
                pub_dt = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M"):
                    try:
                        parsed = datetime.strptime(time_str, fmt)
                        if fmt == "%H:%M":
                            parsed = parsed.replace(
                                year=datetime.now().year,
                                month=datetime.now().month,
                                day=datetime.now().day,
                            )
                        pub_dt = parsed.replace(tzinfo=_BJT)
                        break
                    except ValueError:
                        continue

                if pub_dt is None or pub_dt < cutoff_bjt:
                    continue

                summary = str(row.get(col_summary, "")) if col_summary else ""
                fresh.append({
                    "title": title,
                    "summary": summary[:100],
                    "published": pub_dt.strftime("%m-%d %H:%M"),
                    "key": f"macro:{hash(title)}",
                })
            except Exception:
                continue
        return fresh
    except Exception as e:
        print(f"[Alert] 全球财经快讯拉取失败: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 今日推荐缓存（用于信号冲突检测）
# ─────────────────────────────────────────────────────────────────────────────

def _load_today_recs() -> dict[str, str]:
    """从 SQLite 加载今日 PM 裁决，返回 {ticker: recommendation}"""
    try:
        db_path = _resolve_db(None)
        init_db(db_path)
        today = datetime.now(_BJT).strftime("%Y-%m-%d")
        with _conn(db_path) as con:
            rows = con.execute(
                "SELECT ticker, recommendation FROM recommendations WHERE date = ?", (today,)
            ).fetchall()
        return {r["ticker"]: r["recommendation"] for r in rows}
    except Exception as e:
        print(f"[Alert] 加载今日推荐失败（信号冲突检测跳过）: {e}")
        return {}


_BEARISH_RECS = {"减仓", "卖出"}
_BULLISH_RECS = {"买入"}


def _conflict_label(ticker: str, sentiment: str, today_recs: dict[str, str]) -> str:
    """
    返回冲突标签字符串，无冲突时返回空字符串。
    利好新闻 + 今日减仓/卖出 → "⚠️ 信号冲突"
    利空新闻 + 今日买入     → "⚠️ 信号冲突"
    """
    rec = today_recs.get(ticker, "")
    if not rec:
        return ""
    if sentiment == "利好" and rec in _BEARISH_RECS:
        return f"⚠️ **信号冲突**：今日裁决「{rec}」，但出现利好消息，建议重新评估后再执行"
    if sentiment == "利空" and rec in _BULLISH_RECS:
        return f"⚠️ **信号冲突**：今日裁决「{rec}」，但出现利空消息，注意止损"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# DeepSeek 评分
# ─────────────────────────────────────────────────────────────────────────────

ALERT_SYSTEM = """你是一个股票新闻影响评估助手。
根据提供的新闻标题，评估该新闻对指定股票的影响。

严格按以下 JSON 格式输出（不要有其他内容）：
{
  "impact": 1-10,
  "sentiment": "利好" | "利空" | "中性",
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（财报超预期/暴雷、重大并购、监管处罚、CEO 离职、重大贸易/外交政策）
5-7:  中等影响（行业政策变化、竞争对手动态、分析师调级）
1-4:  低影响（一般行业新闻、重申评级、常规发布会）"""

ALERT_USER = """股票/资产：{ticker}
新闻标题：{title}
发布时间：{published}
{summary_line}
请评估这条新闻对 {ticker} 的影响程度。"""

MACRO_SYSTEM = """你是一个宏观经济新闻影响评估助手。
根据提供的宏观新闻，评估该新闻对中国港股/A股市场的整体影响。

严格按以下 JSON 格式输出（不要有其他内容）：
{
  "impact": 1-10,
  "sentiment": "利好" | "利空" | "中性",
  "affected_sectors": ["科技", "消费", "金融"],
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（中美关系重大转变、联储加息/降息、贸易战升级/缓和、重大外交事件）
5-7:  中等影响（PMI 超预期、行业政策、外资流入流出）
1-4:  低影响（常规数据发布、无实质影响的声明）"""

MACRO_USER = """宏观新闻标题：{title}
发布时间：{published}
摘要：{summary}

请评估这条宏观新闻对中国港股市场的影响。"""

US_MACRO_SYSTEM = """你是一个美股市场宏观新闻影响评估助手。
根据提供的新闻标题，评估对美股市场的整体影响。

严格按以下 JSON 格式输出（不要有其他内容）：
{
  "impact": 1-10,
  "sentiment": "利好" | "利空" | "中性",
  "affected_sectors": ["半导体/AI算力", "互联网/AI", "宽基ETF"],
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（Fed 加息/降息决定、CPI 大幅超预期、标普/纳指单日跌超 2%、芯片出口管制）
5-7:  中等影响（经济数据小幅超预期、分析师集体调级、板块轮动）
1-4:  低影响（个股财报、常规声明、重申评级）"""

US_MACRO_USER = """美股宏观新闻标题：{title}
发布时间：{published}

请评估这条宏观新闻对美股市场（尤其是科技/半导体/AI板块）的影响。"""


async def _classify_stock_news(ticker: str, title: str, published: str, summary: str = "") -> dict:
    summary_line = f"摘要：{summary}" if summary else ""
    try:
        raw = await deepseek_chat(
            ALERT_SYSTEM,
            ALERT_USER.format(ticker=ticker, title=title, published=published,
                              summary_line=summary_line),
        )
        start, end = raw.find("{"), raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"impact": 0, "sentiment": "中性", "reason": "解析失败"}


async def _classify_macro_news(title: str, published: str, summary: str) -> dict:
    try:
        raw = await deepseek_chat(
            MACRO_SYSTEM,
            MACRO_USER.format(title=title, published=published, summary=summary),
        )
        start, end = raw.find("{"), raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"impact": 0, "sentiment": "中性", "affected_sectors": [], "reason": "解析失败"}


async def _classify_us_macro_news(title: str, published: str) -> dict:
    try:
        raw = await deepseek_chat(
            US_MACRO_SYSTEM,
            US_MACRO_USER.format(title=title, published=published),
        )
        start, end = raw.find("{"), raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception:
        return {"impact": 0, "sentiment": "中性", "affected_sectors": [], "reason": "解析失败"}


# ─────────────────────────────────────────────────────────────────────────────
# 飞书推送
# ─────────────────────────────────────────────────────────────────────────────

async def _send_stock_alert(ticker: str, market: str, title: str, published: str,
                            impact: int, sentiment: str, reason: str,
                            conflict: str = "") -> None:
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)

    # 有信号冲突时 header 固定用橙色，标题加标记
    has_conflict = bool(conflict)
    header_title = f"{'⚠️ 信号冲突 · ' if has_conflict else '⚡ 持仓快讯 · '}{ticker}（{market_label}）"
    header_color = "orange" if has_conflict else (
        "red" if sentiment == "利空" else ("green" if sentiment == "利好" else "blue")
    )

    elements = [
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
        {"tag": "div", "text": {"tag": "lark_md", "content": f"💡 {reason}"}},
    ]
    if conflict:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": conflict}},
        ]
    elements += [
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text",
                                      "content": "以上为 AI 快速判断，仅供参考，请自行核实原文"}]},
    ]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": header_color,
        },
        "elements": elements,
    }
    await send_feishu_card(card)
    conflict_tag = " [冲突!]" if has_conflict else ""
    print(f"[Alert] 已推送 {ticker} 快讯{conflict_tag}：{title[:40]}... (影响度={impact}, {sentiment})")


async def _send_macro_alert(title: str, published: str, impact: int, sentiment: str,
                            affected_sectors: list, reason: str) -> None:
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    sectors_str = "、".join(affected_sectors) if affected_sectors else "全市场"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🌐 宏观快讯 · 港股/A股影响"},
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
                        f"🕐 {published}   📌 涉及板块：{sectors_str}"
                    ),
                },
            },
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"💡 {reason}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "宏观快讯 · AI 快速判断，仅供参考"}]},
        ],
    }
    await send_feishu_card(card)
    print(f"[Alert] 已推送宏观快讯：{title[:40]}... (影响度={impact}, {sentiment})")


_CNBC_RSS_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # Markets
    "https://www.cnbc.com/id/19854910/device/rss/rss.html",   # Technology
]
_RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


def _get_us_macro_news(hours: int = 2) -> list[dict]:
    """
    从 CNBC RSS（Markets + Technology）拉取美股宏观实时新闻。
    分钟级更新，覆盖 Fed/CPI/贸易政策/科技股动态。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen: set[str] = set()
    fresh: list[dict] = []
    for url in _CNBC_RSS_FEEDS:
        try:
            resp = httpx.get(url, headers=_RSS_HEADERS, timeout=10, follow_redirects=True)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if not pub:
                    continue
                pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                key = f"us_macro:{hash(title)}"
                if pub_dt >= cutoff and key not in seen:
                    seen.add(key)
                    fresh.append({
                        "title": title,
                        "published": pub_dt.astimezone(_BJT).strftime("%m-%d %H:%M"),
                        "key": key,
                    })
        except Exception as e:
            print(f"[Alert] CNBC RSS 拉取失败 {url}: {e}")
    return fresh


async def _send_us_macro_alert(title: str, published: str, impact: int, sentiment: str,
                               affected_sectors: list, reason: str) -> None:
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    sectors_str = "、".join(affected_sectors) if affected_sectors else "科技/半导体"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📈 美股宏观 · 科技/半导体影响"},
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
                        f"🕐 {published}   📌 涉及板块：{sectors_str}"
                    ),
                },
            },
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"💡 {reason}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "美股宏观快讯 · AI 快速判断，仅供参考"}]},
        ],
    }
    await send_feishu_card(card)
    print(f"[Alert] 已推送美股宏观快讯：{title[:40]}... (影响度={impact}, {sentiment})")


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

async def run_news_scan(impact_threshold: int = 7) -> int:
    """
    扫描所有持仓（及竞争对手）+ 全球宏观快讯，高影响推送飞书。
    返回推送条数。
    """
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    holdings = portfolio.get("holdings", []) + portfolio.get("watchlist", [])
    etf_skip = {"QQQM", "VOO", "SCHD", "DRAM"}
    holdings = [h for h in holdings if h["ticker"] not in etf_skip]

    # 构建扫描队列：(scan_ticker, scan_market, holding_ticker, is_peer)
    scan_queue: list[tuple[str, str, str, bool]] = []
    seen_tickers: set[str] = set()
    for item in holdings:
        ticker, market = item["ticker"], item["market"]
        if ticker not in seen_tickers:
            scan_queue.append((ticker, market, ticker, False))
            seen_tickers.add(ticker)
        for peer in item.get("peers", []):
            if peer not in seen_tickers:
                scan_queue.append((peer, market, ticker, True))
                seen_tickers.add(peer)

    today_str = datetime.now(_BJT).strftime("%Y-%m-%d")
    alerted = _load_alerted(today_str)
    pushed = 0
    today_recs = _load_today_recs()
    if today_recs:
        print(f"[Alert] 加载今日推荐（信号冲突检测）：{today_recs}")

    # ── 1. 个股扫描 ──────────────────────────────────────────────────────────
    for scan_ticker, market, holding_ticker, is_peer in scan_queue:
        fresh_news = _get_fresh_news(scan_ticker, market, hours=2)
        if not fresh_news:
            continue

        effective_threshold = impact_threshold + (1 if is_peer else 0)

        for news in fresh_news:
            key = news["key"]
            if key in alerted:
                continue

            result = await _classify_stock_news(
                scan_ticker, news["title"], news["published"],
                news.get("summary", "")
            )
            impact = result.get("impact", 0)
            sentiment = result.get("sentiment", "中性")

            if impact >= effective_threshold:
                title_display = news["title"]
                if is_peer:
                    title_display = f"[竞对 {scan_ticker}] {title_display}"
                # 仅对直接持仓做信号冲突检测（竞对新闻不冲突）
                conflict = "" if is_peer else _conflict_label(
                    holding_ticker, sentiment, today_recs
                )
                await _send_stock_alert(
                    ticker=holding_ticker if is_peer else scan_ticker,
                    market=market,
                    title=title_display,
                    published=news["published"],
                    impact=impact,
                    sentiment=sentiment,
                    reason=result.get("reason", "")
                    + (f"（竞对 {scan_ticker} 动态，间接影响 {holding_ticker}）" if is_peer else ""),
                    conflict=conflict,
                )
                alerted.add(key)
                _save_alerted(key, today_str)
                pushed += 1
            else:
                src = f"{scan_ticker}（竞对）" if is_peer else scan_ticker
                print(f"[Alert] {src} 影响度={impact}，低于阈值，跳过：{news['title'][:50]}")

    # ── 2. 港股/A股宏观快讯（东方财富全球快讯，中文，阈值7）────────────────
    has_hk_cn = any(h["market"] in ("hk", "cn") for h in holdings)
    if has_hk_cn:
        macro_news = _get_macro_news(hours=2)
        print(f"[Alert] 全球快讯（港股视角）：{len(macro_news)} 条（2小时内）")
        for news in macro_news:
            key = news["key"]
            if key in alerted:
                continue
            result = await _classify_macro_news(
                news["title"], news["published"], news.get("summary", "")
            )
            impact = result.get("impact", 0)
            if impact >= impact_threshold:
                await _send_macro_alert(
                    title=news["title"], published=news["published"], impact=impact,
                    sentiment=result.get("sentiment", "中性"),
                    affected_sectors=result.get("affected_sectors", []),
                    reason=result.get("reason", ""),
                )
                alerted.add(key)
                _save_alerted(key, today_str)
                pushed += 1
            else:
                print(f"[Alert] 港股宏观 影响度={impact}，跳过：{news['title'][:50]}")

    # ── 3. 美股宏观快讯（CNBC RSS，分钟级实时，阈值6）────────────────────────
    has_us = any(h["market"] == "us" for h in holdings)
    if has_us:
        us_macro_news = _get_us_macro_news(hours=2)
        print(f"[Alert] 美股快讯（CNBC RSS）：{len(us_macro_news)} 条（2小时内）")
        for news in us_macro_news:
            key = news["key"]
            if key in alerted:
                continue
            result = await _classify_us_macro_news(news["title"], news["published"])
            impact = result.get("impact", 0)
            if impact >= 6:
                await _send_us_macro_alert(
                    title=news["title"], published=news["published"], impact=impact,
                    sentiment=result.get("sentiment", "中性"),
                    affected_sectors=result.get("affected_sectors", []),
                    reason=result.get("reason", ""),
                )
                alerted.add(key)
                _save_alerted(key, today_str)
                pushed += 1
            else:
                print(f"[Alert] 美股宏观 影响度={impact}，跳过：{news['title'][:50]}")

    if pushed == 0:
        print(f"[Alert] 本次扫描无高影响新闻（阈值={impact_threshold}）")
    return pushed
