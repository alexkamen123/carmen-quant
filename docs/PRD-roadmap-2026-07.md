# 卡门智投 能力迭代 PRD + Roadmap（基于 2026-07 AI投资调研）

> 版本：2026-07-01 ｜ 依据：`docs/research/2026-07-01-ai-invest-research.md` + 6 份代码库落地级规格（ultracode 7-agent 生成）
> 本文档所有功能均已对齐到 `src/finance_agent/` 现有模块，每项可直接开 TDD（RED-GREEN）。

---

## 0. 北极星与原则

**唯一终极标准：帮用户真金白银赚钱 / 少亏钱。** 每一项迭代都必须能回答"它凭什么让建议更值钱"，看不见盈亏影响的功能一律降级或砍掉。

**四条不可谈判的原则（贯穿全 Roadmap）：**

1. **改核心建议必 flag 默认 off，enable 须用户点头。** 任何注入 Bull/Bear 辩论、PM 组合决策、或改变线上推荐/仓位/止损的功能，`config/settings.yaml` 里对应开关默认 `false`，且默认关状态下必须做到"prompt 逐字符/逐字节不变、线上行为零变化"（三重护栏：节点透传 + 观测字段全空 + 模板槽填空串）。**纯观测 infra（只读历史、写独立日志、最多推一条"建议人工复核"提醒）可默认 on**，与已上线的 `oos_monitor` 同治理级别。
2. **每个因子必须可 RankIC 检验、失效能降权。** 不追求因子数量，追求每个因子"能被历史命中率反证"。所有因子上线即埋点（把每次建议对应的因子得分同步写入 `recommendations` 表），但**在积累 ≥60 条带 outcome 的样本前，任何 0-10 / [-1,1] 打分公式都是占位 heuristic，禁止当作已验证 alpha 对外宣传、禁止用于加大仓位**。
3. **反过拟合三戒：** ① 绝不让 LLM 直接预测价格；② 绝不做无成本回测（不扣交易成本的回测一律作废）；③ 绝不堆砌因子数（宁可 0-3 条经得起样本检验的结论，也不用 KMeans/决策树在几十笔样本上"给噪声找形状"）。
4. **对自己狠、不许粉饰。** 样本不足时功能**主动"闭嘴"**（返回 None / 静默）而非硬凑结论；漏报要认账；收敛措辞只单向收紧（历史战绩差 → "宜更保守"，绝不倒推成"该更激进"）。

---

## 1. 迭代节奏总览

### 1.1 功能优先级总表

| 优先级 | 功能 | 对盈亏的价值 | 工作量 | 改核心建议? | 硬依赖 | 建议轮次 |
|---|---|---|---|---|---|---|
| **P0a** | 辩论仲裁层（Research Manager 强制五档评级） | 消除多空拼接模糊，逼出明确表态→少踏空/少错过止损 | 中·1 天 | 是（advisory 注入 PM） | 无 | 第 2 轮 |
| **P0b** | 反思闭环（近 N 条已结算建议 alpha 回喂 PM） | 对着实盘战绩收敛，减少重复错判=少亏钱 | 小·0.5 天 | 是（注入 strategy_evidence） | 无（数据已就位） | **第 1 轮（起步）** |
| **P1a** | 基本面量化因子（fundamental_score） | 把基本面从 LLM 自由发挥→可回测的数字证据 | 中·1.5 天 | 是（注入辩手+PM） | 无 | 第 4 轮 |
| **P1b** | 新闻情绪因子化（[-1,1] 滚动+双确认减仓） | 情绪+动量双确认降噪，避免单一噪音误导仓位 | 中·1 天 | 部分（观测默认on / 双确认默认off） | 无 | 第 5 轮 |
| **P1c** | 月度 RankIC 自检（方向 vs 超额收益秩相关） | 把"感觉跑赢"升级为"统计上有没有排序力" | 小·0.5-1 天 | 否（纯观测·默认on） | 无 | **第 3 轮（测量地基）** |
| **P2a** | Shadow Account 操作复盘（FIFO 真实盈亏→规则→月报） | 递镜子给用户看清"多做什么/少做什么" | 中·0.5-1 天 | 否（纯观测·默认on） | 无（软依赖数据量） | 第 6 轮 |
| **P2b** | SUE 盈余惊喜（财报后 >1.5σ 触发 30 天漂移追踪） | 捕捉盈余惊喜后漂移，择时增强 | 中（roadmap 级） | 是 | P1a 财报管线 | 第 7 轮+ |
| **P2c** | 失效触发器（thesis 写明什么事件=论点失效） | 论点失效可被机器识别→及时止损 | 中（roadmap 级） | 是 | theses 表 | 第 7 轮+ |
| **P3a** | A 股资金流（主力净流入+北向资金异常预警） | 补 A 股信息面缺口 | 中（roadmap 级） | 是 | AkShare 扩展 | 第 8 轮+ |
| **P3b** | 波动率仓控（vol 偏离>1.5 收紧止损，与 regime 联动） | 高波动主动收紧止损=少亏 | 中（roadmap 级） | 是 | strategy_weights regime | 第 8 轮+ |
| **P4** | 行情感知合成层（因子按牛/震荡/熊动态加权、月度 IC 校准） | 让所有因子按 regime 动态加权 | 大（roadmap 级） | 是 | P1a/P1b/P1c 全部就位 + ≥60 outcome | 第 9 轮+ |

