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
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 新闻去重用停用词（仅 3+ 字符词，配合 re.findall r'\b[a-z]{3,}\b' 使用）
_NEWS_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "all", "can", "had",
    "has", "him", "his", "how", "its", "may", "new", "now", "old",
    "see", "two", "way", "who", "did", "let", "put", "say", "she",
    "too", "use", "amid", "also", "from", "been", "will", "with",
    "that", "this", "have", "more", "over", "than", "they", "what",
    "when", "your", "were", "into", "just", "said", "each", "which",
    "could", "would", "there", "their", "about", "after", "before",
    "stock", "stocks", "share", "shares", "market", "markets",
})

import akshare as ak
import feedparser
import httpx
import pandas as pd
import yaml
import yfinance as yf

from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.notifications.feishu import send_feishu_card
from finance_agent.db.tracker import _resolve_db, _conn, init_db, save_dip_alert, backfill_dip_outcomes

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
# 新闻去重策略
# ─────────────────────────────────────────────────────────────────────────────

def _extract_dedup_seed(title: str) -> str:
    """
    从标题中提取关键要素用于去重。
    策略：优先实体+事件，无具体事件时用内容词补充区分度。

    例：
      "Cerebras pops 68% in Nasdaq debut"       → "cerebras:ipo"
      "Cerebras almost doubles in Nasdaq debut"  → "cerebras:ipo"   ← 正确去重
      "Apple surges 5% on AI news"              → "apple:price_move"
      "Apple soars on positive AI sentiment"    → "apple:price_move" ← 正确去重
      "Apple faces antitrust scrutiny"          → "apple:regulatory" ← 不与上面去重
    """
    title_lower = title.lower()

    # 提取首个大写词组（通常是主要实体）
    first_entity = ""
    match = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', title)
    if match:
        first_entity = match.group(1).lower()

    # 扩展金融事件分类（有序，优先更具体的事件）
    events = {
        'ipo':         r'\b(ipo|goes\s+public|debuts?|listing|went\s+public)\b',
        'earnings':    r'\b(earnings?|earnings?\s+(?:miss|beat|report)|quarterly\s+results?|guidance|eps)\b',
        'acquisition': r'\b(acqui(?:res?|sition|ring)|buys?\s+\w|buyout|takeover)\b',
        'merger':      r'\b(mergers?|merges?|merged)\b',
        'bankruptcy':  r'\b(bankruptcy|bankrupt|chapter\s+11|insolvenc)\b',
        'dividend':    r'\b(dividend|payout|distribution)\b',
        'split':       r'\b(stock\s+split|splits?|reverse\s+split)\b',
        'restructure': r'\b(restructur|reorganiz|layoffs?|job\s+cuts?)\b',
        'regulatory':  r'\b(antitrust|regulator|doj|ftc|probe|investigation|fine|penalty|lawsuit|sues?|sued)\b',
        'upgrade':     r'\b(upgrades?|downgrades?|price\s+target|outperform|underperform)\b',
        'product':     r'\b(launches?|unveils?|announces?|reveals?|introduces?|partnership)\b',
        'price_move':  r'\b(surges?|soars?|rally|rallies|plunges?|tumbles?|slides?|climbs?|jumps?|spikes?|pops?|sinks?|slumps?|rises?)\b',
    }

    event_found = ""
    for event_key, pattern in events.items():
        if re.search(pattern, title_lower):
            event_found = event_key
            break

    # 提取内容词：去停用词，排序，用于在无具体事件时补充区分度
    entity_parts = set(first_entity.split()) if first_entity else set()
    words = re.findall(r'\b[a-z]{3,}\b', title_lower)
    content_words = sorted(
        w for w in set(words)
        if w not in _NEWS_STOPWORDS and w not in entity_parts
    )[:4]

    if first_entity and event_found:
        # 最佳：实体 + 具体事件类型
        return f"{first_entity}:{event_found}"
    elif first_entity and content_words:
        # 有实体无具体事件：追加前 2 个内容词提高区分度
        # 避免 "Apple surges"（price_move）与 "Apple launches"（product）被错误去重
        return f"{first_entity}:{'_'.join(content_words[:2])}"
    elif first_entity:
        return first_entity
    elif event_found and content_words:
        # 无实体（全小写标题）+ 有事件：事件 + 内容词
        return f"{event_found}:{'_'.join(content_words[:2])}"
    elif content_words:
        # 无实体无事件：内容词排序集合（比 title[:20] 更稳定，不受截断位置影响）
        return "_".join(content_words)
    else:
        # 最终兜底：全标题 SHA256（稳定，无截断碰撞风险）
        return hashlib.sha256(title_lower.encode()).hexdigest()[:16]


