# P2b 盈余惊喜（SUE）因子 — 设计规格

> 日期：2026-07-03 ｜ 依据：`docs/PRD-roadmap-2026-07.md`（P2b 一句话规格）+ 本轮 brainstorming + Explore 子代理只读探索结论
> 状态：设计定档（用户 2026-07-03 批准 3 个关键决策），**待 writing-plans + TDD 实现**
> 北极星对齐：捕捉盈余后漂移（PEAD），**核心价值在负向惊喜的"少亏/避免大亏"** + 正向惊喜的"防卖飞"——直击北极星第 1 维「已有持仓的操作指导」

---

## 0. 用户已拍板的 3 个关键决策（不可在实现阶段擅自改）

1. **MVP 边界 = 观测 + 双向注入**：一轮建成完整对称 slice（观测线 + 双向 PM 注入），不拆成"先观测后注入"。
2. **正向惊喜语气 = 抗卖飞审慎**：beat 注入"减仓/止损前需额外确认"，收紧卖飞方向审慎，**绝不含加仓/买入/更激进字样**。
3. **触发挂载 = 扩展现有 `earnings-check`**：复用已在跑的 carmen-earnings（08:30 每日），零新 launchd 任务、零调度改动。

其余参数为设计阶段合理默认（σ=1.5 / 季度闸=8 / 事件独立表 / 回填搭周六 value-report / 观测档默认 on），已在批准环节一并通过。

---

## 1. 功能形状（一句话）

财报出炉后算 **SUE = (实际EPS − 预期EPS) / 历史 surprise 标准差**；|SUE| 超阈（默认 1.5σ）→ 写一条"盈余惊喜事件"落独立表 + 启动 30 天漂移追踪回填；同时向 PM 注入**双向审慎**提示（miss→防死扛、beat→防卖飞，两向都单向收紧、都不含加仓）。

---

## 2. 铁律与理论的张力 → 双向审慎框架（本设计的灵魂）

PEAD 经典形态是"beat 后继续上漂、miss 后继续下漂"。但项目铁律第 5 条「收敛措辞只单向收紧、绝无更激进」。解法 = **两个方向都注入、但都收紧成审慎，只是收紧对象不同**，与项目已上线的 `sell_guard`（卖飞守门·cycle1）+ `hold_into_weakness`（该减没减守门·cycle2）一脉相承：

| 方向 | SUE 触发 | 注入语气 | 收紧对象 | 守门对齐 |
|---|---|---|---|---|
| miss | SUE ≤ −阈 | 「财报大幅低于预期(SUE=−X.Xσ)，警惕盈余后下行漂移，宜更保守/止损复核」 | 该减没减（基本面恶化别死扛） | `hold_into_weakness` |
| beat | SUE ≥ +阈 | 「该股刚录强正向盈余惊喜(SUE=+X.Xσ)，短期回调或为噪声，减仓/止损前需额外确认」 | 卖飞风险（基本面强化别慌卖） | `sell_guard` |
| \|SUE\| < 阈 | — | `""`（不注入） | — | — |

**关键不变式**：beat 方向注入的是"别急着卖"的审慎，**不是**"该加仓"的激进——前者收紧卖侧决策质量（防卖飞），后者才破戒。两向均绝不出现"加仓/买入/更激进"字样。

---

## 3. 数据可行性（Explore 探索结论 · 诚实缺口）

- ✅ **能做**：`yf.Ticker(t).get_earnings_dates(limit=20)` 实测 AAPL/MU/NVDA 可拿 12–25 个季度历史（覆盖 2020 至今），列 `EPS Estimate / Reported EPS / Surprise(%)`。
- ⚠️ **不能用** `earnings_history`：只给最近 **4 个季度**，样本太薄算不出稳健 σ。两套 API 列名不同（驼峰 vs 空格括号），实现时勿混用。
- ⚠️ **ETF 无数据**：`SPY` 两接口都返回 None/抛异常 → 必须跳过（与 P1a 一致）。
- ⚠️ **覆盖不均**：小盘/次新股历史季度不足 → 样本闸兜底（见 §4.1）。
- ⚠️ **非独立数据源**：`EPS Estimate` 是 Yahoo consensus，与 P1a `analyst_upside` 同源，覆盖同受"分析师覆盖数"限制。
- ⚠️ **代码缺口（唯一）**：`data/yf_utils.py` 无 `earnings_dates` 重试封装，需新增（见 §4.2）。

