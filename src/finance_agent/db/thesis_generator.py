# src/finance_agent/db/thesis_generator.py
"""
一次性（或手动触发）为每只持仓生成初始持仓逻辑（Thesis）。
使用 Claude 生成，DeepSeek 降级。结果写入 SQLite theses 表。
"""
import asyncio
import json
import re
from pathlib import Path

import yaml
import yfinance as yf

from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli, strip_markdown
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.db.tracker import save_thesis, load_thesis


_VALID_TRIGGERS = {"earnings_miss", "news_negative", "price_break",
                   "revenue_decline", "margin_break"}


def _parse_thesis_triggers(text):
    """从 thesis 原始文本（strip_markdown 之前）抽 ```json``` 块 → (pillars|None, stop_conditions)。
    解析失败 → (None, "")。"""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    if not m:
        return None, ""
    try:
        obj = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None, ""
    pillars = obj.get("pillars")
    if not isinstance(pillars, list):
        return None, ""
    clean = [p for p in pillars if isinstance(p, dict) and p.get("trigger_type") in _VALID_TRIGGERS]
    return (clean or None), (obj.get("stop_conditions") or "")


THESIS_SYSTEM = """你是一位价值投资顾问，帮助个人投资者梳理每只持仓的核心投资逻辑。

请生成结构化的持仓逻辑，格式如下（纯文字，禁止使用 markdown 表格）：

**核心论点（一句话）**
用一句话说明为什么持有这只股票。

**支撑理由（3条）**
1. [理由一，基于基本面或行业趋势]
2. [理由二，基于竞争优势或护城河]
3. [理由三，基于估值或成长空间]

**需要关注的破坏条件**
什么情况下以上逻辑不再成立，应该重新评估持仓。

**当前阶段**
目前处于什么阶段（如：早期成长/成熟扩张/估值修复等）。

要求：简洁，总字数不超过 200 字，基于真实数据，不要无根据乐观。

另外，在正文之后附一段 JSON 代码块（```json ... ```），声明这只票的"论点失效条件"，供机器监控：
{"pillars": [{"pillar": "<支柱名>", "trigger_type": "<从下列固定值选>", "threshold": <数值或null>, "status": "intact"}],
 "stop_conditions": "<给人看的一句话止损条件摘要>"}
trigger_type 只能取：earnings_miss / news_negative / price_break / revenue_decline / margin_break。
price_break 与 margin_break 必须给数值 threshold（止损价 / 毛利率百分比）；其余 threshold 填 null。最多 4 条，只列真正会证伪核心逻辑的。"""

THESIS_USER = """股票：{ticker}（{market}市场）
持仓成本：{cost_basis}
持仓股数：{shares}
持有备注：{notes}

当前财务概况：
{financials_str}

请为这只股票生成持仓逻辑。"""


async def _fetch_quick_financials(ticker: str, market: str) -> str:
    """快速拉 yfinance info 返回简要财务摘要"""
    try:
        yf_ticker = f"{int(ticker):04d}.HK" if market == "hk" and ticker.isdigit() else ticker
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: yf.Ticker(yf_ticker).info)
        lines = []
        if info.get("trailingPE"):
            lines.append(f"PE(TTM)：{info['trailingPE']:.1f}x")
        if info.get("revenueGrowth"):
            lines.append(f"营收增速：{info['revenueGrowth']*100:.1f}%")
        if info.get("grossMargins"):
            lines.append(f"毛利率：{info['grossMargins']*100:.1f}%")
        if info.get("marketCap"):
            mc = info["marketCap"]
            lines.append(f"市值：{mc/1e9:.1f}B")
        if info.get("longBusinessSummary"):
            lines.append(f"业务：{info['longBusinessSummary'][:100]}...")
        return "\n".join(lines) if lines else "暂无财务数据"
    except Exception:
        return "暂无财务数据"


async def generate_thesis_for(ticker: str, market: str, cost_basis: float,
                               shares: float, notes: str = "",
                               force: bool = False, db_path=None) -> str:
    """
    为单只股票生成持仓逻辑。
    force=True 时即使已有记录也重新生成。
    返回生成的 thesis 文字。
    """
    if not force:
        existing = load_thesis(ticker, db_path=db_path)
        if existing:
            print(f"[ThesisGen] {ticker} 已有持仓逻辑，跳过（用 --force 覆盖）")
            return existing

    print(f"[ThesisGen] 正在为 {ticker} 生成持仓逻辑...")
    financials_str = await _fetch_quick_financials(ticker, market)

    cost_str = f"{cost_basis}" if cost_basis > 0 else "未记录"
    user_msg = THESIS_USER.format(
        ticker=ticker, market={"us": "美股", "hk": "港股", "cn": "A股"}.get(market, market),
        cost_basis=cost_str, shares=shares, notes=notes or "无",
        financials_str=financials_str,
    )

    try:
        if has_claude_cli():
            raw = await claude_cli_chat(THESIS_SYSTEM, user_msg, timeout=90)
            thesis = strip_markdown(raw)
        else:
            raise RuntimeError("无 Claude CLI")
    except Exception as e:
        print(f"[ThesisGen] Claude 失败，降级 DeepSeek ({ticker}): {e}")
        raw = await deepseek_chat(THESIS_SYSTEM, user_msg)
        thesis = raw

    # 触发器解析要用 strip_markdown 之前的原始文本：strip_markdown 只锚定字符串首尾，
    # 会把正文末尾附带的 ```json ... ``` 代码块的收尾栅栏吃掉，导致正则拿不到闭合围栏。
    pillars, stop_conditions = _parse_thesis_triggers(raw)
    save_thesis(ticker, market, thesis, pillars=pillars,
                stop_conditions=stop_conditions, db_path=db_path)
    return thesis


async def generate_all_theses(force: bool = False) -> dict[str, str]:
    """
    从 portfolio.yaml 读取所有非 ETF 持仓，批量生成持仓逻辑。
    返回 {ticker: thesis_text}。
    """
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    etf_skip = {"QQQM", "VOO", "SCHD", "DRAM"}
    holdings = [
        h for h in portfolio.get("holdings", [])
        if h["ticker"] not in etf_skip
    ]

    results: dict[str, str] = {}
    for h in holdings:
        ticker = h["ticker"]
        thesis = await generate_thesis_for(
            ticker=ticker,
            market=h.get("market", "us"),
            cost_basis=float(h.get("cost_basis") or 0),
            shares=float(h.get("shares", 0)),
            notes=h.get("notes", ""),
            force=force,
        )
        results[ticker] = thesis
        # 串行，避免 Claude CLI 并发冲突
        await asyncio.sleep(1)

    return results
