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

# 单次 news-scan 推送硬上限（06-12 UX 扫描：32 个扫描项极端日可推 32 条）
MAX_PUSH_PER_SCAN = 6

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
        # guidance 独立成类（06-12 UX 扫描 P0）：原与 earnings 同 key，同日"财报超预期"
        # 与"下调指引"（常为反向利空）生成相同 dedup seed，第二条被静默丢弃
        'guidance':    r'\b(guidance|outlook|forecasts?)\b',
        'earnings':    r'\b(earnings?|earnings?\s+(?:miss|beat|report)|quarterly\s+results?|eps)\b',
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
  "priced_in": "已充分预期" | "有意外性" | "部分已知",
  "suggested_action": "立即关注" | "等待确认" | "可忽略",
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（财报超预期/暴雷、重大并购、监管处罚、CEO 离职、重大贸易/外交政策）
5-7:  中等影响（行业政策变化、竞争对手动态、分析师调级）
1-4:  低影响（一般行业新闻、重申评级、常规发布会）

priced_in 判断标准：
- "已充分预期"：市场已提前知晓（如业绩指引早已给出、分析师一致预期一致），新闻发布时价格可能不动或反向
- "有意外性"：超出市场预期（如财报大幅超预期/暴雷、意外政策转变、突发事件），价格反应可能较大
- "部分已知"：市场有预期但程度/细节未定，发布后仍有一定价格反应空间

suggested_action 判断标准：
- "立即关注"：有意外性 + 影响方向明确，需及时评估是否调整持仓
- "等待确认"：方向不明或需等后续细节，建议观察1-2个交易日再行动
- "可忽略"：已充分预期 + 无新信息，或影响度低，无需操作"""

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
  "priced_in": "已充分预期" | "有意外性" | "部分已知",
  "suggested_action": "立即关注" | "等待确认" | "可忽略",
  "affected_sectors": ["科技", "消费", "金融"],
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（中美关系重大转变、联储加息/降息、贸易战升级/缓和、重大外交事件）
5-7:  中等影响（PMI 超预期、行业政策、外资流入流出）
1-4:  低影响（常规数据发布、无实质影响的声明）

priced_in / suggested_action 与个股评估标准相同。"""

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
  "priced_in": "已充分预期" | "有意外性" | "部分已知",
  "suggested_action": "立即关注" | "等待确认" | "可忽略",
  "affected_sectors": ["半导体/AI算力", "互联网/AI", "宽基ETF"],
  "reason": "一句话说明为什么这条新闻重要（或不重要）"
}

impact 评分标准：
8-10: 极重要（Fed 加息/降息决定、CPI 大幅超预期、标普/纳指单日跌超 2%、芯片出口管制）
5-7:  中等影响（经济数据小幅超预期、分析师集体调级、板块轮动）
1-4:  低影响（个股财报、常规声明、重申评级）

