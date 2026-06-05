# src/finance_agent/agents/portfolio_manager.py
import asyncio
import json
from pathlib import Path
import yaml
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import (
    PM_SYSTEM, PM_USER,
    PM_BATCH_SYSTEM, PM_BATCH_USER, PM_BATCH_STOCK_TEMPLATE,
)
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli, strip_markdown

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}

_CONFIG_DIR = Path(__file__).parent.parent.parent.parent.parent / "config"


def _load_strategy_limits() -> str:
    """
    从 portfolio.yaml 读取 strategy.risk_limits，生成供 PM prompt 使用的红线约束段落。
    同时检查 settings.yaml 的 strategy_injection.inject_into_pm_prompt 开关。
    若开关关闭或配置不存在，返回空字符串。
    """
    try:
        settings_path = _CONFIG_DIR / "settings.yaml"
        if settings_path.exists():
            with open(settings_path) as f:
                settings = yaml.safe_load(f) or {}
            injection = settings.get("strategy_injection", {})
            if not injection.get("enabled", True) or not injection.get("inject_into_pm_prompt", True):
                return ""

        portfolio_path = _CONFIG_DIR / "portfolio.yaml"
        if not portfolio_path.exists():
            return ""
        with open(portfolio_path) as f:
            portfolio = yaml.safe_load(f) or {}

        strategy = portfolio.get("strategy", {})
        limits = strategy.get("risk_limits", [])
        hedge = strategy.get("hedge_bucket", {})
        if not limits and not hedge:
            return ""

        lines = [f"【L1战略红线（{strategy.get('last_updated','?')} 粥卡门配置分析，最高优先级，不可被战术信号突破）】"]
        for lim in limits:
            lines.append(f"- {lim['name']}：{lim['rule']}（违反时：{lim.get('action_if_breached','')}）")
        target_pct = hedge.get("target_pct")
        if target_pct:
            lines.append(f"- 对冲桶（IAU/BIL）目标占比：>= {target_pct}%（低于此值不得新建风险仓位）")
        return "\n".join(lines)
    except Exception as e:
        print(f"[PM] 加载战略红线失败（跳过注入）: {e}")
        return ""

# 各市场换算为美元的汇率因子（港元/人民币 → 美元）
_FX_TO_USD = {"us": 1.0, "hk": 1 / 7.8, "cn": 1 / 7.2}


def _usd_value(s: StockAnalysis) -> float:
    """将单只股票的市值统一换算为美元"""
    price = s.signals.close if s.signals else 0.0
    fx = _FX_TO_USD.get(s.market, 1.0)
    return s.shares * price * fx


def _sector_summary(stocks: list[StockAnalysis]) -> str:
    """
    按行业计算持仓市值占比（统一换算为美元），返回一行文字供 PM prompt 使用。
    例如："半导体/AI算力 52% | 互联网/AI 30% | 宽基ETF 14% | 股息ETF 4%"
    """
    sector_value: dict[str, float] = {}
    total = 0.0
    for s in stocks:
        if not s.sector or s.shares <= 0:
            continue
        value = _usd_value(s)
        sector_value[s.sector] = sector_value.get(s.sector, 0.0) + value
        total += value

    if total <= 0:
        return "暂无持仓市值数据"

    parts = sorted(sector_value.items(), key=lambda x: x[1], reverse=True)
    return " | ".join(f"{sec} {val/total*100:.0f}%" for sec, val in parts)


def _parse_decision(d: dict) -> dict:
    return {
        "recommendation":    d.get("recommendation", "观望"),
        "short_term_action": d.get("short_term_action", "立即执行"),
        "confidence":        d.get("confidence", "低"),
        "position_change":   d.get("position_change", "维持"),
        "entry_hint":        d.get("entry_hint", ""),
        "key_risk":          d.get("key_risk", ""),
        "one_line":          d.get("one_line", ""),
        "key_assumption":    d.get("key_assumption", ""),
        "stop_loss_hint":    d.get("stop_loss_hint", ""),
    }


