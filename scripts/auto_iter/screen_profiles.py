#!/usr/bin/env python
"""信号体检（两轴×分行情×幅度）：对信号库逐条跑 backtest_signal_profile，照出每个信号
是【进攻型】(涨市赚)还是【防守型】(跌市抗跌)，并报四象限(真赚/赚少/少亏/真亏)。
比 screen_signals.py 的单一 edge 更诚实——用户洞察：alpha 单看会骗人(跌3%我们跌1%，
alpha+2%但钱仍少了)，且信号涨市靠beta、跌市靠抗跌，单数糊掉真实角色。
用法：uv run python scripts/auto_iter/screen_profiles.py [TICKER ...]
"""
import sys
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
from finance_agent.backtest.advice_backtest import backtest_signal_profile
from finance_agent.backtest import strategies as S
from finance_agent.backtest.strategy_weights import WEIGHTS_FILE  # noqa: F401 (保持包可导入)

# 与 screen_signals.py 同口径的去科技偏 universe
DEFAULT_TICKERS = [
    "NVDA", "AMD", "AVGO", "TSM", "MU", "QCOM", "KLAC", "LRCX",
    "GOOGL", "MSFT", "AAPL", "META", "PLTR", "CRM", "ADBE", "ORCL",
    "JPM", "GS", "BAC", "MS", "V", "MA", "AXP", "SCHW",
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT",
    "XOM", "CVX", "CAT", "BA", "GE", "HON", "UNP", "DE",
    "WMT", "COST", "PG", "KO", "HD", "MCD", "SPY", "QQQ",
]
SIGNALS = [
    ("RSI超卖", S.rsi_oversold), ("布林下轨", S.bollinger_lower),
    ("放量", S.volume_surge_up), ("金叉", S.ma_crossover),
    ("均线多头", S.ma_alignment), ("动量突破", S.momentum_breakout),
]
ROLE = {"defensive": "🛡️防守", "offensive": "⚔️进攻", "all_weather": "🌤️两栖", "weak": "💤两头不灵"}


def _dl(t):
    df = yf.download(t, period="3y", interval="1d", progress=False, auto_adjust=True)
    if df.empty:
        return None
    df.columns = [(c[0] if isinstance(c, tuple) else c).lower() for c in df.columns]
    return df


def main(tickers, bench="SPY", horizon=7):
    bench_df = _dl(bench)
    pm = {t: d for t in tickers if (d := _dl(t)) is not None}
    print(f"universe {len(pm)} 票 + {bench} 基准 · horizon={horizon}d")
    print(f"{'信号':<9}{'触发':>6}{'绝对%':>7}{'超额%':>7}  {'赢均':>6}{'亏均':>7}{'赢亏比':>6}{'最坏':>8}  {'角色':>6}")
    for name, fn in SIGNALS:
        r = backtest_signal_profile(pm, bench_df, fn, horizon=horizon)
        if not r.get("n_flagged"):
            print(f"{name:<9}  (无触发)")
            continue
        print(f"{name:<9}{r['n_flagged']:>6}{r['abs_avg']:>7}{r['alpha_avg']:>7}  "
              f"{str(r['win_avg']):>6}{str(r['loss_avg']):>7}{str(r['payoff']):>6}{str(r['worst']):>8}  "
              f"{ROLE[r['role']]:>6}")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_TICKERS)
