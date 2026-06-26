# tests/test_backtest/test_strategy_weights.py
"""L2c 策略族自适应权重测试（零网络、零真实 DB）。

所有用例显式注入 cfg（monkeypatch _load_settings），不依赖 config/settings.yaml，
保证 CI 与本地一致、与线上配置解耦。
"""
import json

import pytest

from finance_agent.backtest import strategy_weights as sw


def _cfg(**over):
    c = dict(sw._DEFAULTS)
    c.update(over)
    return c


def _edge(beats: dict, n_combos: dict | None = None, total: int = 30):
    """构造 compute_strategy_edge 的最小子集：families + _raw_families。
    beats: {family: beat_rate}; n_combos: {family: 组合数}（默认 5）。"""
    n_combos = n_combos or {f: 5 for f in beats}
    families = [{"family": f, "label": f, "n_combos": n_combos.get(f, 5),
                 "total_signals": total, "w_avg_alpha": 1.0,
                 "reliable_combos": 1} for f in beats]
    raw = {f: {"total_signals": total, "beat_w": beats[f] * total} for f in beats}
    return {"families": families, "_raw_families": raw}


def test_flag_off_weight_is_one_and_no_write(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg(enabled=False))
    wpath = tmp_path / "w.json"
    lpath = tmp_path / "log.jsonl"
    res = sw.update_weights_from_edge(_edge({"rsi": 0.9}), path=wpath, log_path=lpath)
    assert res["changed"] is False
    assert not wpath.exists()          # flag 关绝不写盘
    assert not lpath.exists()
    assert sw.get_strategy_weight("rsi_14_30", path=wpath) == 1.0


def test_broad_edge_snapshot_documented():
    """广历史 edge 快照须带日期、且 ma(金叉) 仍标记为反指——快照陈旧时此测试是改动锚点。"""
    assert sw._BROAD_EDGE_SNAPSHOT_DATE.startswith("2026-")
    assert sw._BROAD_EDGE_7D["ma"] < sw._REVERSE_EDGE      # 金叉=广历史反指（触发护栏的依据）


def test_guardrail_off_by_default_unchanged(tmp_path, monkeypatch):
    """默认 broad_edge_guardrail=False：金叉(ma)高 beat 仍被上调，现有行为零变化。"""
    t = sw.compute_targets(_edge({"ma": 0.9}), _cfg())   # 默认护栏关
    assert t["ma"] > 1.0


def test_guardrail_caps_reverse_indicator():
    """护栏开：广历史反指族(ma 金叉, edge=-0.082)无论近期 beat 多高，压到下限。"""
    t = sw.compute_targets(_edge({"ma": 0.95}), _cfg(broad_edge_guardrail=True))
    assert t["ma"] == sw._DEFAULTS["min_weight"]          # 0.7，被护栏压住


def test_guardrail_spares_positive_edge_family():
    """护栏开：广历史正 edge 族(rsi, +0.065)不受影响、照常按 beat 上调。"""
    t = sw.compute_targets(_edge({"rsi": 0.9}), _cfg(broad_edge_guardrail=True))
    assert t["rsi"] > 1.0


def test_high_beat_raises_weight_low_beat_lowers(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg())
    wpath = tmp_path / "w.json"
    lpath = tmp_path / "log.jsonl"
    res = sw.update_weights_from_edge(
        _edge({"rsi": 0.9, "mom": 0.3}), path=wpath, log_path=lpath)
    assert res["changed"] is True
    # rsi beat 高 → 权重升过 1.0；mom beat 低 → 降到 1.0 以下
    assert res["weights"]["rsi"] > 1.0
    assert res["weights"]["mom"] < 1.0
    assert wpath.exists() and lpath.exists()


