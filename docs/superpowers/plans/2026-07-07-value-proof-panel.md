# 价值证明·多维体温计面板 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在价值体检卡加一个折叠面板「🌡️ 价值证明·多维体温计」，把 4 个当前只有终端 CLI 出口的孤儿读数（RankIC / OOS / SUE edge / 护栏 A/B）**只读**聚进来，各标口径、诚实标注样本不足、每个 guarded、绝不相加。因 value-report 已周六 launchd 自动跑 → 面板自动周更。

**架构：** 新模块 `value/value_proof.py`（只读聚合 4 读数 + 渲染成面板正文字符串）；`run_value_report` 把读数写进 `m["thermometer"]`；`build_value_card` 加一个 `_panel(...)`。纯观测渲染层·零建议/仓位/权重改动·无新 flag/命令/调度。

**技术栈：** Python / 现有折叠面板 schema / pytest（脱网·构造 m dict）

---

## ⚠️ 两条实现铁则（防副作用）

1. **只读**：面板**禁用** `run_rankic_monitor`/`run_oos_monitor`（它们会记录月度快照+联网取数·有副作用）。改用只读裁决：`rankic_monitor.rankic_decay_verdict()`（读 jsonl·无写）、`oos_monitor.oos_decay_verdict(...)`（读 jsonl·无 fetch）、`sue_edge.sue_edge_reading(db_path)`（只读 DB）、`shadow_ab.report_shadow_ab(db_path)`（只读 DB·**非** `sweep()`，sweep 会联网回填）。
2. **绝不相加**：4 读数口径各异（Spearman IC / regime edge / 30日超额 / 篮子超额），面板只并列展示、绝不合成单一"价值分"。面板顶注明。

> 实现首步核实（各函数确切签名·跨改动可能变）：`rankic_decay_verdict` / `oos_decay_verdict` / `report_shadow_ab` 的参数与返回键（下方代码按盘点写·实现时读代码对齐）。`sue_edge_reading` 是本项目本轮自建·结构确定。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| 创建 `src/finance_agent/value/value_proof.py` | 只读聚合 4 读数 `gather_thermometer_readings` + 渲染 `thermometer_section` |
| 改 `src/finance_agent/value/report.py` | `run_value_report` 写 `m["thermometer"]`；`build_value_card` 插面板 |
| 创建 `tests/test_value/test_value_proof.py` | 渲染 + guarded 降级 + 不相加 测试 |

---

## 任务 1：value_proof.py 聚合 + 渲染（纯函数 TDD 主战场）

**文件：**
- 创建：`src/finance_agent/value/value_proof.py`
- 测试：`tests/test_value/test_value_proof.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_value/test_value_proof.py
from finance_agent.value import value_proof as vp


def _full_thermo():
    return {
        "rankic": {"status": "healthy", "n_measured": 3, "k_needed": 2, "current_ic": 0.08},
        "oos": {"status": "healthy", "horizon": 30, "n_testable": 5, "current_edge": 0.4},
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
    monkeypatch.setattr(m, "_read_shadow", lambda db_path: None)
    out = m.gather_thermometer_readings(db_path=None)
    assert out["rankic"] is None and set(out) == {"rankic", "oos", "sue", "shadow"}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_value/test_value_proof.py -v`
预期：FAIL，`ModuleNotFoundError: ... value_proof`

- [ ] **步骤 3：编写最少实现代码**

