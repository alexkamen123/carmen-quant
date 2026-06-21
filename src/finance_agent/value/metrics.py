# src/finance_agent/value/metrics.py
"""
价值度量：把 recommendations / user_actions / dip_alerts 聚合成诚实的"价值记分牌"。

核心约束（来自 opus 设计评审团）：
  - 命中率分母只含【可裁决的方向性信号】(买入/大加/小加/减仓/卖出)，
    持有 / 观望 / 按计划定投一律不计入（否则 91/102 中性记录会虚构出 90%+ 胜率）。
  - 选股(BUY类) 与 择时(减仓) 与 持有/定投 三层分桶，绝不混成一个数。
  - 比率必配 Wilson 95% 置信区间；样本量同屏披露 N / 票数 / 天数。
  - 当前 outcome 仅回填 7 日；更长周期、窗口对齐修复、market 补齐留 L1b。
"""
import math
from datetime import datetime

from finance_agent.db.tracker import (
    _resolve_db, _conn, init_db, get_feedback_accuracy, get_dip_stats,
)

# 下结论闸门（写死可见，防 p-hacking）：方向性样本≥30 且覆盖≥8 票 且 benchmark 覆盖≥70%
GATE_MIN_N = 30
GATE_MIN_TICKERS = 8
GATE_MIN_BM_COV = 0.70
NEUTRAL_BAND = 1.0   # |7日收益| < 1% 记中性，不进命中率分子分母
HOLD_BAD_ALPHA = -5.0  # 持有期 alpha ≤ 此值 = 持有错（该减没减）；写死可见防 p-hacking
# 影子选股轨「攒够才考虑下结论」的展示目标（仅供进度提示，不自动升格为结论；
# 何时把选股轨升格为确定性头条须单独评审，见设计评审 D2）
SHADOW_DISPLAY_TARGET = 20

# ── 暴跌分级（order6）：纯规则零 LLM，词表写死可审计（沿用 GATE_* 防 p-hacking 风格）──
DIP_BUCKET_OPPORTUNITY = "机会型回调"   # thesis 完好 + 情绪/板块/技术性驱动
DIP_BUCKET_BROKEN      = "基本面破裂"   # LLM 判 thesis 已破坏
DIP_BUCKET_WATCH       = "待观察"       # 史前行/模型未给出判断（intact=NULL）或证据矛盾，保守挂起

_DIP_FUND_KW = ("基本面恶化", "基本面转弱", "业绩恶化", "业绩暴雷", "业绩爆雷",
                "暴雷", "爆雷", "不及预期", "指引下调", "下调指引", "指引转弱",
                "证伪", "财务造假", "竞争格局逆转")
_DIP_NEG_PREFIXES = ("非", "并非", "不是", "无", "没有", "排除", "而非",
                     "未见", "无明显", "不存在",
                     "未见明显", "没有明显", "没有看到", "看不到")


def _has_unnegated_fund_kw(text: str) -> bool:
    """定位检查：否定词须紧贴关键词结尾（endswith）才算否定（防'非基本面恶化'误伤，
    也防'业绩暴雷，并非情绪问题'里别处的否定词误放行）。
    长词优先扫描：被否定的长词整段屏蔽后再查短词，防'并非业绩暴雷'经内含'暴雷'误命中。"""
    w = max(len(n) for n in _DIP_NEG_PREFIXES)
    masked = text
    for kw in sorted(_DIP_FUND_KW, key=len, reverse=True):
        start = 0
        while (i := masked.find(kw, start)) != -1:
            prefix = masked[max(0, i - w):i]
            if not any(prefix.endswith(neg) for neg in _DIP_NEG_PREFIXES):
                return True
            masked = masked[:i] + "□" * len(kw) + masked[i + len(kw):]   # 否定命中→屏蔽该段
            start = i + len(kw)
    return False


