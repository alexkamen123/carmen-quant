# 自动调参测试框架

## 功能说明

这套框架基于真实市场数据快照（fixtures），对 `news_monitor._check_price_drop` 的核心逻辑进行参数扫描，帮助找到最优的 `threshold_pct` 值。

全程无需 API 调用，纯本地 fixture 驱动。

---

## 快速开始

### 第一步：捕获市场数据快照

```bash
cd ~/Projects/personal/finance-agent
source .venv/bin/activate
python scripts/capture_fixtures.py
```

- 读取 `config/portfolio.yaml` 中的所有持仓
- 对每个持仓调用 yfinance 拉取最近 5 天的 5min OHLCV
- 快照保存到 `tests/fixtures/price_snapshots/<ticker>_<date>.json`
- 同时生成 `manifest.json` 记录元信息

> 建议在美股/港股交易时段运行，数据更完整。

### 第二步：运行回测测试

```bash
pytest tests/test_backtest/ -v
```

预期输出：

- `test_baseline` — 验证默认参数 `threshold=3.0` 正常运行（无异常）
- `test_threshold_sweep` — 遍历 6 个阈值，输出各阈值平均 F1 并写入 `results.md`

### 第三步：查看参数对比报告

打开 `tests/test_backtest/results.md`，查看表格：

| 列名 | 说明 |
|------|------|
| `threshold_pct` | 参数值 |
| `ticker` | 标的代码 |
| `bars` | 快照 K 线总数 |
| `alerts` | 该参数下触发的告警次数 |
| `TP/FP/FN` | 真正例/假正例/假负例 |
| `precision` | 精确率 |
| `recall` | 召回率 |
| `F1` | 综合评分（越高越好） |

---

## 目录结构

```
tests/
├── fixtures/
│   └── price_snapshots/         ← 快照数据（被 .gitignore 排除）
│       ├── manifest.json
│       ├── NVDA_2026-05-16.json
│       └── ...
└── test_backtest/
    ├── __init__.py
    ├── conftest.py              ← pytest fixtures
    ├── test_parameter_sweep.py  ← 参数扫描测试
    ├── results.md               ← 扫描结果（自动追加，可提交）
    └── README.md                ← 本文件
```

---

## 评估方法说明

- **ground_truth 定义**：某根 K 线的 1 小时跌幅 ≥ 2.0% 被视为真实异动
- **扫描阈值**：`[1.5, 2.0, 2.5, 3.0, 3.5, 4.0]` %
- **窗口大小**：12 根 5min K ≈ 1 小时（与 `_check_price_drop` 生产代码一致）
- **去重**：触发告警后跳过同长度窗口，避免重复计数（与生产 5min dedup 对齐）

---

## 注意事项

- 快照文件（`tests/fixtures/price_snapshots/*.json`）已加入 `.gitignore`，不提交
- `results.md` 会追加写入，可通过 git 追踪历史参数效果变化
- 如果没有 fixture 数据，测试会自动 `skip` 并提示运行 `capture_fixtures.py`
- 港股 ticker 在 `portfolio.yaml` 中格式为 `00700`，脚本自动转换为 yfinance 格式 `700.HK`
