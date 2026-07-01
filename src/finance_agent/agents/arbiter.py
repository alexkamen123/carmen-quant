# src/finance_agent/agents/arbiter.py
"""P0a 辩论仲裁层（Research Manager）——把多空辩论逼成明确五档评级，作为 advisory 强先验
注入 PM 组合裁决材料。MVP 边界：仲裁只注入 PM、不硬改写最终 recommendation；同时落状态为
观测信号，为日后 RankIC 铺路。改核心建议 → flag debate_arbiter.enabled 默认 off。

flag off 时 run_arbiter_batch 纯透传、零 LLM 调用、arbiter_rating 恒空 → PM 模板逐字节不变。
"""
from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path

import yaml

from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.agents.claude_client import (claude_cli_chat, has_claude_cli,
                                                strip_markdown)
from finance_agent.agents.prompts import (ARBITER_STOCK_TEMPLATE, ARBITER_SYSTEM,
                                          ARBITER_USER)
from finance_agent.graph.state import StockAnalysis

_CONFIG_DIR = Path(__file__).parents[3] / "config"
SHADOW_LOG = Path("data/arbiter_shadow.jsonl")
RATINGS = ("买入", "增持", "持有", "减持", "卖出")
_CITE_MARKS = ("采纳", "驳回", "第", "多头", "空头", "基本面", "技术")


def _arbiter_cfg() -> dict:
    try:
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            c = s.get("debate_arbiter", {})
            if isinstance(c, dict):
                return c
    except Exception:
        pass
    return {}


def arbiter_enabled() -> bool:
    """settings.yaml debate_arbiter.enabled，缺省 False（改核心建议，默认关）。"""
    return bool(_arbiter_cfg().get("enabled", False))


def arbiter_shadow() -> bool:
    """影子观测档：enabled 且 shadow=true 时，arbiter 照常算+落盘，但不注入 PM（建议逐字节不变）。
    用于「先只观测比对分歧、再决定是否真注入」。缺省 False。"""
    return bool(_arbiter_cfg().get("shadow", False))


def _cites_evidence(r) -> bool:
    return isinstance(r, str) and any(k in r for k in _CITE_MARKS)


def _parse_arbiter(raw: str, stocks: list[StockAnalysis]) -> dict[str, dict]:
    """解析仲裁 JSON 数组 → {ticker: {rating, balanced, rationale}}。
    非五档丢弃；持有须 balanced=true 否则置空(回避判断不采纳)；rationale 须引用具体证据。"""
    valid = {s.ticker.upper() for s in stocks}
    try:
        s2 = strip_markdown(raw)
        arr = json.loads(s2[s2.find("["):s2.rfind("]") + 1])
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for item in arr if isinstance(arr, list) else []:
        if not isinstance(item, dict):
            continue
        tk = str(item.get("ticker", "")).upper()
        rating = item.get("rating", "")
        if tk not in valid or rating not in RATINGS:
            continue
        balanced = item.get("balanced")
        if rating == "持有" and not balanced:
            continue   # 持有须势均力敌，否则视为回避、不出评级
        rationale = [r for r in (item.get("rationale") or []) if _cites_evidence(r)]
        out[tk] = {"rating": rating,
                   "balanced": bool(balanced) if balanced is not None else None,
                   "rationale": rationale}
    return out


def _normalize_watch(rating: str) -> str:
    """观察池(未持有)语义闸：未持有票只有「建仓」或「不建仓」两个动作，五档只映射到 买入/观望。
    看多(买入/增持)→买入；其余(持有/减持/卖出)→观望(不建仓)。偏空方向由 rationale 保留，
    绝不把看空洗成'持有'——未持有的票本就不能'持有'(审查Important)。"""
    return "买入" if rating in ("买入", "增持") else "观望"


def _arbiter_block(rating: str, balanced, rationale: list[str]) -> str:
    """渲染注入 PM 模板的【仲裁结论】块。无评级 → ""（保证 PM 模板逐字节不变）。
    首字符为换行，紧贴 {next_earnings_date} 之后另起一行。
    「（多空势均力敌）」只在 balanced is True 才贴——绝不凭 rating=持有 硬贴虚假中性标签。"""
    if not rating:
        return ""
    head = f"\n【仲裁结论】五档评级：{rating}"
    if rating == "持有" and balanced is True:
        head += "（多空势均力敌）"
    lines = [head] + [f"- {r}" for r in rationale]
    return "\n".join(lines)


def pm_arbiter_block(s: StockAnalysis) -> str:
    """供 portfolio_manager 拼 PM 材料用。影子观测档下恒返 ""（只观测不注入，建议逐字节不变）。"""
    if arbiter_shadow():
        return ""
    return _arbiter_block(s.arbiter_rating, s.arbiter_balanced, s.arbiter_rationale)


def record_arbiter_shadow(stocks: list[StockAnalysis], today: str,
                          log_path: Path = SHADOW_LOG) -> None:
    """影子留痕：把算出五档评级的票落盘 JSONL，供日后比对 arbiter vs PM vs outcome。
    无评级的票不落。落盘失败不抛（不拖垮日报）。"""
    rows = [{"date": today, "ticker": s.ticker, "rating": s.arbiter_rating,
             "balanced": s.arbiter_balanced, "rationale": s.arbiter_rationale}
            for s in stocks if s.arbiter_rating]
    if not rows:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[Arbiter] 影子留痕失败（跳过）: {e}")


def _stock_block(s: StockAnalysis) -> str:
    return ARBITER_STOCK_TEMPLATE.format(
        ticker=s.ticker,
        bull_thesis=s.bull_thesis or "无",
        bear_thesis=s.bear_thesis or "无",
        signals_str=s.signals.to_prompt_str() if s.signals else "无",
        fundamental_view=s.earnings.fundamental_view or "暂无基本面数据",
    )


async def run_arbiter_batch(stocks: list[StockAnalysis]) -> list[StockAnalysis]:
    """对非 ETF 股票批量仲裁多空辩论，回填 arbiter_rating/balanced/rationale。
    flag off / 无目标 / 两路 LLM 全失败 → 原样返回（不改任何字段、不拖垮日报）。"""
    if not arbiter_enabled():
        return stocks
    targets = [s for s in stocks if not s.is_etf]
    if not targets:
        return stocks

    user_msg = ARBITER_USER.format(stocks_block="\n\n".join(_stock_block(s) for s in targets))
    raw = ""
    if has_claude_cli():
        try:
            raw = strip_markdown(await claude_cli_chat(ARBITER_SYSTEM, user_msg, timeout=300))
        except Exception as e:
            print(f"[Arbiter] Claude CLI 失败，降级 DeepSeek: {e}")
    if not raw:
        try:
            raw = await deepseek_chat(ARBITER_SYSTEM, user_msg)
        except Exception as e:
            print(f"[Arbiter] DeepSeek 也失败，跳过仲裁: {e}")
            return stocks

    ratings = _parse_arbiter(raw, targets)
    if not ratings:
        return stocks
    out: list[StockAnalysis] = []
    for s in stocks:
        r = ratings.get(s.ticker.upper())
        if not r or s.is_etf:
            out.append(s)
            continue
        rating = _normalize_watch(r["rating"]) if not s.shares else r["rating"]
        out.append(s.model_copy(update={
            "arbiter_rating": rating, "arbiter_balanced": r["balanced"],
            "arbiter_rationale": r["rationale"]}))
    print(f"[Arbiter] 仲裁完成，{len(ratings)} 只出五档评级")
    return out
