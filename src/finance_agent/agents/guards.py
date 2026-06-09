# src/finance_agent/agents/guards.py
"""
防追高护栏（L3a）：在 PM 裁决之后做【确定性后置 clamp】——只把"加仓/买入"降级为
"维持/观望"，绝不反向激进。防住两类真实亏钱行为：
  G1 扎堆追高：近窗集中买入(去重票数≥4) AND 个股技术超买(≥2项) → 硬拦
  G2 板块/单票超红线：板块已破集中度红线 或 单票>15% 仍要加仓 → 硬拦

设计要点（opus 设计评审团）：
  - 代码层硬 clamp，不信任 LLM 自觉（历史教训：PM prompt 早有"情绪冷却"软规则，
    LLM 照样放行了 9 笔追高 → 必须代码兜底）
  - 误报无害化：硬拦只更保守（加仓→维持/观望），最坏=踏空，绝不造成乱买亏钱
  - 多条件叠加才硬拦；定投ETF/对冲桶豁免；signal_weight=='low' 不用技术因子硬拦
  - 阈值读 portfolio.yaml risk_limits，不写死；理由写进 key_risk（可解释、可反驳）
"""
import re
from pathlib import Path

import yaml

from finance_agent.weekly.drift import HEDGE_TICKERS
from finance_agent.db.tracker import get_action_history

_CONFIG_DIR = Path(__file__).parents[3] / "config"   # .../finance-agent/config

DEFAULT_SINGLE_LIMIT = 15.0       # 单票占比红线（缺省，可被 portfolio.yaml 覆盖）
BUY_WINDOW_DAYS = 10              # 扎堆统计窗口（自然日）
CLUSTER_MIN_DISTINCT = 4         # 近窗去重 BUY 票数 ≥ 此值 = 扎堆
OVERBOUGHT_MIN_HITS = 2          # 技术超买指标命中 ≥ 此值 = 高位


def guard_enabled() -> bool:
    """settings.yaml anti_chase_guard.enabled，缺省 True（一键可关，避免误报时只能删码）。"""
    try:
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            return bool(s.get("anti_chase_guard", {}).get("enabled", True))
    except Exception:
        pass
    return True


def load_portfolio() -> dict:
    try:
        with open(_CONFIG_DIR / "portfolio.yaml") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def parse_limits(portfolio: dict) -> tuple[dict, float]:
    """解析 risk_limits → ({sector: limit%}, 单票 limit%)。"""
    sector_limits: dict[str, float] = {}
    single_limit = DEFAULT_SINGLE_LIMIT
    for lim in portfolio.get("strategy", {}).get("risk_limits", []):
        rule = lim.get("rule", "")
        m = re.search(r"<=\s*([\d.]+)", rule)
        if not m:
            continue
        val = float(m.group(1))
        if rule.strip().startswith("sector:"):
            sec = rule.split("sector:")[1].split("<=")[0].strip()
            sector_limits[sec] = val
        elif "single_ticker" in rule:
            single_limit = val
    return sector_limits, single_limit


def exempt_tickers(portfolio: dict) -> set[str]:
    """定投ETF + 对冲桶 → 不计入扎堆、不触发护栏（补这些是被鼓励的纪律操作）。"""
    exempt = {t.upper() for t in HEDGE_TICKERS}
    for h in portfolio.get("holdings", []):
        sec = h.get("sector") or ""
        if h.get("is_dca") or "ETF" in sec:
            exempt.add(str(h.get("ticker", "")).upper())
    return exempt


def recent_buy_velocity(exempt: set[str], days: int = BUY_WINDOW_DAYS,
                        db_path=None) -> tuple[int, list[str]]:
    """近 days 天内、非豁免标的的去重 BUY 票数 + 列表（数据源：user_actions）。"""
    rows = get_action_history(ticker=None, days=days, db_path=db_path)
    buys = {
        str(r["ticker"]).upper() for r in rows
        if r.get("action") == "BUY" and str(r["ticker"]).upper() not in exempt
    }
    return len(buys), sorted(buys)


def _overbought_hits(sig) -> int:
    """技术超买指标命中数：RSI≥70 / 布林位置≥0.90 / 高于MA20≥12%。"""
    if sig is None:
        return 0
    hits = 0
    if getattr(sig, "rsi", 0) >= 70:
        hits += 1
    if getattr(sig, "bb_position", 0) >= 0.90:
        hits += 1
    if getattr(sig, "price_vs_ma20", 0) >= 12.0:
        hits += 1
    return hits


def _is_bullish(dec: dict) -> bool:
    return dec.get("recommendation") == "买入" or (dec.get("position_change") or "").startswith(("大加", "小加"))


def apply_anti_chase(stock, dec: dict, *, n_distinct: int,
                     sector_pct: dict, ticker_pct: dict,
                     sector_limits: dict, single_limit: float,
                     exempt: set[str]) -> tuple[dict, list[str]]:
    """
    对单只票的 PM 决策做护栏 clamp。返回 (可能被降级的 decision, 命中理由列表)。
    只降级偏多决策（买入/加仓 → 维持/观望），绝不把保守决策改激进。
    """
    if not _is_bullish(dec):
        return dec, []
    tk = (stock.ticker or "").upper()
    if tk in exempt:
        return dec, []

    reasons: list[str] = []

    # G1 扎堆追高：扎堆 AND 技术超买（signal_weight=='low' 的票技术信号无效，不用技术硬拦）
    if stock.signal_weight != "low":
        ob = _overbought_hits(stock.signals)
        if n_distinct >= CLUSTER_MIN_DISTINCT and ob >= OVERBOUGHT_MIN_HITS:
            rsi = getattr(stock.signals, "rsi", 0)
            pvm = getattr(stock.signals, "price_vs_ma20", 0)
            reasons.append(
                f"追高（近{BUY_WINDOW_DAYS}天已扎堆买入{n_distinct}只票，"
                f"且本票RSI{rsi:.0f}/高于20日线{pvm:+.0f}%处高位）"
            )

    # G2 板块红线（已破红线仍加仓）
    sec = stock.sector or ""
    sec_limit = sector_limits.get(sec)
    if sec_limit is not None and sector_pct.get(sec, 0.0) >= sec_limit:
        reasons.append(f"板块红线（{sec}已占{sector_pct[sec]:.0f}%≥{sec_limit:.0f}%）")

    # G2 单票红线
    if ticker_pct.get(tk, 0.0) >= single_limit:
        reasons.append(f"单票红线（{tk}已占{ticker_pct[tk]:.0f}%≥{single_limit:.0f}%）")

    if not reasons:
        return dec, []

    clamped = dict(dec)
    clamped["position_change"] = "维持"          # 加仓档位一律降为维持（更保守）
    if clamped.get("recommendation") == "买入":   # 新买入→观望；持有+小加→保持"持有"(继续持有但不加，语义正确)
        clamped["recommendation"] = "观望"
    clamped["short_term_action"] = "等情绪冷却·本周暂缓加仓"
    # 清掉 LLM 的买入入场点，否则卡片会"观望/维持"旁边又挂"📌回调买入"，自相矛盾
    clamped["entry_hint"] = "护栏期暂不给入场点（等情绪冷却后再评估）"
    reason_str = "；".join(reasons)
    clamped["key_risk"] = f"⛔防追高护栏：{reason_str}。" + (dec.get("key_risk") or "")
    # 措辞对"观望"和"持有(不加)"两种结果都准确，不写死"降级观望"
    clamped["one_line"] = "⚠护栏拦截·暂缓加仓 " + (dec.get("one_line") or "")
    return clamped, reasons
