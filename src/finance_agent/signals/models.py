from pydantic import BaseModel
from typing import Literal

class TechnicalSignals(BaseModel):
    ticker: str = ""
    close: float
    change_pct: float

    rsi: float
    rsi_signal: Literal["overbought", "oversold", "neutral"]

    ma5: float
    ma20: float
    ma60: float
    ma_signal: Literal["golden_cross", "death_cross", "neutral"]
    price_vs_ma20: float

    macd: float
    macd_signal: float
    macd_hist: float
    macd_trend: Literal["bullish", "bearish", "neutral"]

    bb_upper: float
    bb_lower: float
    bb_position: float

    volume_ratio: float
    atr: float = 0.0
    atr_pct: float = 0.0
    adx: float = 0.0
    trend_mode: bool = False  # True = 强趋势（ADX>25 且价格在MA20/MA60上方）
    composite_score: float

    def to_prompt_str(self) -> str:
        atr_tag = " ⚡高波动" if self.atr_pct > 3.0 else ""
        mode_tag = "【趋势模式】" if self.trend_mode else "【震荡模式】"
        return f"""技术指标摘要（{self.ticker}）{mode_tag}：
- 当前价格：{self.close:.2f}，今日涨跌：{self.change_pct:+.2f}%
- RSI(14): {self.rsi:.1f} → {self.rsi_signal}
- MA5={self.ma5:.2f} / MA20={self.ma20:.2f} / MA60={self.ma60:.2f}
  均线信号：{self.ma_signal}，价格相对MA20: {self.price_vs_ma20:+.1f}%
- MACD趋势：{self.macd_trend}（MACD={self.macd:.3f}, Signal={self.macd_signal:.3f}）
- 布林带位置：{self.bb_position:.0%}（0%=下轨,100%=上轨）
- 成交量比率：{self.volume_ratio:.1f}x
- ATR(14): {self.atr:.2f}（价格的 {self.atr_pct:.1f}%）{atr_tag}
- ADX(14): {self.adx:.1f}{"（强趋势）" if self.adx > 25 else ""}
- 综合量化评分：{self.composite_score:+.2f}""".strip()