---

## 4. 落地设计（逐组件）

### 4.1 数据线 · `signals/sue_factor.py`（新建，纯函数 TDD 主战场）

- **SUE 计算纯函数**：
  ```
  compute_sue(reported_eps: float, estimate_eps: float,
              hist_surprises: list[float]) -> float | None
  ```
  - `current_surprise = reported_eps − estimate_eps`
  - `σ = stdev(hist_surprises)`（样本标准差；`hist_surprises` = 财报日**之前**已披露季度的 `reported_i − estimate_i` 序列）
  - `SUE = current_surprise / σ`
  - 闭嘴闸：`len(hist_surprises) < min_quarters(默认8)` → None；`σ == 0` → None；任一输入 None/NaN → None。
- **不猜、不落假值**：季度不足、estimate 缺失、ETF、非美股 → 一律 None。
- **flag 读取**：`sue_factor_enabled()`（观测档）+ `sue_pead_alert_enabled()` / `_sue_pead_cfg()`（注入档，返回 sigma_threshold/track_days），照抄 `signals/fundamental_score.py::fundamental_factors_enabled()` 的 `yaml.safe_load` 模式。
- **注入文案纯函数**：`format_sue_note(ticker, asof=None, db_path=None) -> str`
  - 读注入档 flag，off → `return ""`（三重护栏第一层）。
  - 取该 ticker 最近一条**已成熟**（asof 闸，见 §6）SUE 事件；`|sue| ≥ sigma_threshold` 才出文字，否则 `""`。
  - 确定性模板、零 LLM、零网络（抄 `db/tracker.py::format_reflection`）。措辞照 §2 表，**单向收紧兜底**：只可能出"更保守/需额外确认"两态之一，代码层杜绝"加仓"字样。

### 4.2 取数 · `data/yf_utils.py`

- 新增 `_ticker_earnings_dates_with_retry(ticker, retries=3, limit=20)`，照抄 `_ticker_calendar_with_retry` 结构，`async with _YF_SEM:` 保护、顶层 try/except 吞异常返回 None。
- 解析层单独拆一个纯函数 `_extract_surprises(df) -> list[tuple[date, reported, estimate]]`（处理列名 `EPS Estimate/Reported EPS`、NaN 行剔除、按日期升序），供单测（不 mock 网络，直接构造 DataFrame 形状，照 `tests/test_data/test_fundamental_fetch.py`）。

### 4.3 事件表 + 30 天漂移回填 · `db/tracker.py`

- **新建独立表** `earnings_surprise_alerts`（**不塞 recommendations**——财报事件生命周期 ≠ 每日建议，硬塞破坏"一天一票一行"不变式）：
  ```
  id, ticker, market, earnings_date, sue_score, eps_reported, eps_estimate,
  surprise_std, price_at_event, price_30d, return_30d, benchmark_return_30d, created_at
  ```
  DDL 进 `init_db`，迁移进 `_migrate_*`（照 dip_alerts 模式加列以兼容旧库）。
- CRUD：`save_sue_alert(...)`（写事件行，按 `(ticker, earnings_date)` 幂等查重）、`get_sue_alerts(...)`（读，供 format_sue_note 与 RankIC）。
- **回填** `backfill_sue_outcomes(db_path=None)`：**抄 `fill_long_returns` 的 30d 分支**——
  - 成熟度严格闸：`n_td < fwd_td(21 交易日)` 整行跳过（防写盘中价）；
  - 个股/基准**原子两腿**：都成功才写 `price_30d/return_30d/benchmark_return_30d`，任一失败该行留 NULL；
  - `cutoff`/`floor` 双边界让"永久无法回填"的行退出 pending（零无谓网络请求）。
- **回填挂载**：搭 `value-report`（周六 carmen-value 已有车）的 sweep，与影子 A/B 同款 guarded 跑，静默不进飞书卡、失败不拖垮体检。**零新 launchd。**
- **RankIC 自检就绪**：事件表自带 `sue_score` + `(return_30d − benchmark_return_30d)`，可直接喂 P1c `value/rankic_monitor.py::spearman_rank_ic()` 检验，无需跨表 JOIN。

