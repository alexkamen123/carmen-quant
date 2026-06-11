# tests/test_signals/test_opportunities.py
"""task#31：今日机会扫描（scan_opportunities / format_opportunity_section）测试（零网络）。"""
from finance_agent.signals import opportunities as opp


def _sig(t_stat, label="RSI超卖", n=100, alpha=2.5, beat=0.65):
    return {"strategy": "x", "label": label, "n_signals": n,
            "avg_alpha": alpha, "beat_rate": beat, "t_stat": t_stat, "reliable": True}


def _wire(monkeypatch, universe, active_map):
    monkeypatch.setattr(opp, "_load_valid_map",
                        lambda: {(t, "s"): {} for t in universe})
    monkeypatch.setattr(opp, "get_active_signals",
                        lambda tk: active_map.get(tk, []))


def test_scan_excludes_held_and_tags_watch(monkeypatch):
    _wire(monkeypatch, ["MU", "AVGO", "PANW"], {
        "MU": [_sig(7.4)],          # 已持仓 → 必须排除（最强也不进机会区）
        "AVGO": [_sig(5.7)],        # 观察池 → 标注
        "PANW": [_sig(4.3)],
    })
    out = opp.scan_opportunities(exclude_tickers={"MU"}, watch_tickers={"AVGO"})
    assert [o["ticker"] for o in out] == ["AVGO", "PANW"]   # 按 t 降序
    assert out[0]["in_watch"] is True and out[1]["in_watch"] is False


def test_scan_single_ticker_failure_isolated(monkeypatch):
    monkeypatch.setattr(opp, "_load_valid_map", lambda: {("A", "s"): {}, ("B", "s"): {}})

    def boom_or_ok(tk):
        if tk == "A":
            raise RuntimeError("取数失败")
        return [_sig(3.0)]

    monkeypatch.setattr(opp, "get_active_signals", boom_or_ok)
    out = opp.scan_opportunities()
    assert [o["ticker"] for o in out] == ["B"]   # A 失败不拖垮 B


def test_format_section(monkeypatch):
    opps = [
        {"ticker": "AVGO", "in_watch": True, "signals": [_sig(5.7, beat=0.69), _sig(3.0)]},
        {"ticker": "PANW", "in_watch": False, "signals": [_sig(4.3, beat=0.61)]},
    ]
    out = opp.format_opportunity_section(opps, semi_room_note="⚠️ 红线提示")
    assert "AVGO" in out and "跑赢SPY 69%" in out and "t=5.7" in out
    assert "（另 1 个信号）" in out and "已在观察池" in out
    assert "⚠️ 红线提示" in out and "in-sample" in out          # 诚实口径必须在
    assert "不构成下单指令" in out


def test_format_empty_and_truncation():
    assert opp.format_opportunity_section([]) == ""
    many = [{"ticker": f"T{i}", "in_watch": False, "signals": [_sig(5.0 - i * 0.1)]}
            for i in range(8)]
    out = opp.format_opportunity_section(many)
    assert "另有 3 只触发中" in out                  # MAX_SHOW=5，8-5=3
    assert "T6" not in out.split("另有")[0]          # 截断真的生效
