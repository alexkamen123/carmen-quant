# P3b 波动率仓控 实现计划

> 必需子技能：superpowers:subagent-driven-development。步骤用 `- [ ]`。

**目标：** 日报对高波动持仓(realized vol 偏离历史>1.5σ)注入"收紧止损"警示(跌市加重)·单向收紧·不改数值·flag off 默认。
**架构：** `signals/vol_guard.py`(z-score 纯函数 + format_vol_note) → strategy_node join 第7项 → PM 红线 + settings flag。
**技术栈：** Python / statistics / pytest 脱网。

---

## 任务 1：vol_guard.py 纯函数 + 注入模板

**文件：** 创建 `src/finance_agent/signals/vol_guard.py`、`tests/test_signals/test_vol_guard.py`

- [ ] **步骤1：写失败测试**
```python
# tests/test_signals/test_vol_guard.py
from finance_agent.signals import vol_guard as vg


def _closes(prefix_flat=80, tail_wild=10):
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
```
跑 `uv run pytest tests/test_signals/test_vol_guard.py -v` → RED。

- [ ] **步骤2：实现**
```python
# src/finance_agent/signals/vol_guard.py
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
```
跑测试 → 8 PASS。

- [ ] **步骤3：commit** `feat(P3b): vol_guard realized-vol z-score + 收紧止损警示·单向·flag（TDD）`

---

## 任务 2：注入 strategy_node + PM 红线 + settings

**文件：** 改 `graph/workflow.py`、`agents/prompts.py`、`config/settings.yaml`

- [ ] **步骤1：settings.yaml 加块**（放 thesis_invalidation 之后）
```yaml
# ── P3b 波动率仓控（改核心建议·默认 off）──────────────────
# flag on 且美股：日报对高波动持仓(realized vol 偏离历史>1.5σ)注入"收紧止损"警示·跌市加重。
# 纯警示·不改 stop_loss_hint 数值/不改仓位·绝无加仓字样。默认 off：format_vol_note 恒返 ""·逐字节不变。
# ⚠️ 波动阈值是占位启发式·enable 须用户点头。
vol_guard:
  enabled: false
  vol_window: 20
  baseline_window: 60
  sigma_threshold: 1.5
```
校验 `uv run python -c "import yaml; yaml.safe_load(open('config/settings.yaml')); print('ok')"`。

- [ ] **步骤2：workflow.py strategy_node join 加 vol**（读 workflow.py 定位 sue 块 + combined 行·仿 sue 块）
```python
        vol = ""
        try:
            from finance_agent.signals.vol_guard import format_vol_note
            from finance_agent.signals.opportunities import _current_regime
            vol = format_vol_note(s.ticker, regime=_current_regime())
        except Exception:
            vol = ""
        combined = "\n".join(x for x in (evidence, live, refl, dip_note, senti, sue, vol) if x)
```
（把原 combined 行的 tuple 末尾加 `vol`·新增 vol 块在 combined 之前。`_current_regime` 若 import 路径不符据实调整/或直接 market_regime_from_spy guarded。）

- [ ] **步骤3：prompts.py PM_BATCH_SYSTEM 补红线**（紧随现有【盈余惊喜】红线）
```
- 个股材料若含【波动异常】：只用于收紧审慎(收紧止损/控仓)，禁止倒推成加仓/更激进依据。
```

- [ ] **步骤4：验证**
```
uv run python -c "import yaml; yaml.safe_load(open('config/settings.yaml'))"
uv run python -c "from finance_agent.graph.workflow import build_graph; build_graph(); print('graph ok')"  # 入口名不符先 grep
uv run python -c "from finance_agent.signals.vol_guard import format_vol_note; print(repr(format_vol_note('AAPL')))"  # flag off→''
uv run pytest tests/ -k "reflection or pm or strategy or workflow or sell_guard" -q
```
预期 flag off 打印 `''`；回归绿。

- [ ] **步骤5：commit** `feat(P3b): strategy_node join vol_note + PM 红线 + settings（护栏1&3层·flag off）`

---

## 任务 3：全量回归 + 收口
- [ ] 全量 `uv run pytest tests/ -x -q`（唯一允许失败=test_guards portfolio.yaml 正交）
- [ ] flag-off 逐字节：确认 vol_guard.enabled=false → format_vol_note 恒 ""·strategy_evidence 不变
- [ ] 进展.md 留痕（现在段 + 决策史"P3b 警示不改数值·新算z-score不复用ATR" + 台账）
- [ ] commit `docs(P3b): 波动率仓控落地留痕`

---

## 自检
覆盖：§2 判据→任务1 compute_vol_zscore；§3.1 纯函数→任务1；§3.2 注入→任务2；§3.3 flag→任务2；§6 测试→任务1/2；§8 验收→任务3。占位符：无(代码完整·regime import 标"据实调整"非占位)。类型一致：compute_vol_zscore/format_vol_note/_vg_cfg 跨任务一致。

## 执行：子代理驱动（superpowers:subagent-driven-development）。
