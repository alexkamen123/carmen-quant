# P3b 波动率仓控（Vol Guard）— 设计规格

> 日期：2026-07-07 ｜ 依据：PRD-roadmap P3b + Explore 地基摸底 ｜ 自主设计（用户已授权"几乎全自主·设计岔口自决"）
> 状态：设计定档，待 writing-plans + TDD
> 北极星：个股波动率异常放大时提示收紧止损 → **高波动少扛 = 少亏**（第 1 维·已有持仓操作指导）

---

## 0. 自主设计决策（地基摸底后拍板·记录备查）

1. **新建 `signals/vol_guard.py`·不扩展 `dip_atr_adaptive`**：后者是盘中暴跌告警的"触发阈值自适应"（降噪/告警向），P3b 是日报注入给 PM 的仓位/建议向，不同管线不重叠。
2. **新算 realized-vol z-score·不复用 ATR%**：现有只有单点 `atr_pct`（绝对波动），无"波动 vs 自身历史分布的 σ 偏离"。P3b 判据须新算。
3. **警示不改数值**（照 `sell_guard`）：止损是 `Stock.stop_loss_hint` 自由文本、由 PM 产出；P3b 追加一行"收紧止损"警示注入 PM 材料，**绝不改写 stop_loss_hint 数值、绝无加仓字样**（单向收紧）。
4. **改核心建议 → flag off 默认**（`vol_guard.enabled: false`·enable 待用户点头）；MVP 不建观测事件表（保持精简·警示类非因子分）。

---

## 1. 功能形状（一句话）

日报 `strategy_node` 对每只美股持仓算"近期 realized 波动率相对自身历史分布的 z-score"；`z > +1.5σ`（波动异常放大）→ 向 PM 材料注入一行"⚠️ 波动异常放大·建议收紧止损/审慎"警示（跌市 regime 再加重）；单向收紧、零 LLM、flag off 时逐字节不变。

---

## 2. 判据与联动

- **realized vol**：日线收益率 `close.pct_change()` 的滚动 std（窗口 `VOL_WINDOW=20` 交易日）。
- **z-score**：`z = (最新 vol − mean(历史 vol 序列)) / std(历史 vol 序列)`，历史序列取 trailing `BASELINE_WINDOW=60` 个滚动 vol 值。
- **触发**：`z > SIGMA(默认 1.5)` → 出警示。**只警示高波动**（低波动 z<−1.5 不触发·收紧止损只对高波动有意义）。
- **regime 联动**：读 `market_regime_from_spy`（返 up/down/None）；`regime == "down"`（跌市）时措辞加重"跌市高波动·尤需收紧/缩仓"。
- **闭嘴闸**：价格序列不足（< VOL_WINDOW+BASELINE_WINDOW）/ 取数失败 / std==0 → z=None → 不注入（静默·宁漏不误）。

---

## 3. 落地设计（逐组件）

### 3.1 纯函数 · `signals/vol_guard.py`（新建·TDD 主战场）
- `vol_guard_enabled()` / `_vg_cfg()`：读 settings `vol_guard` 块（照 `sue_factor._load_block`·缺省 off）。
- `compute_vol_zscore(closes, vol_window=20, baseline_window=60) -> float | None`：纯函数（脱网单测主战场）。收益率滚动 std → 最新 vol 的 z-score；样本不足/std==0/含 NaN → None。
- `format_vol_note(ticker, regime=None, closes_fn=None) -> str`：
  - flag off → `""`（护栏第一层）；
  - 取 closes（`closes_fn` 可注入测试；生产默认用 `backtest/discovery.fetch_ohlcv` 拿收盘序列·try/except→""）；
  - `compute_vol_zscore`；`z <= SIGMA` 或 None → `""`；
  - `z > SIGMA` → 确定性模板：`⚠️【波动异常】{ticker} 近{VOL_WINDOW}日波动率放大到历史 {z:.1f}σ，建议收紧止损/审慎控仓。` regime=="down" 追加"（当前跌市，高波动尤需收紧、勿高波动里死扛）"。**单向收紧·绝无加仓/买入/更激进字样。**

