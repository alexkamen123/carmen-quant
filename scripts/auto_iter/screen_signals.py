#!/usr/bin/env python
"""信号库边际筛选：把系统的技术信号逐条丢进广历史回测，看哪些真有预测力、哪些是噪声/反指。
用法：uv run python scripts/auto_iter/screen_signals.py [TICKER ...]
输出按 edge(=precision−基率) 排序；只有 validated 且 edge>0 的信号才值得据此调建议权重。
"""
import sys
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
from finance_agent.backtest.advice_backtest import backtest_signal_edge
from finance_agent.backtest import strategies as S

DEFAULT_TICKERS = ["NVDA", "GOOGL", "AAPL", "MSFT", "TSM", "MU", "AVGO", "AMD", "META", "QCOM"]
# (名称, 信号函数, 是否看涨)；strategies 的入场信号均为看涨类
CANDIDATES = [
    ("RSI超卖反弹", S.rsi_oversold, True),
    ("均线金叉", S.ma_crossover, True),
    ("布林下轨", S.bollinger_lower, True),
    ("动量突破", S.momentum_breakout, True),
    ("放量上涨", S.volume_surge_up, True),
    ("均线多头排列", S.ma_alignment, True),
]


def fetch(tickers, period="3y"):
    pm = {}
    for t in tickers:
        try:
            df = yf.download(t, period=period, interval="1d", progress=False, auto_adjust=True)
            if df.empty:
                continue
            df.columns = [(c[0] if isinstance(c, tuple) else c).lower() for c in df.columns]
            pm[t] = df
        except Exception:
            pass
    return pm


def screen(pm, horizons=(7, 30)):
    rows = []
    for name, fn, up in CANDIDATES:
        for h in horizons:
            r = backtest_signal_edge(pm, mask_fn=fn, horizon=h, predict_up=up)
            rows.append((name, h, r))
    rows.sort(key=lambda x: -(x[2]["edge"] if x[2]["edge"] is not None else -9))
    return rows


if __name__ == "__main__":
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    pm = fetch(tickers)
    print(f"取到 {len(pm)} 票 × 3年")
    print(f"{'信号':<16}{'h':<4}{'触发n':>7}{'precision':>10}{'基率':>7}{'edge':>7}  判决")
    for name, h, r in screen(pm):
        print(f"{name:<16}{h:<4}{r['n_flagged']:>7}{str(r['precision']):>10}"
              f"{str(r['base_rate']):>7}{str(r['edge']):>7}  {r['verdict']}")
