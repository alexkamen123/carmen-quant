import math
from finance_agent.signals import sue_factor as sf


def _hist(n, base=0.05):
    return [base + (i % 3) * 0.01 for i in range(n)]


def test_compute_sue_beat_positive():
    sue = sf.compute_sue(reported_eps=2.01, estimate_eps=1.80, hist_surprises=_hist(8))
    assert sue is not None and sue > 0


def test_compute_sue_miss_negative():
    sue = sf.compute_sue(reported_eps=1.50, estimate_eps=1.80, hist_surprises=_hist(8))
    assert sue is not None and sue < 0


def test_compute_sue_sigma_zero_none():
    assert sf.compute_sue(2.0, 1.8, [0.1] * 8) is None


def test_compute_sue_too_few_quarters_none():
    assert sf.compute_sue(2.0, 1.8, _hist(7)) is None


def test_compute_sue_nan_input_none():
    assert sf.compute_sue(float("nan"), 1.8, _hist(8)) is None
    assert sf.compute_sue(2.0, None, _hist(8)) is None


def test_flags_default_off(monkeypatch, tmp_path):
    monkeypatch.setattr(sf, "_CONFIG_DIR", tmp_path)
    assert sf.sue_factor_enabled() is False
    assert sf.sue_pead_alert_enabled() is False