### 1.2 推荐的 loop 建设顺序（每轮 ultracode 建 1 个 slice + 验收 + 对抗审查）

- **第 1 轮：P0b 反思闭环**（起步）
- **第 2 轮：P0a 辩论仲裁层**（另一 P0，补辩论→PM 的仲裁缺口）
- **第 3 轮：P1c 月度 RankIC 自检**（先立"测量地基"，纯观测默认 on）
- **第 4 轮：P1a 基本面因子**（第一批可 RankIC 检验的结构化因子，上线即埋点）
- **第 5 轮：P1b 新闻情绪因子化**（第二批因子，复用 P1c 检验能力）
- **第 6 轮：P2a Shadow Account 操作复盘**（转向"交易者行为对不对"，纯观测，可穿插空档）
- **第 7 轮起：P2b/P2c → P3 → P4**（P4 合成层必须等 P1a/P1b/P1c 全就位且 ≥60 条 outcome 后才动）

**为什么第 1 轮从 P0b 反思闭环起步（高影响·低风险）：** ① 直击北极星最高频维度"已有持仓的操作指导"——把 recommendations 里"上次判对没/跑赢基准没"的铁证第一次回喂 PM，逼模型对着实盘战绩收敛，减少重复错判；② 风险最低——确定性模板渲染、零 LLM/零网络、100% 离线 TDD、flag 默认 off 时逐字节一致；③ 数据地基已就位（benchmark_return_7d 每日回填）；④ 工作量最小（~0.5 天），最快跑通一个完整"RED-GREEN→验收→对抗审查→flag off 合并"闭环，为后续立范式。

---

## 2. 各功能详细 spec

### P0b 反思闭环（第 1 轮 · 起步）

**目标：** 给 PM 装一条 alpha 感知的自我批判回路——把某票最近 N 条已结算方向性建议（含 vs SPY/HSI 超额）浓缩成 2-4 句反思注入下次同 ticker 决策。区别于已上线的 `live_feedback`（池化 60 条胜率、丢弃 benchmark），本项聚焦"最近 N 条具体案例 + 超额收益"，填补 live_feedback 从不 surface alpha 的缺口。

**落地设计（三处，零新表零新字段）：**
- `db/tracker.py` 新增两函数（紧挨 `get_live_feedback`，~90 行）：`get_recent_reflections(ticker, n=3, asof=None)`——SQL 取 `outcome IS NOT NULL AND IFNULL(is_watch,0)=0` 且过 `_is_directional` 的最近 n 行，`alpha = round(return_7d - benchmark_return_7d, 1)`（benchmark NULL 时 alpha=None 退化只报绝对涨跌），方向性样本 <1 返回 None；`format_reflection(...)`——读 `reflection_injection` 开关（默认 False→返回 ""），确定性文本（无 LLM），末行只输出"宜更保守/可维持"两态。
- `graph/workflow.py::strategy_node`（行 218-251）：`refl = format_reflection(s.ticker)`（try/except），追加进现有 `combined = "\n".join(...)`——零新槽位、零模板改动。
- `config/settings.yaml`：`reflection_injection: {enabled: false, n: 3}`；`prompts.py::PM_BATCH_SYSTEM` 补一行红线（含【历史反思】只用于收敛、禁止反向放大成更激进）。