priced_in / suggested_action 与个股评估标准相同。"""

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
                            conflict: str = "",
                            priced_in: str = "", suggested_action: str = "") -> None:
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)

    # 有信号冲突时 header 固定用橙色，标题加标记
    has_conflict = bool(conflict)
    header_title = f"{'⚠️ 信号冲突 · ' if has_conflict else '⚡ 持仓快讯 · '}{ticker}（{market_label}）"
    header_color = "orange" if has_conflict else (
        "red" if sentiment == "利空" else ("green" if sentiment == "利好" else "blue")
    )

    # 定价状态 + 操作建议行
    ACTION_EMOJI = {"立即关注": "🚨", "等待确认": "⏳", "可忽略": "💤"}
    PRICED_EMOJI = {"已充分预期": "📉已定价", "有意外性": "⚡有意外", "部分已知": "🔔部分已知"}
    action_line = ""
    if priced_in or suggested_action:
        parts = []
        if priced_in:
            parts.append(PRICED_EMOJI.get(priced_in, priced_in))
        if suggested_action:
            parts.append(f"{ACTION_EMOJI.get(suggested_action, '')} **{suggested_action}**")
        action_line = "　".join(parts)

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"{SENT_EMOJI.get(sentiment, '⚪')} **{sentiment}** {IMPACT_BAR}  影响度 {impact}/10\n"
                    f"**{title}**\n"
                    f"🕐 {published}"
                    + (f"\n{action_line}" if action_line else "")
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
    await send_feishu_card(card, fallback_text=_card_fallback_text(card))
    conflict_tag = " [冲突!]" if has_conflict else ""
    print(f"[Alert] 已推送 {ticker} 快讯{conflict_tag}：{title[:40]}... (影响度={impact}, {sentiment})")


# 宏观新闻「涉及板块」→ 持仓 sector 关键词 的同义词映射（用于关联持仓指引）
_MACRO_SECTOR_SYNONYMS = {
    "半导体": ["半导体"], "芯片": ["半导体"], "算力": ["半导体"], "内存": ["半导体"], "存储": ["半导体"],
    "科技": ["互联网", "半导体", "宽基"], "互联网": ["互联网"], "软件": ["互联网"], "云计算": ["互联网"],
    "人工智能": ["互联网", "半导体"], "ai": ["互联网", "半导体"],
    "金融": ["金融"], "券商": ["金融"], "银行": ["金融"], "保险": ["金融"],
    "量子": ["量子"],
    "大盘": ["宽基"], "标普": ["宽基"], "纳斯达克": ["宽基"], "纳指": ["宽基"], "美股大盘": ["宽基"],
    "全市场": ["宽基", "股息"], "股息": ["股息"], "红利": ["股息"], "高股息": ["股息"],
}


def _related_holdings(affected_sectors: list, max_n: int = 8) -> list:
    """根据宏观新闻涉及板块，匹配用户持仓中相关标的，返回 ticker 列表（用于「关联持仓」指引）。"""
    if not affected_sectors:
        return []
    try:
        config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
        with open(config_path) as f:
            portfolio = yaml.safe_load(f)
    except Exception:
        return []

    sec_text = "、".join(affected_sectors).lower()
    matched: list = []
    seen: set = set()
    for h in portfolio.get("holdings", []):
        ticker = h.get("ticker", "")
        h_sector = h.get("sector", "")
        if not ticker or ticker in seen:
            continue
        for kw, targets in _MACRO_SECTOR_SYNONYMS.items():
            if kw in sec_text and any(t in h_sector for t in targets):
                matched.append(ticker)
                seen.add(ticker)
                break
        if len(matched) >= max_n:
            break
    return matched


def _macro_global_key(published: str, sentiment: str) -> str:
    """跨源宏观去重 key：同一 30 分钟窗口 + 同情绪 视为同一事件。
    港股（东财中文）与美股（CNBC 英文）对同一全球事件标题指纹不同，无法语义去重，
    改用「发布时段 + 情绪」粗粒度兜底，避免同一事件被两源各推一条。"""
    try:
        date_part, hm = published.split()
        h, m = hm.split(":")
        slot = f"{date_part} {h}:{int(m) // 30}"
    except Exception:
        slot = published
    return f"macro_global:{sentiment}:{slot}"


async def _send_macro_alert(title: str, published: str, impact: int, sentiment: str,
                            affected_sectors: list, reason: str,
                            priced_in: str = "", suggested_action: str = "") -> None:
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    sectors_str = "、".join(affected_sectors) if affected_sectors else "全市场"
    related = _related_holdings(affected_sectors)
    related_line = "、".join(related) if related else "暂无直接关联（属大盘/情绪面影响）"

    ACTION_EMOJI = {"立即关注": "🚨", "等待确认": "⏳", "可忽略": "💤"}
    PRICED_EMOJI = {"已充分预期": "📉已定价", "有意外性": "⚡有意外", "部分已知": "🔔部分已知"}
    action_parts = []
    if priced_in:
        action_parts.append(PRICED_EMOJI.get(priced_in, priced_in))
    if suggested_action:
        action_parts.append(f"{ACTION_EMOJI.get(suggested_action, '')} **{suggested_action}**")
    action_line = "　".join(action_parts)

    content = (
        f"{SENT_EMOJI.get(sentiment, '⚪')} **{sentiment}** {IMPACT_BAR}  影响度 {impact}/10\n"
        f"**{title}**\n"
        f"🕐 {published}   📌 涉及板块：{sectors_str}\n"
        f"🎯 **与你持仓相关**：{related_line}"
    )
    if action_line:
        content += f"\n{action_line}"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🌐 宏观快讯 · 港股/A股影响"},
            "template": "red" if sentiment == "利空" else (
                "green" if sentiment == "利好" else "blue"
            ),
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"💡 {reason}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "宏观快讯 · AI 快速判断，仅供参考"}]},
        ],
    }
    await send_feishu_card(card, fallback_text=_card_fallback_text(card))
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
                               affected_sectors: list, reason: str,
                               priced_in: str = "", suggested_action: str = "") -> None:
    SENT_EMOJI = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}
    IMPACT_BAR = "🔥" * min(impact // 2, 5)
    sectors_str = "、".join(affected_sectors) if affected_sectors else "科技/半导体"
    related = _related_holdings(affected_sectors or ["科技", "半导体"])
    related_line = "、".join(related) if related else "暂无直接关联（属大盘/情绪面影响）"

    ACTION_EMOJI = {"立即关注": "🚨", "等待确认": "⏳", "可忽略": "💤"}
    PRICED_EMOJI = {"已充分预期": "📉已定价", "有意外性": "⚡有意外", "部分已知": "🔔部分已知"}
    action_parts = []
    if priced_in:
        action_parts.append(PRICED_EMOJI.get(priced_in, priced_in))
    if suggested_action:
        action_parts.append(f"{ACTION_EMOJI.get(suggested_action, '')} **{suggested_action}**")
    action_line = "　".join(action_parts)

    content = (
        f"{SENT_EMOJI.get(sentiment, '⚪')} **{sentiment}** {IMPACT_BAR}  影响度 {impact}/10\n"
        f"**{title}**\n"
        f"🕐 {published}   📌 涉及板块：{sectors_str}\n"
        f"🎯 **与你持仓相关**：{related_line}"
    )
    if action_line:
        content += f"\n{action_line}"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📈 美股宏观 · 科技/半导体影响"},
            "template": "red" if sentiment == "利空" else (
                "green" if sentiment == "利好" else "blue"
            ),
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"💡 {reason}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": "美股宏观快讯 · AI 快速判断，仅供参考"}]},
        ],
    }
    await send_feishu_card(card, fallback_text=_card_fallback_text(card))
    print(f"[Alert] 已推送美股宏观快讯：{title[:40]}... (影响度={impact}, {sentiment})")


# ─────────────────────────────────────────────────────────────────────────────
# 暴跌机会分析
# ─────────────────────────────────────────────────────────────────────────────

DIP_SYSTEM = """你是一位家庭投资顾问。注意：**触发本次分析的标的都是用户【已经持有】的持仓**，
所以你的任务不是"要不要建仓抄底"，而是"这次下跌后，这笔已有持仓该怎么处理"。

严格按以下 JSON 格式输出（不要其他内容）：
{
  "thesis_intact": true | false,
  "impact_chain": {
    "direct": "直接触发因素（一句话：什么事件/情绪导致这次下跌）",
    "spillover": "传导风险（会不会蔓延到其他持仓？如'半导体板块联动影响NVDA/MU'，无则写'暂无明显传导'）",
    "recovery": "修复条件（什么情况下会修复？如'联储暂停加息'或'Q2财报确认增速'）"
  },
  "drop_reason": "一句话综合判断：情绪恐慌 / 板块联动 / 还是基本面恶化",
  "action": "加仓" | "持有观望" | "减仓" | "清仓离场",
  "action_reason": "为什么是这个动作，必须结合 thesis（持仓逻辑）是否被破坏来解释",
  "add_trigger": "仅当 action=加仓：给一个【必须严格低于当前价】的加仓触发价或条件（如'回踩XXX企稳后'）；其他动作一律留空字符串",
  "add_limit": "仅当 action=加仓：加仓上限（如'不超过现有仓位的30%'）；其他动作留空字符串",
  "invalidation": "认错信号：出现什么情况就说明你看错了、应当离场（可定性，如'跌破前低且下季财报指引转弱'，不必是精确止损价）",
  "one_line": "一句大白话告诉用户现在这笔持仓该怎么做"
}

决策原则：
- 持仓逻辑未破坏 + 下跌由大盘情绪/板块联动驱动 → action=加仓 或 持有观望
- 持仓逻辑的核心判断被证伪（基本面恶化/业绩暴雷/竞争格局逆转）→ action=减仓 或 清仓离场，不论当前盈亏
- **add_trigger 必须严格低于当前价**——下跌后是在更低位置加仓，绝不能给出高于或等于现价的"加仓价"
- 禁止输出"分批建仓 30%/30%/20%"这类建新仓话术——用户已经持有，只需说清 加 / 持 / 减 / 离场
- 用 invalidation（认错离场的信号）替代机械的"2×ATR 止损"；可以是价格，也可以是基本面事件
- impact_chain.spillover 要结合用户其他持仓判断传导风险
- 若该股波动极大（ATR 占价很高），价格锚只作参考，更应强调【条件】而非精确点位"""

DIP_USER = """股票：{ticker}（{market}市场）——⚠️ 这是用户的【现有持仓】，请按"持仓如何处理"而非"建仓抄底"来分析。
1小时跌幅：{drop_pct}%（从 {price_1h_ago} 跌到 {price_now}）
当前价：{price_now}

