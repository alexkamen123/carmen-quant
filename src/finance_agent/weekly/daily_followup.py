# src/finance_agent/weekly/daily_followup.py
"""
周二到周五的轻量跟进：
  - 读取周一完整报告（data/weekly_latest.json）
  - 拉取推荐品种当前价格，计算周内涨跌幅
  - 重跑技术初筛，判断机会是否还在
  - Claude 给出 2-3 句跟进意见
  - 发飞书卡片（带股票名称注解，视觉分区清晰）
"""
import json
from datetime import date
from pathlib import Path

import yaml
import yfinance as yf
import pandas as pd

from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.weekly.allocation_advisor import CANDIDATE_POOL, _quick_screen
import asyncio

# 股票代码 → 中文名称注解
TICKER_NAMES: dict[str, str] = {
    # 当前持仓
    "NVDA": "英伟达（GPU/AI芯片）",
    "GOOGL": "谷歌（AI搜索/云）",
    "MU": "美光科技（内存芯片）",
    "QQQM": "纳指100 ETF",
    "VOO": "标普500 ETF",
    "SCHD": "美股股息 ETF",
    "DRAM": "内存半导体 ETF",
    "00700": "腾讯控股",
    "03032": "恒生科技 ETF",
    # 常见推荐/观察标的
    "KWEB": "中国互联网 ETF",
    "TLT": "美国长期国债 ETF",
    "XLU": "公用事业 ETF",
    "VWO": "新兴市场 ETF",
    "IAU": "黄金 ETF（iShares）",
    "GLD": "黄金 ETF（SPDR）",
    "SHY": "短期美债 ETF",
    "SGOV": "超短期美债 ETF",
    "EMCX": "新兴市场科技 ETF",
    "XLV": "医疗健康 ETF",
    "META": "Meta（社交/广告）",
    "JNJ": "强生（医疗健康）",
    "MSFT": "微软（云/AI）",
    "CRM": "Salesforce（企业软件）",
    "PLTR": "Palantir（AI数据分析）",
    "AMD": "超威半导体",
    "INTC": "英特尔",
    "AMZN": "亚马逊",
    "TSLA": "特斯拉",
    "AAPL": "苹果",
    "TSM": "台积电（代工）",
    "AMAT": "应用材料（设备）",
    "2840": "安硕纳指100 ETF（港）",
    "2840.HK": "安硕纳指100 ETF（港）",
}


def _ticker_label(ticker: str) -> str:
    """返回 '**TICKER** 名称' 格式，无名称时只返回加粗 ticker。"""
    name = TICKER_NAMES.get(ticker, "")
    return f"**{ticker}** {name}" if name else f"**{ticker}**"


def _pct_emoji(pct: float) -> str:
    if pct > 0.5:
        return "📈"
    elif pct < -0.5:
        return "📉"
    return "➡️"


