# src/finance_agent/backtest/advice_backtest.py
"""回测裁判：对历史「减仓/卖出」建议回测一条卖出守门规则的判别力，作为「能否据此改核心建议」
的客观闸门——把"改了建议好不好"从拍脑袋变成历史可判。

口径：
  实际卖飞 = 卖出后跑赢大盘，即 (return_7d − benchmark_return_7d) > 0（该卖没卖、踏空）。
  规则 precision = 被规则 flag 的减仓里、真卖飞的占比。
  基率 = 全样本卖飞占比。precision > 基率 = 规则有判别力（一警示就更可能是卖飞）。

反过拟合（关键）：样本不够 / 单一行情下，precision 再高也不判 validated、不许据此自动改核心
建议——19 条且全在牛市的"suppress 减仓更好"极可能是牛市过拟合，一遇崩盘就死扛亏损。
故设样本充分性闸门，沿用价值体检反 p-hacking 风格（写死可见）。
"""
from __future__ import annotations

# 样本充分性闸门（防牛市过拟合；攒够才敢据回测改核心建议）
MIN_EVAL_N = 30     # 至少 30 条可评估减仓
MIN_FLAGGED = 10    # 规则至少 flag 10 条，precision 才有意义


def backtest_sell_rule(recs, rule_fn, sig_provider) -> dict:
    """对历史减仓 recs 回测一条卖出守门规则。

    recs:        list[dict]，每条含 return_7d、benchmark_return_7d（+ ticker/date 供 sig 还原）。
    rule_fn:     (rec, sig) -> bool，是否 flag 这条减仓为"疑似卖飞"。
    sig_provider:(rec) -> sig | None，还原 rec 当时技术面；None=取数失败，跳过该条。

    返回 {n_total, n_eval, n_skip, tp, fp, fn, tn, precision, base_rate,
          discriminating, sufficient, verdict}。
    verdict ∈ {validated, no_discrimination, insufficient_sample}——只有 validated
    才允许据此自动改核心建议。"""
    tp = fp = fn = tn = n_skip = 0
    for rec in recs:
        sig = sig_provider(rec)
        if sig is None:
            n_skip += 1
            continue
        actual_fly = (rec["return_7d"] - rec["benchmark_return_7d"]) > 0
        flagged = bool(rule_fn(rec, sig))
        if flagged and actual_fly:
            tp += 1
        elif flagged and not actual_fly:
            fp += 1
        elif not flagged and actual_fly:
            fn += 1
        else:
            tn += 1

    n_eval = tp + fp + fn + tn
    n_flagged = tp + fp
    precision = (tp / n_flagged) if n_flagged else None
    base_rate = ((tp + fn) / n_eval) if n_eval else None
    discriminating = (precision is not None and base_rate is not None
                      and precision > base_rate)
    sufficient = n_eval >= MIN_EVAL_N and n_flagged >= MIN_FLAGGED

    if not sufficient:
        verdict = "insufficient_sample"
    elif discriminating:
        verdict = "validated"
    else:
        verdict = "no_discrimination"

    return {
        "n_total": len(recs), "n_eval": n_eval, "n_skip": n_skip,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 3) if precision is not None else None,
        "base_rate": round(base_rate, 3) if base_rate is not None else None,
        "discriminating": discriminating, "sufficient": sufficient,
        "verdict": verdict,
    }


