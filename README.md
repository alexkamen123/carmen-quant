# 卡门智投 · Carmen Quant

> 个人/家庭量化交易助手：多 Agent 协作，每日自动分析持仓，推送飞书日报

[![GitHub Actions](https://github.com/alexkamen123/finance-agent/actions/workflows/daily_analysis.yml/badge.svg)](https://github.com/alexkamen123/finance-agent/actions)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)

---

## 效果预览

每个工作日 09:00（北京时间）自动推送飞书卡片日报：

```
📊 卡门智投日报 · 2026-05-09
━━━━ 今日操作建议 ━━━━

🟢 NVDA  买入  ★★★
   基本面强劲，技术面突破，建议逢低加仓
   📌 回调至 MA20（203）附近可加仓，止损 MA60（188）下方
   ⚠️  估值 PE 43x，注意业绩不及预期风险

   📈 基本面：营收同比 +73%，毛利率 71%，PE 43.8x
   🐂 多方：MACD 金叉，均线多头排列，AI 算力需求强劲
   🐻 空方：布林带上轨 92%，RSI 65 接近超买，估值有压力
```

---

## 架构

```
fetch_data → fundamentals → debate → decision → format
    ↓              ↓           ↓         ↓
 AkShare/      Claude      DeepSeek  Claude/     飞书卡片
 yfinance    基本面分析    Bull/Bear  DeepSeek
                           辩论      最终裁决
```

**5 个 LangGraph 节点，3 个角色分工：**

| 角色 | 模型 | 职责 |
|------|------|------|
| 基本面分析师 | Claude | 解读财报数据，输出一句话判断 |
| 多方 / 空方 | DeepSeek V3 | 从技术面 + 基本面各持一方辩论 |
| 组合经理 | Claude（降级 DeepSeek） | 综合辩论结果，给出买/持/观望/减/卖 + 置信度 |

---

## 数据源

| 市场 | 行情（OHLCV） | 基本面 |
|------|------------|------|
| 美股 | yfinance（免费） | yfinance stock.info |
| 港股 | AkShare / 东方财富（免费） | yfinance（0700.HK 格式） |
| A 股 | AkShare / 东方财富（免费） | 暂无（欢迎 PR） |

---

## 快速开始

### 1. 克隆 & 安装

```bash
git clone https://github.com/alexkamen123/finance-agent.git
cd finance-agent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 2. 配置密钥

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY（必填）和其他可选项
```

### 3. 配置持仓

```bash
# 编辑 config/portfolio.yaml，填入你关注的股票
```

支持美股（`market: us`）、港股（`market: hk`）、A 股（`market: cn`）。`is_dca: true` 标记的标的跳过辩论，直接建议按定投计划执行。

### 4. 本地运行

```bash
# 运行分析，不推飞书（调试用）
finance-agent run --skip-notify

# 完整运行（需配置 FEISHU_WEBHOOK_URL）
finance-agent run
```

---

## 自动化：GitHub Actions 每日推送

### 1. Fork 本仓库

### 2. 配置 Secrets

在仓库 → Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 | 必填 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | ✅ |
| `FEISHU_WEBHOOK_URL` | 飞书机器人 Webhook 地址 | ✅ |
| `FEISHU_WEBHOOK_SECRET` | 飞书签名密钥（开启校验时填） | 可选 |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Pro OAuth Token | 可选 |

### 3. 启用 Actions

仓库 → Actions → 启用 workflows。每个工作日北京时间 09:00 自动运行。

也可手动触发：Actions → 每日持仓分析 → Run workflow。

---

## 飞书机器人配置

1. 打开飞书群 → 设置 → 群机器人 → 添加机器人
2. 选择「自定义机器人」，填写名称（如：卡门智投）
3. 复制 Webhook 地址填入 `FEISHU_WEBHOOK_URL`
4. 可选：开启「签名校验」，复制密钥填入 `FEISHU_WEBHOOK_SECRET`

---

## Claude OAuth Token 获取（免额外费用）

如果你有 Claude Pro 订阅（月付），可以不用单独购买 API Key：

```bash
# 需要先安装 Claude Code CLI：https://claude.ai/code
claude setup-token
# 复制输出的 sk-ant-oat01-... 到 CLAUDE_CODE_OAUTH_TOKEN
```

---

## 技术栈

- **LangGraph** — 多 Agent 状态机编排
- **DeepSeek V3** — Bull/Bear 辩论（低成本，高性能）
- **Claude Sonnet** — 基本面分析 + 最终裁决（高质量判断）
- **yfinance / AkShare** — 行情 & 基本面数据（免费）
- **pandas-ta** — 技术指标计算（MA/MACD/RSI/布林带）
- **飞书 Webhook** — 卡片消息推送
- **GitHub Actions** — 定时自动化

---

## 项目结构

```
finance-agent/
├── config/
│   └── portfolio.yaml          # 持仓配置（在这里填你的股票）
├── src/finance_agent/
│   ├── agents/
│   │   ├── bull_agent.py       # 多方分析（DeepSeek）
│   │   ├── bear_agent.py       # 空方分析（DeepSeek）
│   │   ├── fundamental_analyst.py  # 基本面分析（Claude）
│   │   ├── portfolio_manager.py    # 最终裁决（Claude）
│   │   └── prompts.py          # 所有 prompt 模板
│   ├── data/
│   │   ├── yfinance_provider.py    # 美股/港股数据
│   │   └── akshare_provider.py    # 港股/A股行情
│   ├── graph/
│   │   ├── state.py            # LangGraph 状态定义
│   │   └── workflow.py         # 5节点 DAG 编排
│   ├── signals/
│   │   └── technical.py        # 技术指标计算
│   ├── notifications/
│   │   └── feishu.py           # 飞书卡片推送
│   └── storage/
│       └── db.py               # SQLite 历史信号存储
└── .github/workflows/
    └── daily_analysis.yml      # GitHub Actions 定时任务
```

---

## 常见问题

**Q: A 股股票分析质量怎么样？**
A: 行情数据通过 AkShare 获取，技术分析正常。基本面数据暂无（yfinance 对 A 股覆盖有限），欢迎贡献 PR 接入同花顺 / 东方财富财务接口。

**Q: 港股行情本地运行报错怎么办？**
A: 港股数据来自东方财富，代码已自动将其加入 `NO_PROXY` 直连。如仍有问题，检查本地是否设置了全局代理。

**Q: 每天 Claude 会消耗多少额度？**
A: 约 10 次 Sonnet 调用（基本面 × N 只非 ETF 股票 + PM × N），使用 Claude Pro OAuth Token 时在 5-hour 限额的 30-40% 以内。

**Q: 可以只用 DeepSeek 不用 Claude 吗？**
A: 可以。不配置 `CLAUDE_CODE_OAUTH_TOKEN` 和 `ANTHROPIC_API_KEY`，基本面分析会展示原始财务数字，PM 裁决全部由 DeepSeek 完成。

---

## Contributing

欢迎 PR，特别是：
- A 股基本面数据接入
- 更多技术指标（KDJ、OBV 等）
- 回测胜率统计优化
- 支持更多推送渠道（微信、Telegram）

---

## License

MIT