def test_weight_clamped_to_bounds(tmp_path, monkeypatch):
    # 极端 beat + 高 sensitivity，多轮迭代也不能越界
    monkeypatch.setattr(sw, "_load_settings",
                        lambda: _cfg(sensitivity=5.0, damping=1.0, max_step=1.0))
    wpath = tmp_path / "w.json"
    for _ in range(20):
        sw.update_weights_from_edge(_edge({"rsi": 1.0}), path=wpath,
                                    log_path=tmp_path / "log.jsonl")
    w = sw.load_weights(wpath)["weights"]["rsi"]
    assert w <= sw._DEFAULTS["max_weight"] + 1e-9
    # 反向极端
    wpath2 = tmp_path / "w2.json"
    for _ in range(20):
        sw.update_weights_from_edge(_edge({"rsi": 0.0}), path=wpath2,
                                    log_path=tmp_path / "log2.jsonl")
    w2 = sw.load_weights(wpath2)["weights"]["rsi"]
    assert w2 >= sw._DEFAULTS["min_weight"] - 1e-9


def test_max_step_damps_single_round(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings",
                        lambda: _cfg(sensitivity=10.0, damping=1.0, max_step=0.05))
    wpath = tmp_path / "w.json"
    res = sw.update_weights_from_edge(_edge({"rsi": 1.0}), path=wpath,
                                      log_path=tmp_path / "log.jsonl")
    # 单轮从 1.0 出发，max_step=0.05 → 不超过 1.05
    assert res["weights"]["rsi"] == pytest.approx(1.05, abs=1e-9)


def test_min_combos_gate_keeps_one(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg(min_combos=10))
    wpath = tmp_path / "w.json"
    res = sw.update_weights_from_edge(
        _edge({"rsi": 0.9}, n_combos={"rsi": 3}), path=wpath,
        log_path=tmp_path / "log.jsonl")
    # 族内组合数 3 < min_combos 10 → 目标 1.0，从 1.0 出发无变化
    assert res["changed"] is False
    assert res["weights"]["rsi"] == 1.0


def test_param_version_increments_only_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg())
    wpath = tmp_path / "w.json"
    lpath = tmp_path / "log.jsonl"
    r1 = sw.update_weights_from_edge(_edge({"rsi": 0.9}), path=wpath, log_path=lpath)
    assert r1["param_version"] == "v1"
    # beat 中性 0.5 → 目标 1.0，但 rsi 已 >1.0 故仍会回拉（变化），换成完全稳态：
    # 用与当前权重相符的 beat 让 target==current，制造无变化轮
    cur = sw.load_weights(wpath)["weights"]["rsi"]
    beat_eq = 0.5 + (cur - 1.0) / _cfg()["sensitivity"]
    r2 = sw.update_weights_from_edge(_edge({"rsi": beat_eq}), path=wpath, log_path=lpath)
    assert r2["changed"] is False
    assert r2["param_version"] == "v1"   # 无变化不递增


def test_changelog_appended_with_deltas(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg())
    wpath = tmp_path / "w.json"
    lpath = tmp_path / "log.jsonl"
    sw.update_weights_from_edge(_edge({"rsi": 0.9}), path=wpath, log_path=lpath)
    lines = lpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["param_version"] == "v1"
    assert "rsi" in rec["deltas"]
    assert "ts" in rec and "weights" in rec


def test_get_strategy_weight_family_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "_load_settings", lambda: _cfg())
    wpath = tmp_path / "w.json"
    sw.update_weights_from_edge(_edge({"ma_align": 0.9}), path=wpath,
                               log_path=tmp_path / "log.jsonl")
    # ma_align_5_20_60 必须映射到 ma_align 族（不能误吞到 ma）
    w = sw.get_strategy_weight("ma_align_5_20_60", path=wpath)
    assert w > 1.0
    # ma 族无记录 → 1.0
    assert sw.get_strategy_weight("ma_5_20", path=wpath) == 1.0


def test_corrupt_weights_file_falls_back(tmp_path):
    wpath = tmp_path / "w.json"
    wpath.write_text("{ not json", encoding="utf-8")
    d = sw.load_weights(wpath)
    assert d["weights"] == {}
    assert d["param_version"] == "v0"
