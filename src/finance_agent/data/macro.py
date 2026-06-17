# src/finance_agent/data/macro.py
"""
宏观市场背景数据：VIX、大盘指数、5维度机制分类、Exposure Gate、O'Neil 出货日计数。

5维度机制（参考 tradermonty macro-regime-detector 方法论）：
  1. RSP/SPY  — 等权 vs 市值加权标普，判断涨势宽度
  2. IWM/SPY  — 小盘 vs 大盘，风险偏好
  3. HYG/LQD  — 高收益 vs 投资级债，信用状况
  4. XLY/XLP  — 可选消费 vs 必选消费，进攻 vs 防御
  5. VIX 绝对水平

每个维度输出 +1（风险偏好）/ 0（中性）/ -1（风险回避），
综合评分 → 机制标签 → Exposure Posture（是否适合新增仓位）。

O'Neil 出货日（Distribution Day）：
  - 收盘跌幅 ≥ 0.2% 且成交量 > 前一日 → 计为 1 个出货日
  - 统计最近 25 个交易日内 SPY / QQQ 各自的出货日数量
  - ≥5 个出货日 → 机构分批撤退信号，exposure_posture 至少升为 REDUCE_ONLY
"""
import asyncio
from dataclasses import dataclass, field
import yfinance as yf
import pandas as pd
from .yf_utils import _YF_SEM


@dataclass
class MacroContext:
    vix: float
    vix_level: str
    spx_chg: float
    nasdaq_chg: float
    hsi_chg: float
    # 机制分类
    regime: str = "未知"
    regime_score: int = 0
    exposure_posture: str = "NEW_ENTRY_ALLOWED"
    # 10Y 国债收益率（美股宏观锚点）
    yield_10y: float = 0.0
    # O'Neil 出货日计数（25日窗口）
    dist_days_spy: int = 0
    dist_days_qqq: int = 0
    market_top_signal: str = "正常"

    def plain_summary(self) -> str:
        """把 regime + VIX + posture 翻译成一句"今天大盘怎样、该干嘛"的人话（P1）。"""
        if "危机" in self.regime:
            mood = "风险很大、大幅波动"
        elif "收缩" in self.regime:
            mood = "偏弱、以防御为主"
        elif "过渡" in self.regime:
            mood = "方向不明、多空分歧"
        elif "扩散" in self.regime:
            mood = "全面走强"
        elif "集中" in self.regime:
            mood = "龙头带动、整体偏多"
        else:
            mood = "中性"
        if self.vix >= 30:
            mood = "恐慌、" + mood
        elif self.vix < 20 and not ("危机" in self.regime or "收缩" in self.regime):
            mood = "平静、" + mood

        # 出货日偏多会单独压低仓位建议；不点出来会出现"偏多却暂缓买入"的自相矛盾
        if "警告" in self.market_top_signal or "危险" in self.market_top_signal:
            mood += "，但机构正在悄悄撤退（出货日偏多）"

        ACTION = {
            "NEW_ENTRY_ALLOWED": "可以正常加仓 / 买入",
            "REDUCE_ONLY":       "建议暂缓新买入，先持有观望",
            "CASH_PRIORITY":     "建议多留现金、控制风险，别急着抄底",
        }
        action = ACTION.get(self.exposure_posture, "按计划操作")
        return f"今天大盘{mood}，{action}"

    def to_prompt_str(self) -> str:
        def fmt(v: float) -> str:
            return f"{'+' if v >= 0 else ''}{v:.2f}%"

        # VIX 短描述（避免原 vix_level 的"低（平静）"再被括号包一层 → 嵌套括号）
        vix_word = "平静" if self.vix < 20 else ("偏紧张" if self.vix < 30 else "恐慌")
        yield_str = f"　10年美债利率 {self.yield_10y:.2f}%" if self.yield_10y > 0 else ""
        dist_str = (
            f"　出货日 标普{self.dist_days_spy}/纳指{self.dist_days_qqq}"
            f"（25日内机构抛售天数）→ {self.market_top_signal}"
        )

        detail = (
            f"恐慌指数 VIX {self.vix:.1f}（{vix_word}）｜"
            f"标普500(SPX) {fmt(self.spx_chg)}　纳斯达克100(NDX) {fmt(self.nasdaq_chg)}　"
            f"恒生指数(HSI) {fmt(self.hsi_chg)}"
            f"{yield_str}｜"
            f"市场机制：{self.regime}｜"
            f"{dist_str}"
        )
        return f"{detail}\n👉 一句话：{self.plain_summary()}"


def _day_change(ticker_symbol: str) -> float:
    try:
        t = yf.Ticker(ticker_symbol)
        hist = t.history(period="2d")
        if len(hist) < 2:
            return 0.0
        prev, last = float(hist["Close"].iloc[-2]), float(hist["Close"].iloc[-1])
        return (last - prev) / prev * 100
    except Exception:
        return 0.0


def _vix_value() -> float:
    try:
        t = yf.Ticker("^VIX")
        hist = t.history(period="1d")
        return float(hist["Close"].iloc[-1]) if len(hist) > 0 else 20.0
    except Exception:
        return 20.0


