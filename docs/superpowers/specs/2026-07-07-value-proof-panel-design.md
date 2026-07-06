# 价值证明·多维体温计面板 — 设计规格（巩固轮）

> 日期：2026-07-07 ｜ 类型：巩固（接线现有测量·不造新能力）｜ 依据：测量表面盘点（Explore）+ 用户拍板 A+C
> 状态：设计定档（用户 2026-07-07 批准），**待 writing-plans + TDD 实现**
> 北极星对齐：把"持续为建议/行为双向打分"从散落 CLI 变成**每周自动出现在价值体检卡**的统一出口

---

## 0. 关键事实澄清（决定范围）

盘点曾报"value-report 没进调度(crontab-local.txt 无)"——**误报**。已核实 `~/Library/LaunchAgents/com.zhouyihao.carmen-value.plist` **Weekday 6(周六) 10:00 跑 value-report**（launchd 才是权威·GitHub cron 已废·见 CLAUDE.md 调度表）。`crontab-local.txt` 是遗留参考文件。

**结论：C（上 cron）已满足、无需改调度。** 本轮收敛成纯 **A**：把 4 个孤儿读数收进 `build_value_card` 折叠面板。因 value-report 本就周六自动跑，收进去即等于"每周自动出双向打分"。

---

## 1. 功能形状（一句话）

在价值体检卡（`value/report.py::build_value_card`）新增一个折叠面板「🌡️ 价值证明·多维体温计」，聚合 4 个当前只有 CLI 终端出口的孤儿读数（RankIC / OOS walk-forward / SUE 漂移 edge / 护栏 A/B 反事实），各标各的口径、诚实标注样本不足、每个 guarded，**纯观测渲染层·不改任何建议/仓位/权重·不新增推送**。

---

## 2. 面板内容（4 读数 · 各一行 · 复用只读）

| 维度 | 读数来源（复用·只读） | 一行显示 | 口径标签 |
|---|---|---|---|
| 建议方向排序力 | `value/rankic_monitor.py::run_rankic_monitor` | `RankIC=x · {有排序力/衰减/样本不足}` | Spearman·方向 vs 7日超额 |
| 方案A 还有效吗 | `backtest/oos_monitor.py::run_oos_monitor` | `regime 增量=x · {healthy/decaying/可测月不足}` | walk-forward OOS·2年滚动 |
| 盈余惊喜漂移 edge | `value/sue_edge.py::sue_edge_reading` | `beat 命中率 x·均超额 y · {edge/无/样本不足}` | 事件后 30 日 vs 基准 |
| 护栏 A/B 反事实 | `value/shadow_ab.py` 裁决（**value-report 已算好**·传入·不重算） | `on vs off 超额差 x · {helps/hurts/中性/不足}` | 每日两篮子·7日超额 |

---

## 3. 三条铁律焊进面板（延续项目诚实纪律）

1. **各维度独立·绝不合成一个分**：面板顶一句说明"以下是不同维度的体温计，各看各的，不合成总分"（延续 `strategy_scorecard.py:11` "口径独立·绝不相加" + `report.py` 脚注纪律）。
2. **样本不足诚实标注**：任一读数 insufficient → 显示"样本不足·暂不下结论"（早期大多如此·如实不粉饰·对齐 honest-no-whitewash）。
3. **每读数 guarded**：`try/except` 单独包每个读数，任一失败 → 该行显示"该维度暂无数据"，**绝不拖垮整张价值卡**（同 value-report 内 shadow_ab sweep 的 guarded 手法）。

---

## 4. 落地设计（逐组件）

### 4.1 面板构建纯函数 · `value/report.py`（或新建 `value/value_proof.py` 保持 report.py 不膨胀）
- 新增 `build_value_proof_panel(db_path=None, shadow_ab_verdict=None) -> dict`：
  - 依次 guarded 调 4 读数，各渲染成一行 `{label, value, verdict, caveat}`；
  - `shadow_ab_verdict` 优先用传入的（value-report 已算·不重算网络）；为空则跳过该行或标"无数据"；
  - 返回一个符合 `notifications/cards.py` 折叠面板 schema 的 dict（照现有面板格式）。
- **决策：新建 `value/value_proof.py`** 放读数聚合逻辑（report.py 已 28KB·避免继续膨胀·单一职责），`report.py::build_value_card` 只 import + 插入面板。

### 4.2 接进价值卡 · `value/report.py::build_value_card`
- 在现有 8 面板序列里插入「价值证明·多维体温计」面板（建议放"策略 edge 回测记分牌"面板附近·同属"证明能力"族）。
- `run_value_report` 里若已算 shadow_ab 裁决（main.py:454 / report.py），把它传进 `build_value_proof_panel` 复用。

### 4.3 无 CLI/调度改动
- value-report 已周六 launchd 自动跑 → 面板自动周更。无新命令、无新 flag、无调度改动。

---

## 5. 反过拟合 / 诚实边界

- 纯渲染聚合·不新增任何计算/回测/因子·不碰 recommendation/仓位/权重。
- 4 读数本身的诚实闸门（各自 insufficient 逻辑）原样透传·面板不放宽。
- 绝不把 4 个不同口径读数相加/合成单一"价值分"（这正是历史踩坑处·脚注纪律硬约束）。

## 6. 测试计划（离线 mock·全绿零回归）

**`tests/test_value/test_value_proof.py`（新建）**
1. 4 读数都充足 → 面板含 4 行·各带 verdict·无"相加"总分字段
2. 某读数 insufficient → 该行显示"样本不足·暂不下结论"、其余行正常
3. 某读数抛异常 → 该行"该维度暂无数据"、面板仍返回、其余行正常（guarded）
4. shadow_ab_verdict 传入 → 复用不重算；为 None → 该行标无数据不崩
5. 面板 dict 结构符合 cards.py 折叠面板 schema（可被渲染）

**回归重点**：`test_value/test_value.py`（build_value_card 原 8 面板不破）、`test_scorecard`、value-report 干跑不崩。

## 7. 文件清单

**新建**：`value/value_proof.py`、`tests/test_value/test_value_proof.py`
**改**：`value/report.py`（build_value_card 插面板 + 传 shadow_ab 裁决）

## 8. 验收标准 + 对抗审查

**验收**：① 面板出现在价值卡、4 读数各行正确；② insufficient/异常各自诚实降级、不拖垮卡；③ 绝无 4 读数相加/合成总分；④ 新测试全绿 + 全量零回归；⑤ value-report 干跑生成卡不崩、口径标签正确。

**对抗审查**：口径有没有被偷偷相加？insufficient 是否诚实透传（没放宽闸门）？guarded 是否真隔离（一读数挂不拖垮卡）？有没有顺手改到任何建议/仓位/权重路径（应零改动）？shadow_ab 是否复用而非重算（防重复网络）？

## 9. 下一步（writing-plans）

本 spec 定档后调 writing-plans 创建 RED-GREEN 计划：先写面板构建 + guarded 降级测试到 RED，再实现聚合纯函数 + 接进 build_value_card 到 GREEN，全量零回归 + 对抗审查 PASS 后 merge。纯观测渲染层·默认 on（同价值卡其它面板）·无需 flag/点头。
