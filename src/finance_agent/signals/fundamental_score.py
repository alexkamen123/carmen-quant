# src/finance_agent/signals/fundamental_score.py
"""P1a 基本面量化因子（纯计算核心）——把基本面从「LLM 自由发挥」变成可回测、可 RankIC 检验、
可降权的数字证据。四个免费(yfinance)因子：
  1. FCF Yield        自由现金流收益率（现金比盈利难造假，低价买真现金）
  2. GP/A             毛利资产比（Novy-Marx 最强单盈利因子，与价值正交、熊市防御）
  3. 12-1 月动量      趋势惯性+反应迟缓
  4. 分析师上行空间   一致目标价隐含的机构基本面判断

合成 0-10 fundamental_score，作辩手/PM 读到的数字。

⚠️ 诚实边界：0-10 映射系数是 v1【占位 heuristic】(非拟合)——在 recommendations 积累 ≥60 条带
outcome 样本、经月度 RankIC(P1c) 校准前，禁止当作已验证 alpha 对外宣传、禁止用于加大仓位。
可用因子 <2 → score=None(不单因子硬撑)；缺字段/覆盖太薄 → 该因子 None(不猜、不写'None'进 prompt)。
"""
from __future__ import annotations

from pydantic import BaseModel

MIN_FACTORS_FOR_SCORE = 2   # 可用因子少于此 → 综合分 None
_ANALYST_MIN_COVER = 3      # 分析师覆盖 <此 → 上行空间不可信


def _clip(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return round(max(lo, min(hi, v)), 2)


def _fcf_yield(info: dict) -> float | None:
    """自由现金流收益率 %：freeCashflow / marketCap（不解析跨版本不稳的 .cashflow DataFrame）。"""
    fcf, mc = info.get("freeCashflow"), info.get("marketCap")
    if fcf is None or not mc:
        return None
    return round(fcf / mc * 100, 2)


def _gp_to_assets(financials_col: dict | None, balance_col: dict | None) -> float | None:
    """毛利资产比 %：Gross Profit / Total Assets。行名缺失 → None（不猜替代行名）。"""
    if not financials_col or not balance_col:
        return None
    try:
        gp = financials_col.get("Gross Profit")
        ta = balance_col.get("Total Assets")
        if gp is None or not ta:
            return None
        return round(gp / ta * 100, 2)
    except Exception:
        return None


def _momentum_12_1(close: list | None) -> float | None:
    """12-1 月动量 %：(close[-22]/close[-252]-1)*100。历史不足 252 交易日 → None。"""
    if close is None or len(close) < 252:
        return None
    p0, p1 = close[-252], close[-22]
    if not p0:
        return None
    return round((p1 / p0 - 1) * 100, 2)


def _analyst_upside(info: dict) -> float | None:
    """分析师上行空间 %：(targetMeanPrice-current)/current。覆盖 <3 → None（太薄不可信）。"""
    if (info.get("numberOfAnalystOpinions") or 0) < _ANALYST_MIN_COVER:
        return None
    tgt, cur = info.get("targetMeanPrice"), info.get("currentPrice")
    if not tgt or not cur:
        return None
    return round((tgt - cur) / cur * 100, 2)


class FundamentalFactors(BaseModel):
    """四因子原始值(%) + 0-10 综合分。原始值供落库 RankIC，score 供 PM 材料。"""
    fcf_yield: float | None = None
    gp_to_assets: float | None = None
    momentum_12_1: float | None = None
    analyst_upside: float | None = None
    score: float | None = None

    def to_prompt_str(self) -> str:
        """渲染注入 PM 材料的基本面数字行。跳过缺失因子、绝不写 'None'。无任何因子 → ""。"""
        parts = []
        if self.fcf_yield is not None:
            parts.append(f"FCF收益率{self.fcf_yield:+.1f}%")
        if self.gp_to_assets is not None:
            parts.append(f"毛利资产比{self.gp_to_assets:.1f}%")
        if self.momentum_12_1 is not None:
            parts.append(f"12-1月动量{self.momentum_12_1:+.1f}%")
        if self.analyst_upside is not None:
            parts.append(f"分析师上行{self.analyst_upside:+.1f}%")
        if not parts:
            return ""
        head = f"基本面因子分{self.score}/10（占位·待校准）：" if self.score is not None else "基本面因子（不足2项，仅参考）："
        return head + "、".join(parts)


def calculate_fundamental_factors(info: dict | None,
                                  financials_col: dict | None = None,
                                  balance_col: dict | None = None,
                                  momentum_close: list | None = None) -> FundamentalFactors:
    """纯函数：从已取数据算四因子 + 合成 0-10。零网络。占位映射系数见模块 docstring 诚实边界。"""
    info = info or {}
    fcf = _fcf_yield(info)
    gpa = _gp_to_assets(financials_col, balance_col)
    mom = _momentum_12_1(momentum_close)
    ana = _analyst_upside(info)

    subs = []
    if fcf is not None:
        subs.append(_clip(5 + fcf * 1.0))          # FCF 每 +1% → +1 分
    if gpa is not None:
        subs.append(_clip(5 + (gpa - 15) * 0.33))  # GP/A 15% 为中枢
    if mom is not None:
        subs.append(_clip(5 + mom * 0.2))          # 动量每 +5% → +1 分
    if ana is not None:
        subs.append(_clip(5 + ana * 0.25))         # 上行每 +4% → +1 分
    score = round(sum(subs) / len(subs), 1) if len(subs) >= MIN_FACTORS_FOR_SCORE else None

    return FundamentalFactors(fcf_yield=fcf, gp_to_assets=gpa, momentum_12_1=mom,
                              analyst_upside=ana, score=score)