def _make_dedup_key(prefix: str, title: str, published_date: str = "") -> str:
    """
    生成去重 key。
    使用提取的关键要素 + 发布日期，而非原始标题的 hash。
    这样相同事件（IPO、earnings 等）的不同报道会得到相同 key。

    注意：必须用 hashlib 而非内置 hash()，因为 Python 3.3+ 的 hash() 每次进程启动
    都会随机化（PYTHONHASHSEED），跨进程 key 不一致会导致 SQLite 去重失效。
    """
    seed = _extract_dedup_seed(title)
    seed_hash = hashlib.sha256(seed.encode()).hexdigest()[:12]

    # 包含发布日期（月-日），确保同一天的相同事件被认为是重复
    # published_date 格式为 "%m-%d %H:%M"，[:5] 取日期部分 "MM-DD"
    date_part = published_date[:5] if published_date and '-' in published_date else ""

    if date_part:
        return f"{prefix}:{date_part}:{seed_hash}"
    return f"{prefix}:{seed_hash}"


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
                published = pub_dt.astimezone(_BJT).strftime("%m-%d %H:%M")
                title = item.get("title", "")
                fresh.append({
                    "title": title,
                    "url": item.get("link", ""),
                    "published": published,
                    "key": _make_dedup_key(ticker, title, published),
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
        if "新闻标题" not in df.columns:
            print(f"[Alert] {ticker} 新闻格式异常（可能是ETF），列名: {list(df.columns)[:4]}，跳过")
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
                    published = pub_dt.strftime("%m-%d %H:%M")
                    fresh.append({
                        "title": title,
                        "url": str(row.get("新闻链接", "")),
                        "published": published,
                        "key": _make_dedup_key(ticker, title, published),
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
                published = pub_dt.strftime("%m-%d %H:%M")
                fresh.append({
                    "title": title,
                    "summary": summary[:100],
                    "published": published,
                    "key": _make_dedup_key("macro", title, published),
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
                published = pub_dt.astimezone(_BJT).strftime("%m-%d %H:%M")
                key = _make_dedup_key("us_macro", title, published)
                if pub_dt >= cutoff and key not in seen:
                    seen.add(key)
                    fresh.append({
                        "title": title,
                        "published": published,
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
# 暴跌机会分析
# ─────────────────────────────────────────────────────────────────────────────

DIP_SYSTEM = """你是一位家庭投资顾问，专门识别「长期逻辑不变、短期非理性下跌」的买入机会。

严格按以下 JSON 格式输出（不要其他内容）：
{
  "thesis_intact": true | false,
  "drop_reason": "一句话判断：情绪恐慌/板块联动/还是基本面恶化",
  "opportunity": "高" | "中" | "低" | "无",
  "entry_plan": [
    {"tranche": 1, "condition": "第一批什么时候买（用具体价格锚定，如'布林下轨87.2附近'或'当前价稳住'）", "size": "轻仓，总计划仓位的30%"},
    {"tranche": 2, "condition": "第二批条件（再跌1-2个ATR时加仓，给出具体价格）", "size": "加仓，再加30%"},
    {"tranche": 3, "condition": "第三批条件（越后面越轻）", "size": "收尾，最多20%"}
  ],
  "stop_loss": "具体止损价格（建议用2×ATR计算，如'跌破82.6即止损'）",
  "one_line": "一句大白话告诉普通投资者现在该怎么做"
}

分析原则：
- 大盘情绪/板块联动导致的下跌 + 持仓逻辑未破坏 → opportunity=高/中
- 个股基本面利空（业绩暴雷/监管/竞争恶化）→ opportunity=低/无
- 分批入场越往后仓位越轻，不是越买越重
- entry_plan 的 condition 必须包含具体价格，优先用布林下轨和ATR锚定
- stop_loss 必须是具体价格（用 2×ATR 计算），不能只写跌幅百分比"""

DIP_USER = """股票：{ticker}（{market}市场）
1小时跌幅：{drop_pct}%（从 {price_1h_ago} 跌到 {price_now}）

【板块联动判断】
{sector_note}

【技术指标（日线）】
{tech_summary}

【持仓逻辑】
{thesis}

【近期新闻摘要】
{news_summary}

请结合布林下轨和ATR给出有具体价格锚的分批入场建议，止损用2×ATR计算。"""


def _load_thesis(ticker: str) -> str:
    """从 SQLite 读取该股持仓逻辑，没有则返回空字符串。"""
    try:
        db_path = _resolve_db(None)
        with _conn(db_path) as con:
            row = con.execute(
                "SELECT thesis_text FROM theses WHERE ticker = ? ORDER BY updated_at DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        return row["thesis_text"] if row else ""
    except Exception:
        return ""


async def _analyze_dip(ticker: str, market: str, drop_pct: float,
                        price_now: float, price_1h_ago: float,
                        signals=None, mkt_drop: float = 0.0) -> dict:
    """调用 DeepSeek 分析暴跌是否是机会，返回结构化建议。"""
    thesis = _load_thesis(ticker) or "暂无持仓逻辑记录"
    news_list = _get_fresh_news(ticker, market, hours=4)
    news_summary = "\n".join(f"- {n['title']}" for n in news_list[:5]) or "暂无近期新闻"
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)

    if signals:
        tech_summary = (
            f"布林下轨：{signals.bb_lower:.2f}　布林上轨：{signals.bb_upper:.2f}\n"
            f"ATR(14)：{signals.atr:.2f}（占价格 {signals.atr_pct:.1f}%）\n"
            f"RSI(14)：{signals.rsi:.1f}（{signals.rsi_signal}）\n"
            f"MA20：{signals.ma20:.2f}　MA60：{signals.ma60:.2f}\n"
            f"2×ATR止损参考：{price_now - 2 * signals.atr:.2f}"
        )
    else:
        tech_summary = "暂无技术指标数据（建议参考近期支撑位）"

    # 板块联动判断
    if mkt_drop <= -1.5:
        excess = round(abs(drop_pct) - abs(mkt_drop), 2)
        sector_note = (
            f"大盘同期跌幅 {abs(mkt_drop):.2f}%，个股超跌 {excess:.2f}%"
            f"（{'板块联动为主' if abs(drop_pct) < abs(mkt_drop) * 1.5 else '个股超额下跌，需警惕个股因素'}）"
        )
    elif mkt_drop >= 0.5:
        sector_note = f"大盘同期上涨 {mkt_drop:.2f}%，此下跌为明显个股利空，需格外审慎"
    else:
        sector_note = f"大盘同期变动 {mkt_drop:+.2f}%，下跌主要为个股驱动"

    try:
        raw = await deepseek_chat(
            DIP_SYSTEM,
            DIP_USER.format(
                ticker=ticker, market=market_label,
                drop_pct=abs(drop_pct), price_1h_ago=price_1h_ago, price_now=price_now,
                tech_summary=tech_summary, sector_note=sector_note,
                thesis=thesis, news_summary=news_summary,
            ),
        )
        start, end = raw.find("{"), raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except Exception as e:
        print(f"[Alert] 暴跌机会分析失败 {ticker}: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 价格异动预警
# ─────────────────────────────────────────────────────────────────────────────

def _get_market_1h_drop(benchmark: str) -> float:
    """获取基准指数过去 1 小时的涨跌幅（%），失败返回 0.0。"""
    try:
        df = yf.download(benchmark, period="1d", interval="5m",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 4:
            return 0.0
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        close = df["close"].dropna()
        idx_1h = max(0, len(close) - 12)
        p_now, p_1h = float(close.iloc[-1]), float(close.iloc[idx_1h])
        return round((p_now - p_1h) / p_1h * 100, 2) if p_1h > 0 else 0.0
    except Exception:
        return 0.0


def _recheck_price(ticker: str, market: str) -> float | None:
    """LLM 分析完成后、发送前快速取最新价，识别"分析期间已回升"场景。"""
    try:
        yf_ticker = f"{int(ticker):04d}.HK" if market == "hk" else ticker
        df = yf.download(yf_ticker, period="1d", interval="5m",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        close = df["close"].dropna()
        return float(close.iloc[-1]) if len(close) > 0 else None
    except Exception:
        return None


def _check_price_drop(ticker: str, market: str, threshold_pct: float = 3.0) -> dict | None:
    """
    用 yfinance 5min K 线检测价格异动，触发条件之一满足即报警：
    1. 过去 1 小时跌幅 >= threshold_pct（捕捉突发急跌）
    2. 较今日开盘跌幅 >= threshold_pct * 1.5（捕捉开盘就跌、扫描延迟场景）
    """
    try:
        yf_ticker = f"{int(ticker):04d}.HK" if market == "hk" else ticker
        df_5m = yf.download(yf_ticker, period="1d", interval="5m",
                             progress=False, auto_adjust=True)
        if df_5m.empty or len(df_5m) < 4:
            return None
        if isinstance(df_5m.columns, pd.MultiIndex):
            df_5m.columns = [c[0].lower() for c in df_5m.columns]
        else:
            df_5m.columns = [c.lower() for c in df_5m.columns]
        close = df_5m["close"].dropna()
        if len(close) < 4:
            return None
        price_now = float(close.iloc[-1])

        # 条件1：过去 1 小时跌幅（12 根 5min K）
        idx_1h = max(0, len(close) - 12)
        price_1h_ago = float(close.iloc[idx_1h])
        drop_1h = (price_now - price_1h_ago) / price_1h_ago * 100 if price_1h_ago > 0 else 0.0

        # 条件2：较今日开盘跌幅（第一根 K 线开盘价）
        open_price = float(close.iloc[0])
        drop_from_open = (price_now - open_price) / open_price * 100 if open_price > 0 else 0.0
        open_triggered = drop_from_open <= -(threshold_pct * 1.5)

        triggered_by_1h = drop_1h <= -threshold_pct
        if not triggered_by_1h and not open_triggered:
            return None

        # 用更严重的那个作为报告跌幅；保留两个数据供消息展示
        drop_pct = min(drop_1h, drop_from_open)  # 取绝对值更大的（更负的）

        # 回升判断：从扫描窗口低点到现在涨了多少（识别"追晚了"场景）
        scan_window = close.iloc[idx_1h:]
        low_price = float(scan_window.min())
        recovery_pct = (price_now - low_price) / low_price * 100 if low_price > 0 else 0.0
        idx_15m = max(0, len(close) - 3)
        price_15m_ago = float(close.iloc[idx_15m])
        recovering = price_now > price_15m_ago * 1.005  # 15分钟内回升超0.5%

        # 用日线数据计算有意义的技术指标（ATR/BB/RSI 基于日线更稳健）
        signals = None
        try:
            from finance_agent.signals.technical import calculate_signals
            df_daily = yf.download(yf_ticker, period="3mo", interval="1d",
                                   progress=False, auto_adjust=True)
            if not df_daily.empty:
                if isinstance(df_daily.columns, pd.MultiIndex):
                    df_daily.columns = [c[0].lower() for c in df_daily.columns]
                else:
                    df_daily.columns = [c.lower() for c in df_daily.columns]
                if len(df_daily) >= 20:
                    signals = calculate_signals(df_daily, ticker=ticker)
        except Exception as sig_err:
            print(f"[Alert] 技术指标计算失败 {ticker}: {sig_err}")

        return {
            "ticker": ticker,
            "market": market,
            "drop_pct": round(drop_pct, 2),
            "price_now": round(price_now, 4),
            "price_1h_ago": round(price_1h_ago, 4),
            "open_price": round(open_price, 4),
            "drop_from_open": round(drop_from_open, 2),
            "open_triggered": open_triggered and not triggered_by_1h,  # 仅开盘触发时标记
            "low_price": round(low_price, 4),
            "recovery_pct": round(recovery_pct, 2),
            "recovering": recovering,
            "signals": signals,
        }
    except Exception as e:
        print(f"[Alert] 价格异动检查失败 {ticker}: {e}")
    return None


async def _send_price_drop_alert(ticker: str, market: str,
                                  drop_pct: float, price_now: float,
                                  price_1h_ago: float,
                                  signals=None,
                                  analysis: dict | None = None,
                                  low_price: float | None = None,
                                  recovery_pct: float = 0.0,
                                  recovering: bool = False,
                                  price_at_detection: float | None = None,
                                  open_price: float | None = None,
                                  drop_from_open: float = 0.0,
                                  open_triggered: bool = False,
                                  **_extra) -> None:
    from finance_agent.weekly.daily_followup import TICKER_NAMES
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)
    name = TICKER_NAMES.get(ticker, "")
    display = f"**{ticker}** {name}" if name else f"**{ticker}**"

    OPP_EMOJI = {"高": "🔥", "中": "✨", "低": "⚠️", "无": "🚫"}
    THESIS_EMOJI = {True: "✅ 持仓逻辑完好", False: "❌ 持仓逻辑可能破坏"}

    tech_ref = ""
    if signals:
        tech_ref = (
            f"\nBB下轨：{signals.bb_lower:.2f}　ATR：{signals.atr:.2f}"
            f"　RSI：{signals.rsi:.0f}　2×ATR止损：{price_now - 2 * signals.atr:.2f}"
        )

    # 发送时价 vs 检测时价：LLM 分析期间可能已回升
    if price_at_detection is not None and abs(price_now - price_at_detection) > 0.01:
        recheck_chg = (price_now - price_at_detection) / price_at_detection * 100
        chg_str = f"{'↗' if recheck_chg > 0 else '↘'} {recheck_chg:+.1f}%"
        price_line = (
            f"检测价：{price_at_detection}　发送时：**{price_now}**（{chg_str}）　1小时前：{price_1h_ago}"
        )
        if recheck_chg > 1.5:
            price_line = f"⚠️ 价格已回升，请以发送时价为准\n{price_line}"
    else:
        price_line = f"当前价：**{price_now}**　1小时前：{price_1h_ago}"
        # 检测时已从低点回升
        if recovering and recovery_pct >= 1.0:
            price_line += f"\n⚠️ 低点 {low_price}，已回升 **{recovery_pct:.1f}%**"
        elif recovery_pct >= 0.5:
            price_line += f"\n↗ 低点 {low_price}，已回升 {recovery_pct:.1f}%"

    # 跌幅标签：开盘触发时显示"较开盘"，否则显示"1小时内"
    if open_triggered and open_price:
        drop_label = f"较开盘跌 **{abs(drop_from_open):.2f}%**（开盘价 {open_price}）"
    else:
        drop_label = f"跌幅 **{abs(drop_pct):.2f}%**（1小时内）"
    # 如果同时触发两个条件，也附上开盘跌幅
    if not open_triggered and open_price and abs(drop_from_open) >= abs(drop_pct) * 0.8:
        drop_label += f"　较开盘 {drop_from_open:.2f}%"

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"📉 {display}　{drop_label}\n"
                    f"{price_line}"
                    f"{tech_ref}"
                ),
            },
        },
        {"tag": "hr"},
    ]

    if analysis:
        thesis_ok = analysis.get("thesis_intact", True)
        opp = analysis.get("opportunity", "低")
        drop_reason = analysis.get("drop_reason", "")
        one_line = analysis.get("one_line", "")
        entry_plan = analysis.get("entry_plan", [])
        stop_loss = analysis.get("stop_loss", "")

        # 机会判断
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"{THESIS_EMOJI.get(thesis_ok, '')}　"
                    f"机会评级：{OPP_EMOJI.get(opp, '')} **{opp}**\n"
                    f"💡 {drop_reason}"
                ),
            },
        })
        elements.append({"tag": "hr"})

        # 分批入场计划（仅 opportunity != 无 才展示）
        if entry_plan and opp != "无":
            plan_lines = ["**📋 分批入场建议**"]
            for ep in entry_plan:
                plan_lines.append(
                    f"第 {ep.get('tranche', '?')} 批：{ep.get('condition', '')}　{ep.get('size', '')}"
                )
            if stop_loss:
                plan_lines.append(f"🛑 **止损：** {stop_loss}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(plan_lines)},
            })
            elements.append({"tag": "hr"})

        if one_line:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**一句话建议：** {one_line}"},
            })

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "价格异动 · AI 辅助判断，不构成投资建议，请结合自身情况决策"}],
    })

    has_opp = analysis and analysis.get("opportunity") in ("高", "中")
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                       "content": f"{'🔥 暴跌机会' if has_opp else '⚠️ 价格异动'} · {ticker}（{market_label}）"},
            "template": "green" if has_opp else "orange",
        },
        "elements": elements,
    }
    await send_feishu_card(card)
    opp_tag = f" 机会={analysis.get('opportunity')}" if analysis else ""
    print(f"[Alert] 已推送{'暴跌机会' if has_opp else '价格异动'}：{ticker} 1h跌幅={abs(drop_pct):.2f}%{opp_tag}")


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
            # 同一分钟内同类型快讯只推最高分那一条（中文标题无法用语义去重，改用时间槽）
            slot_key = f"macro_slot:{news['published']}"
            if key in alerted or slot_key in alerted:
                if key not in alerted:
                    _save_alerted(key, today_str)  # 标记处理过，下次不重新评分
                print(f"[Alert] 港股宏观 同时刻已推送，跳过：{news['title'][:50]}")
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
                alerted.add(slot_key)
                _save_alerted(key, today_str)
                _save_alerted(slot_key, today_str)
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
            slot_key = f"us_macro_slot:{news['published']}"
            if key in alerted or slot_key in alerted:
                if key not in alerted:
                    _save_alerted(key, today_str)
                print(f"[Alert] 美股宏观 同时刻已推送，跳过：{news['title'][:50]}")
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
                alerted.add(slot_key)
                _save_alerted(key, today_str)
                _save_alerted(slot_key, today_str)
                pushed += 1
            else:
                print(f"[Alert] 美股宏观 影响度={impact}，跳过：{news['title'][:50]}")

    # ── 4. 价格异动预警（watchlist + 非ETF持仓，1h 跌幅 > 3%）─────────────────
    etf_sectors = {"宽基ETF", "股息ETF"}
    price_watch = list(portfolio.get("watchlist", [])) + [
        h for h in portfolio.get("holdings", [])
        if h.get("sector", "") not in etf_sectors and not h.get("is_dca", False)
    ]
    print(f"[Alert] 价格异动扫描：{len(price_watch)} 只标的（阈值 3%/1h）")
    for item in price_watch:
        ticker = item["ticker"]
        market = item.get("market", "us")
        drop_info = await asyncio.get_event_loop().run_in_executor(
            None, _check_price_drop, ticker, market
        )
        if drop_info:
            hour_key = f"price_drop:{ticker}:{datetime.now(_BJT).strftime('%Y-%m-%d %H')}"
            if hour_key not in alerted:
                analysis = await _analyze_dip(**drop_info)
                # 发送前重取最新价，LLM 分析期间价格可能已变化
                fresh_price = await asyncio.get_event_loop().run_in_executor(
                    None, _recheck_price, ticker, market
                )
                price_at_detection = drop_info["price_now"]
                if fresh_price is not None:
                    drop_info = {**drop_info, "price_now": fresh_price}
                await _send_price_drop_alert(**drop_info, analysis=analysis,
                                             price_at_detection=price_at_detection)
                alerted.add(hour_key)
                _save_alerted(hour_key, today_str)
                pushed += 1
            else:
                print(f"[Alert] {ticker} 价格异动本小时已推送，跳过")
        else:
            print(f"[Alert] {ticker} 无异动（1h跌幅 < 3%）")

    if pushed == 0:
        print(f"[Alert] 本次扫描无高影响新闻（阈值={impact_threshold}）")
    return pushed


