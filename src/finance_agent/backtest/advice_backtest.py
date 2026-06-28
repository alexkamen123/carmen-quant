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


def backtest_signal_edge(price_map: dict, mask_fn, horizon: int = 7,
                         predict_up: bool = True) -> dict:
    """泛化广历史回测：测「某信号触发日 → forward horizon 收益方向」是否优于基率。
    用于逐条筛系统的技术信号哪些真有预测力、哪些是噪声（直接影响该信哪些信号下建议）。

    mask_fn(df) -> bool Series：信号在哪些交易日触发（df 含 OHLCV、DatetimeIndex）。
    predict_up=True：信号预测 forward>0（看涨类，如超卖反弹/突破）；False：预测 forward<0（看跌类，如超买回落）。
    precision=触发日里命中方向的占比；base=全样本该方向占比。precision>base 且样本足 → 信号有边际。
    """
    tp = fp = fn = tn = 0
    n_total_points = 0
    for _tk, df in price_map.items():
        if df is None or "close" not in df or len(df) < 60 + horizon + 1:
            continue
        close = df["close"].astype(float)
        n_total_points += len(close)
        try:
            mask = mask_fn(df).reindex(close.index).fillna(False).astype(bool)
        except Exception:
            continue
        fwd = close.shift(-horizon) / close - 1
        target = (fwd > 0) if predict_up else (fwd < 0)
        valid = fwd.notna()
        tp += int((mask & target & valid).sum())
        fp += int((mask & ~target & valid).sum())
        fn += int((~mask & target & valid).sum())
        tn += int((~mask & ~target & valid).sum())

    n_eval = tp + fp + fn + tn
    n_flagged = tp + fp
    precision = round(tp / n_flagged, 3) if n_flagged else None
    base_rate = round((tp + fn) / n_eval, 3) if n_eval else None
    discriminating = (precision is not None and base_rate is not None and precision > base_rate)
    sufficient = n_eval >= MIN_EVAL_N and n_flagged >= MIN_FLAGGED
    verdict = ("insufficient_sample" if not sufficient
               else "validated" if discriminating else "no_discrimination")
    edge = round(precision - base_rate, 3) if (precision is not None and base_rate is not None) else None
    return {
        "n_total_points": n_total_points, "n_eval": n_eval, "n_flagged": n_flagged,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "base_rate": base_rate, "edge": edge,
        "discriminating": discriminating, "sufficient": sufficient, "verdict": verdict,
    }


