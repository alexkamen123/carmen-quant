#!/usr/bin/env python
"""方案A 样本外验证（可复现）：regime-switching 信号选择是否真有增量。
按票 5-fold——训练集学「哪个信号在哪个行情好」、测试集(没见过的票)验，比较：
  naive(全收) / static(丢整体烂的·cycle8思路) / regime(按当前行情丢·方案A)。
判据：regime 必须每折赢 static，否则增量只是「丢烂信号」、与行情无关。regime 行情=SPY vs MA50(因果)。
用法：uv run python scripts/auto_iter/oos_regime.py [TICKER ...]
"""
import sys
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
from finance_agent.backtest.advice_backtest import build_signal_trades, oos_regime_value
from finance_agent.backtest import strategies as S

DEFAULT_TICKERS = [
    "NVDA", "AMD", "AVGO", "TSM", "MU", "QCOM", "KLAC", "LRCX",
    "GOOGL", "MSFT", "AAPL", "META", "PLTR", "CRM", "ADBE", "ORCL",
    "JPM", "GS", "BAC", "MS", "V", "MA", "AXP", "SCHW",
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT",
    "XOM", "CVX", "CAT", "BA", "GE", "HON", "UNP", "DE",
    "WMT", "COST", "PG", "KO", "HD", "MCD",
]
SIGNALS = {
    "rsi": S.rsi_oversold, "boll": S.bollinger_lower, "vol_surge": S.volume_surge_up,
    "ma": S.ma_crossover, "ma_align": S.ma_alignment, "mom": S.momentum_breakout,
}


def _dl(t):
    df = yf.download(t, period="3y", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        return None
    df.columns = [(c[0] if isinstance(c, tuple) else c).lower() for c in df.columns]
    return df


def main(tickers, bench="SPY", k=5):
    spy = _dl(bench)
    pm = {t: d for t in tickers if (d := _dl(t)) is not None}
    print(f"universe {len(pm)} 票 + {bench} · {k}-fold OOS")
    for h in (7, 30):
        trades = build_signal_trades(pm, spy, SIGNALS, horizon=h, regime_ma=50)
        r = oos_regime_value(trades, list(pm.keys()), k=k)
        print(f"\n=== horizon={h}d ({len(trades)} 笔) ===")
        print(f"  naive  alpha={r['naive_alpha']}%")
        print(f"  static alpha={r['static_alpha']}%  (丢整体烂的·不分行情)")
        print(f"  regime alpha={r['regime_alpha']}%  (按当前行情丢) · 每折赢static {r['folds_regime_wins']}/{r['n_folds']}")
        print(f"  判决: {r['verdict']}")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_TICKERS)
