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