【板块联动判断】
{sector_note}

【技术指标（日线）】
{tech_summary}

【这笔持仓的逻辑（thesis）】
{thesis}

【近期新闻摘要】
{news_summary}

【其他持仓（判断传导风险用）】
{other_holdings}

请基于"这笔已有持仓现在该 加 / 持 / 减 / 离场"给出决策。若建议加仓，add_trigger 必须严格低于当前价 {price_now}。"""


def _load_thesis(ticker: str) -> str:
    """从 SQLite 读取该股持仓逻辑，没有则返回空字符串。
    >30 天的旧逻辑加显式标注（06-12 UX 扫描：dip 分析可能引用 60+ 天前的过期叙事
    判断"thesis 完好"，用户无从知晓证据已过期）。"""
    try:
        db_path = _resolve_db(None)
        with _conn(db_path) as con:
            row = con.execute(
                "SELECT thesis_text, updated_at, "
                "CAST(julianday('now') - julianday(updated_at) AS INTEGER) AS age_d "
                "FROM theses WHERE ticker = ? ORDER BY updated_at DESC LIMIT 1",
                (ticker,)
            ).fetchone()
        if not row:
            return ""
        text = row["thesis_text"] or ""
        age = row["age_d"]
        if text and age is not None and age > 30:
            text = f"（注意：该持仓逻辑记录于 {age} 天前，可能已过期，判断时降权）\n{text}"
        return text
    except Exception:
        return ""


def _load_portfolio_account() -> dict:
    """读取 portfolio.yaml 中的 account 配置和持仓列表。"""
    try:
        config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return {
            "account": cfg.get("account", {}),
            "holdings": cfg.get("holdings", []),
        }
    except Exception:
        return {"account": {}, "holdings": []}


def _shares_hint(price_now: float, atr: float, market: str, account: dict) -> int | None:
    """
    ATR-based position sizer：根据账户规模和 ATR 计算建议买入股数（第一批）。
    公式：shares = (account_size × risk_pct%) ÷ (2 × ATR)
    """
    if not account or atr <= 0:
        return None
    size = account.get("hkd" if market == "hk" else "usd", 0)
    risk_pct = account.get("risk_per_trade_pct", 1.0) / 100
    risk_budget = size * risk_pct
    stop_dist = 2 * atr
    shares = int(risk_budget / stop_dist)
    if shares <= 0:
        return None
    return shares


def _thesis_from_portfolio(ticker: str) -> str:
    """theses 表无记录时的兜底：用 portfolio.yaml 的 sector + notes 拼出持仓逻辑，
    避免 AI 因「读不到 thesis」而误判「持仓逻辑不明 / 可能破坏」。"""
    try:
        for h in _load_portfolio_account().get("holdings", []):
            if h.get("ticker") == ticker:
                parts = []
                if h.get("sector"):
                    parts.append(f"板块：{h['sector']}")
                if h.get("notes"):
                    parts.append(f"持仓备注：{h['notes']}")
                return "；".join(parts)
    except Exception:
        pass
    return ""


def _extract_price(text: str) -> float | None:
    """从自然语言中提取第一个像价格的数字（用于加仓价的自洽性校验）。"""
    if not text:
        return None
    import re
    m = re.search(r"\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else None


def _validate_dip_plan(analysis: dict, price_now: float) -> tuple[dict, list]:
    """自洽性闸门：消除「加仓价高于现价」这类自相矛盾，矛盾项就地降级为定性条件。
    这是彻底杜绝「63 买 / 81 止损」式矛盾卡片的最后一道保险。
    返回 (修正后的 analysis, 警告列表)。"""
    warnings: list = []
    if not analysis:
        return analysis, warnings
    if analysis.get("action") == "加仓":
        p = _extract_price(analysis.get("add_trigger", ""))
        # 加仓价必须严格低于现价；否则剔除矛盾的具体价，降级为「等企稳」
        if p is not None and price_now > 0 and p >= price_now:
            analysis["add_trigger"] = "等回踩企稳后再加（模型给出的加仓价不低于现价，已剔除以避免矛盾）"
            warnings.append(f"加仓价 {p} ≥ 现价 {price_now}，已降级")
    return analysis, warnings


async def _analyze_dip(ticker: str, market: str, drop_pct: float,
                        price_now: float, price_1h_ago: float,
                        signals=None, mkt_drop: float = 0.0,
                        gap_up_pct: float = 0.0, **_extra) -> dict:
    """调用 DeepSeek 分析暴跌是否是机会，返回结构化建议（含链式影响和股数建议）。"""
    thesis = _load_thesis(ticker) or _thesis_from_portfolio(ticker) or "暂无持仓逻辑记录"
    news_list = _get_fresh_news(ticker, market, hours=4)
    news_summary = "\n".join(f"- {n['title']}" for n in news_list[:5]) or "暂无近期新闻"
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)

    # 其他持仓（传导风险判断）
    portfolio_data = _load_portfolio_account()
    other = [
        f"{h['ticker']}（{h.get('sector','未知板块')}）"
        for h in portfolio_data["holdings"]
        if h["ticker"] != ticker
    ]
    other_holdings = "、".join(other[:8]) if other else "暂无"

    if signals:
        # BB 下轨合理性检查：偏离当前价超 20% 视为历史数据跨度失真，改用 MA20
        bb_lower_valid = signals.bb_lower >= price_now * 0.8
        if bb_lower_valid:
            bb_anchor = f"布林下轨：{signals.bb_lower:.2f}（可作为参考支撑）"
        else:
            bb_anchor = (
                f"布林下轨：{signals.bb_lower:.2f}（⚠️ 偏离现价过大、数据跨度失真，勿作价格锚）\n"
                f"改用 MA20 {signals.ma20:.2f} 作为近期支撑参考"
            )
        volatility_note = ""
        if signals.atr_pct > 8:
            volatility_note = (
                f"\n⚡ 高波动品种（ATR 占价 {signals.atr_pct:.1f}%），价格锚仅供参考，重在条件而非精确点位"
            )
        tech_summary = (
            f"{bb_anchor}\n"
            f"ATR(14)：{signals.atr:.2f}（占价格 {signals.atr_pct:.1f}%）\n"
            f"RSI(14)：{signals.rsi:.1f}（{signals.rsi_signal}）\n"
            f"MA20：{signals.ma20:.2f}　MA60：{signals.ma60:.2f}"
            f"{volatility_note}"
        )
        # 计算建议股数
        hint = _shares_hint(price_now, signals.atr, market, portfolio_data["account"])
    else:
        tech_summary = "暂无技术指标数据（建议参考近期支撑位）"
        hint = None

    # 当天跳空背景注释（传给 LLM，让其判断"回调是否只是正常散热"）
    gap_context = ""
    if gap_up_pct >= 10:
        gap_context = (
            f"\n⚠️ 注意：该股今天开盘相对前收已跳空上涨 **{gap_up_pct:.1f}%**，"
            f"当前回调 {abs(drop_pct):.1f}% 可能只是超买散热而非真正的暴跌机会，"
            f"请在判断 opportunity 时保持审慎，不要仅凭1小时跌幅就给高评级。"
        )
    elif gap_up_pct >= 5:
        gap_context = f"\n提示：今日已跳空上涨 {gap_up_pct:.1f}%，当前回调需结合全日涨幅评估。"

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
    sector_note += gap_context

    try:
        raw = await deepseek_chat(
            DIP_SYSTEM,
            DIP_USER.format(
                ticker=ticker, market=market_label,
                drop_pct=abs(drop_pct), price_1h_ago=price_1h_ago, price_now=price_now,
                tech_summary=tech_summary, sector_note=sector_note,
                thesis=thesis, news_summary=news_summary,
                other_holdings=other_holdings,
            ),
        )
        start, end = raw.find("{"), raw.rfind("}") + 1
        result = json.loads(raw[start:end])
        # 自洽性闸门：剔除「加仓价 ≥ 现价」等矛盾
        result, _gate_warn = _validate_dip_plan(result, price_now)
        if _gate_warn:
            print(f"[Alert] {ticker} 自洽闸门修正：{'；'.join(_gate_warn)}")
        if hint is not None:
            result["shares_hint"] = hint
        return result
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
        # round 4：裸 float 会原样进卡片（用户截到过 404.20001220703125）
        return round(float(close.iloc[-1]), 4) if len(close) > 0 else None
    except Exception:
        return None


def _fmt_px(x) -> str:
    """卡片价格统一格式：≥10 两位小数、<10 三位；任何来源的裸浮点到此为止。"""
    if x is None:
        return "—"
    x = float(x)
    return f"{x:,.2f}" if abs(x) >= 10 else f"{x:,.3f}"


def _ccy(market: str) -> str:
    """卡片货币前缀（06-12 UX 扫描：港币美元混排无标注，header 折叠后只剩裸数字）。"""
    return {"us": "$", "hk": "HK$", "cn": "¥"}.get(market, "")

def _card_fallback_text(card: dict) -> str:
    """飞书卡片失败时的纯文本兜底（06-12 UX 扫描：多数推送点无 fallback，
    飞书故障=用户静默错过预警）。提取 header 标题 + 各段前 60 字。"""
    try:
        parts = [card.get("header", {}).get("title", {}).get("content", "")]
        for el in card.get("elements", []):
            if el.get("tag") == "div":
                parts.append(el.get("text", {}).get("content", "")[:60])
        return " | ".join(x for x in parts if x)[:400] or "卡门智投提醒（卡片渲染失败）"
    except Exception:
        return "卡门智投提醒（卡片渲染失败）"



def _is_market_open(market: str) -> bool:
    """
    分钟级判断对应市场是否处于交易时段（UTC）。
    06-12 事故：原整点判断让北京 21:00-21:30 成为"假开盘"窗口，盘前扫描拿到
    昨日 session 数据被当成实时大涨推送。夏令时口径（冬令时 +1h 漂移由
    _bars_fresh 新鲜度守卫兜底，此处不做日历级精确）。
      美股：UTC 13:30-20:00；港股：UTC 01:30-08:00
    """
    now = datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    if market == "us":
        return 13 * 60 + 30 <= m <= 20 * 60
    elif market == "hk":
        # 午休 04:00-05:00 UTC（12:00-13:00 HKT）休市
        return (1 * 60 + 30 <= m <= 8 * 60) and not (4 * 60 <= m < 5 * 60)
    return True  # 其他市场不做限制


_MAX_BAR_AGE_MIN = 30


def _bars_fresh(close) -> bool:
    """
    数据新鲜度守卫（06-12 事故根治层）：最后一根 5m K 线距现在超过 30 分钟
    = 拿到的是上一个 session 的旧数据（盘前/节假日/半日市/时钟边界漂移），
    绝不允许当成实时异动报警。索引无时区（仅测试 mock 场景）放行。
    """
    try:
        last_ts = close.index[-1]
        if getattr(last_ts, "tzinfo", None) is None:
            return True
        age = pd.Timestamp.now(tz=last_ts.tz) - last_ts
        return age <= pd.Timedelta(minutes=_MAX_BAR_AGE_MIN)
    except Exception:
        return True


def _load_dip_atr_cfg() -> dict:
    """settings.yaml dip_atr_adaptive 配置块；缺失/解析失败回落默认值。"""
    cfg = {"enabled": True, "base_pct": 3.0, "k": 0.8, "cap_pct": 7.0}
    try:
        p = Path(__file__).parents[3] / "config" / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            user = s.get("dip_atr_adaptive") or {}
            if isinstance(user, dict):
                cfg.update({k: user[k] for k in cfg if k in user})
    except Exception:
        pass
    return cfg


def _effective_drop_threshold(atr_pct: float, gap_up_pct: float,
                              cfg: dict | None = None) -> float:
    """
    ATR 自适应触发阈值（order8）：effective = max(base, min(k*atr_pct, cap))。
    只抬升不下调（>= base），高波动票的日常震荡不再误报为暴跌。
    gap_up > 10% 的抬升叠加在 ATR 结果上（原实现叠加在写死 3.0% 上，已修正）。
    atr_pct 取不到（新股<20日历史/信号失败）→ 取 cap 保守降噪（06-12：00100
    新股按 base 3% 被报 7 连击，它 ±8% 是日常波动）；开关关闭回落 base。
    """
    cfg = cfg or _load_dip_atr_cfg()
    base = float(cfg.get("base_pct", 3.0))
    if not cfg.get("enabled", True):
        eff = base
    elif atr_pct and atr_pct > 0:
        eff = max(base, min(float(cfg.get("k", 0.8)) * atr_pct,
                            float(cfg.get("cap_pct", 7.0))))
    else:
        # ATR 未知=波动未知 → 按最坏假设取 cap（漏报最坏=错过抄底提示，不会乱买）
        eff = float(cfg.get("cap_pct", 7.0))
    if gap_up_pct > 10:
        eff = eff * (1 + gap_up_pct / 20)
    return round(eff, 2)


def _alert_step(atr_pct: float) -> float:
    """台阶宽度：跌/涨幅每加深一个 step 才算有信息增量（06-12 七连击复盘）。"""
    return max(2.0, 0.5 * (atr_pct or 0.0))


def _escalation_decision(prev_levels: list[int], level: int,
                         daily_cap: int = 3) -> str:
    """
    同票同日异动推送的增量决策（纯函数，台阶去重核心）：
      skip  — 未创当日新台阶（重复信息）或已到每日硬上限
      full  — 当日首报，或台阶 ≥ 首报台阶×2（剧烈恶化，值得重跑 LLM 分析）
      light — 加深一档：推免 LLM 的一行增量卡
    """
    if prev_levels and level <= max(prev_levels):
        return "skip"
    if len(prev_levels) >= daily_cap:
        return "skip"
    if not prev_levels:
        return "full"
    # 跳两档才重跑完整 LLM 分析（06-12 UX 扫描：原 2*首报 在首报=1 时 light 几乎不可达）
    return "full" if level >= min(prev_levels) + 2 else "light"


def _check_price_drop(ticker: str, market: str, threshold_pct: float = 3.0) -> dict | None:
    """
    用 yfinance 5min K 线检测价格异动，两阶段判定：
    Stage 1（宽松预筛，base 阈值）：1 小时跌幅或较开盘跌幅(×1.5)任一过线才继续，
      否则直接返回 None，省掉后续日线/技术指标网络开销。
    Stage 2（精确判定，ATR 自适应阈值）：用日线 ATR% 抬升阈值
      effective = max(base, min(k*atr_pct, cap))，gap_up>10% 再叠加抬升；
      两个触发条件都按 effective 重新判（修复原 open_triggered 用 base 的 bug）。
    市场收盘后直接跳过，避免用过期收盘数据误报。
    """
    if not _is_market_open(market):
        return None
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
        if not _bars_fresh(close):
            return None   # 旧 session 数据（盘前/节假日），不许当实时异动
        price_now = float(close.iloc[-1])

        # 条件1：过去 1 小时跌幅（12 根 5min K）
        idx_1h = max(0, len(close) - 12)
        price_1h_ago = float(close.iloc[idx_1h])
        drop_1h = (price_now - price_1h_ago) / price_1h_ago * 100 if price_1h_ago > 0 else 0.0

        # 条件2：较今日开盘跌幅（第一根 K 线开盘价）
        open_price = float(close.iloc[0])
        drop_from_open = (price_now - open_price) / open_price * 100 if open_price > 0 else 0.0

        # Stage 1：宽松预筛（base 阈值），两条件都不过线直接退出，不触日线网络请求
        if drop_1h > -threshold_pct and drop_from_open > -(threshold_pct * 1.5):
            return None

        # 当天跳空幅度：对比前一日收盘（用 2d 日线取昨收）
        gap_up_pct = 0.0
        try:
            df_2d = yf.download(yf_ticker, period="2d", interval="1d",
                                 progress=False, auto_adjust=True)
            if not df_2d.empty and len(df_2d) >= 2:
                if isinstance(df_2d.columns, pd.MultiIndex):
                    df_2d.columns = [c[0].lower() for c in df_2d.columns]
                else:
                    df_2d.columns = [c.lower() for c in df_2d.columns]
                prev_close = float(df_2d["close"].iloc[-2])
                if prev_close > 0:
                    gap_up_pct = (open_price - prev_close) / prev_close * 100
        except Exception:
            pass

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

        # Stage 2：ATR 自适应精确判定（两个条件统一用 effective 阈值）
        atr_pct = signals.atr_pct if signals else 0.0
        cfg = _load_dip_atr_cfg()
        if cfg.get("enabled", True):
            effective_threshold = _effective_drop_threshold(atr_pct, gap_up_pct, cfg)
            open_thr = effective_threshold * 1.5
        else:
            # 一键关=完全忽略 dip_atr_adaptive 块（含 base_pct），两条件统一回退函数
            # 入参 threshold_pct，与改动前逐字一致（06-12 自检 P1：原修法 1h 走
            # cfg.base_pct、open 走 threshold_pct，两条件不同源，base_pct≠3 时口径分裂）
            effective_threshold = (threshold_pct * (1 + gap_up_pct / 20)
                                   if gap_up_pct > 10 else threshold_pct)
            open_thr = threshold_pct * 1.5
        triggered_by_1h = drop_1h <= -effective_threshold
        open_triggered = drop_from_open <= -open_thr
        if not triggered_by_1h and not open_triggered:
            print(f"[Alert] {ticker} 过预筛但未过 ATR 自适应阈值 "
                  f"{effective_threshold:.1f}%（ATR占价 {atr_pct:.1f}%），按正常波动忽略")
            return None

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
            "gap_up_pct": round(gap_up_pct, 2),
            "effective_threshold": effective_threshold,
            "signals": signals,
        }
    except Exception as e:
        print(f"[Alert] 价格异动检查失败 {ticker}: {e}")
    return None


def _check_price_surge(ticker: str, market: str, threshold_pct: float = 3.0) -> dict | None:
    """
    持仓大涨检测（D8，仅对已持仓票调用）：镜像 _check_price_drop 的两阶段结构，
    方向取反。用途=止盈/拿住的决策提示点，不是催卖按钮。
    阈值同走 ATR 自适应（高波动票日常上蹿不报），gap 抬升不适用（传 0）。
    """
    if not _is_market_open(market):
        return None
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
        if not _bars_fresh(close):
            return None   # 旧 session 数据（盘前/节假日），不许当实时异动
        price_now = float(close.iloc[-1])
        idx_1h = max(0, len(close) - 12)
        price_1h_ago = float(close.iloc[idx_1h])
        rise_1h = (price_now - price_1h_ago) / price_1h_ago * 100 if price_1h_ago > 0 else 0.0
        open_price = float(close.iloc[0])
        rise_from_open = (price_now - open_price) / open_price * 100 if open_price > 0 else 0.0

        # Stage 1：宽松预筛（base 阈值），不过线不触日线网络
        if rise_1h < threshold_pct and rise_from_open < threshold_pct * 1.5:
            return None

        # Stage 2：ATR 自适应精确判定（复用 drop 的 effective 公式，gap=0）
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
        atr_pct = signals.atr_pct if signals else 0.0
        effective = _effective_drop_threshold(atr_pct, 0.0)
        if rise_1h < effective and rise_from_open < effective * 1.5:
            return None

        return {
            "ticker": ticker, "market": market,
            "rise_1h": round(rise_1h, 2), "rise_from_open": round(rise_from_open, 2),
            "price_now": round(price_now, 4), "effective_threshold": effective,
            "signals": signals,
        }
    except Exception as e:
        print(f"[Alert] 大涨检查失败 {ticker}: {e}")
    return None


async def _send_drop_deepening_alert(ticker: str, market: str, drop_pct: float,
                                      price_now: float, prev_pct: float) -> None:
    """加深增量卡（零 LLM）：只报"比上次更深了多少 + 上张卡的建议/认错线还作数"。
    完整分析卡当日已发过，这里不重复叙事（06-12 七连击复盘 layer2）。"""
    from finance_agent.weekly.daily_followup import TICKER_NAMES
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)
    name = TICKER_NAMES.get(ticker, "")
    display = f"**{ticker}** {name}" if name else f"**{ticker}**"

    body = (f"📉 {display}　跌幅加深至 **{abs(drop_pct):.1f}%**"
            f"（今日上一档约 -{prev_pct:.1f}%）　现价 {_ccy(market)}{_fmt_px(price_now)}")
    # 带回当日完整卡给过的建议与认错线（查今天最近一条 dip 记录，查不到静默）
    try:
        from finance_agent.db.tracker import _conn, _resolve_db
        with _conn(_resolve_db(None)) as con:
            row = con.execute(
                "SELECT action, invalidation FROM dip_alerts WHERE ticker = ? "
                "AND date(alerted_at) = date('now') ORDER BY id DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        if row and (row["action"] or row["invalidation"]):
            body += (f"\n上一张完整卡：建议 **{row['action'] or '—'}**"
                     f"　认错线：{row['invalidation'] or '—'}（仍作数，触线再说）")
    except Exception:
        pass

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                              "content": f"📉 跌幅加深 · {ticker}（{market_label}）"},
                   "template": "orange"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "增量提醒：完整分析见今日早前卡片；建议未变化不重复推送"}]},
        ],
    }
    await send_feishu_card(card, fallback_text=_card_fallback_text(card))
    print(f"[Alert] 已推送跌幅加深增量卡：{ticker} {drop_pct:.1f}%")


async def _send_price_surge_alert(ticker: str, market: str, rise_1h: float,
                                   rise_from_open: float, price_now: float,
                                   cost_basis: float | None = None,
                                   signals=None, **_extra) -> None:
    """持仓大涨卡片（D8）：事实 + 卖飞签名上下文，止盈裁决留给 PM/用户。
    安全不变量：绝不输出'卖出/止盈'指令——用户签名是系统性卖早(+19.4%)，
    催卖卡片只会放大他最贵的毛病。"""
    from finance_agent.weekly.daily_followup import TICKER_NAMES
    market_label = {"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market)
    name = TICKER_NAMES.get(ticker, "")
    display = f"**{ticker}** {name}" if name else f"**{ticker}**"

    rise_label = (f"1小时 **+{rise_1h:.1f}%**" if rise_1h >= rise_from_open
                  else f"较开盘 **+{rise_from_open:.1f}%**")
    body = f"📈 {display}　{rise_label}　现价 {_ccy(market)}{_fmt_px(price_now)}"
    if cost_basis:
        pnl = (price_now - cost_basis) / cost_basis * 100
        body += f"\n持仓成本 {_ccy(market)}{_fmt_px(cost_basis)}，浮盈 **{pnl:+.1f}%**"

    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]

    # 卖飞签名上下文：该票实盘卖出记录 + 全局行为统计（有则展示，无则静默）
    ctx_lines = []
    try:
        from finance_agent.db.tracker import get_live_feedback
        fb = get_live_feedback(ticker)
        if fb and fb.get("sell") and fb["sell"]["avg_next"] > 0:
            sf = fb["sell"]
            ctx_lines.append(f"你过去对 {ticker} 卖出 {sf['n']} 次，"
                             f"后续7日平均 **+{sf['avg_next']:.1f}%**（卖早）")
    except Exception:
        pass
    try:
        from finance_agent.db.tracker import get_behavior_hint_stats
        bh = get_behavior_hint_stats()
        if bh and bh.get("n_sell_regret", 0) >= 2:
            ctx_lines.append(f"全部持仓口径：{bh['n_sell_regret']} 次卖出后涨了 "
                             f"{bh.get('sell_regret_pct', 5.0):g}%+")
    except Exception:
        pass
    if ctx_lines:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**⚠️ 止盈前先看你的卖飞签名**\n"
                                    + "\n".join(f"· {x}" for x in ctx_lines)}})

    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
        "content": "大涨提醒只报事实，不构成卖出指令；止盈/拿住建议看今晚日报 PM 裁决"}]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                              "content": f"📈 持仓大涨 · {ticker}（{market_label}）"},
                   "template": "green"},
        "elements": elements,
    }
    await send_feishu_card(card, fallback_text=_card_fallback_text(card))
    print(f"[Alert] 已推送持仓大涨：{ticker} +{max(rise_1h, rise_from_open):.1f}%")


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

    ACTION_EMOJI = {"加仓": "🟢 加仓", "持有观望": "⚪ 持有观望", "减仓": "🟠 减仓", "清仓离场": "🔴 清仓离场"}
    THESIS_EMOJI = {True: "✅ 持仓逻辑完好", False: "❌ 持仓逻辑可能破坏"}

    tech_ref = ""
    if signals:
        bb_lower_valid = signals.bb_lower >= price_now * 0.8
        bb_ref = (
            f"BB下轨：{signals.bb_lower:.2f}" if bb_lower_valid
            else f"MA20：{signals.ma20:.2f}（BB下轨失真，用MA20替代）"
        )
        vol_tag = f"　⚡高波动{signals.atr_pct:.0f}%" if signals.atr_pct > 8 else ""
        tech_ref = (
            f"\n{bb_ref}　ATR：{signals.atr:.2f}"
            f"　RSI：{signals.rsi:.0f}{vol_tag}"
        )

    # 发送时价 vs 检测时价：LLM 分析期间可能已回升
    if price_at_detection is not None and abs(price_now - price_at_detection) > 0.01:
        recheck_chg = (price_now - price_at_detection) / price_at_detection * 100
        chg_str = f"{'↗' if recheck_chg > 0 else '↘'} {recheck_chg:+.1f}%"
        price_line = (
            f"检测价：{_fmt_px(price_at_detection)}　发送时：**{_fmt_px(price_now)}**（{chg_str}）　1小时前：{_fmt_px(price_1h_ago)}"
        )
        if recheck_chg > 1.5:
            price_line = f"⚠️ 价格已回升，请以发送时价为准\n{price_line}"
    else:
        price_line = f"当前价：**{_ccy(market)}{_fmt_px(price_now)}**　1小时前：{_ccy(market)}{_fmt_px(price_1h_ago)}"
        # 检测时已从低点回升
        if recovering and recovery_pct >= 1.0:
            price_line += f"\n⚠️ 低点 {_fmt_px(low_price)}，已回升 **{recovery_pct:.1f}%**"
        elif recovery_pct >= 0.5:
            price_line += f"\n↗ 低点 {_fmt_px(low_price)}，已回升 {recovery_pct:.1f}%"

    # 跌幅标签：开盘触发时显示"较开盘"，否则显示"1小时内"
    if open_triggered and open_price:
        drop_label = f"较开盘跌 **{abs(drop_from_open):.2f}%**（开盘价 {_fmt_px(open_price)}）"
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
        action = analysis.get("action", "持有观望")
        action_reason = analysis.get("action_reason", "")
        drop_reason = analysis.get("drop_reason", "")
        one_line = analysis.get("one_line", "")
        add_trigger = analysis.get("add_trigger", "")
        add_limit = analysis.get("add_limit", "")
        invalidation = analysis.get("invalidation", "")
        impact_chain = analysis.get("impact_chain", {})
        shares_hint = analysis.get("shares_hint")

        # 持仓决策（加 / 持 / 减 / 离场）
        decision_content = (
            f"{THESIS_EMOJI.get(thesis_ok, '')}　建议动作：**{ACTION_EMOJI.get(action, action)}**\n"
            f"💡 {drop_reason}"
        )
        if action_reason:
            decision_content += f"\n📌 {action_reason}"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": decision_content},
        })
        elements.append({"tag": "hr"})

        # 链式影响分析
        if impact_chain:
            chain_lines = ["**🔗 影响链分析**"]
            if impact_chain.get("direct"):
                chain_lines.append(f"1️⃣ 直接原因：{impact_chain['direct']}")
            if impact_chain.get("spillover"):
                chain_lines.append(f"2️⃣ 传导风险：{impact_chain['spillover']}")
            if impact_chain.get("recovery"):
                chain_lines.append(f"3️⃣ 修复条件：{impact_chain['recovery']}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(chain_lines)},
            })
            elements.append({"tag": "hr"})

        # 加仓建议（仅 action=加仓 才展示具体加仓计划；加仓价已过自洽闸门校验 < 现价）
        if action == "加仓" and add_trigger:
            plan_lines = ["**📈 加仓建议**", f"触发：{add_trigger}"]
            if add_limit:
                plan_lines.append(f"上限：{add_limit}")
            if shares_hint:
                unit = "股（约1%风险预算）" if market == "hk" else "股"
                plan_lines.append(f"📐 **参考加仓股数：** {shares_hint} {unit}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(plan_lines)},
            })
            elements.append({"tag": "hr"})

        # 行为时机提示（order9）：仅 action=加仓 时展示，描述性不说教，闸门在 stats 函数内
        if action == "加仓":
            try:
                from finance_agent.db.tracker import (format_behavior_hint,
                                                      get_behavior_hint_stats)
                _bh = get_behavior_hint_stats()
                if _bh:
                    elements.append({
                        "tag": "note",
                        "elements": [{"tag": "plain_text",
                                      "content": format_behavior_hint(_bh)}],
                    })
            except Exception as bh_err:
                print(f"[Alert] 行为提示生成失败（跳过）: {bh_err}")

        # 认错离场信号（替代机械止损，可定性可定量）
        if invalidation:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"🚨 **认错离场信号：** {invalidation}"},
            })
            elements.append({"tag": "hr"})

        if one_line:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**一句话建议：** {one_line}"},
            })

    if not analysis:
        # 分析失败时不发"残卡"装完整（06-12 UX 扫描）：明示降级，把建议指回日报
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": "⚠️ AI 分析暂不可用，本卡仅报价格事实；操作建议以今晚日报为准"},
        })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "价格异动 · AI 辅助判断，不构成投资建议，请结合自身情况决策"}],
    })

    _act = analysis.get("action") if analysis else None
    is_add = _act == "加仓"
    HEADER_TPL = {"加仓": "green", "持有观望": "blue", "减仓": "orange", "清仓离场": "red"}
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                       "content": f"{'🟢 暴跌·可加仓' if is_add else '⚠️ 持仓异动'} · {ticker}（{market_label}）"},
            "template": HEADER_TPL.get(_act, "orange"),
        },
        "elements": elements,
    }
    await send_feishu_card(card, fallback_text=_card_fallback_text(card))
    act_tag = f" 动作={_act}" if analysis else ""
    print(f"[Alert] 已推送持仓异动：{ticker} 1h跌幅={abs(drop_pct):.2f}%{act_tag}")


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

    _etf_sectors = {"宽基ETF", "股息ETF"}
    holdings = [
        h for h in portfolio.get("holdings", []) + portfolio.get("watchlist", [])
        if h.get("sector", "") not in _etf_sectors and not h.get("is_dca", False)
    ]

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

            if pushed >= MAX_PUSH_PER_SCAN:
                print(f"[NewsScan] 单次推送达上限 {MAX_PUSH_PER_SCAN}，剩余高分新闻留待下轮")
                break
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
                    priced_in=result.get("priced_in", ""),
                    suggested_action=result.get("suggested_action", ""),
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
                sentiment = result.get("sentiment", "中性")
                gkey = _macro_global_key(news["published"], sentiment)
                if gkey in alerted:
                    print(f"[Alert] 港股宏观 与近30min同情绪宏观重复，跳过：{news['title'][:50]}")
                    _save_alerted(key, today_str)
                    continue
                await _send_macro_alert(
                    title=news["title"], published=news["published"], impact=impact,
                    sentiment=sentiment,
                    affected_sectors=result.get("affected_sectors", []),
                    reason=result.get("reason", ""),
                    priced_in=result.get("priced_in", ""),
                    suggested_action=result.get("suggested_action", ""),
                )
                alerted.add(key)
                alerted.add(slot_key)
                alerted.add(gkey)
                _save_alerted(key, today_str)
                _save_alerted(slot_key, today_str)
                _save_alerted(gkey, today_str)
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
                sentiment = result.get("sentiment", "中性")
                gkey = _macro_global_key(news["published"], sentiment)
                if gkey in alerted:
                    print(f"[Alert] 美股宏观 与近30min同情绪宏观重复，跳过：{news['title'][:50]}")
                    _save_alerted(key, today_str)
                    continue
                await _send_us_macro_alert(
                    title=news["title"], published=news["published"], impact=impact,
                    sentiment=sentiment,
                    affected_sectors=result.get("affected_sectors", []),
                    reason=result.get("reason", ""),
                    priced_in=result.get("priced_in", ""),
                    suggested_action=result.get("suggested_action", ""),
                )
                alerted.add(key)
                alerted.add(slot_key)
                alerted.add(gkey)
                _save_alerted(key, today_str)
                _save_alerted(slot_key, today_str)
                _save_alerted(gkey, today_str)
                pushed += 1
            else:
                print(f"[Alert] 美股宏观 影响度={impact}，跳过：{news['title'][:50]}")

    # 价格异动预警已由独立 price_alert workflow（run_price_scan，每10分钟）专责，
    # 此处不再重复扫描——两者去重 key 不互通（price_drop vs price_drop_10m），
    # 同时跑会导致同一次急跌被 news-scan 与 price-scan 各推一遍。

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
        if not _is_market_open(market):
            print(f"[PriceScan] {ticker} 跳过（{market} 市场已收盘）")
            continue
        drop_info = await loop.run_in_executor(
            None, _check_price_drop, ticker, market, threshold_pct
        )
        if drop_info and not item.get("shares"):
            # 观察池票（未持有）不发"持仓异动"卡：_analyze_dip 的 prompt 假设已持仓、
            # 会产出加仓建议+股数——对未持有票是误导性激进建议（06-12 自检 P1）。
            # 选股维度由日报机会区/影子池覆盖，与大涨方向只查持仓的语义对称。
            print(f"[PriceScan] {ticker} 观察池票跌幅触发（{drop_info['drop_pct']:+.1f}%），"
                  f"未持有不发持仓卡")
        elif drop_info:
            # 台阶去重（06-12 七连击复盘）：同日同票按跌幅台阶判信息增量，
            # 不创新台阶不推；加深一档推免 LLM 增量卡；剧烈恶化才重跑完整分析
            _eff = drop_info.get("effective_threshold", threshold_pct)
            _sig = drop_info.get("signals")
            step = _alert_step(_sig.atr_pct if _sig else 0.0)
            level = max(1, int(abs(drop_info["drop_pct"]) // step))
            lvl_prefix = f"price_drop_lvl:{ticker}:{today_str}:"
            prev_levels = [int(k.rsplit(":", 1)[1]) for k in alerted
                           if k.startswith(lvl_prefix)]
            # 跌+涨合并的每票每日总上限 4（06-12 UX 扫描：两方向 cap 独立时单票可推 6 张）
            n_other = sum(1 for k in alerted
                          if k.startswith(f"price_surge_lvl:{ticker}:{today_str}:"))
            decision = ("skip" if len(prev_levels) + n_other >= 4
                        else _escalation_decision(prev_levels, level))
            print(f"[PriceScan] {ticker} 触发（阈值 {_eff:.1f}%，台阶 {level}"
                  f"/步长 {step:.1f}%，决策 {decision}）")
            if decision == "skip":
                pass
            else:
                if decision == "full":
                    # 现取基准 1h 涨跌（06-12 UX 扫描：扫描开头取的值到此可能已陈旧数分钟）
                    _bm = "^HSI" if market == "hk" else "SPY"
                    mkt_drop = await loop.run_in_executor(None, _get_market_1h_drop, _bm)
                    analysis = await _analyze_dip(**drop_info, mkt_drop=mkt_drop)
                    # 发送前重取最新价，LLM 分析期间价格可能已变化
                    fresh_price = await loop.run_in_executor(None, _recheck_price, ticker, market)
                    price_at_detection = drop_info["price_now"]
                    if fresh_price is not None:
                        drop_info = {**drop_info, "price_now": fresh_price}
                        if analysis:
                            # 自洽闸门用发送价重跑（06-12 UX 扫描 P0）：LLM 分析期间
                            # 价格回升后，按检测价通过的 add_trigger 可能已高于现价
                            analysis, _w = _validate_dip_plan(analysis, fresh_price)
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
                else:  # light：一行增量卡，零 LLM
                    await _send_drop_deepening_alert(
                        ticker, market, drop_info["drop_pct"],
                        drop_info["price_now"],
                        prev_pct=max(prev_levels) * step)
                key = f"{lvl_prefix}{level}"
                alerted.add(key)
                _save_alerted(key, today_str)
                pushed += 1
        else:
            # 跌方向无异动 → 持仓票再查大涨方向（D8；观察池票不查，大涨提醒只服务止盈决策）
            surge_info = None
            if item.get("shares"):
                surge_info = await loop.run_in_executor(
                    None, _check_price_surge, ticker, market, threshold_pct
                )
            if surge_info:
                # 涨方向同款台阶去重（surge 卡本就免 LLM，light/full 同卡）
                _ssig = surge_info.get("signals")
                s_step = _alert_step(_ssig.atr_pct if _ssig else 0.0)
                s_rise = max(surge_info["rise_1h"], surge_info["rise_from_open"])
                s_level = max(1, int(s_rise // s_step))
                s_prefix = f"price_surge_lvl:{ticker}:{today_str}:"
                s_prev = [int(k.rsplit(":", 1)[1]) for k in alerted
                          if k.startswith(s_prefix)]
                n_other_d = sum(1 for k in alerted
                                if k.startswith(f"price_drop_lvl:{ticker}:{today_str}:"))
                if (len(s_prev) + n_other_d >= 4
                        or _escalation_decision(s_prev, s_level) == "skip"):
                    print(f"[PriceScan] {ticker} 大涨未创当日新台阶（{s_level}），跳过")
                else:
                    await _send_price_surge_alert(
                        **surge_info, cost_basis=item.get("cost_basis"))
                    s_key = f"{s_prefix}{s_level}"
                    alerted.add(s_key)
                    _save_alerted(s_key, today_str)
                    pushed += 1
            else:
                print(f"[PriceScan] {ticker} 无异动（预筛：±{threshold_pct}%/1h 与开盘口径均未过；或未过 ATR 自适应阈值）")

    if pushed == 0:
        print(f"[PriceScan] 本次无价格异动")
    return pushed
