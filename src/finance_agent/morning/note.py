# src/finance_agent/morning/note.py
"""
盘前晨报生成：每个交易日北京时间早上 9 点运行。

与日报（逐股裁决）不同，晨报聚焦"隔夜全球市场复盘 + 今日焦点 + 持仓影响"，
数据来源是 Claude 联网搜索（WebSearch），而非 yfinance/数据库。

流程：
  1. 读 portfolio.yaml 获取用户持仓（用于个性化"持仓影响"段落）
  2. 调 claude_websearch_chat 让模型联网搜索隔夜行情/事件，产出结构化 JSON
  3. 解析 JSON → 构建飞书卡片（按市场情绪着色）

输出：(飞书卡片 dict, 纯文本 fallback, note dict)
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from finance_agent.agents.claude_client import claude_websearch_chat, strip_markdown


MORNING_SYSTEM = """你是"卡门智投"的盘前晨报分析师，服务一位持有港股 + 美股的个人投资者（风格：结论先行、能承受波动、追求超额收益，但需要风险提示）。

每个交易日北京时间早上 9 点，你要联网搜索最新市场动态，产出一份"盘前晨报"。

【工作流程】（必须真实联网搜索，禁止凭记忆编造数字）
1. 用 WebSearch 搜索隔夜美股收盘到此刻的关键动态：
   - 三大指数（道指/标普500/纳斯达克）+ 费城半导体指数 当日涨跌幅
   - 10年期美债收益率、美元指数走向
   - 主导市场情绪的关键宏观数据或事件（非农/CPI/FOMC/重磅财报等）
2. 搜索与用户持仓直接相关的板块/个股新闻：半导体与 AI 算力、港股科技（腾讯等）、黄金与避险资产
3. 综合成一份晨报

【输出要求】
- 只输出一个 JSON 对象，不要任何前后缀文字，不要 markdown 代码块包裹
- 所有数字、涨跌幅必须来自真实搜索结果；确实搜不到的字段填"暂无数据"，绝不编造
- 语气口语化，像私人投顾当面讲，结论先行
- holdings_impact 要点名用户的具体持仓（用其代码），讲清今日影响方向
- trade_ideas 每条给出"标的 + 动作 + 一句理由"，3-5 条
- sources 给出你真正参考的新闻链接

【JSON schema】
{
  "sentiment": "risk_off | neutral | risk_on（今日市场整体风险偏好）",
  "headline": "一句话核心判断，不超过 30 字",
  "overnight_macro": "隔夜宏观综述，含主要指数涨跌幅+美债收益率+关键数据，3-5 句",
  "trigger_event": "今日主导市场情绪的关键事件及其影响链，2-4 句",
  "holdings_impact": "针对用户实际持仓的影响分析，点名具体代码，可用换行分美股/港股",
  "hedge_note": "黄金/对冲视角，1-3 句",
  "trade_ideas": ["建议1", "建议2", "建议3"],
  "calendar": "本周需关注的关键事件或数据，1-3 句",
  "sources": [{"title": "标题", "url": "链接"}]
}"""

MORNING_USER = """今天是 {date}（北京时间，{weekday}）。

用户当前持仓：
{holdings}

对冲桶状态：{hedge_status}