**文件：** `db/tracker.py`、`graph/workflow.py`、`config/settings.yaml`、`agents/prompts.py`、`tests/test_db/test_reflection.py`（新建）

**测试计划：** 9 例，tmp_path + 直插 recommendations，照抄 `test_live_feedback.py` 的 _seed 模式。覆盖：无方向性样本→None；alpha 跑输/跑赢×判对/判错措辞；benchmark=NULL 优雅退化；LIMIT 最近 3；保守措辞红线（2 错 0/3 跑赢→"宜更保守"，全对全跑赢→绝无"更激进"）；默认 off；asof 排除未成熟行（<14 天）；is_watch 行排除。

**验收标准：** ① 默认 off 时 strategy_evidence 逐字节相同；② 置 true 对 ≥1 条已结算方向性建议的票出现【历史反思】区块、alpha 数值与措辞正确；③ 样本不足/benchmark 缺失不崩、优雅退化；④ asof 排除未成熟行（无未来函数）；⑤ 9 例全绿 + live_feedback/agents 不回归；⑥ flag 开干跑日志见反思注入、日报正常。

**flag 门控：** 改核心建议 → `reflection_injection.enabled` 默认 `false`。默认关 `format_reflection` 首行 `return ""`。与 `live_feedback_injection`（默认 on）独立。

**风险：** ① 与 live_feedback 双注入冗余（默认 off、口径互补）；② 小样本受极端行情主导（标注分母、样本不足静默、最坏=更保守而非乱加仓）；③ **未来函数生命线：必须过 asof 成熟闸**（测试第 8 条守死）；④ 措辞被 LLM 倒推成加仓（PM_BATCH_SYSTEM 红线句 + 两态汇总兜底）。

**依赖：** 无。与 P0a 正交可并行（文件冲突面仅 `prompts.py::PM_BATCH_SYSTEM` 一处）。

---

### P0a 辩论仲裁层 / Research Manager 强制五档评级（第 2 轮）

**目标：** 解决 debate→PM 缺仲裁、建议模糊，逼出明确五档（买入/增持/持有/减持/卖出），少踏空/少错过止损。

**落地设计：** 新增 `arbiter_node` 插在 `workflow.py:668-681` memory→decision 之间；新文件 `agents/arbiter.py`（~120 行）`run_arbiter_batch`+`arbiter_enabled`（读 `debate_arbiter.enabled` 默认 False），复用 PM 基建（claude_cli→deepseek 降级、JSON 数组解析）；ETF 跳过、观察池只允许"买入/持有"代码兜底归一；新 prompt `ARBITER_SYSTEM/USER`（<50 行，硬规则：必出五档、只有证据均衡才"持有"、rationale 每条引用具体证据、balanced 布尔）；state.py 新字段 `arbiter_rating/balanced/rationale`（独立观测不复用 recommendation 枚举）；PM 注入用可选 `{arbiter_block}` 槽（flag off 填空串→逐字节不变）。**MVP 边界：仲裁=注入 PM 的 advisory 强先验，最终 recommendation 仍由 PM 出，本期不硬改写建议，同时落库为观测信号。**

**文件：** `agents/prompts.py`、`agents/arbiter.py`（新建）、`graph/state.py`、`graph/workflow.py`、`agents/portfolio_manager.py`、`config/settings.yaml`、`tests/test_agents/test_arbiter.py`（新建）

**测试计划：** 9 例离线 mock（仿 `test_pm_fallback.py`）：默认 disabled；flag off PM prompt 逐字符不变；JSON 回填；持有须 balanced=true 否则置空；rationale 无引用判无效；观察池减持/卖出归一；ETF 跳过；claude 失败降级 deepseek；两路全失败 noop 不炸主链路。

**flag 门控：** `debate_arbiter.enabled: false`。三重护栏。**建议 enable 前先"影子观测"一段**（仲裁评级只落库不注入），比对仲裁 vs PM 分歧与 7/30 日 outcome 再决定是否切"注入"。

**依赖：** 无硬前置。与 P0b 解耦但互补（arbiter_rating 观测样本可作反思/RankIC 输入）。

---

### P1a 基本面量化因子 / fundamental_score（第 4 轮）

**目标：** 补 4 个免费(yfinance)结构化因子——**FCF Yield、GP/A 毛利资产比、12-1 月动量、分析师上行空间**——合成 0-10 `fundamental_score`，作辩手+PM 的数字证据，每次建议因子值同步写 recommendations 供月度 RankIC。**v1 仅美股**（AkShare 无对应免费字段，hk/cn 跳过=None 不伪造）。

