# CLAUDE.md

## 🎯 北极星：能帮用户赚到钱（唯一终极标准）

卡门智投存在的唯一理由 = **为用户创造可证明的投资价值**。用"一款值得付费的投资建议软件"来要求自己：
**用户会为我们买单吗？凭什么证明我们能帮他赚钱 / 少亏钱？** 赚不到钱，一切（漂亮的卡片、双轨道分发、UI 精简）都难说。

**核心价值（按对盈亏的影响排序）：**
1. **已有持仓的操作指导**（买 / 持 / 减 / 止损）——最高频、最直接影响盈亏
2. **选股建议**（新机会，价值投资或短线皆可）
3. **持仓回顾**（操作对错复盘）
4. **经济强相关新闻**（影响决策的信息）

**优先级铁律：** 双轨道分发（GitHub 传播）、Docker、README、卡片折叠……都是**次要的传播 / 体验选择**，服务于核心价值，不得凌驾其上。**先拿到核心商业价值，再谈传播。**

### 一切迭代（loop / 全自动）都围绕"证明价值"
每个周期（日 / 周 / 月 / 年度）都要回答：
- **我们的建议对不对？**（命中率、相对基准 SPY/恒指 的超额收益 alpha）
- **交易者的行为对不对？**（实际操作的 7日 / 月度 / 年度结果，是否跑赢"躺平不动"）
- **还能做什么提升赚钱能力？**
- **持续为「我们的建议」和「交易者的行为」双向打分**，用分数驱动下一轮迭代。

**衡量"值不值得付费"的硬指标（持续追踪、诚实呈现，不许粉饰）：**
- 推荐信号 vs 基准的超额收益（alpha）与命中率
- 用户实际操作的盈亏 / 是否跑赢被动持有
- 重大风险预警是否提前（避免大亏，少亏也是赚）

> 行为闭环（P1 采集 / P2 漂移台账 / P3 月报打分）+ recommendations 表（outcome + benchmark_return_7d）已是"证明价值"的地基；后续迭代持续夯实它，并用它反过来提升建议质量。

## 协作约定

- **交互语言：中文**。回复/解释/提问均用中文；代码注释、提交信息、变量名保持英文。
- **范围克制**：只改被明确要求的，不顺手重构周边代码，不加未被要求的功能。
- **有疑问先问**：涉及持仓数据、推送逻辑、破坏性操作（删数据/改 schema）前，先确认再动手。
- **明确完成标准**：接到模糊任务时，先把"怎么算做完"说清楚再开始写代码。

## 主要模块

| 模块 | 职责 |
|---|---|
| `graph/workflow.py` | LangGraph DAG，串联完整日报流水线 |
| `agents/` | Bull/Bear 辩手（DeepSeek）、基本面分析师、组合决策（Claude）、Prompt 模板 |
| `data/` | DataRouter：us→yfinance，hk/cn→AkShare；宏观数据（VIX/大盘） |
| `alerts/` | 盘中新闻扫描（DeepSeek 评分≥7 推送）、财报触发器 |
| `db/tracker.py` | recommendations/theses/user_actions/dip_alerts CRUD + 7日回填胜率 |
| `notifications/feishu.py` | 飞书 Webhook（HMAC-SHA256 签名）+ 邮件兜底 |
| `signals/technical.py` | RSI/MACD/布林带等技术指标，输出 composite_score |
| `weekly/` | 周报生成（3步 Claude 调用）、配置建议、每日跟进 |
| `monthly/review.py` | 月度回顾，汇总胜率 + 用户操作反馈闭环 |
| `storage/db.py` | aiosqlite 异步写入（日报流水线内部用） |
| `memory/mempal_client.py` | Mempal 长期记忆（可选集成） |

## 外部集成

- **飞书**：`FEISHU_WEBHOOK_URL` + `FEISHU_WEBHOOK_SECRET`（HMAC签名），失败自动邮件兜底
- **DeepSeek V3**：`DEEPSEEK_API_KEY`，用于 Bull/Bear 辩论 + 新闻评分
- **Claude**：`ANTHROPIC_API_KEY` 或 `CLAUDE_CODE_OAUTH_TOKEN`，用于基本面分析 + 组合决策 + 周报；Claude 子进程失败自动降级 DeepSeek
- **调度**：**本地 launchd**（`~/Library/LaunchAgents/com.<user>.carmen-*.plist`，`<user>` 为本机登录用户名，实到的 label 用 `launchctl list | grep carmen` 查；备份在 `~/config-backups/launchd-*/`）。⚠️ GitHub Actions 定时任务已于 2026-06-09 全部禁用（仅留"Actions 失败飞书通知"事件触发），**不要再用 GitHub cron**——历史上本地+GitHub 双跑导致日报一天推 3 遍。

