# 卡门个人量化交易助手 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 `subagent-driven-development`（推荐）逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。

**目标：** 构建一个每天 9:00 自动运行、分析持仓股技术信号和新闻、通过 Bull/Bear 辩论生成操作建议并推送飞书的个人量化决策助手。

**架构：** AkShare/YFinance 拉取实时数据 → pandas-ta 计算技术指标 → LangGraph 编排 Bull/Bear/PM 三层 Agent（DeepSeek 做分析、Claude 做最终裁决）→ SQLite 记录历史信号和胜率 → 飞书 Webhook 推送日报。

**技术栈：** Python 3.11+, LangGraph 0.2+, anthropic SDK, openai SDK（接 DeepSeek）, akshare, yfinance, pandas-ta, pydantic v2, aiosqlite, httpx, GitHub Actions

---

## 文件结构

```
~/Projects/personal/finance-agent/
├── pyproject.toml
├── .env.example
├── .env                          # 本地密钥（gitignore）
├── config/
│   ├── portfolio.yaml            # 持仓 + 自选股配置
│   └── settings.yaml             # 模型、阈值、通知配置
├── src/
│   └── finance_agent/
│       ├── __init__.py
│       ├── data/
│       │   ├── base.py           # 抽象 DataProvider
│       │   ├── akshare_provider.py  # A股/港股
│       │   ├── yfinance_provider.py # 美股
│       │   └── router.py         # 按市场路由到对应 provider
│       ├── signals/
│       │   ├── models.py         # Pydantic 信号模型
│       │   └── technical.py      # RSI/MA/MACD/Bollinger 计算
│       ├── agents/
│       │   ├── prompts.py        # 所有 prompt 模板（中文）
│       │   ├── bull_agent.py     # 多方视角 (DeepSeek)
│       │   ├── bear_agent.py     # 空方视角 (DeepSeek)
│       │   └── portfolio_manager.py # 最终裁决 (Claude)
│       ├── graph/
│       │   ├── state.py          # LangGraph AgentState (Pydantic)
│       │   └── workflow.py       # 完整 LangGraph 图定义
│       ├── storage/
│       │   ├── db.py             # SQLite 读写
│       │   └── schema.sql        # 建表 SQL
│       ├── notifications/
│       │   └── feishu.py         # 飞书 Webhook 推送
│       ├── backtest/
│       │   └── engine.py         # 胜率回溯统计
│       └── main.py               # CLI 入口
├── tests/
│   ├── conftest.py
│   ├── test_data/
│   │   └── test_router.py
│   ├── test_signals/
│   │   └── test_technical.py
│   ├── test_agents/
│   │   └── test_debate.py
│   └── test_notifications/
│       └── test_feishu.py
└── .github/
    └── workflows/
        └── daily_analysis.yml
```

---

## 任务 1：项目脚手架

**文件：**
- 创建：`pyproject.toml`
- 创建：`.env.example`
- 创建：`config/portfolio.yaml`
- 创建：`config/settings.yaml`
- 创建：`src/finance_agent/__init__.py`

- [ ] **步骤 1：创建 pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "finance-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "langgraph>=0.2",
    "langchain-anthropic>=0.2",
    "langchain-openai>=0.2",
    "anthropic>=0.30",
    "openai>=1.40",
    "akshare>=1.14",
    "yfinance>=0.2.40",
    "pandas-ta>=0.3.14b",
    "pandas>=2.1",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "aiosqlite>=0.20",
    "httpx>=0.27",
    "python-dotenv>=1.0",
    "pyyaml>=6.0",
    "rich>=13.0",          # 终端彩色输出
    "typer>=0.12",         # CLI
]

[project.scripts]
finance-agent = "finance_agent.main:app"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.14",
    "respx>=0.21",         # mock httpx
]
```

- [ ] **步骤 2：安装依赖**

```bash
cd ~/Projects/personal/finance-agent
pip install -e ".[dev]"
```

- [ ] **步骤 3：创建 .env.example**

```bash
# API Keys
ANTHROPIC_API_KEY=sk-ant-...
DEEPSEEK_API_KEY=sk-...

# 飞书 Webhook（在飞书群里添加自定义机器人获得）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...

# 可选：Tushare（付费，数据质量更高）
TUSHARE_TOKEN=
```

- [ ] **步骤 4：创建 config/portfolio.yaml**（根据卡门家庭实际持仓）

```yaml
# 卡门家庭持仓配置
# market: us | hk | cn
holdings:
  - ticker: NVDA
    market: us
    shares: 2
    cost_basis: null      # 填入买入均价后可计算盈亏
    notes: "AI算力核心持仓"

  - ticker: AAPL
    market: us
    shares: 1
    cost_basis: null

  - ticker: GOOGL
    market: us
    shares: 1
    cost_basis: null

  - ticker: QQQM
    market: us
    shares: 1
    cost_basis: null
    is_dca: true          # 定投标的，不做短期操作建议

  - ticker: VOO
    market: us
    shares: 0             # 盈立自动定投，市值持续增加
    is_dca: true

  - ticker: "00700"
    market: hk
    shares: 3
    cost_basis: null
    notes: "腾讯控股"

  - ticker: "02513"
    market: hk
    shares: 1
    cost_basis: null
    notes: "智谱AI"

# 自选观察池（不持有，但每天跟踪）
watchlist:
  - ticker: MU
    market: us
    notes: "美光科技，AI存储，考虑建仓"

  - ticker: TSM
    market: us
    notes: "台积电，AI算力上游"
```

- [ ] **步骤 5：创建 config/settings.yaml**

```yaml
models:
  # DeepSeek：日常信号分析（便宜）
  analysis:
    provider: deepseek
    model: deepseek-chat        # DeepSeek V3
    temperature: 0.3
    max_tokens: 1000

  # Claude：最终投资决策裁决（复杂推理）
  decision:
    provider: anthropic
    model: claude-sonnet-4-5
    temperature: 0.2
    max_tokens: 1500

signals:
  rsi:
    overbought: 70
    oversold: 30
    lookback: 14

  ma:
    short: 5
    medium: 20
    long: 60

  # 信号置信度阈值（高于此值才生成操作建议）
  min_confidence: 0.55