**落地设计：** 复用 `fundamental_analyst.py::_fmt_financials()` 已验证注入管线（拼出的 `fundamental_view` 已流经 BULL/BEAR/PM/PM_BATCH 全部 6 个槽）——新因子追加 4 行+1 行综合分，**改动面从"6 模板"压到 1 个格式化函数**；新模块 `signals/fundamental_score.py` 纯函数 TDD；**0-10 标准化为 v1 占位公式（非拟合，标注"待 ≥60 条 outcome RankIC 校准"）**；可用因子 <2 → score=None（不单因子硬撑）；数据取值优先 `info` 字段（不解析跨版本不稳的 DataFrame 行名），缺失 except→None 不猜；12-1 动量单独 fetch 290 天（不牵动 composite_score）；分析师覆盖 <3 → None；DB 迁移（加 5 个可空 REAL 列）可脱离主 flag 独立先上。

**文件：** `signals/fundamental_score.py`（新建）+ 测试、`graph/state.py`、`data/yfinance_provider.py`、`graph/workflow.py`、`agents/fundamental_analyst.py`、`db/tracker.py`、`config/settings.yaml`

**flag 门控：** `fundamental_factors.enabled: false`。纯 DDL 加列可独立先上；记账不受 flag 限（非 None 就落库，flag off 恒 None→NULL 天然一致）。

**风险：** 伪精度陷阱（系数占位非拟合，反复标注"待校准"）；GP/A 数据脆弱（except→None 静默降级）；**绝不做：** 不用此分数直接驱动仓位/替代 PM，样本 <60 前不宣称"有效"。

**依赖：** 无强前置。产出正是 P1c 要检验的候选信号（列命名提前对齐）。

---

### P1b 新闻情绪因子化（第 5 轮）

**目标：** 把 DeepSeek 评分（现只用于"是否推送"、未达阈值直接丢弃）全量持久化，聚合成按 ticker 滚动情绪因子（[-1,1] + 7 日均值 + 趋势），**仅"情绪下滑 + 技术动量转负"双确认成立才给减仓提示**；情绪快照记入 recommendations 供 RankIC。

**落地设计：** 新表 `news_sentiment_scores`；新模块 `signals/sentiment_factor.py`（`normalize_sentiment_score`=sign×clamp(impact)/10、`compute_sentiment_factor`、`detect_double_confirm`复用现成 `macd_trend/composite_score`不重造动量、`format_sentiment_note`读开关默认 off）；`alerts/news_monitor.py` 核心改造"点状评分→全量落库"；两 flag 分层——`news_sentiment_factor`（落库+快照）纯观测默认 **true**、`sentiment_double_confirm_alert`（注入 PM）默认 **false**。

**风险：** 归一化是启发式非统计量（注释标局限）；min_items=3 早期恒 insufficient 是预期；"动量转负"借 macd/composite 是代理（上线独立动量因子后应替换避免自我印证）；双确认只出定性文字、绝不反向决定加仓。

---

### P1c 月度 RankIC 自检（第 3 轮 · 测量地基）

**目标：** recommendations 有"方向 + 7 日收益 + 基准"，但从没验证方向信号有没有排序预测力。月度算 RankIC（方向序 vs 后续超额序），**连续两月 <0.03 推飞书提醒复核**。纯观测、不改任何线上建议。

**落地设计（复用 `oos_monitor.py` 骨架，只换统计口径）：** 新文件 `value/rankic_monitor.py`（~150-180 行）：`spearman_rank_ic(x,y)`（秩皮尔逊，n<2 或秩方差 0→None）；`_direction_score`复用 `metrics._is_bullish/_is_bearish`同口径；样本闸 MIN_N=30/MIN_DIRECTIONAL=10/TRAILING_DAYS=180；`monthly_rankic_snapshot`（rows_fn 供测试注入）；`record`/`run`（同月幂等）；`rankic_decay_verdict`（K=2、阈值 0.03，只把 measured 月计入）；**新增飞书告警卡**（仅 decaying 才发，healthy/insufficient 静默）。**MVP 只用 recommendations 单表内"建议方向"作待检因子**（不跨表 JOIN daily_signals），因子得分版留 P1a 上线后复用同一函数。接 `main.py` 加 `rankic-monitor` CLI + `_monthly_review` guarded 区块。