def build_followup_card(
    comment: str,
    holdings_perf: list[dict],
    price_changes: list[dict],
    new_opps: list[dict],
    today_str: str,
) -> dict:
    """构建每日跟进飞书交互卡片。"""
    elements: list[dict] = []

    # ── AI 跟进意见 ────────────────────────────────────────────────────────
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": comment.strip()},
    })
    elements.append({"tag": "hr"})

    # ── 持仓本周表现 ───────────────────────────────────────────────────────
    if holdings_perf:
        lines = ["**📊 持仓本周表现**（周一→今日）"]
        for c in holdings_perf:  # sorted ascending by pct_change (worst first)
            sign = "+" if c["pct_change"] >= 0 else ""
            line = f"{_pct_emoji(c['pct_change'])} {_ticker_label(c['ticker'])}　{sign}{c['pct_change']}%"
            pnl = c.get("unrealized_pnl")
            if pnl is not None:
                pnl_sign = "+" if pnl >= 0 else ""
                line += f"　持仓浮盈 {pnl_sign}{pnl:.1f}%"
            lines.append(line)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        elements.append({"tag": "hr"})

    # ── 推荐品种动态 ───────────────────────────────────────────────────────
    if price_changes:
        lines = ["**🔭 推荐品种动态**（周一→今日）"]
        for c in sorted(price_changes, key=lambda x: x["pct_change"]):
            sign = "+" if c["pct_change"] >= 0 else ""
            lines.append(
                f"{_pct_emoji(c['pct_change'])} {_ticker_label(c['ticker'])}"
                f"　{c['price_start']} → {c['price_now']}　{sign}{c['pct_change']}%"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
        elements.append({"tag": "hr"})

    # ── 新增超卖信号 ───────────────────────────────────────────────────────
    if new_opps:
        lines = ["**⚠️ 新增超卖信号**（RSI < 48）"]
        for c in new_opps[:5]:
            lines.append(f"🔻 {_ticker_label(c['ticker'])}　RSI = {c['rsi']}")
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    else:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "✅ **超卖信号：** 暂无新增"},
        })

    # ── 免责声明 ───────────────────────────────────────────────────────────
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "以上为 AI 辅助参考，不构成投资建议，请结合自身风险偏好决策"}],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📌 卡门智投 · {today_str} 配置跟进"},
            "template": "turquoise",
        },
        "elements": elements,
    }


FOLLOWUP_SYSTEM = """你是家庭投资组合的每日跟进助手。
基于本周周一的配置建议，结合今日的价格变动，给出简短跟进意见。

要求：
- 纯文字，2-4 句话，不超过 120 字
- 先说持仓里本周涨跌最显著的 1-2 只
- 说明推荐品种机会是否还在/是否已错过
- 是否有新增超卖机会需要关注
- 语气轻松，像朋友提醒而非正式报告"""

FOLLOWUP_USER = """周一（{report_date}）的配置建议摘要：
{recommendation_summary}

持仓本周表现（周一→今日）：
{holdings_changes}

推荐品种本周表现：
{price_changes}

今日技术初筛新增机会：{new_opportunities}

请给出 2-4 句跟进意见，重点说持仓里变化最大的标的和机会是否还在。"""