def backtest_signal_profile(price_map: dict, bench_df, mask_fn, horizon: int = 7) -> dict:
    """信号体检：两轴 × 分行情 × 幅度，比单一 precision 诚实。
    信号触发后，分别量【赚没赚】(绝对收益) 与【跑赢没】(超额 alpha = 我们−基准)，按大盘涨/跌拆开，
    给四象限分布与角色。动机(用户洞察)：alpha 单独看会骗人——大盘跌3%我们跌1%，alpha+2% 漂亮但
    钱还是少了；且一个信号涨市靠 beta、跌市靠抗跌，单一数会糊掉它是进攻还是防守。

    bench_df: 基准(SPY/恒指…) DataFrame，含 'close'、DatetimeIndex；注入便于测试、不依赖网络。
    mask_fn(df) -> bool Series：信号触发日。horizon: forward 天数。
    返回 abs_avg/alpha_avg(平均%)、abs_hit/alpha_beat(占比)、up/down 分行情、quadrants 四象限、role。
    四象限(我们涨跌 × 跑赢跑输)：q1真赚 / q2赚少(跑输) / q3少亏(跑赢但亏) / q4真亏。
    角色：defensive(跌市有正alpha、涨市无) / offensive(反之) / all_weather(都正) / weak(都不灵)。"""
    import numpy as np
    if bench_df is None or "close" not in bench_df:
        return {"n_flagged": 0, "role": "weak"}
    bclose = bench_df["close"].astype(float)
    bfwd = bclose.shift(-horizon) / bclose - 1
    our_list, bench_list = [], []
    for _tk, df in price_map.items():
        if df is None or "close" not in df or len(df) < horizon + 1:
            continue
        close = df["close"].astype(float)
        fwd = close.shift(-horizon) / close - 1
        bf = bfwd.reindex(close.index)
        try:
            mask = mask_fn(df).reindex(close.index).fillna(False).astype(bool)
        except Exception:
            continue
        v = fwd.notna() & bf.notna() & mask
        our_list.extend(fwd[v].tolist())
        bench_list.extend(bf[v].tolist())

    our = np.array(our_list)
    ben = np.array(bench_list)
    n = len(our)
    if n == 0:
        return {"n_flagged": 0, "role": "weak"}
    alpha = our - ben

    def pct(x):
        return round(float(x) * 100, 2)

    up = ben > 0
    dn = ~up
    eps = 0.001    # 0.1% 行情中性带（判角色用）
    up_a = float(alpha[up].mean()) if up.any() else 0.0
    dn_a = float(alpha[dn].mean()) if dn.any() else 0.0
    if dn_a > eps and up_a <= eps:
        role = "defensive"
    elif up_a > eps and dn_a <= eps:
        role = "offensive"
    elif up_a > eps and dn_a > eps:
        role = "all_weather"
    else:
        role = "weak"

    # 幅度与尾部风险：平均值会糊掉「多次小赢、一次大亏」——拆赢/亏幅度 + 最坏一次 + 赢亏比。
    wins = our[our > 0]
    losses = our[our <= 0]
    win_avg = pct(wins.mean()) if len(wins) else None
    loss_avg = pct(losses.mean()) if len(losses) else None
    payoff = (round(float(wins.mean() / -losses.mean()), 2)
              if len(wins) and len(losses) and losses.mean() < 0 else None)

    return {
        "n_flagged": n,
        "abs_avg": pct(our.mean()), "abs_hit": round(float((our > 0).mean()), 3),
        "alpha_avg": pct(alpha.mean()), "alpha_beat": round(float((alpha > 0).mean()), 3),
        "win_avg": win_avg, "loss_avg": loss_avg,     # 对时平均赚 / 错时平均亏（%）
        "worst": pct(our.min()),                      # 历史最坏一次（%）
        "payoff": payoff,                             # 赢亏比 = 平均赢 / 平均亏；<1=小赢大亏、危险
        "up": {"n": int(up.sum()),
               "abs_avg": pct(our[up].mean()) if up.any() else None,
               "alpha_avg": pct(up_a) if up.any() else None},
        "down": {"n": int(dn.sum()),
                 "abs_avg": pct(our[dn].mean()) if dn.any() else None,
                 "alpha_avg": pct(dn_a) if dn.any() else None},
        "quadrants": {
            "q1_real_gain": round(float(((our > 0) & (alpha > 0)).mean()), 3),
            "q2_gain_lag": round(float(((our > 0) & (alpha <= 0)).mean()), 3),
            "q3_small_loss": round(float(((our <= 0) & (alpha > 0)).mean()), 3),
            "q4_real_loss": round(float(((our <= 0) & (alpha <= 0)).mean()), 3),
        },
        "role": role,
    }