notifications:
  feishu:
    enabled: true
    # 每日发送时间（用于本地测试，GitHub Actions 用 cron 控制）
    send_hour: 9

backtest:
  # 回溯天数（用于胜率统计）
  lookback_days: 30
  # 次日涨幅超过此值算"信号正确"
  win_threshold_pct: 1.0
```

- [ ] **步骤 6：创建 src/finance_agent/__init__.py**

```python
"""卡门家庭个人量化交易助手"""
__version__ = "0.1.0"
```

- [ ] **步骤 7：Commit**

```bash
git init
echo ".env" >> .gitignore
echo "__pycache__/" >> .gitignore
echo "*.egg-info/" >> .gitignore
echo ".pytest_cache/" >> .gitignore
git add .
git commit -m "feat: project scaffold with config files"
```

---

## 任务 2：数据层

**文件：**
- 创建：`src/finance_agent/data/base.py`
- 创建：`src/finance_agent/data/yfinance_provider.py`
- 创建：`src/finance_agent/data/akshare_provider.py`
- 创建：`src/finance_agent/data/router.py`
- 创建：`tests/test_data/test_router.py`

- [ ] **步骤 1：写失败测试**

```python
# tests/test_data/test_router.py
import pytest
from finance_agent.data.router import DataRouter

def test_router_routes_us_to_yfinance():
    router = DataRouter()
    provider = router.get_provider("NVDA", "us")
    assert provider.__class__.__name__ == "YFinanceProvider"

def test_router_routes_hk_to_akshare():
    router = DataRouter()
    provider = router.get_provider("00700", "hk")
    assert provider.__class__.__name__ == "AkShareProvider"

@pytest.mark.asyncio
async def test_yfinance_returns_dataframe():
    """需要网络，标记为 integration"""
    router = DataRouter()
    df = await router.fetch_ohlcv("NVDA", "us", days=5)
    assert len(df) > 0
    assert "close" in df.columns
```

- [ ] **步骤 2：运行测试确认失败**

```bash
pytest tests/test_data/test_router.py -v
# 预期：ImportError: No module named 'finance_agent.data.router'
```

- [ ] **步骤 3：实现 base.py**

```python
# src/finance_agent/data/base.py
from abc import ABC, abstractmethod
import pandas as pd

class DataProvider(ABC):
    """所有数据源的抽象接口"""

    @abstractmethod
    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        """
        返回 OHLCV DataFrame，列名统一为：
        open, high, low, close, volume，index 为 DatetimeIndex
        """
        ...

    @abstractmethod
    async def fetch_news(self, ticker: str, limit: int = 5) -> list[dict]:
        """
        返回最近新闻列表，每条：
        {"title": str, "summary": str, "published": str}
        """
        ...
```

- [ ] **步骤 4：实现 yfinance_provider.py**

```python
# src/finance_agent/data/yfinance_provider.py
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
from .base import DataProvider

class YFinanceProvider(DataProvider):
    """美股数据，使用 yfinance（免费）"""

    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        end = datetime.today()
        start = end - timedelta(days=days + 10)  # 多取几天以防节假日

        # yfinance 是同步库，用线程池避免阻塞
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(
            None,
            lambda: yf.download(ticker, start=start, end=end, progress=False)
        )

        if df.empty:
            raise ValueError(f"No data returned for {ticker}")

        # 统一列名为小写
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df.tail(days)

    async def fetch_news(self, ticker: str, limit: int = 5) -> list[dict]:
        loop = asyncio.get_event_loop()
        stock = await loop.run_in_executor(None, lambda: yf.Ticker(ticker))
        news_raw = stock.news or []

        result = []
        for item in news_raw[:limit]:
            result.append({
                "title": item.get("title", ""),
                "summary": item.get("summary", item.get("title", "")),
                "published": str(item.get("providerPublishTime", "")),
            })
        return result
```

- [ ] **步骤 5：实现 akshare_provider.py**

```python
# src/finance_agent/data/akshare_provider.py
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import akshare as ak
from .base import DataProvider

class AkShareProvider(DataProvider):
    """港股/A股数据，使用 akshare（免费）"""

    async def fetch_ohlcv(self, ticker: str, days: int = 60) -> pd.DataFrame:
        end = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=days + 10)).strftime("%Y%m%d")

        loop = asyncio.get_event_loop()

        # 港股：ticker 格式 "00700"
        if len(ticker) <= 5 and ticker.isdigit():
            df = await loop.run_in_executor(
                None,
                lambda: ak.stock_hk_hist(
                    symbol=ticker, period="daily",
                    start_date=start, end_date=end, adjust="qfq"
                )
            )
            # akshare 港股列名映射
            rename = {"日期": "date", "开盘": "open", "最高": "high",
                      "最低": "low", "收盘": "close", "成交量": "volume"}
        else:
            # A股：ticker 格式 "600519"
            df = await loop.run_in_executor(
                None,
                lambda: ak.stock_zh_a_hist(
                    symbol=ticker, period="daily",
                    start_date=start, end_date=end, adjust="qfq"
                )
            )
            rename = {"日期": "date", "开盘": "open", "最高": "high",
                      "最低": "low", "收盘": "close", "成交量": "volume"}

        df = df.rename(columns=rename)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df[["open", "high", "low", "close", "volume"]].tail(days)

    async def fetch_news(self, ticker: str, limit: int = 5) -> list[dict]:
        # akshare 新闻接口（港股暂用个股公告）
        loop = asyncio.get_event_loop()
        try:
            df = await loop.run_in_executor(
                None,
                lambda: ak.stock_hk_news(symbol=ticker)
            )
            result = []
            for _, row in df.head(limit).iterrows():
                result.append({
                    "title": str(row.get("标题", "")),
                    "summary": str(row.get("内容", row.get("标题", ""))),
                    "published": str(row.get("时间", "")),
                })
            return result
        except Exception:
            return []  # 新闻获取失败不影响主流程
```

- [ ] **步骤 6：实现 router.py**

```python
# src/finance_agent/data/router.py
import pandas as pd
from .base import DataProvider
from .yfinance_provider import YFinanceProvider
from .akshare_provider import AkShareProvider

