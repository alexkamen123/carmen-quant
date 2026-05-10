# src/finance_agent/weekly/allocation_advisor.py
"""
周度配置建议：三步流程
  Step 1 配置诊断 → 当前持仓集中度 + 建议对冲方向
  Step 2 对冲选品 → 针对每个方向推荐具体品种
  Step 3 机会筛选 → 从候选池中找低估/超卖标的

所有 Claude/DeepSeek 调用均通过 claude_client / bull_agent 共享模块。
"""
import asyncio
import json
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
import yfinance as yf

from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli, strip_markdown
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.data.macro import fetch_macro_context


async def _claude_json(system: str, user: str, timeout: int = 120,
                       array: bool = False) -> str:
    """优先 Claude，失败降级 DeepSeek；返回原始字符串供调用方解析。"""
    try:
        if has_claude_cli():
            return strip_markdown(await claude_cli_chat(system, user, timeout=timeout))
        raise RuntimeError("无 Claude CLI")
    except Exception as e:
        print(f"[AllocationAdvisor] Claude 失败，降级 DeepSeek: {e}")
        return await deepseek_chat(system, user)


# ── Prompts ──────────────────────────────────────────────────────────────────

DIAG_SYSTEM = """你是一位资产配置顾问，专注家庭财富管理。
基于持仓集中度和宏观背景，给出简洁的配置诊断与对冲方向建议。

输出格式（严格 JSON，不要有其他内容）：
{
  "concentration_risk": "一句话说明当前最大集中风险",
  "macro_risk": "一句话说明当前宏观最大风险",
  "hedge_directions": [
    {
      "direction": "方向名称（如：美债/防御股/黄金/新兴市场）",
      "rationale": "为什么这个方向能对冲（一句话）",
      "urgency": "高" | "中" | "低"
    }
  ]
}
hedge_directions 给 2-4 个方向，urgency 高的排前面。"""

DIAG_USER = """【当前持仓集中度】
{sector_summary}

【宏观背景】
{macro_summary}

【持仓明细】
{holdings_str}

请诊断当前配置的主要风险，并给出 2-4 个值得关注的对冲/分散方向。"""

HEDGE_SYSTEM = """你是一位专注 ETF 和全球资产配置的投顾。
针对每个对冲方向，推荐 1-2 个具体的可交易品种（优先美股 ETF，兼顾港股可买品种）。

输出格式（严格 JSON 数组，不要有其他内容）：
[
  {
    "direction": "对冲方向名称",
    "instruments": [
      {
        "ticker": "代码",
        "name": "名称",
        "market": "us" | "hk",
        "rationale": "为什么选这个（一句话）",
        "entry_hint": "参考买入逻辑（一句话，不要预测具体价格）"
      }
    ]
  }
]"""

HEDGE_USER = """当前宏观环境：{macro_summary}

针对以下对冲方向，推荐具体可交易品种：

{directions_str}

要求：
- 优先美股 ETF（流动性好、费率低）
- 港股可考虑南向 ETF 或 ADR
- 避免单一小市值股票，优先指数产品
- 每个方向最多推荐 2 个品种"""

SCREEN_SYSTEM = """你是一位量化选股分析师，专注寻找超卖但基本面扎实的机会。
根据提供的候选标的数据，筛选出最值得关注的 3-5 只。

输出格式（严格 JSON 数组）：
[
  {
    "ticker": "代码",
    "reason": "选中理由（技术面+基本面各一点，不超过 40 字）",
    "signal_strength": "强" | "中",
    "suggested_position": "轻仓试探（<3%）" | "标准配置（3-5%）"
  }
]
如果候选池质量普遍一般，返回空数组 []。"""

SCREEN_USER = """以下是从市场中初筛的候选标的（RSI<45，有一定基本面支撑）：

{candidates_str}

当前宏观环境：{macro_summary}

请从中选出 3-5 只最值得关注的机会，说明理由。"""


# ── 候选池定义（可扩展）────────────────────────────────────────────────────

# 各类别候选品种，weekly screening 会从中筛选超卖标的
CANDIDATE_POOL = {
    "半导体": ["AMAT", "ASML", "TSM", "AMD", "INTC", "QCOM"],
    "AI/云计算": ["MSFT", "META", "AMZN", "CRM", "SNOW", "PLTR"],
    "消费/防御": ["COST", "WMT", "PG", "JNJ", "KO"],
    "新兴市场ETF": ["EEM", "VWO", "KWEB", "MCHI"],
    "债券/固收ETF": ["TLT", "IEF", "BND", "AGG", "HYG"],
    "黄金/大宗": ["GLD", "SLV", "IAU", "DBA"],
    "港股科技": ["09988", "01810", "09618"],  # BABA, 小米, JD.HK
}


