from finance_agent.value import value_proof as vp


def _full_thermo():
    return {
        "rankic": {"status": "healthy", "n_measured": 3, "k_needed": 2, "current_ic": 0.08},
        "oos": {"status": "healthy", "horizon": 7, "n_testable": 5, "current_edge": 0.4},
        "sue": {"beat": {"n": 12, "hit_rate": 0.58, "mean_excess": 1.2, "verdict": "edge_present"},
                "miss": {"n": 8, "hit_rate": 0.6, "mean_excess": -1.5, "verdict": "insufficient"},
                "n_total": 20},
        "shadow": {"verdict": "guardrail_helps", "n": 15, "on_mean": 0.5, "off_mean": 0.1, "edge": 0.4},
    }


def test_section_has_four_dimensions_no_total():
    s = vp.thermometer_section(_full_thermo())
    assert "排序力" in s and "方案A" in s and "盈余惊喜" in s and "护栏" in s
    assert "不合成" in s                      # 顶部"绝不相加"声明
    assert "总分" not in s or "不合成一个总分" in s   # 绝无合成总分


def test_insufficient_shown_honestly():
    thermo = {"rankic": {"status": "insufficient_history", "n_measured": 1, "k_needed": 2, "current_ic": None},
              "oos": None, "sue": None, "shadow": None}
    s = vp.thermometer_section(thermo)
    assert "样本不足" in s or "暂不下结论" in s or "暂无数据" in s


def test_each_reading_none_degrades_not_crash():
    s = vp.thermometer_section({"rankic": None, "oos": None, "sue": None, "shadow": None})
    assert "暂无数据" in s                     # 4 行全降级·不崩
    # 面板仍有 4 个维度标签
    for kw in ("排序力", "方案A", "盈余惊喜", "护栏"):
        assert kw in s


def test_gather_is_guarded(monkeypatch):
    # 某读数抛异常 → gather 该键返 None·不抛
    import finance_agent.value.value_proof as m
    monkeypatch.setattr(m, "_read_rankic", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(m, "_read_oos", lambda: None)
    monkeypatch.setattr(m, "_read_sue", lambda db_path: None)
    monkeypatch.setattr(m, "_read_shadow", lambda: None)
    out = m.gather_thermometer_readings(db_path=None)
    assert out["rankic"] is None and set(out) == {"rankic", "oos", "sue", "shadow"}