**flag 门控：** **纯观测 infra，默认 ON，无需 flag**（与 oos_monitor 同治理级别）。唯一新增主动推送风险敞口有限（最坏误报一条复核消息），K=2 + 对称阈值 0.03 双重防单月噪声。

**依赖：** 依赖 return_7d/benchmark 回填链路（已实现）。不依赖 P0。P1a/P1b 因子上线后复用 `spearman_rank_ic()` 对因子连续值检验（新增非重构）。

---

### P2a Shadow Account 操作复盘（第 6 轮）

**目标：** `user_actions` 已记 BUY/SELL/TRIM 但从没算过"这笔真实赚了多少"。本项 FIFO 配对成"已平仓交易"算真实盈亏，用几条可解释、样本门控的规则挖行为模式（处置效应等），中文注入月报。**不改建议、不改交易，只递镜子。**

**落地设计：** `db/tracker.py` 加只读 `get_actions_for_pairing()`；新文件 `monthly/playbook.py`（~150-200 行纯函数）：`pair_fifo_trades`（TDD 主战场，FIFO 队列消耗、卖出量超剩余丢弃不臆造成本价）；3 条规则（持有期/退出类型/仓位大小，MIN_BUCKET_N=5、DIFF≥15pp 才出，不显著沉默）；`build_shadow_playbook`+`playbook_panel`折叠面板；挂 `monthly/review.py`。**关键取舍：FIFO 而非聚类**（几十笔=给噪声找形状，属"绝不碰"），改 3 条可解释、独立可关、有最小样本门控的规则。

**flag 门控：** **默认 ON**（纯观测，只读 user_actions + 加月报折叠面板）。保险丝=样本门控（<5 笔/胜率差<15pp 不下结论）。

**依赖：** 无强前置，与 P0/P1 完全解耦（读 user_actions 非 recommendations）。

---

### P2b/P2c/P3/P4（roadmap 级，待专项规格化后再开 TDD）

- **P2b SUE 盈余惊喜：** 财报后算 SUE >1.5σ 触发 30 天漂移追踪。flag off。依赖 P1a 财报管线。
- **P2c 失效触发器：** theses 表加"什么事件=论点失效"结构化字段，命中→提醒/止损复核。flag off。
- **P3a A 股资金流：** AkShare 主力净流入 + 北向资金异常预警。flag off。
- **P3b 波动率仓控：** vol 偏离 >1.5 收紧止损，与 regime 护栏联动。flag off。
- **P4 行情感知合成层：** 因子按牛/震荡/熊动态加权、月度 IC 校准。flag off。**必须等 P1a/P1b/P1c 全就位且 ≥60 条 outcome 后才动**（否则在噪声上调权，违反反过拟合三戒）。

---

## 3. 依赖图与关键路径

```
【核心建议增强线（各自独立，可并行）】
  P0b 反思闭环 ──┐
  P0a 仲裁层  ──┼──> 各自 flag off 独立上线；arbiter_rating/反思样本 → 喂 P1c/月报
                 │      （文件冲突面仅 prompts.py::PM_BATCH_SYSTEM 一处）

【因子 → 验证 → 合成 关键路径（严格顺序）】
  P1c RankIC 自检（先立测量地基，纯观测默认on）
        │  提供 spearman_rank_ic() 检验能力
        ▼
  P1a 基本面因子 ─┐  上线即埋点（因子得分写 recommendations 表）
  P1b 情绪因子   ─┤        │
                  │        ▼
                  │   ★ 数据积累前置：每个因子需 ≥60 条带 outcome 的样本 ★
                  │        │   （outcome 由 fill_7d_returns 每日回填，自然积累）
                  │        ▼
                  └──> P1c 月度 RankIC 对因子连续值检验（复用同一函数）
                           │  失效因子降权/剔除
                           ▼
                  P4 行情感知合成层（必须 P1a+P1b+P1c 全就位 且 ≥60 outcome 才可动）

【行为复盘线（完全解耦，任意空档插入）】
  P2a Shadow Account（读 user_actions，与因子体系无关）
```

**关键路径解读：** 最长关键链 = P1c → P1a/P1b（埋点）→ 等 ≥60 outcome → P1c 因子检验 → P4。P4 前置不是"代码写完"而是"数据攒够"——现实需**数月自然积累**，无法工程加速。因此 P1a/P1b 必须"先上线埋点、后验证"。P1c 排在因子前（第 3 轮）先把体温计装好。P0a/P0b 不在关键路径，独立先行快速拿收益。P2a 完全旁路。

