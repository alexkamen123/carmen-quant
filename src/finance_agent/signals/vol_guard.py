"""P3b 波动率仓控：realized vol 偏离历史>1.5σ → 注入"收紧止损"警示。

单向收紧·不改 stop_loss_hint 数值·绝无加仓字样·零 LLM/网络(取数由 closes_fn 注入)。
flag off 时 format_vol_note 恒返 ""(护栏第一层)。阈值/窗口占位启发式·enable 待点头。
"""
from __future__ import annotations

import statistics
from pathlib import Path

_CONFIG_DIR = Path(__file__).parents[3] / "config"
_VG_DEFAULTS = {"enabled": False, "vol_window": 20, "baseline_window": 60, "sigma_threshold": 1.5}


def _load_block(name, defaults):
    try:
        import yaml
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            return {**defaults, **(s.get(name, {}) or {})}
    except Exception:
        pass
    return dict(defaults)


def vol_guard_enabled() -> bool:
    return bool(_load_block("vol_guard", _VG_DEFAULTS)["enabled"])


def _vg_cfg():
    return _load_block("vol_guard", _VG_DEFAULTS)


def _bad(x):
    return x is None or (isinstance(x, float) and x != x)


def _rolling_vol(rets, w):
    """每个位置取前 w 个收益率的样本 std → realized vol 序列。"""
    out = []
    for i in range(w, len(rets) + 1):
        window = rets[i - w:i]
        try:
            out.append(statistics.stdev(window))
        except statistics.StatisticsError:
            return []
    return out


def compute_vol_zscore(closes, vol_window=20, baseline_window=60):
    """最新 realized vol 相对历史 vol 分布的 z-score。样本不足/std==0/NaN → None。"""
    if not closes or any(_bad(c) for c in closes):
        return None
    if len(closes) < vol_window + baseline_window + 1:
        return None
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    vols = _rolling_vol(rets, vol_window)
    if len(vols) < baseline_window + 1:
        return None
    latest = vols[-1]
    hist = vols[-(baseline_window + 1):-1]        # 当前之前的 baseline_window 个(无前视)
    try:
        mu = statistics.mean(hist)
        sd = statistics.stdev(hist)
    except statistics.StatisticsError:
        return None
    if sd == 0:
        return None
    return (latest - mu) / sd


def format_vol_note(ticker, regime=None, closes_fn=None) -> str:
    """波动异常放大→收紧止损警示。flag off→""；z<=阈或数据不足→""。单向收紧·无加仓。"""
    cfg = _vg_cfg()
    if not cfg["enabled"]:
        return ""
    try:
        if closes_fn is None:
            from finance_agent.backtest.discovery import fetch_ohlcv
            df = fetch_ohlcv(ticker)
            closes = df["close"].tolist() if df is not None and "close" in df else None
        else:
            closes = closes_fn(ticker)
    except Exception:
        return ""
    z = compute_vol_zscore(closes, int(cfg["vol_window"]), int(cfg["baseline_window"]))
    if z is None or z <= float(cfg["sigma_threshold"]):
        return ""
    note = (f"⚠️【波动异常】{ticker} 近{int(cfg['vol_window'])}日波动率放大到历史 {z:.1f}σ，"
            f"建议收紧止损/审慎控仓。")
    if regime == "down":
        note += "（当前跌市，高波动尤需收紧、勿在高波动里死扛。）"
    return note
