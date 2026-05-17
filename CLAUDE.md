# CLAUDE.md

## 协作约定

- **交互语言：中文**。所有回复、解释、提问均用中文；代码注释、提交信息、变量名保持英文。

## 常用命令

```bash
# 环境
uv sync --extra dev          # 安装依赖（含测试）
cp .env.example .env         # 配置 API key

# 必须从项目根目录运行，否则 data/agent.db 路径解析出错
finance-agent run --skip-notify        # 日报干跑
finance-agent weekly-report --force    # 强制重跑周报
finance-agent daily-followup --skip-notify
finance-agent news-scan
finance-agent earnings-check --skip-notify
finance-agent generate-theses --ticker NVDA
finance-agent monthly-review --skip-notify
finance-agent dip-stats --days 30

# 反馈闭环
finance-agent log-action NVDA BUY --shares 1 --price 190
finance-agent show-actions --days 30
finance-agent feedback-stats

# 测试
uv run pytest tests/ -x -q
```

## 架构速览

**日报流水线**（`graph/workflow.py`，LangGraph DAG）：
```
fetch_data → thesis → fundamentals → debate → decision → format → track
```
- `fetch_data`：并行拉 OHLCV / 新闻 / 基本面 + 宏观背景（VIX/大盘），ETF 跳过 `fetch_earnings`
- `debate`：DeepSeek 串行跑 Bull / Bear（避免并发限速）；DCA 标的跳过
- `decision`：Claude 批量输出所有持仓决策

**模型分工**：
- DeepSeek V3 → Bull/Bear 辩论（`DEEPSEEK_API_KEY`）
- Claude → 基本面分析 + 组合决策 + 周报（`claude -p` 子进程，失败自动降级 DeepSeek）

**数据路由**（`data/router.py`）：`us` → yfinance，`hk`/`cn` → AkShare，港股代码用 `00700` 格式

**并发保护**：yfinance 共享 `_YF_SEM(2)`，AkShare 串行 `_AK_SEM(1)`，均定义在 `data/yf_utils.py`

**持久化**（`data/agent.db`，`AGENT_DB_PATH` 可覆盖）：
- `recommendations` — 日报推荐 + 7日回填胜率
- `theses` — 持仓逻辑，>30天自动重生成
- `user_actions` — 手动操作记录
- `dip_alerts` — 价格异动追踪，24h/7d 回填收益

**周报**（`weekly/allocation_advisor.py`）：3步 Claude 调用，结果缓存到 `data/weekly_latest.json`（ISO周粒度）

## 关键配置

- `config/portfolio.yaml` — 持仓，`cost_basis` 填原始货币（美股 USD、港股 HKD、A股 CNY）
- `config/settings.yaml` — 模型参数 + 信号阈值；`send_hour` 无效，调度靠 GitHub Actions
- `.env` — `DEEPSEEK_API_KEY`（必填）、`FEISHU_WEBHOOK_URL`、`CLAUDE_CODE_OAUTH_TOKEN`

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

## 已知陷阱

- **AkShare 东方财富**：TLS 指纹被拦截，`retries=1` 快速失败后自动降级 yfinance，不是代理问题
- **ETF 基本面**：`sector` 含 `ETF` 或 `is_dca=True` 的标的跳过 `fetch_earnings`（Yahoo 无此数据）
- **HK 股格式**：portfolio.yaml 填 `00700`（无 .HK），AkShare/yfinance 内部自动转换
- **货币归一化**：HKD÷7.8、CNY÷7.2 换算 USD，仅用于集中度计算，不影响盈亏显示
- **Known Issues 原则**：已在代码注释中说明的不重复写这里，只记录非显而易见的行为