---

## 4. 每轮 loop 的验收 + 对抗审查清单（合并前硬闸门）

**A. 测试闸门（RED-GREEN 纪律）**
- [ ] 先写测试到 RED（有 RED 证据），再实现到 GREEN，不允许"先写实现再补测试"。
- [ ] 本 slice 新增测试 100% 通过。
- [ ] 全量零回归 `uv run pytest tests/ -x -q`，重点确认被点名既有用例（test_debate/test_pm_fallback/test_live_feedback/test_scorecard/test_technical）不受影响。
- [ ] 纯逻辑优先、离线 mock，不打真实 API；`AGENT_DB_PATH` 用 tmp。

**B. flag 与线上零影响闸门（核心建议改动专用）**
- [ ] 改核心建议：`config/settings.yaml` 开关默认 `false`。
- [ ] **flag off 逐字节验证**——有一条测试锁定"默认关时 PM prompt/strategy_evidence/日报输出与改动前逐字节相等"。
- [ ] 纯观测 infra 可默认 on，但确认绝不改任何 recommendation/position_change/权重生成路径。
- [ ] 默认配置干跑一次，无新增网络调用、无新字段泄漏进飞书卡/prompt。

**C. 对抗审查闸门（PASS 才算过）**
- [ ] 过一遍对抗审查，质询：**过拟合三戒有没有破**？占位公式有没有被误当验证过的 alpha？样本不足时是否诚实静默？收敛措辞是否只单向收紧？
- [ ] **未来函数生命线**：涉及回测/历史回喂的，确认有 asof 成熟闸测试，否则回测作废。
- [ ] 降级/隔离：新节点 try/except 兜底，任一外部调用失败不拖垮日报/月报。
- [ ] 阈值/常量在模块顶部可读命名，直抄用户给定值未做"找好看阈值"的回测调参。

**D. 收口闸门**
- [ ] 进展.md 记一行：flag 现状、观察窗口、"校准前禁对外宣传"声明。
- [ ] **核心建议改动 enable 前必须用户点头**——merge 只代表代码进主线且 flag off；真正开启须单独用户确认，建议先"影子观测"一段。

---

## 5. 里程碑与现实预期

**第 1 个月（第 1-3 轮）：** P0b/P0a 就位（flag off，可影子观测）；P1c RankIC 默认 on 开始每月产出可测/insufficient 裁决。
**第 2-3 个月（第 4-6 轮）：** P1a/P1b 因子上线埋点，P2a 操作剧本进月报。此阶段 outcome 样本仍在积累，**尚不足以判定任何因子有效**。
**第 4-6 个月：** ≥60 条方向性带 outcome 样本后，P1c 首次对因子跑 RankIC，失效降权；视结果决定是否 enable P0a/P0b 注入（用户点头）。

### 诚实的成功指标（持续追踪、不许粉饰）

| 指标 | 及格线 | 说明 |
|---|---|---|
| 推荐信号 vs 基准超额 | 跑赢被动持有 **3-8%**（年化，扣成本） | 不扣成本的数字一律作废 |
| 组合 Sharpe | **>0.7** | 风险调整后收益 |
| 因子 RankIC | **>0.05** | <0.03 连续两月触发复核、失效降权 |
| 重大风险预警提前率 | 持续追踪 | 大亏前是否提前预警 |
| 用户操作 vs 躺平 | 持续追踪 | Shadow Account 真实平仓盈亏是否跑赢被动 |

**不吹牛边界：** 全行业无可验证实盘 alpha——我们做的是把"感觉跑赢"升级为"统计可检验"，不承诺稳定超额；占位公式 ≠ 已验证 alpha；前几月大概率 insufficient_sample（诚实设计非"坏了"）；数据积累是硬约束（P4 等的是样本不是代码）；能证明有效 > 看着高级。

---

**起步动作：第 1 轮 ultracode 从 `P0b 反思闭环` 开始**——`test_reflection.py` 先写 9 例到 RED，再实现 `tracker.get_recent_reflections/format_reflection` + `strategy_node` 6 行注入 + settings 开关到 GREEN，flag 默认 off 逐字节验证 + 对抗审查 PASS 后 merge（enable 待用户点头）。