def classify_dip(thesis_intact: int | None, drop_reason: str | None) -> str:
    """dip 分桶：以 thesis_intact 布尔为主轴，词表只做矛盾检测。
    错误退化方向恒保守（宁待观察、不虚标机会）。"""
    if thesis_intact is None:
        return DIP_BUCKET_WATCH                 # 史前行或模型未给出判断（NULL 三态保留）
    if not thesis_intact:
        return DIP_BUCKET_BROKEN
    if _has_unnegated_fund_kw(drop_reason or ""):
        return DIP_BUCKET_WATCH                 # intact=1 却归因基本面 → 证据矛盾，挂起
    return DIP_BUCKET_OPPORTUNITY


_PASSIVE_RECS = ("持有", "观望", "按计划定投")  # 立场=不动，position_change 仅注解不构成方向信号


def _is_bullish(rec: str, pc: str) -> bool:
    # rec 优先于 pc：「持有/观望 + 小加」语义是持有，不得计为买入（防 00700 类伪买入污染头条）
    if rec in _PASSIVE_RECS:
        return False
    return rec == "买入" or (pc or "").startswith(("大加", "小加"))


def _is_bearish(rec: str, pc: str) -> bool:
    if rec in _PASSIVE_RECS:
        return False
    return rec in ("减仓", "卖出") or (pc or "").startswith("减仓")


