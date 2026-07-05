"""P2b 盈余惊喜(SUE)因子。

SUE = (实际EPS − 预期EPS) / 历史 surprise 标准差。
观测档 sue_factor：算 SUE + 落 earnings_surprise_alerts 表 + 30天漂移回填（纯观测·默认on）。
注入档 sue_pead_alert：向 PM 注入双向审慎（miss→更保守·beat→防卖飞），改核心建议·默认off。

反过拟合：SUE 是占位因子，≥60条 outcome 经 RankIC 校准前禁当已验证 alpha、禁用于加仓。
未来函数：hist_surprises 只取 earnings_date 之前已披露季度；outcome 回填/RankIC 走成熟闸。
"""
from __future__ import annotations

import statistics
from datetime import date, datetime
from pathlib import Path

_CONFIG_DIR = Path(__file__).parents[3] / "config"

_SUE_FACTOR_DEFAULTS = {"enabled": False}
_SUE_PEAD_DEFAULTS = {
    "enabled": False,
    "sigma_threshold": 1.5,
    "min_quarters": 8,
    "track_days": 30,
}


def _load_block(name: str, defaults: dict) -> dict:
    """读 settings.yaml 的 name 块，缺省回落 defaults（照 fundamental_score.py 模式）。"""
    try:
        import yaml
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            block = s.get(name, {}) or {}
            return {**defaults, **block}
    except Exception:
        pass
    return dict(defaults)


def sue_factor_enabled() -> bool:
    """观测档：算+落库+回填。缺省 False。"""
    return bool(_load_block("sue_factor", _SUE_FACTOR_DEFAULTS)["enabled"])


def sue_pead_alert_enabled() -> bool:
    """注入档：向 PM 注入双向审慎。缺省 False。"""
    return bool(_load_block("sue_pead_alert", _SUE_PEAD_DEFAULTS)["enabled"])


def _sue_pead_cfg() -> dict:
    return _load_block("sue_pead_alert", _SUE_PEAD_DEFAULTS)


def _is_bad(x) -> bool:
    return x is None or (isinstance(x, float) and x != x)  # None 或 NaN


def compute_sue(reported_eps, estimate_eps, hist_surprises, min_quarters: int = 8):
    """SUE = (reported − estimate) / stdev(历史 surprise)。

    hist_surprises: 财报日**之前**已披露季度的 (reported_i − estimate_i) 序列。
    闭嘴闸：样本 < min_quarters / σ==0 / 任一输入 None/NaN → None（不猜不落假值）。
    """
    if _is_bad(reported_eps) or _is_bad(estimate_eps):
        return None
    clean = [s for s in (hist_surprises or []) if not _is_bad(s)]
    if len(clean) < min_quarters:
        return None
    try:
        sigma = statistics.stdev(clean)
    except statistics.StatisticsError:
        return None
    if sigma == 0:
        return None
    return (reported_eps - estimate_eps) / sigma


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def format_sue_note(ticker, asof=None, db_path=None) -> str:
    """【盈余惊喜】双向审慎注入（strategy_evidence 槽）。

    注入档 off → ""（三重护栏第一层）。确定性模板、零 LLM/网络。
    取该 ticker 最近一条『已发布且仍在漂移窗口内(earnings_date ∈ [asof−track_days, asof])』的事件，
    |SUE| ≥ sigma_threshold 才出文字。措辞只单向收紧：
      miss(SUE≤−阈) → 更保守/止损复核；beat(SUE≥+阈) → 减仓/止损前多确认防卖飞。绝无加仓字样。
    """
    from finance_agent.db.tracker import get_sue_alerts  # 延迟导入避免循环依赖

    cfg = _sue_pead_cfg()
    if not cfg["enabled"]:
        return ""
    asof_d = _to_date(asof) or datetime.today().date()
    track = int(cfg.get("track_days", 30))
    thr = float(cfg.get("sigma_threshold", 1.5))
    try:
        rows = get_sue_alerts(ticker, db_path=db_path)  # 按 earnings_date DESC
    except Exception:
        return ""
    for r in rows:
        ed = _to_date(r.get("earnings_date"))
        if ed is None or ed > asof_d:
            continue                       # 未来事件不注入（未来函数防护）
        if (asof_d - ed).days > track:
            continue                       # 超出漂移窗口不再警示
        sue = r.get("sue_score")
        if sue is None or abs(sue) < thr:
            return ""                       # 最近一条也不过阈 → 不注入
        if sue <= -thr:
            return (f"【盈余惊喜】{ticker} 最新财报大幅低于预期(SUE={sue:+.1f}σ)，"
                    f"警惕盈余后下行漂移，宜更保守/止损复核。")
        return (f"【盈余惊喜】{ticker} 最新财报录强正向盈余惊喜(SUE={sue:+.1f}σ)，"
                f"短期回调或为噪声，减仓/止损前需额外确认，别慌卖。")
    return ""
