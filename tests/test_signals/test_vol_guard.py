from finance_agent.signals import vol_guard as vg


def _closes(prefix_flat=80, tail_wild=5):
    # 前段平稳(小幅)、末段剧烈波动 → 末段 realized vol 相对历史应显著抬升
    import math
    out = [100.0]
    for i in range(prefix_flat):
        out.append(out[-1] * (1 + 0.002 * ((i % 2) * 2 - 1)))   # ±0.2% 交替
    for i in range(tail_wild):
        out.append(out[-1] * (1 + 0.05 * ((i % 2) * 2 - 1)))    # ±5% 交替
    return out


def test_compute_zscore_high_vol_positive():
    z = vg.compute_vol_zscore(_closes(), vol_window=5, baseline_window=20)
    assert z is not None and z > 1.5


def test_compute_zscore_flat_low():
    flat = [100.0 * (1 + 0.002 * ((i % 2) * 2 - 1)) for i in range(60)]
    z = vg.compute_vol_zscore(flat, vol_window=5, baseline_window=20)
    assert z is None or z <= 1.5


def test_compute_zscore_insufficient_none():
    assert vg.compute_vol_zscore([100.0, 101.0, 102.0], vol_window=20, baseline_window=60) is None


def test_compute_zscore_zero_std_none():
    flat = [100.0] * 100          # 收益率全 0 → 历史 vol std==0
    assert vg.compute_vol_zscore(flat, vol_window=5, baseline_window=20) is None


def _cfg_on():
    return {"enabled": True, "vol_window": 5, "baseline_window": 20, "sigma_threshold": 1.5}


def test_format_flag_off_empty(monkeypatch):
    monkeypatch.setattr(vg, "_vg_cfg", lambda: {"enabled": False, "vol_window": 5,
                                                "baseline_window": 20, "sigma_threshold": 1.5})
    assert vg.format_vol_note("NVDA", closes_fn=lambda t: _closes()) == ""


def test_format_high_vol_note_tightens(monkeypatch):
    monkeypatch.setattr(vg, "_vg_cfg", _cfg_on)
    note = vg.format_vol_note("NVDA", regime=None, closes_fn=lambda t: _closes())
    assert "收紧止损" in note and "NVDA" in note
    assert "加仓" not in note and "买入" not in note


def test_format_regime_down_heavier(monkeypatch):
    monkeypatch.setattr(vg, "_vg_cfg", _cfg_on)
    note = vg.format_vol_note("NVDA", regime="down", closes_fn=lambda t: _closes())
    assert "跌市" in note


def test_format_below_threshold_empty(monkeypatch):
    monkeypatch.setattr(vg, "_vg_cfg", _cfg_on)
    flat = [100.0 * (1 + 0.002 * ((i % 2) * 2 - 1)) for i in range(60)]
    assert vg.format_vol_note("NVDA", closes_fn=lambda t: flat) == ""
