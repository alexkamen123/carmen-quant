# src/finance_agent/backtest/universe.py
"""
策略发现用的股票池（美股）。
优先覆盖 AI/半导体/大科技，兼顾金融、消费、医疗做对照组。
港股暂不纳入（AkShare 历史数据不稳定，影响回测质量）。
"""

# AI / 半导体算力核心
SEMIS = [
    "NVDA", "AMD", "INTC", "QCOM", "AVGO", "MRVL", "MU",
    "AMAT", "LRCX", "KLAC", "ASML", "TSM", "ARM", "SMCI", "ON",
]

# 大科技
MEGA_TECH = [
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NFLX", "TSLA",
]

# 云 / SaaS / 网络安全
CLOUD_SAAS = [
    "CRM", "ORCL", "NOW", "SNOW", "PLTR", "PANW", "CRWD",
    "ZS", "NET", "DDOG",
]

# 金融（对照组，低科技相关性）
FINANCIALS = [
    "JPM", "GS", "MS", "V", "MA",
]

# 消费 / 医疗（对照组）
DEFENSIVE = [
    "JNJ", "PG", "KO", "UNH", "LLY",
]

# 完整美股池
US_UNIVERSE: list[str] = (
    SEMIS + MEGA_TECH + CLOUD_SAAS + FINANCIALS + DEFENSIVE
)

# 策略评估基准（US 用 SPY，也可选 QQQ）
US_BENCHMARK = "SPY"
TECH_BENCHMARK = "QQQ"