class DataRouter:
    """根据市场类型路由到对应数据源"""

    def __init__(self):
        self._providers: dict[str, DataProvider] = {
            "us": YFinanceProvider(),
            "hk": AkShareProvider(),
            "cn": AkShareProvider(),
        }

    def get_provider(self, ticker: str, market: str) -> DataProvider:
        if market not in self._providers:
            raise ValueError(f"Unsupported market: {market}. Use: us, hk, cn")
        return self._providers[market]

    async def fetch_ohlcv(self, ticker: str, market: str, days: int = 60) -> pd.DataFrame:
        provider = self.get_provider(ticker, market)
        return await provider.fetch_ohlcv(ticker, days)

    async def fetch_news(self, ticker: str, market: str, limit: int = 5) -> list[dict]:
        provider = self.get_provider(ticker, market)
        return await provider.fetch_news(ticker, limit)
```

- [ ] **步骤 7：运行测试**

```bash
pytest tests/test_data/test_router.py::test_router_routes_us_to_yfinance \
       tests/test_data/test_router.py::test_router_routes_hk_to_akshare -v
# 预期：PASS（不需网络）
```

- [ ] **步骤 8：Commit**

```bash
git add src/finance_agent/data/ tests/test_data/
git commit -m "feat: data providers for US/HK/CN stocks"
```

---

## 任务 3：技术信号计算

**文件：**
- 创建：`src/finance_agent/signals/models.py`
- 创建：`src/finance_agent/signals/technical.py`
- 创建：`tests/test_signals/test_technical.py`

- [ ] **步骤 1：写失败测试**

```python
# tests/test_signals/test_technical.py
import pandas as pd
import numpy as np
import pytest
from finance_agent.signals.technical import calculate_signals
from finance_agent.signals.models import TechnicalSignals

def make_fake_ohlcv(n=60) -> pd.DataFrame:
    """生成假价格数据用于测试"""
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = np.cumsum(np.random.randn(n)) + 100
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": np.random.randint(1_000_000, 10_000_000, n),
    }, index=dates)

def test_returns_technical_signals_model():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert isinstance(result, TechnicalSignals)

def test_rsi_in_valid_range():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert 0 <= result.rsi <= 100

def test_ma_cross_signal_is_valid():
    df = make_fake_ohlcv(60)
    result = calculate_signals(df)
    assert result.ma_signal in ("golden_cross", "death_cross", "neutral")

def test_needs_at_least_30_rows():
    df = make_fake_ohlcv(10)
    with pytest.raises(ValueError, match="至少需要 30 条"):
        calculate_signals(df)
```

- [ ] **步骤 2：运行确认失败**

```bash
pytest tests/test_signals/test_technical.py -v
# 预期：ImportError
```

- [ ] **步骤 3：实现 signals/models.py**

```python
# src/finance_agent/signals/models.py
from pydantic import BaseModel
from typing import Literal

class TechnicalSignals(BaseModel):
    ticker: str = ""
    close: float
    change_pct: float           # 当日涨跌幅 %

    # RSI
    rsi: float                  # 0-100
    rsi_signal: Literal["overbought", "oversold", "neutral"]

    # Moving Averages
    ma5: float
    ma20: float
    ma60: float
    ma_signal: Literal["golden_cross", "death_cross", "neutral"]
    price_vs_ma20: float        # (close - ma20) / ma20 * 100，百分比

    # MACD
    macd: float
    macd_signal: float
    macd_hist: float            # 柱状图
    macd_trend: Literal["bullish", "bearish", "neutral"]

    # Bollinger Bands
    bb_upper: float
    bb_lower: float
    bb_position: float          # 0=下轨, 1=上轨，当前位置

    # Volume
    volume_ratio: float         # 当日成交量 / 20日均量

    # 综合评分（-1 到 1，正值偏多，负值偏空）
    composite_score: float

    def to_prompt_str(self) -> str:
        """格式化为 Agent Prompt 可读字符串"""
        return f"""
技术指标摘要（{self.ticker}）：
- 当前价格：{self.close:.2f}，今日涨跌：{self.change_pct:+.2f}%
- RSI({14}): {self.rsi:.1f} → {self.rsi_signal}
- MA5={self.ma5:.2f} / MA20={self.ma20:.2f} / MA60={self.ma60:.2f}
  均线信号：{self.ma_signal}，价格相对MA20: {self.price_vs_ma20:+.1f}%
- MACD趋势：{self.macd_trend}（MACD={self.macd:.3f}, Signal={self.macd_signal:.3f}）
- 布林带位置：{self.bb_position:.0%}（0%=下轨,100%=上轨）
- 成交量比率：{self.volume_ratio:.1f}x（>1.5 为放量）
- 综合量化评分：{self.composite_score:+.2f}（+正偏多，-负偏空）
""".strip()
```

- [ ] **步骤 4：实现 signals/technical.py**

```python
# src/finance_agent/signals/technical.py
import pandas as pd
import pandas_ta as ta
from .models import TechnicalSignals