```python
# src/finance_agent/value/value_proof.py
"""价值证明·多维体温计：把 4 个孤儿读数只读聚进价值卡一个面板。

铁则：① 只读（用各读数的只读裁决·不触发月度记录/联网）；② 绝不相加（4 口径各异·只并列）；
③ 每读数 guarded（任一失败→该行"暂无数据"·不拖垮价值卡）；④ 样本不足诚实标注。
纯观测渲染层·不碰任何 recommendation/仓位/权重。
"""
from __future__ import annotations

_HEADER = "_以下是不同维度的体温计，各看各的口径，**不合成一个总分**；样本不足会诚实标注。_"


# ---- 只读读数（各自 guarded 由 gather 兜） ----
def _read_rankic():
    from finance_agent.value.rankic_monitor import rankic_decay_verdict
    return rankic_decay_verdict()            # 读 jsonl·无写；{status,n_measured,k_needed,current_ic}


def _read_oos():
    from finance_agent.backtest.oos_monitor import oos_decay_verdict
    return oos_decay_verdict(horizon=30)     # 读 jsonl·无 fetch；{status,horizon,n_testable,current_edge}


def _read_sue(db_path):
    from finance_agent.value.sue_edge import sue_edge_reading
    return sue_edge_reading(db_path=db_path)  # 只读 DB


def _read_shadow(db_path):
    from finance_agent.value.shadow_ab import report_shadow_ab
    return report_shadow_ab(db_path=db_path)  # 只读 DB·非 sweep（不联网回填）


def gather_thermometer_readings(db_path=None) -> dict:
    """4 读数各自 guarded 聚成 dict；任一失败该键 None。"""
    out = {}
    for key, fn in (("rankic", lambda: _read_rankic()),
                    ("oos", lambda: _read_oos()),
                    ("sue", lambda: _read_sue(db_path)),
                    ("shadow", lambda: _read_shadow(db_path))):
        try:
            out[key] = fn()
        except Exception:
            out[key] = None
    return out


# ---- 各维度渲染（verdict→中文 label·各套词表各写） ----
def _rankic_line(d):
    if not d:
        return "• 建议方向排序力：⚪ 暂无数据"
    lab = {"healthy": "✅ 方向仍有排序力", "decaying": "🚨 连续衰减·建议复核策略",
           "insufficient_history": "⚪ 可测月不足·暂不下结论"}.get(d.get("status"), d.get("status"))
    ic = d.get("current_ic")
    return (f"• 建议方向排序力（RankIC·方向vs7日超额）：{lab}"
            f"（可测月 {d.get('n_measured', 0)}/{d.get('k_needed', 2)}·当前 IC {ic}）")


def _oos_line(d):
    if not d:
        return "• 方案A 还有效吗：⚪ 暂无数据"
    lab = {"healthy": "✅ regime 增量仍在", "decaying": "🚨 衰减·建议复核护栏",
           "insufficient_regime_data": "⚪ 跌市样本不足·暂不下结论"}.get(d.get("status"), d.get("status"))
    return (f"• 方案A 还有效吗（walk-forward OOS·{d.get('horizon', 30)}日）：{lab}"
            f"（可测 {d.get('n_testable', 0)}·当前 edge {d.get('current_edge')}）")


def _sue_line(d):
    if not d:
        return "• 盈余惊喜漂移 edge：⚪ 暂无数据"
    b = d.get("beat", {}) or {}
    lab = {"edge_present": "✅ 漂移兑现", "no_edge": "❌ 无边际",
           "insufficient": "⚪ 样本不足·暂不下结论"}.get(b.get("verdict"), b.get("verdict"))
    return (f"• 盈余惊喜漂移 edge（beat 后30日vs基准）：{lab}"
            f"（beat n={b.get('n', 0)}·命中率 {b.get('hit_rate')}·均超额 {b.get('mean_excess')}）")


def _shadow_line(d):
    if not d:
        return "• 护栏 A/B 反事实：⚪ 暂无数据"
    lab = {"guardrail_helps": "✅ 护栏有帮助", "guardrail_hurts": "🚨 护栏有害·建议复核",
           "neutral": "➖ 中性", "insufficient": "⚪ 分歧样本不足·暂不下结论"}.get(d.get("verdict"), d.get("verdict"))
    return (f"• 护栏 A/B 反事实（开vs关·7日超额）：{lab}"
            f"（分歧 n={d.get('n', 0)}·on {d.get('on_mean')} / off {d.get('off_mean')}）")


def thermometer_section(thermo: dict) -> str:
    """渲染面板正文（markdown 多行·绝不相加·各行 guarded label）。"""
    thermo = thermo or {}
    lines = [_HEADER, "",
             _rankic_line(thermo.get("rankic")),
             _oos_line(thermo.get("oos")),
             _sue_line(thermo.get("sue")),
             _shadow_line(thermo.get("shadow"))]
    return "\n".join(lines)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_value/test_value_proof.py -v`
预期：4 项 PASS

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/value/value_proof.py tests/test_value/test_value_proof.py
git commit -m "feat(巩固): 价值证明体温计聚合+渲染·只读·绝不相加·guarded（TDD）"
```

---

## 任务 2：接进价值卡（run_value_report 写 m + build_value_card 插面板）

**文件：**
- 修改：`src/finance_agent/value/report.py`
- 测试：`tests/test_value/test_value_proof.py`（追加整卡断言）

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_value/test_value_proof.py 追加
import json
from finance_agent.value.report import build_value_card


def _min_metrics():
    # 造 build_value_card 能吃的最小 m（照 test_db/test_value.py 的 compute_value_metrics 字段·此处直接给）
    # 若字段不全导致其它面板崩，实现时用 compute_value_metrics(tmp 空库) 造 m 再塞 thermometer
    return {"data_through": "2026-07-06", "thermometer": {"rankic": None, "oos": None,
            "sue": None, "shadow": None}}


def test_card_contains_thermometer_panel(tmp_path):
    from finance_agent.value.metrics import compute_value_metrics
    from finance_agent.db import tracker
    db = tmp_path / "t.db"; tracker.init_db(db)
    m = compute_value_metrics(str(db))
    m["thermometer"] = {"rankic": None, "oos": None, "sue": None, "shadow": None}
    card = build_value_card(m)
    panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
    therm = next(p for p in panels if "体温计" in p["header"]["title"]["content"])
    body = therm["elements"][0]["content"]
    assert "排序力" in body and "护栏" in body and "暂无数据" in body


def test_card_still_builds_without_thermometer_key(tmp_path):
    # 向后兼容：m 没 thermometer 键也不崩（面板缺省空读数）
    from finance_agent.value.metrics import compute_value_metrics
    from finance_agent.db import tracker
    db = tmp_path / "t.db"; tracker.init_db(db)
    m = compute_value_metrics(str(db))
    m.pop("thermometer", None)
    card = build_value_card(m)   # 不崩
    assert card["schema"] == "2.0"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_value/test_value_proof.py -k card -v`