def wilson_ci(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """正版 Wilson 95% 置信区间（不会越界到 [0,1] 之外，小样本比 normal-approx 稳健）。"""
    if n <= 0:
        return (0.0, 0.0)
    p = correct / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _score_window(rows: list[dict], ret_key: str, bm_key: str,
                  neutral_band: float) -> dict | None:
    """对给定窗口(7d/30d/90d)算命中率 + 组合 alpha，复用方向判定与 Wilson CI。
    中性带随窗口缩放（短窗噪声大、长窗放宽）。无可用样本返回 None。
    benchmark 子集做组合 alpha：bullish=return-bench，bearish 反号（少亏即赚）。"""
    correct = wrong = 0
    effs: list[float] = []
    for r in rows:
        ret = r.get(ret_key)
        if ret is None:
            continue
        bull = _is_bullish(r["recommendation"], r["position_change"])
        bear = _is_bearish(r["recommendation"], r["position_change"])
        if abs(ret) >= neutral_band:
            hit = (ret > 0) if bull else ((ret < 0) if bear else None)
            if hit is not None:
                correct += 1 if hit else 0
                wrong += 0 if hit else 1
        bm = r.get(bm_key)
        if bm is not None:
            d = ret - bm
            effs.append(-d if bear else d)
    n_judged = correct + wrong
    if n_judged == 0 and not effs:
        return None
    ci = wilson_ci(correct, n_judged) if n_judged else None
    return {
        "n_judged": n_judged, "correct": correct, "wrong": wrong,
        "win_rate": round(correct / n_judged * 100, 1) if n_judged else None,
        "ci_low": round(ci[0] * 100, 1) if ci else None,
        "ci_high": round(ci[1] * 100, 1) if ci else None,
        "alpha_n": len(effs),
        "alpha_avg": round(sum(effs) / len(effs), 2) + 0.0 if effs else None,
    }


def _dedup_weekly(rows: list[dict]) -> list[dict]:
    """同一 (ticker, ISO周) 的连续推荐视为同一独立信号，只保留该周首条。
    消除"同票连推数日"造成的伪独立膨胀（7天窗口高度重叠、同一 thesis，
    计多条会虚增 n 并重复计入相关结果）。按日期升序取首条，确定性。"""
    seen: set = set()
    out: list[dict] = []
    for r in sorted(rows, key=lambda x: x["date"]):
        try:
            y, w, _ = datetime.strptime(r["date"], "%Y-%m-%d").isocalendar()
            key = (r["ticker"], y, w)
        except (ValueError, TypeError):
            key = (r["ticker"], r["date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _span_days(dates: list[str]) -> int:
    ds = sorted(d for d in dates if d)
    if len(ds) < 2:
        return len(ds)
    try:
        a = datetime.strptime(ds[0], "%Y-%m-%d")
        b = datetime.strptime(ds[-1], "%Y-%m-%d")
        return (b - a).days
    except ValueError:
        return len(ds)


def compute_value_metrics(db_path=None) -> dict:
    """
    返回诚实的价值记分牌（纯数据）。结构：
      gate / composition / hit_rate / buy_alpha / combined_alpha / behavior / dip / verdict / data_through
    """
    p = _resolve_db(db_path)
    init_db(p)

    with _conn(p) as con:
        total = con.execute("SELECT COUNT(*) AS n FROM recommendations").fetchone()["n"]
        filled = con.execute(
            "SELECT COUNT(*) AS n FROM recommendations WHERE return_7d IS NOT NULL"
        ).fetchone()["n"]
        # 操作建议命中率只统计真实持仓建议（影子选股 is_watch=1 单独分栏，绝不混算）
        # pc-based 方向匹配须排除被动立场（持有/观望/定投）——否则「持有+小加」会经
        # position_change LIKE '小加%' 混进方向性闸门量 n_dir（实测 00700 三条伪买入）
        _DIR_FILTER = ("recommendation IN ('买入','减仓','卖出') "
                       "OR (IFNULL(recommendation,'') NOT IN ('持有','观望','按计划定投') "
                       "AND (position_change LIKE '大加%' OR position_change LIKE '小加%' "
                       "OR position_change LIKE '减仓%'))")
        dir_rows = [dict(r) for r in con.execute(
            "SELECT date, ticker, recommendation, position_change, return_7d, "
            "benchmark_return_7d, return_30d, benchmark_return_30d, "
            "return_90d, benchmark_return_90d, market FROM recommendations "
            f"WHERE return_7d IS NOT NULL AND IFNULL(is_watch, 0) = 0 AND ({_DIR_FILTER})"
        ).fetchall()]
        # 连推去重（_meta_section 早已自承的缺陷）：同 (票,ISO周) 只算一条独立信号
        dir_rows = _dedup_weekly(dir_rows)
        shadow_rows = [dict(r) for r in con.execute(
            "SELECT date, ticker, recommendation, position_change, return_7d, "
            "benchmark_return_7d FROM recommendations "
            f"WHERE return_7d IS NOT NULL AND is_watch = 1 AND ({_DIR_FILTER})"
        ).fetchall()]
        data_through = con.execute(
            "SELECT MAX(date) AS m FROM recommendations"
        ).fetchone()["m"]

    # ── 闸门量 ──
    n_dir = len(dir_rows)
    n_tk = len({r["ticker"] for r in dir_rows})
    n_days = len({r["date"] for r in dir_rows})
    n_buy = sum(1 for r in dir_rows if _is_bullish(r["recommendation"], r["position_change"]))
    n_bm = sum(1 for r in dir_rows if r["benchmark_return_7d"] is not None)
    bm_cov_dir = (n_bm / n_dir) if n_dir else 0.0
    span = _span_days([r["date"] for r in dir_rows])
    gate_passed = (n_dir >= GATE_MIN_N and n_tk >= GATE_MIN_TICKERS
                   and bm_cov_dir >= GATE_MIN_BM_COV)

    gate = {
        "n_dir": n_dir, "n_tk": n_tk, "n_days": n_days, "n_buy": n_buy,
        "span_days": span, "bm_cov_dir": round(bm_cov_dir, 2),
        "filled": filled, "total": total, "gate_passed": gate_passed,
        "thresholds": {"min_n": GATE_MIN_N, "min_tickers": GATE_MIN_TICKERS,
                       "min_bm_cov": GATE_MIN_BM_COV},
    }

    composition = {
        "filled": filled,
        "directional": n_dir,
        "neutral_or_passive": filled - n_dir,
        "actionable_ratio": round(n_dir / filled, 3) if filled else 0.0,
    }

    # ── 方向命中率（窄口径 + Wilson CI）；买/卖分开计数供"能力总评"三问 ──
    correct = wrong = neutral = 0
    sell_right = sell_wrong = 0   # 卖/减：续跌=对(躲跌)、续涨=错(卖飞)
    judged_tickers: set = set()
    for r in dir_rows:
        ret = r["return_7d"]
        if abs(ret) < NEUTRAL_BAND:
            neutral += 1
            continue
        bull = _is_bullish(r["recommendation"], r["position_change"])
        bear = _is_bearish(r["recommendation"], r["position_change"])
        hit = (ret > 0) if bull else ((ret < 0) if bear else None)
        if hit is None:
            continue
        judged_tickers.add(r["ticker"])
        if hit:
            correct += 1
        else:
            wrong += 1
        if bear:
            sell_right += 1 if ret < 0 else 0
            sell_wrong += 1 if ret >= 0 else 0
    n_judged = correct + wrong
    win_rate = (correct / n_judged) if n_judged else None
    ci = wilson_ci(correct, n_judged) if n_judged else None
    hit_rate = {
        "n_judged": n_judged, "correct": correct, "wrong": wrong, "neutral": neutral,
        "n_tickers": len(judged_tickers),
        "win_rate": round(win_rate * 100, 1) if win_rate is not None else None,
        "ci_low": round(ci[0] * 100, 1) if ci else None,
        "ci_high": round(ci[1] * 100, 1) if ci else None,
        "buckets_note": "仅减仓择时" if n_buy == 0 and n_dir > 0 else None,
        "sell_right": sell_right, "sell_wrong": sell_wrong,  # 卖/减专项（能力总评第3问）
    }

    # ── 多窗口信号超额（7/30/90 天）：消费已回填的长周期列，回应"7天太短"──
    # 7天噪声大、30/90天才是价值兑现窗口；中性带随窗口放宽。90天暂多为空→显示"未到"。
    alpha_by_window = {
        "7d": _score_window(dir_rows, "return_7d", "benchmark_return_7d", NEUTRAL_BAND),
        "30d": _score_window(dir_rows, "return_30d", "benchmark_return_30d", 3.0),
        "90d": _score_window(dir_rows, "return_90d", "benchmark_return_90d", 5.0),
    }

    # ── BUY类（选股/加仓）alpha —— 当前多为 N=0，显式占位 ──
    buy_alpha_rows = [r for r in dir_rows
                      if _is_bullish(r["recommendation"], r["position_change"])
                      and r["benchmark_return_7d"] is not None]
    if buy_alpha_rows:
        alphas = [r["return_7d"] - r["benchmark_return_7d"] for r in buy_alpha_rows]
        buy_alpha = {"n": len(alphas), "avg": round(sum(alphas) / len(alphas), 2),
                     "status": "ok"}
    else:
        buy_alpha = {"n": 0, "avg": None, "status": "no_sample",
                     "note": "尚无可评估的买入信号（回测窗口未到 / 未回填）"}

    # ── 组合 alpha（有 benchmark 子集）—— 覆盖率不足则标灰不结论 ──
    bm_rows = [r for r in dir_rows if r["benchmark_return_7d"] is not None]
    if bm_rows:
        # bullish: return - bench；bearish: bench - return（少亏即赚，符号反号）
        eff = []
        for r in bm_rows:
            d = r["return_7d"] - r["benchmark_return_7d"]
            if _is_bearish(r["recommendation"], r["position_change"]):
                d = -d
            eff.append(d)
        avg_alpha = sum(eff) / len(eff)
        combined_alpha = {
            "n": len(eff), "avg": round(avg_alpha, 2),
            "reliable": bm_cov_dir >= GATE_MIN_BM_COV,
            "status": "ok" if bm_cov_dir >= GATE_MIN_BM_COV else "low_coverage",
        }
    else:
        avg_alpha = None
        combined_alpha = {"n": 0, "avg": None, "reliable": False, "status": "no_sample"}

    # ── 影子选股（watchlist 推荐，无真实仓位，纯测量选股能力）──
    # 本轮只展示明细+Wilson CI，措辞固定"样本不足不下结论"，不开"证明了选股能力"头条
    # （N 小且无自动来源时设头条=复刻它本要解决的 p-hacking，详见设计评审）。
    s_correct = s_wrong = 0
    s_alphas = []
    s_picks = []
    for r in sorted(shadow_rows, key=lambda x: x["date"], reverse=True):
        ret = r["return_7d"]
        bull = _is_bullish(r["recommendation"], r["position_change"])
        bear = _is_bearish(r["recommendation"], r["position_change"])
        if abs(ret) < NEUTRAL_BAND:
            verdict_t = "中性"
        else:
            hit = (ret > 0) if bull else ((ret < 0) if bear else None)
            if hit is None:
                verdict_t = "—"
            else:
                s_correct += 1 if hit else 0
                s_wrong += 0 if hit else 1
                verdict_t = "对" if hit else "错"
                if r["benchmark_return_7d"] is not None:
                    a = ret - r["benchmark_return_7d"]
                    s_alphas.append(-a if bear else a)
        s_picks.append({"date": r["date"], "ticker": r["ticker"],
                        "rec": r["recommendation"], "ret": ret, "verdict": verdict_t})
    s_judged = s_correct + s_wrong
    s_ci = wilson_ci(s_correct, s_judged) if s_judged else None
    shadow_picks = {
        "n": len(shadow_rows), "correct": s_correct, "wrong": s_wrong,
        "n_judged": s_judged, "n_tickers": len({r["ticker"] for r in shadow_rows}),
        "win_rate": round(s_correct / s_judged * 100, 1) if s_judged else None,
        "ci_low": round(s_ci[0] * 100, 1) if s_ci else None,
        "ci_high": round(s_ci[1] * 100, 1) if s_ci else None,
        "avg_alpha": round(sum(s_alphas) / len(s_alphas), 2) + 0.0 if s_alphas else None,
        "target": SHADOW_DISPLAY_TARGET,
        "picks": s_picks[:8],
    }

    # ── 持有判断质量（与方向命中率分栏，绝不混算）──
    # 持有/观望也是立场（隐含"别卖也别加"），口径 = 持有期 vs 基准的 alpha：
    # 跑赢基准 = 持有对（幸亏没卖）；跑输 ≤ HOLD_BAD_ALPHA = 持有错（该减没减）；
    # 其间 = 中性。按计划定投是机械执行不是判断，排除。
    with _conn(p) as con:
        hold_rows = [dict(r) for r in con.execute(
            "SELECT date, ticker, return_7d, benchmark_return_7d FROM recommendations "
            "WHERE return_7d IS NOT NULL AND benchmark_return_7d IS NOT NULL "
            "AND recommendation IN ('持有', '观望') AND IFNULL(is_watch, 0) = 0"
        ).fetchall()]   # 影子票没有真实持仓，"持有判断"无意义，排除
    # 与 dir_rows 同口径去重：同票同 ISO 周的连续持有是同一立场，计多条会灌爆分母
    # （实测 00700×23 / GOOGL×23 等单票日频膨胀）
    hold_rows = _dedup_weekly(hold_rows)
    h_right = h_wrong = h_neutral = 0
    h_alphas = []
    h_wrong_cases = []
    for r in hold_rows:
        a = r["return_7d"] - r["benchmark_return_7d"]
        h_alphas.append(a)
        if a > 0:
            h_right += 1
        elif a <= HOLD_BAD_ALPHA:
            h_wrong += 1
            h_wrong_cases.append({"date": r["date"], "ticker": r["ticker"],
                                  "alpha": round(a, 2)})
        else:
            h_neutral += 1
    h_wrong_cases.sort(key=lambda c: c["alpha"])
    hold_quality = {
        "n": len(hold_rows), "right": h_right, "wrong": h_wrong, "neutral": h_neutral,
        "avg_alpha": round(sum(h_alphas) / len(h_alphas), 2) + 0.0 if h_alphas else None,
        "bad_alpha_threshold": HOLD_BAD_ALPHA,
        "wrong_cases": h_wrong_cases[:5],
    }

    # ── 行为价值：逐笔（不聚合成胜率）──
    with _conn(p) as con:
        act_rows = [dict(r) for r in con.execute(
            "SELECT date, ticker, action, actual_return FROM user_actions "
            "WHERE actual_return IS NOT NULL ORDER BY date"
        ).fetchall()]
    behavior_trades = []
    for r in act_rows:
        act, ret = r["action"], r["actual_return"]
        if act == "BUY":
            verdict_t = "赚" if ret > 0 else ("亏" if ret < 0 else "平")
        elif act in ("SELL", "TRIM"):
            verdict_t = "躲跌✓" if ret < 0 else ("踏空" if ret > 0 else "平")
        else:
            verdict_t = "—"
        behavior_trades.append({"date": r["date"], "ticker": r["ticker"],
                                "action": act, "ret": ret, "verdict": verdict_t})
    # 还在 7 天观察期的操作（含首笔系统荐股 AVGO）——给用户交代"为什么没显示"
    with _conn(p) as con:
        pending = [dict(r) for r in con.execute(
            "SELECT date, ticker, action FROM user_actions "
            "WHERE action IN ('BUY','SELL','TRIM') AND actual_return IS NULL "
            "ORDER BY date DESC"
        ).fetchall()]
    behavior = {
        "n": len(behavior_trades),
        "trades": behavior_trades,
        "pending": pending,   # 未满 7 天、结果待揭晓
        "feedback": get_feedback_accuracy(db_path=p),  # bought/sold/skipped 聚合（仅作参考）
        "symbol_note": "卖出：续跌=对(躲跌)、续涨=踏空(卖飞，绝对收益虽正但属卖错时点)",
    }

    # ── 主动 vs 被动（用户调风投/定投比例的依据，不评判断只比收益）──
    # 定投：机械纪律不算"判断"，只取平均 7 日收益作横向标尺
    with _conn(p) as con:
        dca = [r["return_7d"] for r in con.execute(
            "SELECT return_7d FROM recommendations "
            "WHERE recommendation = '按计划定投' AND return_7d IS NOT NULL"
        ).fetchall()]
    # 主动：你真实买入操作（user_actions BUY）的 7 日实际涨跌，与定投同口径（都是"投进去后涨多少"）
    active_buys = [r["actual_return"] for r in act_rows
                   if r["action"] == "BUY" and r["actual_return"] is not None]
    active_vs_passive = {
        "dca_n": len(dca),
        "dca_avg": round(sum(dca) / len(dca), 2) + 0.0 if dca else None,
        "active_n": len(active_buys),
        "active_avg": round(sum(active_buys) / len(active_buys), 2) + 0.0 if active_buys else None,
    }

    # ── 风险预警：规则分桶 + 每桶事后 7 日表现（个案保留向后兼容）──
    dips = get_dip_stats(days=3650, db_path=p)
    buckets: dict[str, dict] = {}
    for c in dips:
        c["bucket"] = classify_dip(c.get("thesis_intact"), c.get("drop_reason"))
        st = buckets.setdefault(c["bucket"], {"n": 0, "filled": 0, "up": 0, "_sum": 0.0})
        st["n"] += 1
        r7 = c.get("return_7d")
        if r7 is not None:
            st["filled"] += 1
            st["up"] += 1 if r7 > 0 else 0
            st["_sum"] += r7
    for st in buckets.values():
        s = st.pop("_sum")
        # +0.0 归一负零（IEEE: -0.0+0.0==+0.0），防渲染出 "+-0.0%"
        st["avg_ret7"] = round(s / st["filled"], 2) + 0.0 if st["filled"] else None
    dip = {"n": len(dips), "cases": dips, "buckets": buckets}

    # ── 置顶三态结论（确定性）──
    badge = (f"n={n_dir} · {n_tk}票 · {span}天 · 回填{filled}/{total}")
    ci_txt = (f"（95%CI {hit_rate['ci_low']}%~{hit_rate['ci_high']}%）"
              if hit_rate["ci_low"] is not None else "")
    # 方向性信号全为减仓(无买入)时，跑赢/跑输的是"减仓择时"而非"选股能力"，须显式声明
    buy_caveat = ("（注：当前方向性信号全为减仓择时，选股买入类样本 N=0，不代表选股能力）"
                  if n_buy == 0 and n_dir > 0 else "")
    if not gate_passed:
        dir_kind = "减仓" if n_buy == 0 and n_dir > 0 else "买卖混合"
        verdict = {
            "state": 0, "color": "grey",
            "text": (f"⏳ 卡门智投仍在积累战绩：到目前**我们发出的明确买/卖建议**共 {n_dir} 条"
                     f"（不是你的操作次数，是系统每天给每只票打分里方向明确的那些）、"
                     f"覆盖 {n_tk} 只票、约 {span} 天，其中买入类已满 7 天的 {n_buy} 条"
                     f"（方向以{dir_kind}为主），**样本不足以证明能否帮你跑赢躺平**。"
                     f"下方是已有真实记录的诚实记分牌；要等累计到 "
                     f"{GATE_MIN_N} 条买卖建议、覆盖 {GATE_MIN_TICKERS} 只票再下定论。"),
            "badge": badge,
        }
    elif avg_alpha is None:
        # 闸门通过但无 benchmark 可比样本（当前不可达，防御性降级，绝不 round(None)）
        verdict = {
            "state": 0, "color": "grey",
            "text": (f"⏳ 已过样本闸门，但暂无 benchmark 可比样本，无法判定是否跑赢躺平。"
                     f"下方为已有记录明细。"),
            "badge": badge,
        }
    elif avg_alpha < 0:
        # 真·跑输基准：橙（仅此情形可说"不如躺平"）
        verdict = {
            "state": 1, "color": "orange",
            "text": (f"📉 我们的买卖信号·**操作后第7天定格**·平均跑输基准 "
                     f"{abs(round(avg_alpha, 2))}%{ci_txt}，n={n_dir}。"
                     f"⚠️ 这是「信号」的7天表现，**不是你账户的累计盈亏**；"
                     f"我们更看这条曲线随迭代是否回升，逐条对错见下方明细。{buy_caveat}"),
            "badge": badge,
        }
    elif win_rate is not None and win_rate < 0.5:
        # alpha≥0 但命中率<50%：少数大赢+多数小输，不否定已证明的正超额收益
        verdict = {
            "state": 1, "color": "orange",
            "text": (f"⚖️ 我们的买卖信号·7天平均超额为正（**+{round(avg_alpha, 2)}%**），但方向命中率仅 "
                     f"{hit_rate['win_rate']}%{ci_txt}，n={n_dir}——盈亏由少数信号驱动，"
                     f"稳定性待验证（此为信号7天表现，非你账户累计），明细见下方。{buy_caveat}"),
            "badge": badge,
        }
    else:
        # 绿：alpha>0 且命中率≥50%
        wr_txt = f"、命中率 {hit_rate['win_rate']}%" if hit_rate["win_rate"] is not None else ""
        verdict = {
            "state": 2, "color": "green",
            "text": (f"📈 初步证据：我们的买卖信号·**操作后第7天**·平均跑赢基准 +{round(avg_alpha, 2)}%{wr_txt}，"
                     f"n={n_dir}、覆盖 {n_tk} 只票 {span} 天（信号7天表现，非你账户累计盈亏）。样本仍在增长，明细见下。{buy_caveat}"),
            "badge": badge,
        }

    return {
        "gate": gate, "composition": composition, "hit_rate": hit_rate,
        "alpha_by_window": alpha_by_window,
        "buy_alpha": buy_alpha, "combined_alpha": combined_alpha,
        "hold_quality": hold_quality, "shadow_picks": shadow_picks,
        "active_vs_passive": active_vs_passive,
        "behavior": behavior, "dip": dip, "verdict": verdict,
        "data_through": data_through,
    }