def calculate_signals(df: pd.DataFrame, ticker: str = "") -> TechnicalSignals:
    """
    输入：60日 OHLCV DataFrame（列名：open/high/low/close/volume）
    输出：TechnicalSignals Pydantic 模型
    """
    if len(df) < 30:
        raise ValueError(f"至少需要 30 条数据，当前只有 {len(df)} 条")

    close = df["close"]
    volume = df["volume"]

    # RSI
    rsi_series = ta.rsi(close, length=14)
    rsi = float(rsi_series.iloc[-1])
    if rsi >= 70:
        rsi_signal = "overbought"
    elif rsi <= 30:
        rsi_signal = "oversold"
    else:
        rsi_signal = "neutral"

    # Moving Averages
    ma5  = float(ta.sma(close, length=5).iloc[-1])
    ma20 = float(ta.sma(close, length=20).iloc[-1])
    ma60 = float(ta.sma(close, length=min(60, len(df))).iloc[-1])
    current_close = float(close.iloc[-1])

    prev_ma5  = float(ta.sma(close, length=5).iloc[-2])
    prev_ma20 = float(ta.sma(close, length=20).iloc[-2])
    if prev_ma5 <= prev_ma20 and ma5 > ma20:
        ma_signal = "golden_cross"
    elif prev_ma5 >= prev_ma20 and ma5 < ma20:
        ma_signal = "death_cross"
    else:
        ma_signal = "neutral"

    price_vs_ma20 = (current_close - ma20) / ma20 * 100

    # MACD
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    macd_val    = float(macd_df.iloc[-1, 0])   # MACD_12_26_9
    macd_sig    = float(macd_df.iloc[-1, 1])   # MACDs_12_26_9
    macd_hist   = float(macd_df.iloc[-1, 2])   # MACDh_12_26_9
    prev_hist   = float(macd_df.iloc[-2, 2])
    if macd_hist > 0 and macd_hist > prev_hist:
        macd_trend = "bullish"
    elif macd_hist < 0 and macd_hist < prev_hist:
        macd_trend = "bearish"
    else:
        macd_trend = "neutral"

    # Bollinger Bands
    bb = ta.bbands(close, length=20, std=2)
    bb_upper = float(bb.iloc[-1, 0])  # BBU
    bb_lower = float(bb.iloc[-1, 2])  # BBL
    bb_range = bb_upper - bb_lower
    bb_position = (current_close - bb_lower) / bb_range if bb_range > 0 else 0.5

    # Volume ratio
    vol_ma20 = float(ta.sma(volume.astype(float), length=20).iloc[-1])
    volume_ratio = float(volume.iloc[-1]) / vol_ma20 if vol_ma20 > 0 else 1.0

    # Change pct
    prev_close = float(close.iloc[-2]) if len(close) > 1 else current_close
    change_pct = (current_close - prev_close) / prev_close * 100

    # Composite score (-1 to 1)
    score = 0.0
    score += 0.3 * (1 - rsi / 100) if rsi_signal == "oversold" else \
             -0.3 * (rsi / 100) if rsi_signal == "overbought" else 0
    score += 0.25 if ma_signal == "golden_cross" else \
             -0.25 if ma_signal == "death_cross" else 0
    score += 0.2 if macd_trend == "bullish" else \
             -0.2 if macd_trend == "bearish" else 0
    score += 0.15 * (1 - bb_position * 2)  # 下轨附近加分，上轨附近减分
    score += 0.1 * min(volume_ratio - 1, 1) if change_pct > 0 else 0
    score = max(-1.0, min(1.0, score))

    return TechnicalSignals(
        ticker=ticker,
        close=current_close,
        change_pct=change_pct,
        rsi=rsi,
        rsi_signal=rsi_signal,
        ma5=ma5, ma20=ma20, ma60=ma60,
        ma_signal=ma_signal,
        price_vs_ma20=price_vs_ma20,
        macd=macd_val, macd_signal=macd_sig, macd_hist=macd_hist,
        macd_trend=macd_trend,
        bb_upper=bb_upper, bb_lower=bb_lower, bb_position=bb_position,
        volume_ratio=volume_ratio,
        composite_score=score,
    )
```

- [ ] **步骤 5：运行测试**

```bash
pytest tests/test_signals/test_technical.py -v
# 预期：4 passed
```

- [ ] **步骤 6：Commit**

```bash
git add src/finance_agent/signals/ tests/test_signals/
git commit -m "feat: technical signal calculation (RSI/MA/MACD/Bollinger)"
```

---

## 任务 4：LangGraph 状态模型

**文件：**
- 创建：`src/finance_agent/graph/state.py`

- [ ] **步骤 1：实现 state.py**

```python
# src/finance_agent/graph/state.py
from __future__ import annotations
from typing import Annotated
from pydantic import BaseModel, Field
from langgraph.graph import add_messages
from finance_agent.signals.models import TechnicalSignals

class NewsItem(BaseModel):
    title: str
    summary: str
    published: str

class StockAnalysis(BaseModel):
    """单只股票的完整分析结果（Pydantic 确保 Agent 间信息无衰减）"""
    ticker: str
    market: str
    signals: TechnicalSignals
    news: list[NewsItem] = Field(default_factory=list)

    # Agent 输出（由 LangGraph 节点填充）
    bull_thesis: str = ""       # 多方论点（DeepSeek）
    bear_thesis: str = ""       # 空方论点（DeepSeek）

    # 最终裁决（Claude）
    recommendation: str = ""    # "买入" | "持有" | "减仓" | "观望" | "卖出"
    confidence: str = ""        # "高" | "中" | "低"
    entry_hint: str = ""        # 进场/止损建议
    key_risk: str = ""          # 最大风险点
    one_line: str = ""          # 一句话结论（飞书卡片用）

class AgentState(BaseModel):
    """LangGraph 全局状态，贯穿整个工作流"""
    date: str = ""
    stocks: list[StockAnalysis] = Field(default_factory=list)
    # 当前正在处理的 ticker 索引（用于节点间传递进度）
    current_index: int = 0
    # 最终报告文本（发飞书用）
    report_text: str = ""
    # 错误记录
    errors: list[str] = Field(default_factory=list)
```

- [ ] **步骤 2：Commit**

```bash
git add src/finance_agent/graph/state.py
git commit -m "feat: LangGraph state models with Pydantic"
```

---

## 任务 5：Bull/Bear/PM Agent Prompts 和逻辑

**文件：**
- 创建：`src/finance_agent/agents/prompts.py`
- 创建：`src/finance_agent/agents/bull_agent.py`
- 创建：`src/finance_agent/agents/bear_agent.py`
- 创建：`src/finance_agent/agents/portfolio_manager.py`
- 创建：`tests/test_agents/test_debate.py`

- [ ] **步骤 1：写失败测试**

```python
# tests/test_agents/test_debate.py
import pytest
from unittest.mock import AsyncMock, patch
from finance_agent.agents.bull_agent import run_bull_analysis
from finance_agent.agents.bear_agent import run_bear_analysis
from finance_agent.graph.state import StockAnalysis, NewsItem
from finance_agent.signals.models import TechnicalSignals

def make_mock_analysis() -> StockAnalysis:
    signals = TechnicalSignals(
        ticker="NVDA", close=850.0, change_pct=1.5,
        rsi=55.0, rsi_signal="neutral",
        ma5=840.0, ma20=820.0, ma60=780.0,
        ma_signal="neutral", price_vs_ma20=3.7,
        macd=5.2, macd_signal=4.1, macd_hist=1.1,
        macd_trend="bullish",
        bb_upper=900.0, bb_lower=760.0, bb_position=0.6,
        volume_ratio=1.2,
        composite_score=0.3,
    )
    return StockAnalysis(
        ticker="NVDA", market="us",
        signals=signals,
        news=[NewsItem(title="NVDA beats earnings", summary="Strong Q2", published="2026-05-01")],
    )