async def run_portfolio_manager_batch(
    stocks: list[StockAnalysis],
    macro_summary: str = "",
    exposure_posture: str = "NEW_ENTRY_ALLOWED",
) -> list[StockAnalysis]:
    """
    一次 Claude CLI 调用处理所有非 ETF 股票的 PM 裁决。
    失败时逐只降级到 DeepSeek。
    """
    needs_pm = [s for s in stocks if not s.is_etf]
    result_map: dict[str, StockAnalysis] = {}

    # ETF 直接标记（由 is_etf 字段决定，无需硬编码 ticker）
    for s in stocks:
        if s.is_etf:
            result_map[s.ticker] = s.model_copy(update={
                "recommendation": "按计划定投",
                "confidence": "高",
                "one_line": f"{s.ticker} 按定投计划执行，无需额外操作",
            })

    if not needs_pm:
        return [result_map[s.ticker] for s in stocks]

    # 计算总持仓市值（统一换算为美元，含 ETF）用于仓位比例——必须在 blocks 循环之前
    total_value = sum(_usd_value(s) for s in stocks if s.shares > 0 and s.signals)
    sector_str = _sector_summary(stocks)
    print(f"[PM] 持仓集中度：{sector_str}")

    # 构建批量 prompt
    blocks = []
    for s in needs_pm:
        val = _usd_value(s) if s.shares > 0 and s.signals else 0.0
        pct = round(val / total_value * 100, 1) if total_value > 0 else 0.0
        # 成本/盈亏字符串
        if s.cost_basis and s.cost_basis > 0:
            pnl_str = (
                f"{s.unrealized_pnl_pct:+.1f}%" if s.unrealized_pnl_pct is not None else "计算中"
            )
            cost_basis_str = f"均价 {s.cost_basis}，浮盈 {pnl_str}"
        else:
            cost_basis_str = "未记录成本"

        signals_str = s.signals.to_prompt_str()
        if s.signal_weight == "low":
            signals_str += (
                "\n⚠️ 技术信号仅供参考（历史回测显示该标的技术信号无效）"
                "——PM 裁决应以持仓逻辑和基本面为主，忽略 composite_score 的方向性。"
            )
        thesis_text = s.thesis or "暂未记录持仓逻辑"
        if s.memory_context:
            thesis_text = (
                f"{thesis_text}\n\n"
                f"【历史决策记忆（最近 3 次）】\n{s.memory_context}"
            )
        blocks.append(PM_BATCH_STOCK_TEMPLATE.format(
            ticker=s.ticker,
            market=MARKET_LABEL.get(s.market, s.market),
            position_pct=pct,
            shares=s.shares,
            current_price=round(s.signals.close, 2) if s.signals else "N/A",
            cost_basis_str=cost_basis_str,
            thesis=thesis_text,
            signals_str=signals_str,
            fundamental_view=s.earnings.fundamental_view or "暂无基本面数据",
            bull_thesis=s.bull_thesis or "无",
            bear_thesis=s.bear_thesis or "无",
            next_earnings_date=s.earnings.next_earnings_date or "未知",
        ))

    POSTURE_NOTE = {
        "NEW_ENTRY_ALLOWED": "市场机制健康，可正常评估新增仓位",
        "REDUCE_ONLY":       "当前机制偏弱，建议以持有/减仓为主，新增需充分理由",
        "CASH_PRIORITY":     "市场处于收缩/危机机制，建议优先保留现金，避免新增仓位",
    }
    strategy_limits_str = _load_strategy_limits()
    user_msg = PM_BATCH_USER.format(
        macro_summary=macro_summary or "暂无宏观数据",
        exposure_note=POSTURE_NOTE.get(exposure_posture, exposure_posture),
        sector_summary=sector_str,
        n=len(needs_pm),
        stocks_block="\n\n".join(blocks),
        strategy_limits=strategy_limits_str,
    )

    decisions: list[dict] = []

    # 优先 claude CLI（Pro 订阅路径，无 API 限速）
    if has_claude_cli():
        try:
            raw = await claude_cli_chat(PM_BATCH_SYSTEM, user_msg, timeout=240)
            raw = strip_markdown(raw)
            start, end = raw.find("["), raw.rfind("]") + 1
            decisions = json.loads(raw[start:end])
            print(f"[PM] Claude CLI 批量裁决成功（{len(decisions)} 只）")
        except Exception as e:
            print(f"[PM] Claude CLI 失败，降级到 DeepSeek: {e}")

    # 降级：DeepSeek 并行处理（比串行快 N 倍）
    if not decisions:
        print(f"[PM] 降级到 DeepSeek 并行处理 {len(needs_pm)} 只股票...")

        async def _deepseek_one(s: StockAnalysis) -> dict:
            msg = PM_USER.format(
                ticker=s.ticker,
                market=MARKET_LABEL.get(s.market, s.market),
                signals_str=s.signals.to_prompt_str(),
                fundamental_view=s.earnings.fundamental_view or "暂无基本面数据",
                bull_thesis=s.bull_thesis or "无",
                bear_thesis=s.bear_thesis or "无",
            )
            try:
                raw = await deepseek_chat(PM_SYSTEM, msg)
                start, end = raw.find("{"), raw.rfind("}") + 1
                result = json.loads(raw[start:end])
                print(f"[PM] DeepSeek 完成 {s.ticker}")
                return {"ticker": s.ticker, **result}
            except Exception as e2:
                print(f"[PM] DeepSeek 也失败 {s.ticker}: {e2}，默认观望")
                return {"ticker": s.ticker, "recommendation": "观望", "confidence": "低",
                        "one_line": "数据处理异常，建议观望"}

        sem = asyncio.Semaphore(3)

        async def _deepseek_one_limited(s: StockAnalysis) -> dict:
            async with sem:
                return await _deepseek_one(s)

        decisions = list(await asyncio.gather(*[_deepseek_one_limited(s) for s in needs_pm]))
        print(f"[PM] DeepSeek 降级完成（{len(decisions)} 只）")

    decision_by_ticker = {d.get("ticker", ""): d for d in decisions}
    for s in needs_pm:
        d = decision_by_ticker.get(s.ticker, {})
        result_map[s.ticker] = s.model_copy(update=_parse_decision(d))

    return [result_map[s.ticker] for s in stocks]


async def run_portfolio_manager(analysis: StockAnalysis) -> StockAnalysis:
    """单只股票 PM（保留向后兼容，内部用 DeepSeek）"""
    user_msg = PM_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        signals_str=analysis.signals.to_prompt_str(),
        fundamental_view=analysis.earnings.fundamental_view or "暂无基本面数据",
        bull_thesis=analysis.bull_thesis or "无",
        bear_thesis=analysis.bear_thesis or "无",
    )
    try:
        raw = await deepseek_chat(PM_SYSTEM, user_msg)
        start, end = raw.find("{"), raw.rfind("}") + 1
        decision = json.loads(raw[start:end])
    except Exception:
        decision = {}
    return analysis.model_copy(update=_parse_decision(decision))