## 调度时刻表（北京时间 · 本地 launchd 实况，2026-06-13 校准）

| launchd 任务 | 命令 | 触发时间 | 频率 |
|---|---|---|---|
| `carmen-morning` | `morning-note` | 08:55 | 周一–五 |
| `carmen-earnings` | `earnings-check` | 08:30 | 周一–五 |
| `carmen-health` | `health-check` | 08:45 | 每天（异常才推） |
| `carmen-followup` | `daily-followup` | 09:30 | 周二–五 |
| `carmen-news` | `news-scan` | 09:30 / 13:30 / 22:00 | 周一–五，每天 3 次 |
| `carmen-pricescan` | `price-scan` | 每 15 分钟（StartInterval 900s） | 全天，休市自动跳过 |
| `carmen-evening` | `run`（晚间日报） | 21:30 | 周一–五 |
| `carmen-weekly` | `weekly-report` | 09:30 | 每周一 |
| `carmen-value` | `value-report`（价值体检卡） | 10:00 | 每周六 |
| `carmen-monthly` | `monthly-review` | 10:00 | 每月 1 日 |

> launchd `Weekday`：1=周一…5=周五，6=周六，0/7=周日。改调度务必先 `cp` 备份 plist → `plutil -lint` 校验 → `launchctl unload && load`。心跳自检（carmen-health）每天检查各任务日志新鲜度，静默死亡会自动告警。

## 约定与关键逻辑

- **HK 代码格式**：`portfolio.yaml` 填 `00700`（无 `.HK`），内部调用 `_to_yf_hk()` 自动转换
- **货币归一化**：HKD÷7.8、CNY÷7.2 → USD，仅用于集中度计算，不影响盈亏显示
- **推荐去重**：`save_recommendations()` 按 `(date, ticker)` 查重，同天已有记录则跳过（幂等）
- **新闻去重**：`news_alerted` 表以 `(key, date)` 为联合主键；key = 标题关键词指纹（去停用词后 SHA256）
- **并发保护**：yfinance `_YF_SEM(2)`，AkShare `_AK_SEM(1)`，定义在 `data/yf_utils.py`
- **Thesis 新鲜度**：`get_thesis_ages()` 检查，>30天自动重生成

## 本地测试

```bash
uv sync --extra dev          # 安装依赖（含测试）
cp .env.example .env         # 填入 API key

uv run pytest tests/ -x -q   # 全量测试，-x 遇错即停

# 必须从项目根运行，否则 data/agent.db 路径错误
finance-agent run --skip-notify     # 日报干跑
finance-agent weekly-report --force
finance-agent news-scan
finance-agent earnings-check --skip-notify
finance-agent generate-theses --ticker NVDA
finance-agent log-action NVDA BUY --shares 1 --price 190
finance-agent feedback-stats
```

## 已知陷阱

- **AkShare 东方财富**：TLS 指纹拦截，`retries=1` 快速失败后自动降级 yfinance，非代理问题；`eastmoney.com` 需加入 `NO_PROXY`（代码自动注入）
- **ETF 基本面**：`sector` 含 `ETF` 或 `is_dca=True` 的标的跳过 `fetch_earnings`（Yahoo 无此字段）
- **`send_hour` 无效**：`settings.yaml` 里该字段不控制推送时间，调度由本地 launchd plist 决定（见上方时刻表）
- **DB 路径**：默认 `data/agent.db`，可用 `AGENT_DB_PATH` 环境变量覆盖；CI 通过 artifact 跨 run 恢复
- **DeepSeek 串行**：Bull/Bear 辩手串行调用（非并发），避免 DeepSeek API 限速

## 主线与出片（起手即收口）
- 本项目主线只认 `进展.md`（4段：现在/接下来/为什么/素材台账），新会话开局先读它，禁止再建散文件。
- 出片档 = ②只发能力·数据打码：系统/方法/打码截图可发 GitHub，**真实持仓 / 金额 / agent.db / portfolio.yaml 绝不外泄**。
- 凡产出可对外产物（量化卡 / 架构图 / 打码报告），cp 一份进 `ship/github/` 并在 进展.md 素材台账记一行。