@pytest.mark.asyncio
async def test_bull_returns_non_empty_thesis():
    analysis = make_mock_analysis()
    with patch("finance_agent.agents.bull_agent.deepseek_chat", new_callable=AsyncMock) as mock:
        mock.return_value = "1. AI需求强劲 2. 技术面向好 3. 财报超预期"
        result = await run_bull_analysis(analysis)
    assert len(result.bull_thesis) > 10

@pytest.mark.asyncio
async def test_bear_returns_non_empty_thesis():
    analysis = make_mock_analysis()
    with patch("finance_agent.agents.bear_agent.deepseek_chat", new_callable=AsyncMock) as mock:
        mock.return_value = "1. 估值偏高 2. RSI接近超买 3. 竞争加剧"
        result = await run_bear_analysis(analysis)
    assert len(result.bear_thesis) > 10
```

- [ ] **步骤 2：运行确认失败**

```bash
pytest tests/test_agents/test_debate.py -v
# 预期：ImportError
```

- [ ] **步骤 3：实现 agents/prompts.py**

```python
# src/finance_agent/agents/prompts.py

BULL_SYSTEM = """你是一位专注寻找买入机会的资深股票分析师。
你的任务是：基于提供的技术指标和新闻，给出 3 条支持持有或买入该股票的具体理由。
要求：简洁，每条不超过 30 字，基于数据说话，不要无根据的乐观。"""

BULL_USER = """请分析以下股票，给出 3 条多头理由：

{signals_str}

最近新闻摘要：
{news_str}

请直接给出编号列表（1. 2. 3.），不需要其他说明。"""

BEAR_SYSTEM = """你是一位专注风险识别的资深股票分析师。
你的任务是：基于提供的技术指标和新闻，给出 3 条应该谨慎或卖出该股票的具体理由。
要求：简洁，每条不超过 30 字，必须找到真实风险点，不允许无根据的悲观。"""

BEAR_USER = """请分析以下股票，给出 3 条空头/风险理由：

{signals_str}

最近新闻摘要：
{news_str}

请直接给出编号列表（1. 2. 3.），不需要其他说明。"""

PM_SYSTEM = """你是一位家庭财富管理顾问，帮助一位普通投资者管理港股和美股持仓。
你已经看到了多空双方的分析，现在需要给出最终操作建议。

输出格式（严格按此 JSON 输出，不要有其他内容）：
{
  "recommendation": "买入" | "持有" | "减仓" | "观望" | "卖出",
  "confidence": "高" | "中" | "低",
  "entry_hint": "具体的进场价位或止损建议（一句话）",
  "key_risk": "最需要警惕的一个风险点（一句话）",
  "one_line": "给非专业投资者的一句话总结"
}"""

PM_USER = """股票：{ticker}（{market}市场）

【技术面】
{signals_str}

【多头观点】
{bull_thesis}

【空头观点】
{bear_thesis}

请综合判断，给出最终建议。"""
```

- [ ] **步骤 4：实现 agents/bull_agent.py 和 bear_agent.py**

```python
# src/finance_agent/agents/bull_agent.py
from openai import AsyncOpenAI
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import BULL_SYSTEM, BULL_USER
import os

async def deepseek_chat(system: str, user: str) -> str:
    """调用 DeepSeek API（OpenAI 兼容接口）"""
    client = AsyncOpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    response = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()

async def run_bull_analysis(analysis: StockAnalysis) -> StockAnalysis:
    news_str = "\n".join(
        f"- {n.title}" for n in analysis.news
    ) or "暂无新闻"

    user_msg = BULL_USER.format(
        signals_str=analysis.signals.to_prompt_str(),
        news_str=news_str,
    )
    thesis = await deepseek_chat(BULL_SYSTEM, user_msg)
    # 返回更新后的对象（Pydantic immutable，用 model_copy）
    return analysis.model_copy(update={"bull_thesis": thesis})
```

```python
# src/finance_agent/agents/bear_agent.py
from finance_agent.agents.bull_agent import deepseek_chat  # 复用同一函数
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import BEAR_SYSTEM, BEAR_USER

async def run_bear_analysis(analysis: StockAnalysis) -> StockAnalysis:
    news_str = "\n".join(
        f"- {n.title}" for n in analysis.news
    ) or "暂无新闻"

    user_msg = BEAR_USER.format(
        signals_str=analysis.signals.to_prompt_str(),
        news_str=news_str,
    )
    thesis = await deepseek_chat(BEAR_SYSTEM, user_msg)
    return analysis.model_copy(update={"bear_thesis": thesis})
```

- [ ] **步骤 5：实现 agents/portfolio_manager.py**

```python
# src/finance_agent/agents/portfolio_manager.py
import json
import anthropic
import os
from finance_agent.graph.state import StockAnalysis
from finance_agent.agents.prompts import PM_SYSTEM, PM_USER

MARKET_LABEL = {"us": "美股", "hk": "港股", "cn": "A股"}

async def run_portfolio_manager(analysis: StockAnalysis) -> StockAnalysis:
    """使用 Claude 做最终裁决（复杂推理）"""
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_msg = PM_USER.format(
        ticker=analysis.ticker,
        market=MARKET_LABEL.get(analysis.market, analysis.market),
        signals_str=analysis.signals.to_prompt_str(),
        bull_thesis=analysis.bull_thesis,
        bear_thesis=analysis.bear_thesis,
    )

    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=PM_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = message.content[0].text.strip()
    # 提取 JSON（Claude 有时会在 JSON 前后加说明文字）
    start = raw.find("{")
    end = raw.rfind("}") + 1
    decision = json.loads(raw[start:end])

    return analysis.model_copy(update={
        "recommendation": decision.get("recommendation", "观望"),
        "confidence":     decision.get("confidence", "低"),
        "entry_hint":     decision.get("entry_hint", ""),
        "key_risk":       decision.get("key_risk", ""),
        "one_line":       decision.get("one_line", ""),
    })