def build_signal_trades(price_map: dict, bench_df, signal_fns: dict, horizon: int = 7,
                        regime_ma: int = 50) -> list[tuple]:
    """构建 (ticker, family, regime, fwd_alpha) 交易表，供方案A样本外验证。
    regime【因果】：SPY 收盘 vs 其自身 regime_ma 日均线(当天可知，无前视)——up=在均线上方。
    fwd_alpha = 个股 forward 收益 − 同期 SPY forward 收益(超额)。signal_fns: {family: mask_fn}。"""
    if bench_df is None or "close" not in bench_df:
        return []
    bclose = bench_df["close"].astype(float)
    bfwd = bclose.shift(-horizon) / bclose - 1
    bregime = bclose > bclose.rolling(regime_ma).mean()   # 因果：用截至当天的均线
    rows = []
    for tk, df in price_map.items():
        if df is None or "close" not in df or len(df) < regime_ma + horizon + 1:
            continue
        close = df["close"].astype(float)
        fwd = close.shift(-horizon) / close - 1
        bf = bfwd.reindex(close.index)
        reg = bregime.reindex(close.index)
        for fam, fn in signal_fns.items():
            try:
                mask = fn(df).reindex(close.index).fillna(False).astype(bool)
            except Exception:
                continue
            v = mask & fwd.notna() & bf.notna() & reg.notna()
            for dt in close.index[v]:
                rows.append((tk, fam, "up" if bool(reg[dt]) else "down",
                             float(fwd[dt] - bf[dt])))
    return rows


def oos_regime_value(trades: list[tuple], tickers: list, k: int = 5) -> dict:
    """方案A样本外验证：按票 k-fold——训练集学规则、测试集(没见过的票)验，
    比较三策略的 OOS 平均超额(alpha)：
      naive  = 所有触发信号一视同仁；
      static = 只留训练集里【整体】超额>0 的族(=cycle8护栏思路，不分行情)；
      regime = 只留训练集里【当前行情】超额>0 的族(=方案A)。
    关键判据：regime 必须赢过 static，否则增量只是「丢烂信号」、与行情无关，不值得碰引擎。
    trades: (ticker, family, regime, fwd_alpha) 列表(build_signal_trades 产)。确定性取模分折。"""
    import numpy as np
    from collections import defaultdict
    tks = sorted(set(tickers))
    if len(tks) < k:
        k = max(2, len(tks))
    folds = [tks[i::k] for i in range(k)]
    res = {"naive": [], "static": [], "regime": []}
    regime_wins = 0
    n_folds = 0
    for f in range(k):
        test_tk = set(folds[f])
        if not test_tk:
            continue
        train = [t for t in trades if t[0] not in test_tk]
        test = [t for t in trades if t[0] in test_tk]
        if not train or not test:
            continue
        ov, rg = defaultdict(list), defaultdict(list)
        for _tk, fam, reg, a in train:
            ov[fam].append(a)
            rg[(fam, reg)].append(a)
        good_static = {fam for fam, v in ov.items() if np.mean(v) > 0}
        good_regime = {key for key, v in rg.items() if np.mean(v) > 0}
        naive = [a for _, _, _, a in test]
        static = [a for _, fam, _, a in test if fam in good_static]
        regime = [a for _, fam, reg, a in test if (fam, reg) in good_regime]
        if not naive:
            continue
        n_folds += 1
        nm, sm, rm = np.mean(naive), (np.mean(static) if static else 0.0), (np.mean(regime) if regime else 0.0)
        res["naive"].append(nm)
        res["static"].append(sm)
        res["regime"].append(rm)
        if rm > sm:
            regime_wins += 1

    def agg(v):
        return round(float(np.mean(v)) * 100, 3) if v else None

    naive_a, static_a, regime_a = agg(res["naive"]), agg(res["static"]), agg(res["regime"])
    eps = 0.05   # 0.05% 增量阈值（OOS 噪声带）
    if regime_a is None or static_a is None:
        verdict = "insufficient"
    elif regime_a > static_a + eps and regime_wins >= max(1, n_folds * 0.6):
        verdict = "regime_adds_value"           # 方案A 有真增量 → 值得碰引擎
    elif static_a > (naive_a or 0) + eps:
        verdict = "static_enough"               # 丢烂信号够了，分行情没多余价值
    else:
        verdict = "no_value"
    return {
        "n_folds": n_folds, "folds_regime_wins": regime_wins,
        "naive_alpha": naive_a, "static_alpha": static_a, "regime_alpha": regime_a,
        "verdict": verdict,
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
