# CLAUDE.md

## 协作约定

- **交互语言：中文**。回复/解释/提问均用中文；代码注释、提交信息、变量名保持英文。

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
- **GitHub Actions**：7 个 workflow，SQLite 通过 artifact 跨 run 持久化（保留60天）

## GitHub Actions 时刻表（北京时间）

| Workflow | 触发时间 | 命令 |
|---|---|---|
| `daily_analysis` | 工作日 09:00 & 21:30 | `run` |
| `daily_followup` | 周二–五 09:30 | `daily-followup` |
| `weekly_report` | 周一 09:30 | `weekly-report` |
| `price_alert` | 工作日 09:00–16:00 & 21:00–05:00 每5分钟 | `price-scan` |
| `news_alert` | 工作日每小时 | `news-scan` |
| `earnings_check` | 工作日 08:30 | `earnings-check` |
| `monthly_review` | 每月1日 10:00 | `monthly-review` |

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
- **`send_hour` 无效**：`settings.yaml` 里该字段不控制推送时间，调度由 GitHub Actions cron 决定
- **DB 路径**：默认 `data/agent.db`，可用 `AGENT_DB_PATH` 环境变量覆盖；CI 通过 artifact 跨 run 恢复
- **DeepSeek 串行**：Bull/Bear 辩手串行调用（非并发），避免 DeepSeek API 限速