### 4.4 触发挂载 · `alerts/earnings_trigger.py` + `earnings-check`

- 现有 `check_and_alert_earnings` 只看未来财报日；新增**回看逻辑**（美股 only，与现有覆盖一致）：对每只持仓票，`get_earnings_dates` 最近一行日期若落在近 N（默认 3）个交易日内 → 判"刚出炉" → 算 SUE → `save_sue_alert`。
- **观测档门控**：整段回看+落库受 `sue_factor_enabled()` 包裹，off → 完全不算不落，earnings-check 行为逐字节不变。
- 去重复用现成 `news_alerted` 表模式，key = `sue:{ticker}:{earnings_date}`（同一次财报只落一次）。
- **职责解耦**：earnings-check 只负责**落库**（观测线）；PM 注入（注入线）发生在日报 `strategy_node`，两条线独立 flag。

### 4.5 注入核心建议 · `graph/workflow.py::strategy_node` + `agents/prompts.py`

- `strategy_node` 加 `sue_note = ""` + try/except，`sue_note = format_sue_note(s.ticker)`，追加进现成 `combined = "\n".join(x for x in (evidence, live, refl, dip_note, senti, sue_note) if x)`——**一行改动**，空串被 `if x` 天然过滤 → 注入档 off 时 strategy_evidence 逐字节不变（三重护栏第三层）。
- `PM_BATCH_SYSTEM` 补一行红线：【盈余惊喜提示】只用于收紧审慎（miss→更保守、beat→防卖飞），**禁止**倒推成加仓/更激进。

### 4.6 flag · `config/settings.yaml`（附中文注释块，照仓库强约定）

```yaml
# 观测档：财报后算 SUE + 落 earnings_surprise_alerts 表 + 30天漂移回填。
# 纯观测(只读+写独立表·不碰任何 recommendation/仓位/权重)，默认 on，与 P1b/P1c/P2a 同治理级。
# off 则 earnings-check 逐字节不变、永不积累样本(无法验证覆盖度)——故默认 on 才能攒数据。
sue_factor:
  enabled: true
# 注入档：向 PM 注入双向审慎提示(miss→更保守·beat→防卖飞)。改核心建议，默认 off，enable 须用户点头。
sue_pead_alert:
  enabled: false
  sigma_threshold: 1.5   # PRD 给定，直抄不做"找好看阈值"的回测调参
  min_quarters: 8        # 历史 surprise 样本闸，不足则 SUE=None(不硬凑)
  track_days: 30         # 漂移追踪窗口(交易日 21，日历约 30)
```

---

## 5. 反过拟合 / 诚实边界守则（写进实现 + 对抗审查必查）

- SUE/PEAD 学术上真实（Bernard-Thomas 1989），但本项目框架下 SUE 仍是**占位因子**：**≥60 条带 outcome 前禁当已验证 alpha、禁用于加仓**。
- **绝不做**：不用 SUE 直接驱动仓位/止损数值、不替代 PM、不做无成本回测、不堆因子。
- 样本不足/数据缺失 → 主动返 None（静默），绝不硬凑结论。
- σ=1.5、季度闸=8、回看=3 交易日 均为顶部命名常量，直抄给定值，**未做"找好看阈值"的调参**。

## 6. 未来函数生命线（asof 成熟闸）

- **算 SUE 时**：`hist_surprises` 只取 earnings_date **之前**已披露的季度（earnings_dates 天然按日期排序，切片到当期之前）。
- **30 天漂移追踪**：回填成熟闸（`n_td < 21` 整行跳过）+ format_sue_note 的 asof 闸（距 earnings_date < track_days 的事件不用于"漂移已实现"判断/不进 RankIC）。
- **测试守死**：一条 `test_asof_excludes_immature`（抄 `tests/test_db/test_reflection.py:89`）：距 asof < 30 天的事件必须被排除。

## 7. 待实现时核实的数据点（不阻塞设计，实现首步验证）