async def run_price_scan(threshold_pct: float = 3.0) -> int:
    """
    轻量价格异动扫描：只检测价格，跳过新闻，约 30 秒跑完。
    去重粒度为 10 分钟（而非 news-scan 的 1 小时），适合高频触发。
    返回推送条数。
    """
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    etf_sectors = {"宽基ETF", "股息ETF"}
    price_watch = list(portfolio.get("watchlist", [])) + [
        h for h in portfolio.get("holdings", [])
        if h.get("sector", "") not in etf_sectors and not h.get("is_dca", False)
    ]

    today_str = datetime.now(_BJT).strftime("%Y-%m-%d")
    alerted = _load_alerted(today_str)
    # 10 分钟粒度去重 key（每 10 分钟最多推一次）
    slot_5m = datetime.now(_BJT).strftime("%Y-%m-%d %H:") + str(datetime.now(_BJT).minute // 10)

    # 顺手回填历史暴跌记录（24h/7d 实际涨跌），不阻塞主流程
    try:
        backfill_dip_outcomes()
    except Exception:
        pass

    # 一次性取得大盘基准 1h 涨跌（用于板块联动判断）
    loop = asyncio.get_event_loop()
    us_mkt_drop, hk_mkt_drop = await asyncio.gather(
        loop.run_in_executor(None, _get_market_1h_drop, "SPY"),
        loop.run_in_executor(None, _get_market_1h_drop, "^HSI"),
    )
    print(f"[PriceScan] 基准涨跌：SPY {us_mkt_drop:+.2f}%  HSI {hk_mkt_drop:+.2f}%")

    pushed = 0
    print(f"[PriceScan] 扫描 {len(price_watch)} 只标的（阈值 {threshold_pct}%/1h）")
    for item in price_watch:
        ticker = item["ticker"]
        market = item.get("market", "us")
        drop_info = await loop.run_in_executor(
            None, _check_price_drop, ticker, market, threshold_pct
        )
        if drop_info:
            dedup_key = f"price_drop_10m:{ticker}:{slot_5m}"
            if dedup_key not in alerted:
                mkt_drop = hk_mkt_drop if market == "hk" else us_mkt_drop
                analysis = await _analyze_dip(**drop_info, mkt_drop=mkt_drop)
                # 发送前重取最新价，LLM 分析期间价格可能已变化
                fresh_price = await loop.run_in_executor(None, _recheck_price, ticker, market)
                price_at_detection = drop_info["price_now"]
                if fresh_price is not None:
                    drop_info = {**drop_info, "price_now": fresh_price}
                await _send_price_drop_alert(**drop_info, analysis=analysis,
                                             price_at_detection=price_at_detection)
                if analysis:
                    try:
                        save_dip_alert(
                            ticker=ticker, market=market,
                            drop_pct=drop_info["drop_pct"],
                            price_at_alert=drop_info["price_now"],
                            analysis=analysis,
                        )
                    except Exception as e:
                        print(f"[PriceScan] 保存暴跌记录失败 {ticker}: {e}")
                alerted.add(dedup_key)
                _save_alerted(dedup_key, today_str)
                pushed += 1
            else:
                print(f"[PriceScan] {ticker} 10分钟内已推送，跳过")
        else:
            print(f"[PriceScan] {ticker} 无异动（1h跌幅 < {threshold_pct}%，较开盘跌幅 < {threshold_pct*1.5:.1f}%）")

    if pushed == 0:
        print(f"[PriceScan] 本次无价格异动")
    return pushed
