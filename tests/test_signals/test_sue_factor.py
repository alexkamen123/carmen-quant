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


from finance_agent.db import tracker


def _enable_inject(monkeypatch):
    monkeypatch.setattr(sf, "_sue_pead_cfg",
                        lambda: {"enabled": True, "sigma_threshold": 1.5,
                                 "min_quarters": 8, "track_days": 30})


def test_format_miss_wording(tmp_path, monkeypatch):
    _enable_inject(monkeypatch)
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_sue_alert("AAPL", "us", "2026-06-25", -2.0, 1.5, 1.8, 0.15, db_path=db)
    note = sf.format_sue_note("AAPL", asof="2026-06-27", db_path=db)
    assert "更保守" in note or "止损复核" in note
    assert "加仓" not in note and "买入" not in note and "更激进" not in note


def test_format_beat_wording(tmp_path, monkeypatch):
    _enable_inject(monkeypatch)
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_sue_alert("NVDA", "us", "2026-06-25", 2.3, 1.87, 1.77, 0.04, db_path=db)
    note = sf.format_sue_note("NVDA", asof="2026-06-27", db_path=db)
    assert "额外确认" in note or "别慌卖" in note
    assert "加仓" not in note and "买入" not in note and "更激进" not in note


def test_format_below_threshold_empty(tmp_path, monkeypatch):
    _enable_inject(monkeypatch)
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_sue_alert("AAPL", "us", "2026-06-25", 0.8, 1.9, 1.8, 0.12, db_path=db)
    assert sf.format_sue_note("AAPL", asof="2026-06-27", db_path=db) == ""


def test_format_off_returns_empty_bytewise(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "_sue_pead_cfg",
                        lambda: {"enabled": False, "sigma_threshold": 1.5,
                                 "min_quarters": 8, "track_days": 30})
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_sue_alert("AAPL", "us", "2026-06-25", -2.0, 1.5, 1.8, 0.15, db_path=db)
    assert sf.format_sue_note("AAPL", asof="2026-06-27", db_path=db) == ""


def test_format_stale_event_out_of_window_empty(tmp_path, monkeypatch):
    _enable_inject(monkeypatch)
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_sue_alert("AAPL", "us", "2026-05-01", -2.0, 1.5, 1.8, 0.15, db_path=db)
    assert sf.format_sue_note("AAPL", asof="2026-06-27", db_path=db) == ""


def test_format_future_event_excluded(tmp_path, monkeypatch):
    _enable_inject(monkeypatch)
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_sue_alert("AAPL", "us", "2026-07-30", -2.0, 1.5, 1.8, 0.15, db_path=db)
    assert sf.format_sue_note("AAPL", asof="2026-06-27", db_path=db) == ""