```

- [ ] **步骤 6：运行测试（mock 模式）**

```bash
pytest tests/test_agents/test_debate.py -v
# 预期：2 passed
```

- [ ] **步骤 7：Commit**

```bash
git add src/finance_agent/agents/ tests/test_agents/
git commit -m "feat: Bull/Bear/PM three-layer agent debate"
```

---

## 任务 6：LangGraph 工作流

**文件：**
- 创建：`src/finance_agent/graph/workflow.py`

- [ ] **步骤 1：实现 workflow.py**

```python
# src/finance_agent/graph/workflow.py
import asyncio
from datetime import datetime
import yaml
from pathlib import Path
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from finance_agent.graph.state import AgentState, StockAnalysis, NewsItem
from finance_agent.data.router import DataRouter
from finance_agent.signals.technical import calculate_signals
from finance_agent.agents.bull_agent import run_bull_analysis
from finance_agent.agents.bear_agent import run_bear_analysis
from finance_agent.agents.portfolio_manager import run_portfolio_manager

router = DataRouter()

# ─── 节点函数 ────────────────────────────────────────────

async def fetch_data_node(state: AgentState) -> AgentState:
    """并行拉取所有持仓的行情数据"""
    config_path = Path(__file__).parents[3] / "config" / "portfolio.yaml"
    with open(config_path) as f:
        portfolio = yaml.safe_load(f)

    all_holdings = portfolio.get("holdings", []) + portfolio.get("watchlist", [])

    async def fetch_one(item: dict) -> StockAnalysis | None:
        ticker = item["ticker"]
        market = item["market"]
        try:
            df = await router.fetch_ohlcv(ticker, market, days=60)
            signals = calculate_signals(df, ticker=ticker)
            news_raw = await router.fetch_news(ticker, market, limit=3)
            news = [NewsItem(**n) for n in news_raw]
            return StockAnalysis(ticker=ticker, market=market, signals=signals, news=news)
        except Exception as e:
            state.errors.append(f"{ticker}: {e}")
            return None

    results = await asyncio.gather(*[fetch_one(h) for h in all_holdings])
    stocks = [r for r in results if r is not None]
    return state.model_copy(update={"stocks": stocks, "date": datetime.today().strftime("%Y-%m-%d")})

async def debate_node(state: AgentState) -> AgentState:
    """对每只股票依次运行 Bull/Bear 辩论（串行避免 API 限速）"""
    updated_stocks = []
    for analysis in state.stocks:
        # 定投标的跳过辩论，直接标记为 "按计划定投"
        # （从 portfolio.yaml 中读取 is_dca，此处简化为 ETF 判断）
        if analysis.ticker in ("QQQM", "VOO"):
            updated = analysis.model_copy(update={
                "bull_thesis": "定投标的，按月计划执行",
                "bear_thesis": "定投标的，不做短期判断",
            })
        else:
            bull_result = await run_bull_analysis(analysis)
            updated = await run_bear_analysis(bull_result)
        updated_stocks.append(updated)
    return state.model_copy(update={"stocks": updated_stocks})

async def decision_node(state: AgentState) -> AgentState:
    """Portfolio Manager 对每只股票做最终裁决"""
    updated_stocks = []
    for analysis in state.stocks:
        if analysis.ticker in ("QQQM", "VOO"):
            updated = analysis.model_copy(update={
                "recommendation": "按计划定投",
                "confidence": "高",
                "one_line": f"{analysis.ticker} 按月定投计划执行，无需额外操作",
            })
        else:
            updated = await run_portfolio_manager(analysis)
        updated_stocks.append(updated)
    return state.model_copy(update={"stocks": updated_stocks})

async def format_report_node(state: AgentState) -> AgentState:
    """生成飞书消息文本"""
    EMOJI = {"买入": "🟢", "持有": "🟡", "观望": "🟡",
              "减仓": "🟠", "卖出": "🔴", "按计划定投": "⬜"}
    CONF  = {"高": "★★★", "中": "★★☆", "低": "★☆☆"}

    lines = [
        f"📊 卡门持仓日报 · {state.date}",
        "",
        "━━━━ 今日操作建议 ━━━━",
    ]
    for s in state.stocks:
        emoji = EMOJI.get(s.recommendation, "⬜")
        conf  = CONF.get(s.confidence, "")
        lines.append(f"\n{emoji} {s.ticker}  {s.recommendation} {conf}")
        lines.append(f"   {s.one_line}")
        if s.entry_hint:
            lines.append(f"   📌 {s.entry_hint}")
        if s.key_risk:
            lines.append(f"   ⚠️  {s.key_risk}")

    if state.errors:
        lines.append(f"\n⚙️ 获取失败：{', '.join(state.errors)}")

    lines.append("\n以上仅供参考，操作前请自行判断")
    return state.model_copy(update={"report_text": "\n".join(lines)})

# ─── 构建图 ──────────────────────────────────────────────

def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("fetch_data", fetch_data_node)
    builder.add_node("debate",     debate_node)
    builder.add_node("decision",   decision_node)
    builder.add_node("format",     format_report_node)

    builder.set_entry_point("fetch_data")
    builder.add_edge("fetch_data", "debate")
    builder.add_edge("debate",     "decision")
    builder.add_edge("decision",   "format")
    builder.add_edge("format",      END)

    return builder.compile(checkpointer=checkpointer)

async def run_workflow(db_path: str = "data/agent.db") -> AgentState:
    """带 SQLite checkpoint 的完整运行"""
    async with AsyncSqliteSaver.from_conn_string(db_path) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "daily_run"}}
        final_state = await graph.ainvoke(AgentState(), config=config)
    return final_state
