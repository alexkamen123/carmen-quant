# 卡门智投 · Carmen Quant

> **多 Agent 对抗式投研助手**：让多空双方各自举证辩论，再由裁决 Agent 出结论——
> 用架构而不是 prompt 去对冲单模型的一面之词。日/周/月三档管线，全自动推送飞书。

[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![部署形态](https://img.shields.io/badge/部署-GitHub_Actions_或_本机_launchd-green)](#-5-分钟快速开始推荐github-actions无需本地环境)

---

## 功能概览

| 功能 | 频率 | 说明 |
|------|------|------|
| **持仓日报** | 每个工作日 | 技术面 + 基本面 + 多空辩论 → 操作建议，推送飞书 |
| **周度配置建议** | 每周一 | 持仓诊断 → 对冲方向 → 市场机会筛选，推送飞书 |
| **每日轻量跟进** | 周二至周五 | 基于周报持续跟踪推荐标的价格变化 |
| **新闻预警** | 每 2 小时 | 持仓股 + 竞争对手新闻实时评分，超阈值即推送 |
| **财报预警** | 每天 08:30 | 持仓美股 7 天内有财报则发送飞书橙色预警 |
| **月度回顾** | 每月 1 日 | 上月推荐准确率统计 + Claude 复盘总结 + 操作反馈分析 |
| **名词解释** | 每期自动 | 检测本期报告出现的金融术语，底部附大白话说明 |

---

## 架构

### 日报流水线（LangGraph 7 节点）

```
fetch_data → thesis → fundamentals → debate → decision → format → track
    ↓           ↓           ↓           ↓         ↓         ↓       ↓
行情/新闻    加载持仓    Claude      DeepSeek  Claude    飞书卡片  SQLite
技术指标     逻辑(DB)   基本面分析  多空辩论   最终裁决  + 名词解释  + 回填
```

**三个 Agent 角色：**

| 角色 | 模型 | 职责 |
|------|------|------|
| 基本面分析师 | Claude CLI | 解读财务数据，输出白话判断 |
| 多头 / 空头 | DeepSeek V3 | 从价值/成长/动量三视角各持一方辩论 |
| 组合经理 | Claude CLI（降级 DeepSeek） | 综合持仓成本/浮盈/集中度，给出操作建议 |

### 周报流水线（三步）

```
持仓集中度计算（统一换算为美元）
      ↓
Step 1 配置诊断（Claude）  →  集中风险 + 宏观风险 + 对冲方向
      ↓
Step 2 对冲选品（Claude）  →  每个方向推荐 1-2 个可交易品种
      ↓
Step 3 机会筛选           →  RSI<48 技术初筛 + 基本面双重过滤
                              → Claude 结合持仓集中度精选 3-5 只
      ↓
结果缓存（同一 ISO 周内复用，--force 可强制重跑）
```

---

## 数据源

| 市场 | 行情（OHLCV） | 基本面 |
|------|------------|------|
| 美股 | yfinance（免费） | yfinance stock.info |
| 港股 | AkShare / 东方财富（免费） | yfinance（0700.HK 格式） |
| A 股 | AkShare / 东方财富（免费） | 暂无（欢迎 PR） |

---

## 设计取舍

做这套东西时反复回答的问题是：**哪些事必须由代码保证，哪些才交给模型。**

| 决策 | 怎么做的 | 为什么 |
|---|---|---|
| **对冲单模型的一面之词** | 不让一个模型"客观分析"，而是强制多头/空头各持一方举证，再由第三个 Agent 裁决 | 单模型面对同一份数据容易顺着自己第一句话往下写。让立场对立是**架构层面**的纠偏，比在 prompt 里叮嘱"请客观"可靠 |
| **按任务难度分档用模型** | 基本面解读与最终裁决用 Claude；多空辩论用 DeepSeek V3 | 辩论要跑两个角色、每天都跑，是成本大头；裁决只跑一次但最吃推理质量。不是所有环节都值得上最贵的模型 |
| **候选集先由代码收窄** | 机会筛选先跑 RSI<48 + 基本面双重过滤，再让 Claude 在小候选集里精选 3–5 只 | 让 LLM 扫全市场既慢又贵且不稳定。确定性规则负责"缩小到几十个"，模型负责"从几十个里挑理由最充分的" |
| **关键环节留降级链** | 组合经理 Claude CLI 不可用时降级到 DeepSeek | 分析可以差一点，但管线不能断——每天要出报告 |
| **重跑不重复计费** | 周报结果按 ISO 周缓存，`--force` 才强制重算 | 调试期反复跑同一周会烧钱；幂等是省钱也是防重 |
| **结论要能被回头检验** | `track` 节点把每次建议回填 SQLite，月度跑准确率复盘 | 不留痕的建议无法证伪，也就无法改进 |

## 已知边界

**这个项目不声称盈利，也不构成投资建议。** 它的定位是「帮我判断能不能赚钱」的分析工具，不是收益承诺。
以下是它现在明确做不到的：

- **辩论纠不了输入错误**：多空对抗能对冲模型的立场偏差，但双方读的是同一批行情与新闻。数据源错了或漏了，辩论只会把错误论证得更充分。
- **A 股缺基本面**：行情有，基本面数据源暂缺（见上表），A 股标的的分析质量明显低于美股。
- **免费数据源的代价**：yfinance / AkShare 有延迟与限流，盘中信号并不实时；财报数据偶有字段缺失。
- **准确率复盘是自评**：月度统计基于自己回填的操作记录，样本量小、且只覆盖被推荐过的标的，不能等同于策略胜率。
- **人仍然是决策者**：所有输出都是"建议"，下单动作不自动化，也不打算自动化。

## ⚡ 5 分钟快速开始（推荐：GitHub Actions，无需本地环境）

> 最简单的方式：Fork + 配置 3 个 secrets = 自动每天分析 + 飞书推送

### Step 1. Fork 本仓库

点击右上角 Fork 按钮，复制到你的账号下。

### Step 2. 配置 3 个必填 Secrets

**仓库首页 → Settings → Secrets and variables → Actions → New repository secret**

| Secret | 说明 | 获取方式 |
|--------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | https://platform.deepseek.com/ |
| `FEISHU_WEBHOOK_URL` | 飞书机器人 Webhook | 见下方[飞书配置](#飞书机器人配置) |
| `DEEPSEEK_API_KEY` 或 `ANTHROPIC_API_KEY` | Claude 相关密钥（选其一） | 见下方[Claude 配置](#claude-oauth-token-获取) |

### Step 3. 配置你的持仓

编辑 `config/portfolio.yaml`，填入你的实际持仓：

```yaml
holdings:
  - ticker: NVDA
    market: us
    shares: 3.04
    cost_basis: 194.82  # 买入均价（USD，美股填美元）
    sector: 半导体/AI算力
    peers: ["AMD", "INTC"]

  - ticker: "00700"
    market: hk
    shares: 3
    cost_basis: 541.50  # ⚠️ 港股填港币 HKD，不要填美元
    sector: 互联网/AI
```

> **⚠️ 货币说明**：`cost_basis` 必须填写股票原始货币。美股填 USD，港股填 HKD，A股填 CNY。填错货币会导致盈亏计算出现 -89% 等严重偏差。

提交后自动触发，**每个工作日 9 点和 21 点**飞书推送日报。

### 📅 自动任务时间表

| 任务 | 频率 | 北京时间 |
|------|------|--------|
| 持仓日报（港股场） | 工作日 | 09:00 |
| 持仓日报（美股场） | 工作日 | 21:30 |
| 周度配置建议 | 每周一 | 09:00 |
| 每日跟进 | 周二至周五 | 09:00 |
| 新闻实时预警 | 工作日 | 每 2 小时 |
| 财报日期提醒 | 工作日 | 08:30 |
| 月度回顾 | 每月 1 日 | 09:00 |

> **关于两种部署形态**：同一套管线支持 GitHub Actions 与本机 launchd 两种调度。
> Actions 的好处是 fork 完配好 Secrets 就能跑、不需要一台常开的机器，**推荐新用户走这条路**。
> 但作者本人已把调度迁到本机 launchd —— 免费额度有限、冷启动慢、且本地跑可以直接调用装在机器上的
> Claude Code CLI。所以**本仓库的定时 workflow 处于手动停用状态**（你 fork 后自行启用即可），
> 这不是坏掉了。调度器可换，管线本身两边共用同一套代码。

---

## 🔧 进阶：本地开发 / 自己迭代

如果想在本地修改代码、使用 Claude Code Agent View 等高级功能：

### 1. 克隆 & 安装

```bash
git clone https://github.com/alexkamen123/finance-agent.git
cd finance-agent
pip install uv
uv sync
```

### 2. 配置密钥

```bash
cp .env.example .env
# 填入 DEEPSEEK_API_KEY、ANTHROPIC_API_KEY 或 CLAUDE_CODE_OAUTH_TOKEN
```

### 3. 本地测试运行

```bash
# 日报分析（不发飞书）
finance-agent run --skip-notify

# 周度配置建议
finance-agent weekly-report --skip-notify

# 新闻预警扫描
finance-agent news-scan --skip-notify

# 财报检查
finance-agent earnings-check --skip-notify
```

---

## 记录实际操作（反馈闭环）

直接在对话中用自然语言告诉 Claude 即可，无需记忆命令：

> "今天买了英伟达，190块"
> "卖了一股腾讯"

或者手动 CLI：

```bash
finance-agent log-action NVDA BUY --shares 1 --price 190
finance-agent log-action TLT SKIP --note "等降息信号"
finance-agent show-actions          # 查看操作历史
finance-agent feedback-stats        # 查看实际操作胜率
```

**7 天后系统自动回填盈亏**，月度回顾时 Claude 会对比"模型推荐"和"你实际操作"的差异。

---

## 飞书机器人配置

1. 打开飞书群 → 设置 → 群机器人 → 添加机器人
2. 选择「自定义机器人」，填写名称（如：卡门智投）
3. 复制 Webhook 地址填入 `FEISHU_WEBHOOK_URL`
4. 可选：开启签名校验，密钥填入 `FEISHU_WEBHOOK_SECRET`

---

## Claude OAuth Token 获取（Claude Pro 订阅免费用）

```bash
# 安装 Claude Code CLI：https://claude.ai/code
claude setup-token
# 复制输出的 token 到 CLAUDE_CODE_OAUTH_TOKEN
```

---

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent 编排 | LangGraph（StateGraph） |
| 多空辩论 | DeepSeek V3（低成本高性能） |
| 基本面 + 裁决 | Claude Sonnet（Claude CLI / API） |
| 行情数据 | yfinance（美股/港股）、AkShare（港股/A股） |
| 技术指标 | pandas-ta（MA / MACD / RSI / 布林带） |
| 持久化 | SQLite（推荐历史 / 持仓逻辑 / 操作记录） |
| 推送 | 飞书 Webhook（交互式卡片） |
| 自动化 | GitHub Actions（6 个 Workflow） |

---

## 项目结构

```
finance-agent/
├── config/
│   └── portfolio.yaml              # 持仓配置（在这里填你的股票）
├── src/finance_agent/
│   ├── agents/
│   │   ├── bull_agent.py           # 多方分析（DeepSeek）
│   │   ├── bear_agent.py           # 空方分析（DeepSeek）
│   │   ├── fundamental_analyst.py  # 基本面分析（Claude）
│   │   ├── portfolio_manager.py    # 最终裁决（Claude，含成本/浮盈/集中度）
│   │   ├── claude_client.py        # Claude CLI 异步封装（含重试）
│   │   └── prompts.py              # 所有 prompt 模板
│   ├── alerts/
│   │   ├── news_monitor.py         # 新闻实时预警
│   │   └── earnings_trigger.py     # 财报日期预警
│   ├── data/
│   │   ├── router.py               # 数据源路由（美股/港股/A股）
│   │   ├── yfinance_provider.py    # 美股/港股行情
│   │   ├── akshare_provider.py     # 港股/A股行情
│   │   └── macro.py                # 宏观背景（VIX/利率/指数）
│   ├── db/
│   │   ├── tracker.py              # 推荐历史 + 准确率回填 + 用户操作记录
│   │   └── thesis_generator.py     # 持仓逻辑生成（Claude）
│   ├── graph/
│   │   ├── state.py                # LangGraph 状态定义
│   │   └── workflow.py             # 7 节点 DAG 编排
│   ├── weekly/
│   │   ├── allocation_advisor.py   # 周度三步配置建议
│   │   ├── report_card.py          # 周报飞书卡片渲染
│   │   └── daily_followup.py       # 周二至周五轻量跟进
│   ├── monthly/
│   │   └── review.py               # 月度回顾（准确率 + Claude 复盘）
│   ├── notifications/
│   │   ├── feishu.py               # 飞书卡片推送
│   │   └── glossary.py             # 金融术语检测 + 白话解释
│   ├── signals/
│   │   └── technical.py            # 技术指标计算
│   └── main.py                     # CLI 入口（Typer）
├── .github/workflows/              # 6 个自动化任务
└── data/
    ├── agent.db                    # SQLite 数据库
    └── weekly_latest.json          # 本周周报缓存
```

---

## 迭代记录

### Phase 1 · 基础多 Agent 框架
- LangGraph 7 节点流水线（fetch → thesis → fundamentals → debate → decision → format → track）
- DeepSeek Bull/Bear 辩论 + Claude 最终裁决
- SQLite 推荐历史记录 + 7 日准确率自动回填
- 飞书交互卡片推送

### Phase 2 · 分析质量提升
- **持仓成本注入**：PM 裁决时知道你每支股的均价和当前浮盈，不再盲目建议加仓
- **Investor Lens**：Bull/Bear 辩论植入三种投资哲学框架（价值/成长/动量），避免千篇一律
- **Thesis Tracker**：持仓逻辑持久化进 SQLite，每次分析都有历史逻辑可对比
- **基本面双重门槛**：机会筛选加入 PE+营收增速过滤，排除价值陷阱
- **竞争对手新闻**：peers 字段一并扫描，NVIDIA 有消息时 GOOGL 也会被关注

### Phase 3 · 周/月报 + 自动化
- 周度三步配置建议（诊断 → 对冲选品 → 机会筛选）
- 每日轻量跟进（周二至周五）
- 新闻实时预警（双市场时段）
- 财报季事件驱动预警（7 天内财报自动提醒）
- 月度投资回顾（准确率统计 + Claude 复盘）
- GitHub Actions 6 个 Workflow 全自动运行

### Phase 4 · 稳定性 & 一致性
- **港股虚胖修复**：HKD ÷ 7.8、CNY ÷ 7.2 统一换算为美元，持仓集中度计算准确
- **周报逻辑矛盾修复**：持仓集中度注入筛选 prompt，不再出现「诊断科技超配但推荐更多科技」
- **Claude CLI 重试**：偶发 exit 1（空 stderr）自动重试最多 2 次
- **周报版本锁定**：同一 ISO 周内缓存结果，--force 可强制重跑，保证口径一致
- **RSI 缓冲区**：过滤阈值 <45 → <48，减少边缘标的因微小波动进出筛选

### Phase 5 · 反馈闭环 & 可读性
- **用户操作记录**：自然语言告知 Claude 操作，自动记录进 DB
- **操作胜率回填**：BUY 操作 7 天后自动拉价格计算盈亏，月报呈现「你的决策质量」
- **日报精简**：多空辩论压缩为一行（最强论点各一条），去掉冗余段落
- **名词解释模块**：自动检测本期出现的金融术语，底部附白话说明（RSI/对冲/回撤等）
- **全面白话化**：所有 prompt 要求用普通人能听懂的语言输出，禁止 EV/EBITDA 等缩写

---

## 常见问题

**Q: 只有 DeepSeek 没有 Claude 可以用吗？**
A: 可以。不配置 Claude 相关密钥时，基本面分析会展示原始财务数字，PM 裁决全部由 DeepSeek 完成，质量略低但可用。

**Q: 港股行情报错怎么办？**
A: 港股数据来自东方财富，已自动加入 `NO_PROXY` 直连。如仍有问题，检查本地代理配置。

**Q: 每天 Claude 消耗多少额度？**
A: 约 10-15 次 Sonnet 调用（基本面 × N 只非 ETF 股票 + PM 批量裁决）。使用 Claude Pro OAuth Token 时在 5 小时限额的 30-40% 以内。

**Q: 周报运行了多次，推荐结果不一样是正常的吗？**
A: 已通过 ISO 周锁定解决。同一周内第一次跑完后结果缓存，后续自动复用。如需重跑请加 `--force`。

---

## Contributing

欢迎 PR，特别是：
- A 股基本面数据接入（同花顺 / 东方财富财务接口）
- 更多技术指标（KDJ、OBV 等）
- 飞书卡片交互回调（点按钮直接记录操作）
- 支持更多推送渠道（微信、Telegram）

---

## License

MIT
