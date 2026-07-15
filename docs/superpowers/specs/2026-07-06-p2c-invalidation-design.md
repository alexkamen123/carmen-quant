# P2c 失效触发器（Thesis Invalidation Trigger）— 设计规格

> 日期：2026-07-06 ｜ 依据：`docs/PRD-roadmap-2026-07.md`（P2c 一句话规格）+ 本轮 brainstorming（用户 4 决策拍板）+ Explore 子代理只读探索（theses 基础设施摸底）
> 状态：设计定档（用户 2026-07-06 批准），**待 writing-plans + TDD 实现**
> 北极星对齐：**直击第 1 维「已有持仓的操作指导」——论点失效可被机器识别 → 及时止损 → 避免大亏/少亏**

---

## 0. 用户已拍板的 4 个关键决策（不可在实现阶段擅自改）

1. **检测范围 = 5 类机器可查失效事件**：财报爆雷 / 重大利空新闻 / 跌破止损价 / 营收连续 2 季转负 / 毛利率跌破阈值。前三类复用现成检测器，后两类新增（yfinance 季度财报·数据脆弱→None 静默）。
2. **触发条件 = LLM 逐票定制**：thesis 生成时让 Claude 从**固定词表**为每只票选出"这只票的真正破坏条件"+ 阈值，写进 theses 表现成的死列 `pillars`/`stop_conditions`；命中比对时**只有事件对上这只票自己声明的破坏条件才算失效**（区别于"又报一遍坏消息"）。
3. **输出 = 即时飞书告警 + 落库观测**：命中即推飞书"⚠️论点失效·止损复核"（纯提醒·不改建议/仓位）+ 事件落独立表。**PM 日报注入留后续轮**（本轮不做）。
4. **告警治理 = 默认 on**（纯提醒·同 sell_guard / news 告警治理级），严格去重防刷屏；观测落库纯观测默认 on。

---

## 1. 功能形状（一句话）

给每只持仓的论点(thesis)在生成时声明"什么事件=论点失效"（从固定词表逐票定制、写进结构化列）；已有扫描器（earnings-check / news-scan / price-scan）检测到事件后，若命中**该票自己声明的**破坏条件 → 落一条"论点失效事件"到独立表 + 即时推飞书"止损复核"告警（纯提醒·去重·不改建议）。

---

## 2. 固定触发词表（machine-checkable·唯一真相源）

顶部命名常量，thesis 生成的 `trigger_type` 只能取这 5 个值；命中比对逻辑按 `trigger_type` 分发到对应检测器：

| trigger_type | 判据（阈值来自 thesis 声明或常量兜底） | 数据源（复用） | 挂载扫描器 |
|---|---|---|---|
| `earnings_miss` | 最近财报 SUE ≤ −`SIGMA`（默认 −1.5σ） | **P2b `earnings_surprise_alerts`**（`get_sue_alerts`） | earnings-check |
| `news_negative` | 新闻 impact ≥ `IMPACT`（默认 7）+ sentiment 负面 + 事件类型 ∈ 利空集 | `_classify_stock_news` + `_extract_dedup_seed` 事件分类 | news-scan |
| `price_break` | 收盘价跌破 thesis 声明的 `stop_price` | 价格（yfinance last close / dip 扫描） | price-scan |
| `revenue_decline` | 营收连续 2 季**同比**转负 | yfinance 季度利润表（`quarterly_financials`） | earnings-check |
| `margin_break` | 毛利率跌破 thesis 声明的 `margin_floor` | yfinance 季度利润表 | earnings-check |

- 阈值优先用 thesis 逐票声明值（`price_break`/`margin_break` 必须有声明值否则该触发器无效跳过）；`earnings_miss`/`news_negative`/`revenue_decline` 用词表常量兜底。
- 利空事件集（`news_negative`）：`{guidance_cut, regulatory, litigation, key_customer_loss, fraud, downgrade}` —— 复用 `_extract_dedup_seed` 已有的 events 分类词典子集，非全部负面新闻（避免噪声刷屏）。

---

## 3. thesis 结构化触发器授权 + 向后兼容