```

- [ ] **步骤 2：Commit**

```bash
git add src/finance_agent/graph/workflow.py
git commit -m "feat: LangGraph workflow with SQLite checkpoint"
```

---

## 任务 7：SQLite 存储（历史信号 + 胜率）

**文件：**
- 创建：`src/finance_agent/storage/schema.sql`
- 创建：`src/finance_agent/storage/db.py`
- 创建：`src/finance_agent/backtest/engine.py`

- [ ] **步骤 1：实现 schema.sql**

```sql
-- src/finance_agent/storage/schema.sql
CREATE TABLE IF NOT EXISTS daily_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    market          TEXT NOT NULL,
    close_price     REAL,
    composite_score REAL,
    recommendation  TEXT,
    confidence      TEXT,
    one_line        TEXT,
    -- 次日收盘价（由 backtest engine 在次日回填）
    next_day_close  REAL,
    next_day_change_pct REAL,
    -- 信号是否正确（recommendation 为买入/持有且次日上涨 > 1%，或减仓/卖出且次日下跌 > 1%）
    signal_correct  INTEGER,  -- 1=正确, 0=错误, NULL=未回填
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_date_ticker ON daily_signals(date, ticker);

CREATE TABLE IF NOT EXISTS win_rate_stats (
    ticker          TEXT PRIMARY KEY,
    total_signals   INTEGER DEFAULT 0,
    correct_signals INTEGER DEFAULT 0,
    win_rate        REAL DEFAULT 0.0,
    last_updated    TEXT
);
```

- [ ] **步骤 2：实现 storage/db.py**

```python
# src/finance_agent/storage/db.py
import aiosqlite
from pathlib import Path
from finance_agent.graph.state import AgentState

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_PATH.read_text())
        await db.commit()

async def save_daily_signals(state: AgentState, db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        for s in state.stocks:
            await db.execute("""
                INSERT INTO daily_signals
                    (date, ticker, market, close_price, composite_score,
                     recommendation, confidence, one_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.date, s.ticker, s.market,
                s.signals.close, s.signals.composite_score,
                s.recommendation, s.confidence, s.one_line,
            ))
        await db.commit()

async def get_signal_history(ticker: str, db_path: str, days: int = 30) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM daily_signals
            WHERE ticker = ?
            ORDER BY date DESC LIMIT ?
        """, (ticker, days)) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]
```

- [ ] **步骤 3：实现 backtest/engine.py（次日回填胜率）**

```python
# src/finance_agent/backtest/engine.py
"""
每天运行完毕后，此脚本回填昨天信号的实际结果，并更新胜率。
在 GitHub Actions 中，在每日分析之前先运行此脚本。
"""
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from finance_agent.data.router import DataRouter

router = DataRouter()