- yfinance `EPS Estimate` 须确认是"财报**发布时**的 consensus"而非事后修正值——若是事后修正会污染 SUE（未来函数）。实现取数处加注释标注；若发现是事后值，降级为"标注局限的占位"或改用发布时快照。
- `get_earnings_dates` 返回的时区/日期口径（是否含盘后），确认"最近一行日期"判断"刚出炉"的边界正确。

---

## 8. 测试计划（离线 mock，tmp_path + AGENT_DB_PATH，全绿 + 零回归）

**`tests/test_signals/test_sue_factor.py`（纯函数主战场）**
1. `compute_sue` 正惊喜（beat）→ 正 SUE
2. `compute_sue` 负惊喜（miss）→ 负 SUE
3. σ=0 边界 → None
4. 历史季度 < 8 → None
5. estimate 缺失/NaN → None
6. `_extract_surprises` 正确解析 DataFrame 形状 + 剔除 NaN 行 + 升序
7. `format_sue_note` miss 措辞含"更保守/止损复核"、无"加仓"
8. `format_sue_note` beat 措辞含"减仓/止损前需额外确认"、无"加仓/买入/更激进"
9. `|SUE| < 阈` → `""`
10. **注入档 off → `format_sue_note` 逐字节 `""`**（三重护栏锁死）
11. asof 排除未成熟事件（距 asof < 30 天）

**`tests/test_db/test_sue_alerts.py`（事件表 + 回填）**
12. `save_sue_alert` 写入 + `(ticker,earnings_date)` 幂等查重
13. `backfill_sue_outcomes` 成熟闸：`n_td < 21` 整行跳过、留 NULL
14. 原子两腿：基准腿失败 → 该行 return_30d 留 NULL
15. cutoff/floor 边界：永久无法回填的行退出 pending

**回归重点**：`test_earnings`（earnings-check 回看不破原预警）、`test_reflection`/`test_pm_fallback`（strategy_node join 未变原有注入）、`test_scorecard`。

## 9. 文件清单

**新建**：`signals/sue_factor.py`、`tests/test_signals/test_sue_factor.py`、`tests/test_db/test_sue_alerts.py`
**改**：`data/yf_utils.py`、`db/tracker.py`、`alerts/earnings_trigger.py`、`graph/workflow.py`、`agents/prompts.py`、`config/settings.yaml`；`main.py`（如 earnings-check 需 sue 回看开关/日志）

## 10. 验收标准 + 对抗审查清单

**验收**
- ① 注入档 off 干跑 → strategy_evidence 逐字节不变；观测档 on 干跑 → 只写独立表、不碰任何 recommendation/仓位/权重路径。
- ② SUE 数值与措辞正确；样本不足/缺数据优雅返 None、不崩。
- ③ asof 排除未成熟（无未来函数）。
- ④ 新测试全绿 + 全量零回归 `uv run pytest tests/ -x -q`。
- ⑤ earnings-check 观测档 off 时逐字节不变；on 时落库幂等。

**对抗审查（PASS 才算过）**
- 过拟合三戒有没有破？占位公式有没有被误当验证过的 alpha？
- 样本不足是否诚实静默？收敛措辞是否**只单向收紧**（尤其 beat 方向绝无加仓）？
- 未来函数生命线：asof 成熟闸测试是否守死？
- 降级/隔离：新节点 try/except 兜底，任一外部调用失败不拖垮日报/月报/earnings-check？
- 阈值/常量是否为顶部命名、直抄给定值？

## 11. flag 门控与 enable 路径

- 观测档 `sue_factor.enabled` **默认 on**（纯观测，随代码 merge 即开始积累）。
- 注入档 `sue_pead_alert.enabled` **默认 off**（改核心建议）。merge 只代表代码进主线 + flag off；**真正 enable 须用户单独点头**，建议先"影子观测"一段：看 SUE 事件表落库质量 + 30 天漂移读数 + RankIC 首检，再决定是否开注入。
- 收口时进展.md 记一行：flag 现状、观察窗口、"校准前禁对外宣传"声明。

---

## 12. 下一步（writing-plans）

本 spec 定档后，下个会话从此文件调 **writing-plans** 技能创建 RED-GREEN 实现计划，按 §8 测试计划先写测试到 RED，再逐组件实现到 GREEN，flag off 逐字节验证 + 对抗审查 PASS 后 merge（enable 待用户点头）。