请先联网搜索隔夜全球市场动态，再产出今日盘前晨报 JSON。"""


_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _beijing_now() -> datetime:
    """当前北京时间（UTC+8）"""
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _load_portfolio(config_path: str = "config/portfolio.yaml") -> dict:
    with open(Path(config_path)) as f:
        return yaml.safe_load(f) or {}


def _format_holdings(portfolio: dict) -> str:
    """把持仓按市场分组，格式化成 prompt 可读的文本"""
    holdings = portfolio.get("holdings", [])
    us_lines, hk_lines, cn_lines = [], [], []
    for h in holdings:
        ticker = h.get("ticker", "?")
        sector = h.get("sector", "")
        cost = h.get("cost_basis")
        market = h.get("market", "us")
        cur = {"us": "$", "hk": "HK$", "cn": "¥"}.get(market, "")
        cost_str = f"成本{cur}{cost}" if cost else ""
        line = f"  {ticker}（{sector}，{cost_str}）"
        if market == "hk":
            hk_lines.append(line)
        elif market == "cn":
            cn_lines.append(line)
        else:
            us_lines.append(line)

    blocks = []
    if us_lines:
        blocks.append("美股：\n" + "\n".join(us_lines))
    if hk_lines:
        blocks.append("港股：\n" + "\n".join(hk_lines))
    if cn_lines:
        blocks.append("A股：\n" + "\n".join(cn_lines))
    return "\n".join(blocks) if blocks else "（暂无持仓记录）"


def _hedge_status(portfolio: dict) -> str:
    """判断对冲桶是否已建仓"""
    holdings = portfolio.get("holdings", [])
    held = {h.get("ticker", "").upper() for h in holdings}
    hedge_tickers = {"IAU", "BIL", "GLD", "SGOV"}
    have = held & hedge_tickers
    if have:
        return f"已持有对冲资产：{', '.join(sorted(have))}"
    return "对冲桶尚未建仓（目标 IAU 黄金 + BIL 短期美债）"


def _parse_note_json(raw: str) -> dict:
    """从模型输出中稳健地抽取 JSON 对象"""
    text = strip_markdown(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到最后一个 }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("晨报模型输出无法解析为 JSON")


_SENTIMENT_STYLE = {
    "risk_off": ("red", "🔴 避险（risk-off）"),
    "neutral":  ("blue", "🔵 中性"),
    "risk_on":  ("green", "🟢 偏多（risk-on）"),
}


def _build_morning_card(note: dict, date_str: str) -> dict:
    """把晨报 dict 渲染成飞书 interactive 卡片"""
    sentiment = note.get("sentiment", "neutral")
    template, sentiment_label = _SENTIMENT_STYLE.get(
        sentiment, _SENTIMENT_STYLE["neutral"]
    )

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{note.get('headline', '')}**\n\n市场情绪：{sentiment_label}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"🌍 **隔夜宏观**\n\n{note.get('overnight_macro', '暂无数据')}",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"⚡ **今日焦点**\n\n{note.get('trigger_event', '暂无数据')}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"💼 **你的持仓影响**\n\n{note.get('holdings_impact', '暂无数据')}",
            },
        },
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"🛡️ **对冲 / 黄金**\n\n{note.get('hedge_note', '暂无数据')}",
            },
        },
        {"tag": "hr"},
    ]

    # 今日建议（列点）
    ideas = note.get("trade_ideas", []) or []
    ideas_md = "\n".join(f"• {x}" for x in ideas) if ideas else "暂无"
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"🎯 **今日建议**\n\n{ideas_md}"},
    })

    # 关键日历
    if note.get("calendar"):
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📅 **关键日历**\n\n{note['calendar']}"},
        })

    elements.append({"tag": "hr"})

    # 来源 + 免责声明
    sources = note.get("sources", []) or []
    src_parts = []
    for s in sources[:6]:
        if isinstance(s, dict):
            title = s.get("title", "来源")
            url = s.get("url", "")
            if url:
                src_parts.append(f"[{title}]({url})")
        elif isinstance(s, str):
            src_parts.append(s)
    src_md = " · ".join(src_parts) if src_parts else "—"
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "lark_md", "content": f"📎 来源：{src_md}"},
        ],
    })
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text",
             "content": "以上为 AI 联网搜索辅助晨报，不构成投资建议"},
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🌅 卡门智投 · 盘前晨报 {date_str}"},
            "template": template,
        },
        "elements": elements,
    }


def _build_plain_text(note: dict, date_str: str) -> str:
    """纯文本版（飞书失败时邮件兜底用）"""
    lines = [
        f"🌅 卡门智投 · 盘前晨报 {date_str}",
        f"【核心判断】{note.get('headline', '')}",
        "",
        f"【隔夜宏观】{note.get('overnight_macro', '')}",
        f"【今日焦点】{note.get('trigger_event', '')}",
        f"【持仓影响】{note.get('holdings_impact', '')}",
        f"【对冲/黄金】{note.get('hedge_note', '')}",
        "",
        "【今日建议】",
    ]
    for x in note.get("trade_ideas", []) or []:
        lines.append(f"  • {x}")
    if note.get("calendar"):
        lines.append(f"\n【关键日历】{note['calendar']}")
    return "\n".join(lines)


async def run_morning_note(
    config_path: str = "config/portfolio.yaml",
    timeout: int = 300,
) -> tuple[dict, str, dict]:
    """
    生成盘前晨报，返回 (飞书卡片 dict, 纯文本 fallback, note dict)。
    """
    now = _beijing_now()
    date_str = now.strftime("%Y-%m-%d")
    weekday = _WEEKDAY_CN[now.weekday()]

    portfolio = _load_portfolio(config_path)
    holdings_str = _format_holdings(portfolio)
    hedge_status = _hedge_status(portfolio)

    user_msg = MORNING_USER.format(
        date=date_str, weekday=weekday,
        holdings=holdings_str, hedge_status=hedge_status,
    )

    raw = await claude_websearch_chat(MORNING_SYSTEM, user_msg, timeout=timeout)
    try:
        note = _parse_note_json(raw)
    except ValueError:
        # 降级而非崩溃（06-12 UX 扫描 P1）：原 raise 会让 launchd 任务静默挂掉，
        # 用户整天没晨报也不知道为什么。退化为"原文卡"——有总比没有强。
        print("[MorningNote] ⚠️ JSON 解析失败，降级推送模型原文")
        degraded = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                  "content": f"🌅 晨报（降级版·结构化失败）· {date_str}"},
                       "template": "grey"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": raw[:2800]}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                    "content": "模型输出未能结构化，以上为原文；功能异常已记录"}]},
            ],
        }
        return degraded, raw[:2000], {}

    card = _build_morning_card(note, date_str)
    text = _build_plain_text(note, date_str)
    return card, text, note
