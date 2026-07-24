# src/finance_agent/monthly/playbook.py
"""P2a Shadow Account 操作复盘——user_actions 的 BUY/SELL/TRIM 用 FIFO 配对成
「已平仓交易」算真实盈亏，3 条可解释、样本门控的规则挖行为模式，中文注入月报折叠面板。
不改建议、不改交易，只递镜子（纯观测默认 on，保险丝=样本门控）。

关键取舍：FIFO 而非聚类——几十笔样本上用 KMeans/决策树 = 给噪声找形状，属「绝不碰」；
改用 3 条独立可关、有最小样本门控、人能一句话复述的规则。

诚实铁律：
  - 卖出量超剩余 / 无配对买入 → 丢弃并计数呈现在面板注脚，绝不臆造成本价。
  - 桶内 <MIN_BUCKET_N 笔 或 胜率差 <MIN_DIFF_PP → 该规则沉默（不显著不下结论）。
  - 已平仓 <MIN_CLOSED_TRADES 笔 → 整个剧本返 None，月报不出面板（不硬凑）。
  - 措辞描述统计事实（递镜子），不说教、不给命令式指令。
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date as _date

MIN_CLOSED_TRADES = 5   # 物理卖出退出次数少于此 → 剧本闭嘴（切片数不算：单次定投清仓拆5切片仍=1次）
MIN_BUCKET_N = 5        # 规则分桶任一桶少于此 → 该规则沉默
MIN_DIFF_PP = 15.0      # 两桶胜率差(百分点)低于此 → 沉默（可读性门槛·非统计显著性检验）
HOLD_SPLIT_DAYS = 30    # 持有期分桶线：短持 <30 天 / 长持 ≥30 天（可读常量·非调参）

# 名义额折美元（与 CLAUDE.md/value/cumulative.py 同口径），只用于仓位分桶，防止
# 港币量级(×7.8)被误当成「大仓位」——审查 CONFIRMED：币种伪影会伪装成仓位效应。
_CCY_USD = {"us": 1.0, "hk": 1.0 / 7.8, "cn": 1.0 / 7.2}


def _ticker_fx(ticker: str) -> float:
    """按本项目代码约定推断币种：纯数字=港股(5位,00700)/A股(6位)，其余美股。"""
    t = (ticker or "").strip()
    if t.isdigit():
        return _CCY_USD["cn"] if len(t) == 6 else _CCY_USD["hk"]
    return _CCY_USD["us"]


def pair_fifo_trades(actions: list[dict]) -> tuple[list[dict], int]:
    """FIFO 配对：BUY 入队，SELL/TRIM 从队头消耗，产出已平仓交易列表。
    输入须按时间升序（get_actions_for_pairing 保证）。
    返回 (trades, dropped)：dropped = 全部或部分无法配对/定价的卖出笔数。

    审查 CONFIRMED 后的对齐语义（缺数据行绝不能静默跳过，否则队列错位、后续卖出
    配到「其实已卖掉」的批次报出自信错误的盈亏）：
      - 缺价 BUY：入队占位（消耗到它时不产出盈亏、计 dropped），保持批次顺序。
      - 缺价 SELL/TRIM：照常消耗队头但不产出盈亏，计 dropped。
      - 缺股数（任何动作）：队列对齐没法维持 → 该票从此整票停记（宁缺勿假），
        其后卖出计 dropped、买入忽略；此前已产出的配对仍有效。"""
    queues: dict[str, deque] = defaultdict(deque)
    polluted: set[str] = set()
    trades: list[dict] = []
    dropped = 0
    for a in actions:
        act = (a.get("action") or "").upper()
        if act not in ("BUY", "SELL", "TRIM"):
            continue
        tk = a["ticker"]
        sh, px = a.get("shares"), a.get("price")
        px = float(px) if px else None
        if px is not None and px <= 0:
            px = None
        if tk in polluted:
            if act in ("SELL", "TRIM"):
                dropped += 1
            continue
        if not sh or float(sh) <= 0:
            polluted.add(tk)
            if act in ("SELL", "TRIM"):
                dropped += 1
            continue
        sh = float(sh)
        if act == "BUY":
            queues[tk].append({"shares": sh, "price": px, "date": a["date"]})
            continue
        # SELL / TRIM：从队头逐批消耗（无论本笔有没有价，都要消耗以保对齐）
        remaining = sh
        q = queues[tk]
        unpriceable = False
        while remaining > 1e-9 and q:
            lot = q[0]
            take = min(lot["shares"], remaining)
            if px is not None and lot["price"] is not None:
                trades.append({
                    "ticker": tk,
                    "shares": take,
                    "buy_date": lot["date"],
                    "sell_date": a["date"],
                    "buy_price": lot["price"],
                    "sell_price": px,
                    "pnl_pct": round((px / lot["price"] - 1) * 100, 1),
                    "holding_days": (_date.fromisoformat(a["date"])
                                     - _date.fromisoformat(lot["date"])).days,
                    "exit_type": act,
                })
            else:
                unpriceable = True   # 批次或卖出缺价：消耗但不产出，绝不臆造
            lot["shares"] -= take
            remaining -= take
            if lot["shares"] <= 1e-9:
                q.popleft()
        if remaining > 1e-9 or unpriceable:
            dropped += 1   # 残量无配对 或 有无法定价的部分——计数呈现在面板注脚
    return trades, dropped


def _win_rate(bucket: list[dict]) -> float:
    return round(sum(1 for t in bucket if t["pnl_pct"] > 0) / len(bucket) * 100, 1)


def _bucket_finding(name: str, label_a: str, bucket_a: list[dict],
                    label_b: str, bucket_b: list[dict]) -> str | None:
    """两桶对比：任一桶 <MIN_BUCKET_N 或差 <MIN_DIFF_PP → None（沉默）。措辞只描述事实。"""
    if len(bucket_a) < MIN_BUCKET_N or len(bucket_b) < MIN_BUCKET_N:
        return None
    wa, wb = _win_rate(bucket_a), _win_rate(bucket_b)
    if abs(wa - wb) < MIN_DIFF_PP:
        return None
    return (f"{name}：{label_a}胜率 {wa:.0f}%（{len(bucket_a)}笔）"
            f" vs {label_b} {wb:.0f}%（{len(bucket_b)}笔），差 {abs(wa - wb):.0f}pp")


def rule_findings(trades: list[dict]) -> list[str]:
    """3 条规则：持有期 / 退出方式 / 仓位大小。每条独立、可一句话复述、样本门控。"""
    findings = []
    # 规则1 持有期
    short = [t for t in trades if t["holding_days"] < HOLD_SPLIT_DAYS]
    long_ = [t for t in trades if t["holding_days"] >= HOLD_SPLIT_DAYS]
    f = _bucket_finding("持有期", f"短持(<{HOLD_SPLIT_DAYS}天)", short,
                        f"长持(≥{HOLD_SPLIT_DAYS}天)", long_)
    if f:
        findings.append(f)
    # 规则2 退出方式
    sells = [t for t in trades if t["exit_type"] == "SELL"]
    trims = [t for t in trades if t["exit_type"] == "TRIM"]
    f = _bucket_finding("退出方式", "清仓(SELL)", sells, "减仓(TRIM)", trims)
    if f:
        findings.append(f)
    # 规则3 仓位大小（名义额折美元后按中位数分桶——币种量级不得伪装成仓位效应）
    if trades:
        def _usd_notional(t: dict) -> float:
            # round 到美分：防浮点误差(7800×1/7.8=999.99…)把等值名义额劈成两桶
            return round(t["shares"] * t["buy_price"] * _ticker_fx(t["ticker"]), 2)
        notionals = sorted(_usd_notional(t) for t in trades)
        median = notionals[len(notionals) // 2]
        big = [t for t in trades if _usd_notional(t) >= median]
        small = [t for t in trades if _usd_notional(t) < median]
        f = _bucket_finding("仓位大小", "大单(≥中位)", big, "小单(<中位)", small)
        if f:
            findings.append(f)
    return findings


def build_shadow_playbook(actions: list[dict]) -> dict | None:
    """FIFO 配对 + 汇总 + 规则发现。物理退出 <MIN_CLOSED_TRADES 次 → None（整体闭嘴）。
    审查采纳：门按物理卖出事件计而非批次切片——定投仓一次清仓拆 5 个切片仍只算 1 次
    决策，单次决策不能解锁面板、也不能在样本量上伪装成 5 笔。"""
    trades, dropped = pair_fifo_trades(actions)
    n_exits = len({(t["ticker"], t["sell_date"], t["exit_type"]) for t in trades})
    if n_exits < MIN_CLOSED_TRADES:
        return None
    n = len(trades)
    return {
        "n_closed": n,          # 批次切片数（FIFO 逐 lot 口径，同券商税务 lot 级已实现盈亏）
        "n_exits": n_exits,     # 物理卖出决策次数（闭嘴门与标题按此计）
        "win_rate": _win_rate(trades),
        "avg_pnl": round(sum(t["pnl_pct"] for t in trades) / n, 1),
        "dropped": dropped,
        "findings": rule_findings(trades),
        "trades": trades,
    }


def playbook_panel(play: dict | None) -> dict | None:
    """月报折叠面板：主屏只显标题，点开看真实盈亏与模式。play=None → None（不出面板）。"""
    if not play:
        return None
    from finance_agent.notifications.cards import collapsible_panel
    lines = [
        f"卖出退出 **{play['n_exits']}** 次，FIFO 拆成 **{play['n_closed']}** 个批次切片"
        f" · 切片胜率 **{play['win_rate']:.0f}%** · 切片均盈亏 **{play['avg_pnl']:+.1f}%**\n"
        f"（切片胜率＝你已平仓批次里真金实盈的比例，非模型信号方向对错；按已记录成交价配对，"
        f"auto 检测行用检测日近似价，非成交回单）",
    ]
    if play["findings"]:
        lines.append("")
        lines.append("**模式发现**（描述性统计·门槛=桶≥5笔且差≥15pp，是可读性约定非统计检验）：")
        lines += [f"· {f}" for f in play["findings"]]
    else:
        lines.append("")
        lines.append("本期无可报告的行为模式（分桶样本<5笔或胜率差<15pp，门槛内不下结论）")
    lines.append("")
    lines.append("**逐笔明细**：")
    for t in play["trades"]:
        lines.append(
            f"{'🟢' if t['pnl_pct'] > 0 else '🔴'} {t['ticker']}"
            f" {t['shares']:g}股 {t['buy_price']:g}→{t['sell_price']:g}"
            f" · {t['pnl_pct']:+.1f}% · 持有{t['holding_days']}天 · {t['exit_type']}")
    if play["dropped"]:
        lines.append("")
        lines.append(f"⚠️ 另有 {play['dropped']} 笔卖出存在无法配对/无法定价的部分，"
                     "该部分未计入上述统计（追踪前建仓或记录缺价——不臆造成本价，宁缺勿假）")
    lines.append("")
    lines.append("_以上为系统依据当前数据给出的明确建议，仓位调整请结合自身风险承受能力执行_")
    title = (f"**📒 操作剧本 · 卖出退出 {play['n_exits']} 次"
             f" · 切片胜率 {play['win_rate']:.0f}%**　_点开看盈亏与模式_")
    return collapsible_panel(title, "\n".join(lines))