def backtest_momentum_sell_broad(price_map: dict, horizon: int = 7) -> dict:
    """广历史回测卖飞守门的核心主张：「强势上行(RSI<70·收>MA20>MA60·MACD>0)的股票会接着涨」
    在多票多年历史上是否成立——突破实际减仓 recs 只有少数、单一行情的样本不足。

    向量化算指标，对每个 (票, 交易日) 点：strong=是否满足强势条件；fly=forward horizon 收益>0
    （强势日卖出会踏空=卖飞）。precision=强势日里 forward>0 的占比；base=全样本 forward>0 占比。
    precision>base 且样本足 → 动量主张成立、卖飞守门有据 → 可安全据此改核心建议。

    price_map: {ticker: DataFrame(含 'close' 列、DatetimeIndex)}；注入便于测试、不依赖网络。
    """
    import pandas as pd
    tp = fp = fn = tn = 0
    n_total_points = 0
    for _tk, df in price_map.items():
        if df is None or "close" not in df or len(df) < 60 + horizon + 1:
            continue
        close = df["close"].astype(float)
        n_total_points += len(close)
        delta = close.diff()
        up = delta.clip(lower=0).rolling(14).mean()
        down = (-delta.clip(upper=0)).rolling(14).mean()
        rs = up / down               # down=0 → inf → rsi=100；up=down=0 → nan（罕见，落入 invalid）
        rsi = 100 - 100 / (1 + rs)
        sma20 = close.rolling(20).mean()
        sma60 = close.rolling(60).mean()
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        fwd = close.shift(-horizon) / close - 1
        strong = (rsi < 70) & (close > sma20) & (sma20 > sma60) & (macd > 0)
        valid = rsi.notna() & sma60.notna() & macd.notna() & fwd.notna()
        fly = fwd > 0
        s, f = strong & valid, fly & valid
        tp += int((s & f).sum())
        fp += int((s & ~fly & valid).sum())
        fn += int((~strong & f).sum())
        tn += int((~strong & ~fly & valid).sum())

    n_eval = tp + fp + fn + tn
    n_flagged = tp + fp
    precision = round(tp / n_flagged, 3) if n_flagged else None
    base_rate = round((tp + fn) / n_eval, 3) if n_eval else None
    discriminating = (precision is not None and base_rate is not None and precision > base_rate)
    sufficient = n_eval >= MIN_EVAL_N and n_flagged >= MIN_FLAGGED
    verdict = ("insufficient_sample" if not sufficient
               else "validated" if discriminating else "no_discrimination")
    return {
        "n_total_points": n_total_points, "n_eval": n_eval, "n_flagged": n_flagged,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "base_rate": base_rate,
        "discriminating": discriminating, "sufficient": sufficient, "verdict": verdict,
    }


def load_sell_recs(db_path) -> list[dict]:
    """从 DB 取历史「减仓/卖出」建议（已回填 return_7d + benchmark、非影子）。"""
    from finance_agent.db.tracker import _conn, _resolve_db
    with _conn(_resolve_db(db_path)) as con:
        return [dict(r) for r in con.execute(
            "SELECT date, ticker, recommendation, position_change, return_7d, "
            "benchmark_return_7d, COALESCE(market,'us') AS market FROM recommendations "
            "WHERE recommendation IN ('减仓','卖出') AND return_7d IS NOT NULL "
            "AND benchmark_return_7d IS NOT NULL AND IFNULL(is_watch,0)=0 ORDER BY date"
        ).fetchall()]


def _default_sig_provider(rec):
    """还原 rec 当时技术面：拉 rec 日往前 120 天 OHLCV → calculate_signals。失败返 None。"""
    from datetime import datetime, timedelta
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    from finance_agent.signals.technical import calculate_signals
    from finance_agent.db.tracker import _yf_ticker
    try:
        d0 = datetime.strptime(rec["date"], "%Y-%m-%d")
        df = yf.download(_yf_ticker(rec["ticker"], rec.get("market", "us")),
                         start=(d0 - timedelta(days=120)).strftime("%Y-%m-%d"),
                         end=(d0 + timedelta(days=1)).strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return calculate_signals(df, ticker=rec["ticker"])
    except Exception:
        return None


def backtest_sell_guard(db_path, sig_provider=None) -> dict:
    """实跑：卖飞守门规则(flag_sell_into_strength)在历史减仓建议上的判别力回测。
    sig_provider 可注入（测试用）；默认走真实 yfinance 还原历史技术面。"""
    from finance_agent.signals.sell_guard import flag_sell_into_strength
    recs = load_sell_recs(db_path)
    provider = sig_provider or _default_sig_provider

    def rule_fn(rec, sig):
        return flag_sell_into_strength(
            rec["recommendation"], rec.get("position_change") or "",
            sig.rsi, sig.close, sig.ma20, sig.ma60, sig.macd) is not None

    return backtest_sell_rule(recs, rule_fn, provider)