### 3.1 生成侧（`db/thesis_generator.py`）
- 改 `THESIS_SYSTEM`：在现有 4 段自由文本 thesis **之外**，额外要求输出一段结构化 JSON：
  ```json
  {"pillars": [{"pillar": "AI 数据中心需求", "trigger_type": "revenue_decline", "threshold": null, "status": "intact"},
               {"pillar": "毛利率护城河", "trigger_type": "margin_break", "threshold": 40.0, "status": "intact"},
               {"pillar": "技术面止损", "trigger_type": "price_break", "threshold": 150.0, "status": "intact"}],
   "stop_conditions": "跌破 150 止损；营收连续两季转负；毛利率跌破 40%"}
  ```
  硬规则：`trigger_type` 只能取 §2 词表 5 值；`price_break`/`margin_break` **必须带数值 `threshold`**（价格/毛利阈值就是 pillar 的 `threshold`·**不另开列**·否则该 pillar 触发器无效跳过）；每票 0-4 条 pillar（宁缺勿凑，YAGNI）；`stop_conditions` 是给人看的文字摘要（非机器比对源，机器只认 pillars 的结构化 trigger_type + threshold）。
- 改 `generate_thesis_for`：解析出 `pillars`（含各 trigger 的 threshold）+ `stop_conditions` 文字，传进**已有的** `save_thesis(ticker, market, thesis_text, pillars=..., stop_conditions=...)`（tracker.py:729-749，本就支持这俩参数，现在只是没传）。
- **解析失败降级**：JSON 解析不出 → `pillars=None`、`stop_conditions=""`（与现状一致），thesis_text 照常存，P2c 对该票静默。

### 3.2 关键护栏（thesis 是 LLM 非确定性·"逐字节不变"不适用）
- PM 只读 `thesis_text`（自由文本段，portfolio_manager.py:158-171 的 `{thesis}` 槽）；结构化触发器进**独立列** `pillars`/`stop_conditions`，PM 不读 → **对 PM 材料零影响**。
- prompt 改造护栏 = "PM 消费的 `thesis_text` 段落语义不缩水"（新增结构化输出是**追加**，不替换 4 段自由文本）。测试用 mock LLM 返回固定 JSON 验证解析+落列，不测 LLM 本身。

### 3.3 向后兼容
- 旧库 theses 行 `pillars` NULL / 新生成没产出触发器 → P2c 对该票**静默不误报**。
- thesis 每 30 天重生成（thesis_node·workflow.py:102-148·`_THESIS_STALE_DAYS=30`）自然补齐结构化触发器；无需一次性迁移。**theses 表 schema 不动**（复用现成死列），故无需新 `_migrate_theses_table`。

---

## 4. 落地设计（逐组件）

### 4.1 读取 · `db/tracker.py`
- 新增 `load_thesis_triggers(ticker, db_path=None) -> dict | None`：取该票 `pillars`（`json.loads`·含各 trigger 的 `threshold`）+ `stop_conditions` 文字；`pillars` 为 NULL/空/解析失败 → None。供命中比对读取（现有 `load_all_theses` 只读 thesis_text，不够用）。price_break/margin_break 的阈值即取自对应 pillar 的 `threshold`。

### 4.2 失效事件表 + CRUD · `db/tracker.py`
- **新独立表** `thesis_invalidation_events`（照 `earnings_surprise_alerts` schema）：
  ```
  id, ticker, market, trigger_type, pillar, triggered_at, detail,
  price_at_event, price_30d, return_30d, benchmark_return_30d, created_at
  ```
  DDL 进 `_CREATE_SQL` + 两 index；`UNIQUE(ticker, trigger_type, triggered_at)` 幂等去重。
  新增 `_migrate_invalidation_table`（补 outcome 列·照 `_migrate_sue_table`）挂进 `init_db`。
- CRUD：`save_invalidation_event(...)`（`INSERT OR IGNORE` 幂等）、`get_invalidation_events(ticker=None, matured_only=False, asof=None)`（照 `get_sue_alerts`）。

### 4.3 命中比对纯函数 · `signals/thesis_invalidation.py`（新建·TDD 主战场）
- `thesis_invalidation_enabled()` / `_ti_cfg()`：读 `config/settings.yaml` 的 `thesis_invalidation` 块（照 `sue_factor_enabled` 模式）。默认 **on**。
- **纯判定函数**（脱网单测主战场，每类一个）：
  - `check_earnings_miss(sue_score, sigma) -> bool`
  - `check_news_negative(impact, sentiment, event_type, impact_min) -> bool`
  - `check_price_break(close, stop_price) -> bool`
  - `check_revenue_decline(yoy_growths: list[float]) -> bool`（最近 2 季同比均 < 0）
  - `check_margin_break(gross_margin, margin_floor) -> bool`
  - `match_triggers(triggers: dict, event: dict) -> list[dict]`：给定该票声明的 triggers + 一个检测到的 event，返回命中的 pillar 列表（trigger_type 对上 + 判据过阈）。