# ── 技术面快速评估 ──────────────────────────────────────────────────────────

def _quick_screen(tickers: list[str], market: str = "us") -> list[dict]:
    """
    对候选池做技术面 + 基本面双重筛选：
      技术面：RSI < 45 且成交量未大幅萎缩
      基本面（防价值陷阱）：
        - 非 ETF 品种：排除 PE > 60 且营收增速 < 5% 的标的（贵且不增长）
        - 允许亏损成长股（PE=None 时不排除）
        - 排除营收连续下滑（revenueGrowth < -20%）的衰退型公司
    """
    results = []
    for ticker in tickers:
        try:
            yf_ticker = f"{int(ticker):04d}.HK" if ticker.isdigit() else ticker
            df = yf.download(yf_ticker, period="65d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 20:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]

            close = df["close"].dropna()
            if len(close) < 15:
                continue

            # RSI-14
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi = (100 - 100 / (1 + rs)).iloc[-1]

            if rsi > 45:
                continue  # 不超卖，跳过

            # 成交量（近5日均量 vs 前20日均量，排除量能大幅萎缩）
            vol = df["volume"].dropna()
            avg_recent = vol.iloc[-5:].mean()
            avg_prior = vol.iloc[-20:-5].mean()
            if avg_prior > 0 and avg_recent / avg_prior < 0.5:
                continue  # 量能萎缩超过 50%，缺乏支撑

            # ── 基本面过滤（ETF 跳过，纯技术判断即可）──────────────
            is_etf = any(etf in ticker for etf in ["ETF", "TLT", "IEF", "BND", "AGG",
                                                     "HYG", "EEM", "VWO", "GLD", "SLV",
                                                     "IAU", "DBA", "KWEB", "MCHI"])
            fundamentals_note = ""
            if not is_etf:
                try:
                    info = yf.Ticker(yf_ticker).info
                    pe = info.get("trailingPE")
                    rev_growth = info.get("revenueGrowth")  # 小数，如 0.15 = 15%

                    # 排除"贵且不增长"：PE > 60 且营收增速 < 5%
                    if pe and pe > 60 and (rev_growth is None or rev_growth < 0.05):
                        continue

                    # 排除营收严重下滑（> -20%）的衰退型公司
                    if rev_growth is not None and rev_growth < -0.20:
                        continue

                    # 记录基本面概况
                    parts = []
                    if pe:
                        parts.append(f"PE={pe:.0f}x")
                    if rev_growth is not None:
                        parts.append(f"营收增速={rev_growth*100:.0f}%")
                    fundamentals_note = " | ".join(parts)
                except Exception:
                    pass  # info 拉取失败则不排除，让 Claude 自己判断

            # 52周内回撤
            high_52w = close.rolling(252, min_periods=len(close)).max().iloc[-1]
            drawdown = (close.iloc[-1] / high_52w - 1) * 100

            results.append({
                "ticker": ticker,
                "market": market,
                "close": round(float(close.iloc[-1]), 2),
                "rsi": round(float(rsi), 1),
                "drawdown_from_high": round(float(drawdown), 1),
                "fundamentals": fundamentals_note,
            })
        except Exception as e:
            print(f"[Screen] {ticker} 筛选失败: {e}")
    return results


# ── 三步主流程 ────────────────────────────────────────────────────────────────

async def run_allocation_advisor() -> dict[str, Any]:
    """
    执行完整三步配置建议流程，返回结构化结果供飞书卡片使用。
    """
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    holdings = portfolio.get("holdings", [])

    # ── 计算持仓集中度 ──
    sector_value: dict[str, float] = {}
    total_value = 0.0
    holdings_lines = []
    for h in holdings:
        ticker = h["ticker"]
        market = h.get("market", "us")
        shares = h.get("shares", 0)
        sector = h.get("sector", "其他")
        yf_ticker = f"{int(ticker):04d}.HK" if market == "hk" and ticker.isdigit() else ticker
        try:
            price_df = yf.download(yf_ticker, period="2d", progress=False, auto_adjust=True)
            if not price_df.empty:
                if isinstance(price_df.columns, pd.MultiIndex):
                    price_df.columns = [c[0].lower() for c in price_df.columns]
                else:
                    price_df.columns = [c.lower() for c in price_df.columns]
                price = float(price_df["close"].iloc[-1])
            else:
                price = h.get("cost_basis", 0)
        except Exception:
            price = h.get("cost_basis", 0)
        value = shares * price
        sector_value[sector] = sector_value.get(sector, 0.0) + value
        total_value += value
        holdings_lines.append(
            f"  {ticker}（{sector}）{shares}股 × {price:.2f} ≈ {value:.0f}"
        )

    sector_summary = " | ".join(
        f"{k} {v / total_value * 100:.0f}%"
        for k, v in sorted(sector_value.items(), key=lambda x: x[1], reverse=True)
    ) if total_value > 0 else "暂无数据"

    # ── 宏观背景 ──
    macro = await fetch_macro_context()
    macro_summary = macro.to_prompt_str()

    # ── Step 1: 配置诊断 ──
    print("[AllocationAdvisor] Step 1: 配置诊断...")
    diag_user = DIAG_USER.format(
        sector_summary=sector_summary,
        macro_summary=macro_summary,
        holdings_str="\n".join(holdings_lines),
    )
    raw = await _claude_json(DIAG_SYSTEM, diag_user, timeout=90)
    start, end = raw.find("{"), raw.rfind("}") + 1
    diagnosis: dict = json.loads(raw[start:end]) if start >= 0 else {}
    hedge_directions = diagnosis.get("hedge_directions", [])

    # ── Step 2: 对冲选品（Claude，需要宏观判断能力）──
    print("[AllocationAdvisor] Step 2: 对冲选品...")
    directions_str = "\n".join(
        f"- {d['direction']}（urgency={d.get('urgency', '中')}）：{d.get('rationale', '')}"
        for d in hedge_directions
    )
    hedge_user = HEDGE_USER.format(
        directions_str=directions_str,
        macro_summary=macro_summary,
    )
    hedge_instruments: list[dict] = []
    try:
        raw2 = await _claude_json(HEDGE_SYSTEM, hedge_user, timeout=90, array=True)
        start2, end2 = raw2.find("["), raw2.rfind("]") + 1
        hedge_instruments = json.loads(raw2[start2:end2]) if start2 >= 0 else []
    except Exception as e:
        print(f"[AllocationAdvisor] 对冲选品解析失败: {e}")

    # ── Step 3: 机会筛选（技术初筛 → Claude 精选）──
    print("[AllocationAdvisor] Step 3: 市场机会筛选...")
    all_candidates: list[dict] = []
    loop = asyncio.get_event_loop()
    for category, tickers in CANDIDATE_POOL.items():
        market = "hk" if any(t.isdigit() for t in tickers) else "us"
        screened = await loop.run_in_executor(None, _quick_screen, tickers, market)
        for c in screened:
            c["category"] = category
        all_candidates.extend(screened)

    print(f"[AllocationAdvisor] 初筛通过 {len(all_candidates)} 只候选标的")

    opportunities: list[dict] = []
    if all_candidates:
        candidates_str = "\n".join(
            f"  {c['ticker']}（{c['category']}）现价={c['close']}，RSI={c['rsi']}，"
            f"较高点回撤={c['drawdown_from_high']}%"
            + (f"，{c['fundamentals']}" if c.get("fundamentals") else "")
            for c in all_candidates
        )
        screen_user = SCREEN_USER.format(
            candidates_str=candidates_str,
            macro_summary=macro_summary,
        )
        try:
            raw3 = await _claude_json(SCREEN_SYSTEM, screen_user, timeout=90, array=True)
            start3, end3 = raw3.find("["), raw3.rfind("]") + 1
            opportunities = json.loads(raw3[start3:end3]) if start3 >= 0 else []
        except Exception as e:
            print(f"[AllocationAdvisor] 机会筛选解析失败: {e}")

    result: dict[str, Any] = {
        "date": __import__("datetime").date.today().isoformat(),
        "sector_summary": sector_summary,
        "macro_summary": macro_summary,
        "diagnosis": diagnosis,
        "hedge_instruments": hedge_instruments,
        "opportunities": opportunities,
        "candidates_screened": len(all_candidates),
        # 跟进用：本次推荐的所有 instrument tickers
        "watch_tickers": list({
            ins["ticker"]
            for block in hedge_instruments
            for ins in block.get("instruments", [])
        } | {op["ticker"] for op in opportunities}),
    }

    # 持久化到 data/ 供每日跟进读取
    import datetime as _dt
    Path("data").mkdir(exist_ok=True)
    out_path = Path("data/weekly_latest.json")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[AllocationAdvisor] 结果已保存到 {out_path}")

    return result