### 3.2 注入 · `graph/workflow.py::strategy_node` + `agents/prompts.py`
- `strategy_node`（workflow.py:290 combined tuple）加第 7 项 `vol`：仿 sue 块 try/except，`vol = format_vol_note(s.ticker, regime=_current_regime())`（regime 复用 `signals/opportunities._current_regime` 或 `market_regime_from_spy`·guarded）。`combined = "\n".join(x for x in (evidence, live, refl, dip_note, senti, sue, vol) if x)`。空串被 `if x` 过滤 → flag off 逐字节不变（护栏第三层）。
- `PM_BATCH_SYSTEM` 补红线：【波动异常】只用于收紧审慎（收紧止损/控仓）·禁止倒推成加仓/更激进。

### 3.3 flag · `config/settings.yaml`
```yaml
# ── P3b 波动率仓控（改核心建议·默认 off）──────────────────
# flag on 且美股：日报对高波动持仓(realized vol 偏离历史>1.5σ)注入"收紧止损"警示·跌市加重。
# 纯警示·不改 stop_loss_hint 数值/不改仓位·绝无加仓字样。默认 off：format_vol_note 恒返 ""·逐字节不变。
# ⚠️ 波动阈值是占位启发式(非拟合)·enable 须用户点头。
vol_guard:
  enabled: false
  vol_window: 20
  baseline_window: 60
  sigma_threshold: 1.5
```

---

## 4. 反过拟合 / 诚实边界
- z-score 阈值 1.5σ、窗口 20/60 均为顶部命名常量·占位启发式（非拟合·未做"找好看阈值"调参）。
- 不预测价格·不做无成本回测·不堆因子——只是"波动异常→提示审慎"的规则警示。
- 数据不足/std==0 静默返 None·宁漏不误。措辞只单向收紧。

## 5. 未来函数
- realized vol 只用**已收盘**的历史日线（`pct_change` 天然只用过去）·无前视。z-score 的 baseline 是 trailing 窗口（当前之前）。

## 6. 测试计划（脱网 mock·全绿零回归）
**`tests/test_signals/test_vol_guard.py`**
1. `compute_vol_zscore` 高波动尾段 → 正大 z（构造前段平稳+末段剧烈的 closes）
2. 平稳序列 → z 小/不触发
3. 样本不足（< window+baseline）→ None
4. std==0（全等收益）→ None；含 NaN → None
5. `format_vol_note` flag off → `""`（monkeypatch cfg enabled=False）
6. `format_vol_note` z>1.5σ → 含"收紧止损"、无"加仓/买入"
7. regime=="down" → 措辞含"跌市"加重；regime=="up"/None → 不加重
8. z<=1.5σ → `""`
**回归重点**：`test_pm_fallback`/`test_reflection`（strategy_node join 不破原注入）、`test_sell_guard`。

## 7. 文件清单
**新建**：`signals/vol_guard.py`、`tests/test_signals/test_vol_guard.py`
**改**：`graph/workflow.py`、`agents/prompts.py`、`config/settings.yaml`

## 8. 验收 + 对抗审查
**验收**：① flag off → strategy_evidence 逐字节不变；② z 计算正确·样本不足静默 None；③ 警示单向收紧·无加仓·不改 stop_loss_hint；④ regime 联动措辞正确；⑤ 新测试全绿 + 全量零回归。
**对抗审查**：占位阈值有没有被当验证过的信号？未来函数（vol/z 只用已收盘）？措辞只单向收紧（无加仓）？flag off 三重护栏？取数失败 guarded 不拖垮日报？regime 读取失败 graceful？

## 9. 下一步 writing-plans。flag off 合并·enable 待用户点头。