- **告警文案纯函数** `format_invalidation_alert(ticker, pillar, trigger_type, detail) -> str`：确定性模板（抄 `format_sue_note`）：`⚠️【论点失效】{ticker} 触发你写的失效条件『{pillar}』——{detail}。建议止损复核。` **纯提醒·绝无加仓/仓位数值字样**。

### 4.4 检测挂载 · 复用扫描器回调（零新 launchd）
- `alerts/thesis_invalidation_trigger.py`（新建）：
  - `scan_earnings_invalidation(db_path)`：对每只美股持仓，取 `load_thesis_triggers`；若声明了 `earnings_miss`/`revenue_decline`/`margin_break` → 拉最近 SUE（`get_sue_alerts`）/季度财报比对 → 命中则 `save_invalidation_event` + `_send_stock_alert` 告警。挂 `earnings-check`（record_sue_events 之后）。
  - `scan_news_invalidation(ticker, news_class)`：news-scan 分类出一条负面新闻时回调——若该票声明了 `news_negative` 且命中 → 落库+告警。挂 `news_monitor` 分类回调处。
  - `scan_price_invalidation(db_path)`：对声明了 `price_break` 的票，last close 跌破 stop_price → 落库+告警。挂 `price-scan`。
- 去重复用 `news_alerted` 表：key = `invalidation:{ticker}:{trigger_type}:{date}`（同一票同一触发器每天至多告一次·`_load_alerted`/`_save_alerted`/`_key_exists`）。
- **门控**：整段受 `thesis_invalidation_enabled()` 包裹，off → 完全不扫不告，扫描器逐字节不变。
- **降级**：单票 try/except continue；财报/财务数据缺失→静默；`_send_stock_alert` 失败不拖垮扫描器。

### 4.5 CLI · `main.py`
- 新增 `check-invalidation`（照 `generate-theses` typer 模板）：手动跑一次全持仓失效扫描 + 打印命中（`--skip-notify` 不推飞书只打印）。
- earnings-check / price-scan 处理函数末尾挂 guarded 调用（`thesis_invalidation_enabled()` 门控）。

### 4.6 flag · `config/settings.yaml`
```yaml
# ── P2c 失效触发器（提醒复核·不改建议·默认 on）──────────────────
# thesis 逐票声明失效条件(结构化词表)；扫描器命中→落 thesis_invalidation_events 表 + 飞书"止损复核"告警。
# 纯提醒·不碰任何 recommendation/仓位/权重(同 sell_guard/news 告警治理级)，默认 on。
# 严格去重(每票×触发器每天至多一告)防刷屏；off 则扫描器逐字节不变、不扫不告。
thesis_invalidation:
  enabled: true
  sigma: 1.5          # earnings_miss 阈（复用 P2b SUE 口径）
  impact_min: 7       # news_negative 冲击分阈
```

---

## 5. 反过拟合 / 诚实边界守则

- P2c 不预测价格、不做无成本回测、不堆因子——只是"把用户/系统**事先声明**的失效条件，用已披露事件机器化比对"，是**规则命中**不是统计因子。
- 阈值（1.5σ / impact 7）为顶部命名常量，直抄 P2b/news 既有口径，未做"找好看阈值"调参。
- 数据缺失/无声明触发器 → 静默不误报（宁漏告不错告）。
- 告警是"提醒**复核**"不是"命令止损"——最终止损与否由用户判断；文案绝无仓位/金额指令。

## 6. 未来函数生命线

- 触发条件是**持仓期 thesis 生成时**声明的（过去），命中是**实时事件**——天然无未来函数（不是用未来数据反推该不该持有）。
- `revenue_decline`「连续 2 季同比转负」只用**已披露**季报（yfinance quarterly 已发布行）。
- 事件表 30 天 outcome 回填（测量伏笔·§11）走成熟闸（抄 `backfill_sue_outcomes`）——回填的是"失效后真实走势"，不回喂命中判定，不构成未来函数。

## 7. 待实现时核实的数据点（不阻塞设计）

- yfinance `quarterly_financials` / `quarterly_income_stmt` 的营收行名与毛利可得性（跨版本行名不稳→优先 `info` 字段或 except→None，照 P1a `fundamental_score` 数据脆弱处理）。
- `_classify_stock_news` 返回的 `event_type`/字段名与利空事件集的对齐（实现首步核对 news_monitor 分类输出结构）。

## 8. 测试计划（离线 mock·tmp_path + AGENT_DB_PATH·全绿零回归）