预期：FAIL（面板未接入·`next(...)` StopIteration）

- [ ] **步骤 3：编写最少实现代码**

3a. `build_value_card` 里插面板（在"🧪 名词小课堂"面板附近·同属"证明能力"族）。用 `m.get("thermometer")` 兜底空 dict：
```python
        _panel("🌡️ 价值证明 · 多维体温计（建议/行为双向打分）",
               _thermometer_from_m(m)),
```
并在 report.py 加薄封装（import value_proof）：
```python
def _thermometer_from_m(m):
    from finance_agent.value.value_proof import thermometer_section
    return thermometer_section(m.get("thermometer") or {})
```

3b. `run_value_report`（report.py:483 区，`m = compute_value_metrics(...)` 之后、`build_value_card(m)` 之前）guarded 写入读数：
```python
    try:
        from finance_agent.value.value_proof import gather_thermometer_readings
        m["thermometer"] = gather_thermometer_readings(db_path=db_path)
    except Exception:
        m["thermometer"] = {}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_value/test_value_proof.py -v`
预期：6 项全 PASS（4 渲染 + 2 整卡）

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/value/report.py tests/test_value/test_value_proof.py
git commit -m "feat(巩固): 价值证明体温计面板接进价值体检卡（run_value_report 只读聚合·周六自动周更）"
```

---

## 任务 3：全量回归 + 收口

- [ ] **步骤 1：全量零回归** `uv run pytest tests/ -x -q`（唯一允许失败=已知 test_guards portfolio.yaml 正交）。重点确认 `test_db/test_value.py`、`test_card_fold.py`、`test_scorecard` 不破。
- [ ] **步骤 2：value-report 干跑冒烟** `uv run finance-agent value-report --skip-notify 2>&1 | tail -30`——确认卡生成含「体温计」面板、4 行读数（多为"暂无数据/样本不足"·诚实）、不崩、无网络挂起。
- [ ] **步骤 3：更新 进展.md**（现在段加"巩固轮·价值证明面板落地" + 决策史"C 已满足/只读避副作用" + 素材台账）。
- [ ] **步骤 4：Commit** `docs(巩固): 价值证明面板落地留痕`

---

## 自检结果（对照 spec）

**规格覆盖度：** §1 面板→任务2；§2 四读数→任务1 `_read_*`+`_*_line`；§3 三铁律（不相加/insufficient/guarded）→任务1 测试 test_section_no_total/insufficient/none_degrades/gather_guarded；§4.1 value_proof.py 新模块→任务1；§4.2 接卡→任务2；§4.3 无调度改动（C 已满足）→计划无 cron 任务；§6 测试→任务1/2；§8 验收→任务3。

**占位符扫描：** §"实现首步核实"标注 rankic/oos/shadow 只读函数确切签名——非占位（给了盘点确认的键名·实现读代码对齐）。任务2 步骤1 的 `_min_metrics` 注明"字段不全则用 compute_value_metrics 造 m"·并在正式测试里就用了 compute_value_metrics·非占位。

**类型一致性：** `gather_thermometer_readings`/`thermometer_section`/`_read_*`/`_*_line` 跨任务一致；`m["thermometer"]` 的 4 键（rankic/oos/sue/shadow）在 gather/render/test 三处一致；各 verdict 词表（healthy/decaying/insufficient_history · edge_present/no_edge/insufficient · guardrail_helps/hurts/neutral）与盘点确认的返回值一致。

---

## 执行交接

**计划已保存到 `docs/superpowers/plans/2026-07-07-value-proof-panel.md`。两种执行方式：**
1. **子代理驱动（推荐）** — 每任务一个新子代理 + 两阶段审查（superpowers:subagent-driven-development）
2. **内联执行** — 当前会话批量执行（superpowers:executing-plans）

**选哪种方式？**
