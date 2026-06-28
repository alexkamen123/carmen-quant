# src/finance_agent/signals/opportunities.py
"""
今日机会扫描（task#31）：在因子库 42 票宇宙里找"此刻正在触发"的高信赖信号，
渲染进日报卡片顶部——把"今日无买卖建议"换成有证据的机会清单。

诚实边界（渲染时同屏给出）：
  - 跑赢SPY比例/超额是 2 年区间的 in-sample 回测（7 日窗口），影子池在实战验证中
  - 已持仓票不进机会区（加仓时机由 PM 逐股裁决，不重复轰炸）
  - 半导体票标注红线余量；机会区只给证据，不替用户下单
"""
from finance_agent.backtest.signal_lookup import _load_valid_map, get_active_signals

MAX_SHOW = 5          # 卡片只展示 Top N（按最强 t 值），防信息轰炸
SEMI_NOTE_SECTORS = "半导体"   # 红线提示用


def scan_opportunities(exclude_tickers: set[str] | None = None,
                       watch_tickers: set[str] | None = None) -> list[dict]:
    """
    扫描因子库宇宙，返回触发中的高信赖信号（按票聚合，取每票最强信号排序）。
    exclude_tickers：已持仓票（机会区只放"新机会"）。
    watch_tickers：观察池票（标注"已在观察池"，有 PM 裁决与影子计分）。
    每项：{ticker, in_watch, signals: [{label, strategy, n_signals, avg_alpha, beat_rate, t_stat}]}
    """
    exclude = {t.upper() for t in (exclude_tickers or set())}
    watch = {t.upper() for t in (watch_tickers or set())}
    universe = sorted({t for (t, _) in _load_valid_map().keys()})

    out = []
    for tk in universe:
        if tk.upper() in exclude:
            continue
        try:
            active = [a for a in get_active_signals(tk) if a.get("reliable")]
        except Exception:
            continue   # 单票取数失败不拖垮整体扫描
        if active:
            out.append({"ticker": tk, "in_watch": tk.upper() in watch,
                        "signals": active})
    out.sort(key=lambda o: -max(s["t_stat"] for s in o["signals"]))
    return out


def format_opportunity_section(opps: list[dict], semi_room_note: str = "") -> str:
    """渲染卡片"今日机会"区文本；无触发时返回 ""（不占卡片）。"""
    if not opps:
        return ""
    lines = ["**🎯 今日机会（高信赖信号触发中，非持仓票）**"]
    for o in opps[:MAX_SHOW]:
        s = o["signals"][0]   # 每票只展示最强一条，其余计数
        more = f"（另 {len(o['signals']) - 1} 个信号）" if len(o["signals"]) > 1 else ""
        tag = "｜已在观察池" if o["in_watch"] else ""
        role = f" {s['role']}" if s.get("role") else ""   # 信号性格标签（纯展示）
        lines.append(
            f"· **{o['ticker']}** {s['label']}{role}：历史{s['n_signals']}次 · "
            f"7日跑赢SPY {s['beat_rate'] * 100:.0f}% · 超额{s['avg_alpha']:+.1f}% · "
            f"t={s['t_stat']}{more}{tag}"
        )
    if len(opps) > MAX_SHOW:
        lines.append(f"_……另有 {len(opps) - MAX_SHOW} 只触发中（按证据强度截断展示）_")
    if semi_room_note:
        lines.append(semi_room_note)
    lines.append("_口径：2年in-sample回测、7日窗口；影子池实战验证中；不构成下单指令_")
    return "\n".join(lines)