**`tests/test_signals/test_thesis_invalidation.py`（纯函数主战场）**
1. `check_earnings_miss`：SUE=−2σ→True，−1σ→False
2. `check_news_negative`：impact8+负面+利空事件→True；impact8+正面→False；impact5→False
3. `check_price_break`：close<stop→True，=stop→False（含 stop=None→False）
4. `check_revenue_decline`：[−3,−1]→True；[−3,+2]→False；样本<2→False
5. `check_margin_break`：margin<floor→True；floor=None→False
6. `match_triggers`：event 对上声明的 trigger_type 且过阈→返回该 pillar；对不上→空；未声明该 type→空
7. `format_invalidation_alert`：含"止损复核"+pillar+detail、**无"加仓/买入/仓位"**
8. flag off → 扫描门控函数早退（`thesis_invalidation_enabled()` monkeypatch False）

**`tests/test_db/test_invalidation.py`（表 + CRUD）**
9. `save_invalidation_event` 写入 + `(ticker,trigger_type,triggered_at)` 幂等
10. `get_invalidation_events` matured_only 成熟闸（<30 天排除·抄 SUE 测试）
11. `load_thesis_triggers` 解析 pillars JSON + stop_conditions；无结构化→None

**`tests/test_alerts/test_invalidation_trigger.py`（挂载·mock 检测器 + mock 告警）**
12. 命中声明触发器→落库+告警调用；未声明→不告
13. 单票取数抛异常→continue 不拖垮整轮
14. 去重：同票同触发器同日第二次不重复告

**`tests/test_db/test_thesis_gen.py`（生成侧·mock LLM）**
15. mock LLM 返回结构化 JSON → `pillars`/`stop_conditions` 正确落列；解析失败→None 降级、thesis_text 照存

**回归重点**：`test_pm_fallback`（thesis→PM 材料未变）、news-scan/earnings-check/price-scan 既有测试、`test_sue_alerts`（复用未破）。

## 9. 文件清单

**新建**：`signals/thesis_invalidation.py`、`alerts/thesis_invalidation_trigger.py`、`tests/test_signals/test_thesis_invalidation.py`、`tests/test_db/test_invalidation.py`、`tests/test_alerts/test_invalidation_trigger.py`
**改**：`db/thesis_generator.py`（THESIS_SYSTEM + generate_thesis_for 解析）、`agents/prompts.py`（若 THESIS_SYSTEM 在此则改此）、`db/tracker.py`（事件表 DDL+迁移+CRUD+load_thesis_triggers）、`alerts/news_monitor.py`（分类回调挂 scan_news_invalidation）、`main.py`（check-invalidation CLI + earnings-check/price-scan 挂载）、`config/settings.yaml`

## 10. 验收标准 + 对抗审查清单

**验收**
- ① flag off → 三个扫描器逐字节不变、不扫不告；on 时命中才告、去重生效。
- ② thesis 生成落结构化触发器正确；解析失败优雅降级、thesis_text 不变、PM 材料零影响。
- ③ 5 类判据纯函数正确；数据缺失/无声明触发器静默不误报。
- ④ 告警文案纯提醒·无加仓/仓位字样；单票失败不拖垮扫描器。
- ⑤ 新测试全绿 + 全量零回归 `uv run pytest tests/ -x -q`。

**对抗审查（PASS 才算过）**
- 误报风险：会不会把"没声明该触发器"的票也告？会不会对普通负面新闻乱告（利空事件集是否够窄）？
- 未来函数：连续 2 季营收是否只用已披露季报？触发条件是否确为持仓期声明？
- 降级/隔离：任一检测器/告警失败是否不拖垮扫描器与日报？
- 单向性：告警是否只"提醒复核"、绝无仓位/止损数值指令、无加仓字样？
- 去重：同事件是否只告一次（防刷屏惹用户烦）？

## 11. 测量伏笔（照 P2b sue_edge·本轮只建表·读数留后续）

事件表留 `return_30d`/`benchmark_return_30d`，30 天后回填（抄 `backfill_sue_outcomes` 成熟闸+原子两腿）。将来加 `invalidation_edge` 读数：**失效告警后 30 天，该票是否真的继续跑输基准**——证明"这个止损信号到底准不准、值不值得听"。命中判定不用 outcome（无未来函数）。MVP 只建表+留列，回填/edge 读数下一轮。

## 12. 下一步（writing-plans）

本 spec 定档后调 **writing-plans** 创建 RED-GREEN 实现计划：按 §8 先写测试到 RED，再逐组件到 GREEN，flag off 验证（扫描器逐字节不变）+ 对抗审查 PASS 后 merge。**告警默认 on（纯提醒），无需额外 enable 点头**；若后续加 PM 日报注入（改核心建议）那档再走 flag off + 点头。