async def backfill_yesterday(db_path: str) -> dict[str, float]:
    """
    找出昨天的信号，获取今天收盘价，回填 next_day_close 和 signal_correct。
    返回：{ticker: win_rate} 字典
    """
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # 找出昨天未回填的信号
        async with db.execute("""
            SELECT id, ticker, market, close_price, recommendation
            FROM daily_signals
            WHERE date = ? AND next_day_close IS NULL
        """, (yesterday,)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        for row in rows:
            try:
                df = await router.fetch_ohlcv(row["ticker"], row["market"], days=3)
                today_close = float(df["close"].iloc[-1])
                prev_close  = row["close_price"]
                change_pct  = (today_close - prev_close) / prev_close * 100

                # 判断信号是否正确
                rec = row["recommendation"]
                if rec in ("买入",) and change_pct > 1.0:
                    correct = 1
                elif rec in ("减仓", "卖出") and change_pct < -1.0:
                    correct = 1
                elif rec in ("持有", "观望", "按计划定投"):
                    correct = None  # 不纳入胜率统计
                else:
                    correct = 0

                await db.execute("""
                    UPDATE daily_signals
                    SET next_day_close = ?, next_day_change_pct = ?, signal_correct = ?
                    WHERE id = ?
                """, (today_close, change_pct, correct, row["id"]))
            except Exception:
                continue

        await db.commit()

        # 重新计算胜率
        async with db.execute("""
            SELECT ticker,
                   COUNT(*) FILTER (WHERE signal_correct IS NOT NULL) as total,
                   SUM(signal_correct) as correct
            FROM daily_signals
            GROUP BY ticker
        """) as cur:
            stats = [dict(r) for r in await cur.fetchall()]

        for s in stats:
            total   = s["total"] or 0
            correct = int(s["correct"] or 0)
            rate    = correct / total if total > 0 else 0.0
            await db.execute("""
                INSERT INTO win_rate_stats (ticker, total_signals, correct_signals, win_rate, last_updated)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(ticker) DO UPDATE SET
                    total_signals   = excluded.total_signals,
                    correct_signals = excluded.correct_signals,
                    win_rate        = excluded.win_rate,
                    last_updated    = excluded.last_updated
            """, (s["ticker"], total, correct, rate))

        await db.commit()

    return {s["ticker"]: (int(s["correct"] or 0) / s["total"] if s["total"] else 0.0)
            for s in stats}
```

- [ ] **步骤 4：Commit**

```bash
git add src/finance_agent/storage/ src/finance_agent/backtest/
git commit -m "feat: SQLite storage + backtest win-rate engine"
```

---

## 任务 8：飞书推送

**文件：**
- 创建：`src/finance_agent/notifications/feishu.py`
- 创建：`tests/test_notifications/test_feishu.py`

- [ ] **步骤 1：写失败测试**

```python
# tests/test_notifications/test_feishu.py
import pytest
import respx
import httpx
from finance_agent.notifications.feishu import send_feishu_message

@pytest.mark.asyncio
async def test_send_feishu_success(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/test")
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(200, json={"code": 0})
        )
        result = await send_feishu_message("测试消息")
    assert result is True

@pytest.mark.asyncio
async def test_send_feishu_failure_returns_false(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/test")
    with respx.mock:
        respx.post("https://open.feishu.cn/hook/test").mock(
            return_value=httpx.Response(500)
        )
        result = await send_feishu_message("测试消息")
    assert result is False
```

- [ ] **步骤 2：运行确认失败**

```bash
pytest tests/test_notifications/test_feishu.py -v
# 预期：ImportError
```

- [ ] **步骤 3：实现 feishu.py**

```python
# src/finance_agent/notifications/feishu.py
import os
import httpx

async def send_feishu_message(text: str) -> bool:
    """
    发送纯文本消息到飞书群机器人。
    飞书机器人设置：群聊 → 设置 → 机器人 → 添加机器人 → 自定义机器人 → 获取 Webhook URL
    """
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not webhook_url:
        print("[飞书] FEISHU_WEBHOOK_URL 未设置，跳过推送")
        return False

    payload = {
        "msg_type": "text",
        "content": {"text": text}
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 0:
                return True
            print(f"[飞书] 推送失败：{result}")
            return False
    except Exception as e:
        print(f"[飞书] 推送异常：{e}")
        return False
```

- [ ] **步骤 4：运行测试**

```bash
pytest tests/test_notifications/test_feishu.py -v
# 预期：2 passed
```

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/notifications/ tests/test_notifications/
git commit -m "feat: Feishu webhook notification"
```

---

## 任务 9：主入口 + 完整串联

**文件：**
- 创建：`src/finance_agent/main.py`

- [ ] **步骤 1：实现 main.py**

```python
# src/finance_agent/main.py
import asyncio
from pathlib import Path
import typer
from rich.console import Console
from dotenv import load_dotenv

from finance_agent.graph.workflow import run_workflow
from finance_agent.storage.db import init_db, save_daily_signals
from finance_agent.notifications.feishu import send_feishu_message
from finance_agent.backtest.engine import backfill_yesterday

load_dotenv()

app = typer.Typer(help="卡门家庭量化交易助手")
console = Console()
DB_PATH = "data/agent.db"

@app.command()
def run(
    skip_notify: bool = typer.Option(False, "--skip-notify", help="不发飞书，只打印"),
    backfill:    bool = typer.Option(True,  "--backfill/--no-backfill", help="运行前回填昨日胜率"),
):
    """运行每日分析并推送飞书"""
    asyncio.run(_run(skip_notify=skip_notify, backfill=backfill))

async def _run(skip_notify: bool, backfill: bool):
    Path("data").mkdir(exist_ok=True)
    await init_db(DB_PATH)

    # Step 1: 回填昨日胜率
    if backfill:
        console.print("📊 回填昨日信号胜率...")
        win_rates = await backfill_yesterday(DB_PATH)
        for ticker, rate in win_rates.items():
            console.print(f"   {ticker}: {rate:.0%}")

    # Step 2: 运行今日分析
    console.print("🤖 开始今日分析...")
    state = await run_workflow(db_path=DB_PATH)

    # Step 3: 保存到 SQLite
    await save_daily_signals(state, DB_PATH)
    console.print(f"💾 已保存 {len(state.stocks)} 只股票信号")

    # Step 4: 打印报告
    console.print("\n" + state.report_text)

    # Step 5: 推送飞书
    if not skip_notify:
        ok = await send_feishu_message(state.report_text)
        console.print("✅ 飞书推送成功" if ok else "❌ 飞书推送失败")

@app.command()
def backfill_only():
    """仅回填昨日胜率，不运行新分析"""
    asyncio.run(backfill_yesterday(DB_PATH))
    console.print("✅ 胜率回填完成")

if __name__ == "__main__":
    app()
```

- [ ] **步骤 2：本地测试跑通（skip-notify 模式）**

先在 `.env` 填入 API Key，然后：

```bash
# 设置 .env
cp .env.example .env
# 编辑 .env，填入 ANTHROPIC_API_KEY 和 DEEPSEEK_API_KEY

# 先用 skip-notify 模式测试
finance-agent run --skip-notify --no-backfill
```

预期：终端输出完整日报文字，无错误。

- [ ] **步骤 3：Commit**

```bash
git add src/finance_agent/main.py
git commit -m "feat: main CLI entry point with full pipeline"
```

---

## 任务 10：GitHub Actions 定时运行

**文件：**
- 创建：`.github/workflows/daily_analysis.yml`

- [ ] **步骤 1：实现 daily_analysis.yml**

```yaml
# .github/workflows/daily_analysis.yml
name: 每日持仓分析

on:
  schedule:
    # 北京时间 09:00 = UTC 01:00
    - cron: "0 1 * * 1-5"   # 周一到周五
  workflow_dispatch:          # 允许手动触发

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: 安装 Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: 安装依赖
        run: pip install -e .

      - name: 运行每日分析
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          DEEPSEEK_API_KEY:  ${{ secrets.DEEPSEEK_API_KEY }}
          FEISHU_WEBHOOK_URL: ${{ secrets.FEISHU_WEBHOOK_URL }}
        run: finance-agent run

      - name: 上传 SQLite 数据库（保留历史）
        uses: actions/upload-artifact@v4
        with:
          name: agent-db-${{ github.run_number }}
          path: data/agent.db
          retention-days: 30
```

- [ ] **步骤 2：在 GitHub 仓库设置 Secrets**

进入仓库 → Settings → Secrets and variables → Actions → New repository secret：
- `ANTHROPIC_API_KEY`
- `DEEPSEEK_API_KEY`
- `FEISHU_WEBHOOK_URL`

- [ ] **步骤 3：推送并验证 Actions 触发**

```bash
git add .github/
git commit -m "feat: GitHub Actions daily schedule at 09:00 CST"
git push origin main
```

手动触发一次验证：Actions → 每日持仓分析 → Run workflow

---

## 自检清单

**规格覆盖度：**
- ✅ 数据层（AkShare/YFinance）— 任务 2
- ✅ 技术信号（RSI/MA/MACD/Bollinger）— 任务 3
- ✅ LangGraph 状态 Pydantic 模型 — 任务 4
- ✅ Bull/Bear/PM 三层辩论 — 任务 5
- ✅ LangGraph 工作流 + SQLite checkpoint — 任务 6
- ✅ 历史信号存储 + 胜率回溯 — 任务 7
- ✅ 飞书推送 — 任务 8
- ✅ CLI 主入口 + 完整串联 — 任务 9
- ✅ GitHub Actions 定时 — 任务 10
- ✅ DeepSeek 做分析（降本），Claude 做最终裁决 — 任务 5

**类型一致性：**
- `AgentState.stocks: list[StockAnalysis]` 贯穿任务 4→5→6→7→8→9 ✅
- `TechnicalSignals.to_prompt_str()` 在 Bull/Bear prompt 中使用 ✅
- `StockAnalysis.model_copy(update=...)` 模式统一 ✅

---

## 执行方式

**推荐：子代理驱动（每任务一个子代理，快速迭代）**
- 每个任务独立，有测试验证，任务间 commit

**内联执行：** 在当前会话用 `executing-plans` 批量执行

选哪种方式？