def _fetch_holdings_performance() -> list[dict]:
    """拉取实际持仓本周一到今日的价格变化，附带未实现盈亏。"""
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    results = []
    for item in portfolio.get("holdings", []):
        ticker = item["ticker"]
        market = item.get("market", "us")
        shares = item.get("shares", 0)
        cost = item.get("cost_basis", 0)
        try:
            yf_ticker = f"{int(ticker):04d}.HK" if market == "hk" else ticker
            df = yf.download(yf_ticker, period="7d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            close = df["close"].dropna()
            price_start = float(close.iloc[0])
            price_now = float(close.iloc[-1])
            pct = (price_now / price_start - 1) * 100
            # cost_basis 与 price 同币种，直接比较
            unrealized_pnl = (price_now - cost) / cost * 100 if cost else 0
            results.append({
                "ticker": ticker,
                "pct_change": round(pct, 1),
                "price_now": round(price_now, 2),
                "unrealized_pnl": round(unrealized_pnl, 1),
            })
        except Exception as e:
            print(f"[Followup] {ticker} 持仓价格拉取失败: {e}")
    return sorted(results, key=lambda x: x["pct_change"])


def _fetch_price_changes(tickers: list[str]) -> list[dict]:
    """拉取本周一到今日的价格变化。"""
    changes = []
    for ticker in tickers:
        try:
            yf_ticker = f"{int(ticker):04d}.HK" if ticker.isdigit() else ticker
            df = yf.download(yf_ticker, period="7d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            close = df["close"].dropna()
            price_start = float(close.iloc[0])
            price_now = float(close.iloc[-1])
            pct = (price_now / price_start - 1) * 100
            changes.append({
                "ticker": ticker,
                "price_start": round(price_start, 2),
                "price_now": round(price_now, 2),
                "pct_change": round(pct, 1),
            })
        except Exception as e:
            print(f"[Followup] {ticker} 价格拉取失败: {e}")
    return changes


async def run_daily_followup() -> dict | None:
    """
    执行每日跟进逻辑。
    返回 {"card": ..., "text": ...}，或 None（若无周报数据则跳过）。
    card 用于飞书卡片推送，text 用于控制台打印。
    """
    weekly_path = Path("data/weekly_latest.json")
    if not weekly_path.exists():
        print("[Followup] 未找到 weekly_latest.json，跳过今日跟进")
        return None

    with open(weekly_path) as f:
        weekly = json.load(f)

    report_date = weekly.get("date", "本周一")
    watch_tickers = weekly.get("watch_tickers", [])
    hedge_instruments = weekly.get("hedge_instruments", [])
    opportunities = weekly.get("opportunities", [])

    # ── 持仓本周表现 ──
    loop = asyncio.get_event_loop()
    holdings_perf = await loop.run_in_executor(None, _fetch_holdings_performance)
    holdings_str = "  ".join(
        f"{c['ticker']} {'+' if c['pct_change'] >= 0 else ''}{c['pct_change']}%"
        for c in holdings_perf
    ) if holdings_perf else "（无法获取）"

    # ── 推荐品种价格变化 ──
    price_changes = await loop.run_in_executor(None, _fetch_price_changes, watch_tickers)
    price_change_str = "\n".join(
        f"  {c['ticker']}：{c['price_start']} → {c['price_now']} "
        f"（{'+' if c['pct_change'] >= 0 else ''}{c['pct_change']}%）"
        for c in sorted(price_changes, key=lambda x: x["pct_change"])
    ) if price_changes else "（无法获取价格数据）"

    # ── 今日快速初筛，看是否有新机会 ──
    all_new: list[dict] = []
    for category, tickers in CANDIDATE_POOL.items():
        market = "hk" if any(t.isdigit() for t in tickers) else "us"
        screened = await loop.run_in_executor(None, _quick_screen, tickers, market)
        all_new.extend(screened)

    existing_tickers = set(watch_tickers)
    new_opps = [c for c in all_new if c["ticker"] not in existing_tickers]
    new_opp_str = (
        "、".join(f"{c['ticker']}（RSI={c['rsi']}）" for c in new_opps[:3])
        if new_opps else "暂无新增超卖机会"
    )

    # ── 推荐摘要（供 LLM 参考）──
    hedge_summary_parts = [
        f"{block['direction']}→{'/'.join(ins['ticker'] for ins in block.get('instruments', []))}"
        for block in hedge_instruments
    ]
    opp_tickers = [op["ticker"] for op in opportunities]
    rec_summary = (
        f"对冲方向：{' | '.join(hedge_summary_parts)}\n"
        f"机会标的：{', '.join(opp_tickers) or '无'}"
    )

    # ── Claude 跟进意见 ──
    user_msg = FOLLOWUP_USER.format(
        report_date=report_date,
        recommendation_summary=rec_summary,
        holdings_changes=holdings_str,
        price_changes=price_change_str,
        new_opportunities=new_opp_str,
    )
    try:
        if has_claude_cli():
            comment = await claude_cli_chat(FOLLOWUP_SYSTEM, user_msg, timeout=60)
        else:
            raise RuntimeError("无 Claude CLI")
    except Exception as e:
        print(f"[Followup] Claude 失败，降级 DeepSeek: {e}")
        comment = await deepseek_chat(FOLLOWUP_SYSTEM, user_msg)

    today_str = date.today().strftime("%m/%d")

    card = build_followup_card(
        comment=comment,
        holdings_perf=holdings_perf,
        price_changes=price_changes,
        new_opps=new_opps,
        today_str=today_str,
    )

    text = (
        f"📌 卡门智投 · {today_str} 配置跟进\n\n"
        f"{comment.strip()}\n\n"
        f"持仓本周：{holdings_str}\n"
        f"推荐品种：\n{price_change_str}\n"
        f"新增超卖：{new_opp_str}"
    )

    return {"card": card, "text": text}
