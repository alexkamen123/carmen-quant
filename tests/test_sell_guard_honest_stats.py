# tests/test_sell_guard_honest_stats.py
"""守门警示里的历史统计必须标注审计快照日期，且集中单一常量——
避免「6/10·7.4%·错7例」随新数据变化后文案仍引旧值而「撒谎」（北极星：诚实）。"""
import finance_agent.signals.sell_guard as sg


def test_snapshot_date_constant_exists():
    assert hasattr(sg, "_AUDIT_SNAPSHOT_DATE")
    assert "2026-" in sg._AUDIT_SNAPSHOT_DATE


def test_sell_warning_labels_snapshot_date():
    out = sg.flag_sell_into_strength("减仓", "", rsi=60, price=110, ma20=100, ma60=90, macd=1.2)
    assert out is not None
    assert sg._AUDIT_SNAPSHOT_DATE in out and "截至" in out      # 注明快照，不冒充实时
    assert "6/10" in out and "7.4%" in out                        # 统计仍在


def test_hold_warning_labels_snapshot_date():
    out = sg.flag_hold_into_weakness("持有", rsi=40, price=90, ma20=100, ma60=110, macd=-1.2)
    assert out is not None
    assert sg._AUDIT_SNAPSHOT_DATE in out and "截至" in out