def _fetch_regime_data() -> tuple[str, int, float]:
    """
    一次性下载 7 支 ETF + ^TNX，计算 5 维度机制评分。
    返回 (regime_label, score, yield_10y)。
    失败时静默返回默认值。
    """
    TICKERS = ["RSP", "SPY", "IWM", "HYG", "LQD", "XLY", "XLP", "^TNX"]
    try:
        raw = yf.download(TICKERS, period="35d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw.empty:
            return "未知", 0, 0.0

        # 展平 MultiIndex
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"]
        else:
            close = raw

        def ratio_signal(a: str, b: str) -> int:
            """当前比值相对 20 日均线，高于 +1% → +1；低于 -1% → -1；否则 0"""
            if a not in close.columns or b not in close.columns:
                return 0
            ratio = (close[a] / close[b]).dropna()
            if len(ratio) < 20:
                return 0
            ma20 = float(ratio.rolling(20).mean().iloc[-1])
            cur = float(ratio.iloc[-1])
            pct_dev = (cur - ma20) / ma20 * 100 if ma20 > 0 else 0.0
            return 1 if pct_dev > 1.0 else (-1 if pct_dev < -1.0 else 0)

        score = 0
        score += ratio_signal("RSP", "SPY")   # 宽度信号
        score += ratio_signal("IWM", "SPY")   # 风险偏好
        score += ratio_signal("HYG", "LQD")   # 信用状况
        score += ratio_signal("XLY", "XLP")   # 进攻 vs 防御

        # 10Y 国债收益率（^TNX 报价单位是 %，如 4.5 = 4.5%）
        yield_10y = 0.0
        if "^TNX" in close.columns:
            yield_10y = round(float(close["^TNX"].dropna().iloc[-1]), 2)

        # regime 分类（含 VIX 隐式权重：高 VIX 不覆盖 score，让 posture 逻辑处理）
        if score >= 3:
            regime = "扩散（全面上涨）"
        elif score >= 1:
            regime = "集中（龙头带动）"
        elif score == 0:
            regime = "过渡（信号分歧）"
        elif score >= -2:
            regime = "收缩（防御为主）"
        else:
            regime = "危机（全面风险）"

        return regime, score, yield_10y

    except Exception as e:
        print(f"[Macro] 机制检测失败: {e}")
        return "未知", 0, 0.0


def _fetch_distribution_days() -> tuple[int, int, str]:
    """
    O'Neil 出货日计数：统计 SPY / QQQ 最近 25 个交易日内的出货日数量。
    出货日 = 收盘跌幅 ≥0.2% 且 成交量 > 前一日成交量。
    返回 (spy_count, qqq_count, signal_label)。
    """
    try:
        raw = yf.download(["SPY", "QQQ"], period="65d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw.empty or not isinstance(raw.columns, pd.MultiIndex):
            return 0, 0, "数据不足"

        close = raw["Close"]
        volume = raw["Volume"]

        def count_dist(ticker: str) -> int:
            if ticker not in close.columns or ticker not in volume.columns:
                return 0
            c = close[ticker].dropna()
            v = volume[ticker].dropna()
            idx = c.index.intersection(v.index)
            c, v = c[idx], v[idx]
            if len(c) < 26:
                return 0
            # 最近 25 个交易日（对比各自的前一日）
            chg = c.pct_change() * 100          # 每日涨跌幅 %
            vol_up = v > v.shift(1)             # 成交量放大
            dist = (chg <= -0.2) & vol_up
            return int(dist.iloc[-25:].sum())

        spy_n = count_dist("SPY")
        qqq_n = count_dist("QQQ")
        worst = max(spy_n, qqq_n)

        if worst >= 7:
            label = "🚨 危险（顶部确认）"
        elif worst >= 5:
            label = "⚠️ 警告（机构撤退）"
        elif worst >= 4:
            label = "📌 注意（轻微出货）"
        else:
            label = "✅ 正常"

        return spy_n, qqq_n, label

    except Exception as e:
        print(f"[Macro] 出货日计数失败: {e}")
        return 0, 0, "计算失败"


def _compute_posture(regime: str, vix: float, dist_days: int = 0) -> str:
    """根据机制、VIX、出货日计数决定仓位行动建议。"""
    if "危机" in regime or ("收缩" in regime and vix > 28):
        return "CASH_PRIORITY"
    if "收缩" in regime or "过渡" in regime or vix > 25:
        return "REDUCE_ONLY"
    if dist_days >= 5:
        return "REDUCE_ONLY"
    return "NEW_ENTRY_ALLOWED"


async def fetch_macro_context() -> MacroContext:
    """异步获取完整宏观背景（大盘涨跌 + VIX + 5维度机制 + O'Neil 出货日）。"""
    loop = asyncio.get_event_loop()

    async def _sem_vix():
        async with _YF_SEM:
            return await loop.run_in_executor(None, _vix_value)

    async def _sem_chg(sym: str):
        async with _YF_SEM:
            return await loop.run_in_executor(None, _day_change, sym)

    async def _sem_regime():
        async with _YF_SEM:
            return await loop.run_in_executor(None, _fetch_regime_data)

    async def _sem_dist():
        async with _YF_SEM:
            return await loop.run_in_executor(None, _fetch_distribution_days)

    vix, spx_chg, nasdaq_chg, hsi_chg, regime_tuple, dist_tuple = await asyncio.gather(
        _sem_vix(),
        _sem_chg("^GSPC"),
        _sem_chg("^IXIC"),
        _sem_chg("^HSI"),
        _sem_regime(),
        _sem_dist(),
    )

    regime, regime_score, yield_10y = regime_tuple
    dist_spy, dist_qqq, top_signal = dist_tuple

    if vix < 20:
        level = "低（平静）"
    elif vix < 30:
        level = "中（偏高）"
    else:
        level = "高（恐慌）"

    posture = _compute_posture(regime, vix, dist_days=max(dist_spy, dist_qqq))

    return MacroContext(
        vix=vix,
        vix_level=level,
        spx_chg=spx_chg,
        nasdaq_chg=nasdaq_chg,
        hsi_chg=hsi_chg,
        regime=regime,
        regime_score=regime_score,
        exposure_posture=posture,
        yield_10y=yield_10y,
        dist_days_spy=dist_spy,
        dist_days_qqq=dist_qqq,
        market_top_signal=top_signal,
    )
