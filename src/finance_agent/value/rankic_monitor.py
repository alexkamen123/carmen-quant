# src/finance_agent/value/rankic_monitor.py
"""P1c 月度 RankIC 自检——把「感觉跑赢」升级为「统计上有没有排序力」。

recommendations 表已有「建议方向 + 7 日收益 + 基准收益」，但从没验证过方向信号本身有没有
排序预测力。月度算一次 Spearman RankIC（方向序 vs 后续超额序），连续 2 个可测月 IC<0.03
→ 推飞书提醒复核策略。纯观测·默认 on·不改任何线上建议（与 oos_monitor 同治理级别）。

诚实核心：样本不足(n<30 或方向性<10)→ insufficient_sample 不下结论；可测月<2 →
insufficient_history 不告警。宁可长期「先不下结论」也不误报，K=2 + 阈值 0.03 防单月噪声。

MVP：只用 recommendations 单表内「建议方向」作待检因子（不跨表 JOIN daily_signals，避免
async/sync 双写链路口径耦合）。P1a/P1b 因子上线后可复用 spearman_rank_ic() 检因子连续值。
"""
from __future__ import annotations

import json
from datetime import date as _date, timedelta
from pathlib import Path

from finance_agent.value.metrics import _is_bearish, _is_bullish

LOG_PATH = Path("data/rankic_log.jsonl")
MIN_N = 30            # 窗口内总样本闸门
MIN_DIRECTIONAL = 10  # 方向性(非持有)样本闸门——IC 需要方向有区分度
TRAILING_DAYS = 180   # 回看窗口
K_CONSECUTIVE = 2     # 连续几个可测月 IC 低于阈值才告警
IC_THRESHOLD = 0.03   # RankIC 及格线（<此值连续 K 月 = 排序力可疑）


def _rank(values: list[float]) -> list[float]:
    """平均秩（并列取平均，1-based）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_rank_ic(x: list[float], y: list[float]) -> float | None:
    """Spearman 秩相关（秩上做皮尔逊）。n<2 或任一侧秩方差为 0 → None（不返 0 假装无关）。"""
    if len(x) != len(y) or len(x) < 2:
        return None
    rx, ry = _rank(x), _rank(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx ** 0.5 * vy ** 0.5), 4)


def _direction_score(rec: str, pc: str) -> int:
    """建议方向打分：看多 +1 / 看空 -1 / 持有观望 0（复用 metrics 同口径）。"""
    if _is_bullish(rec, pc):
        return 1
    if _is_bearish(rec, pc):
        return -1
    return 0


def _default_rows(today: str, db_path, trailing_days: int) -> list[dict]:
    """生产取数：窗口内 return_7d 与 benchmark 均非空的持仓建议行（is_watch=0）。"""
    from finance_agent.db.tracker import _conn, _resolve_db, init_db
    p = _resolve_db(db_path)
    init_db(p)
    since = (_date.fromisoformat(today) - timedelta(days=trailing_days)).isoformat()
    with _conn(p) as con:
        return [dict(r) for r in con.execute(
            """SELECT recommendation, position_change, return_7d, benchmark_return_7d
               FROM recommendations
               WHERE date >= ? AND return_7d IS NOT NULL AND benchmark_return_7d IS NOT NULL
                 AND IFNULL(is_watch, 0) = 0""",
            (since,),
        ).fetchall()]


def monthly_rankic_snapshot(today: str, rows_fn=None, db_path=None,
                            trailing_days: int = TRAILING_DAYS) -> dict:
    """算一次月度 RankIC 快照。rows_fn 供测试注入，缺省走 DB。"""
    rows = rows_fn() if rows_fn else _default_rows(today, db_path, trailing_days)
    pairs = []
    for r in rows:
        ret, bm = r.get("return_7d"), r.get("benchmark_return_7d")
        if ret is None or bm is None:
            continue
        d = _direction_score(r.get("recommendation") or "", r.get("position_change") or "")
        pairs.append((d, ret - bm))
    n = len(pairs)
    n_dir = sum(1 for d, _ in pairs if d != 0)
    if n < MIN_N or n_dir < MIN_DIRECTIONAL:
        return {"date": today, "n": n, "n_directional": n_dir,
                "ic": None, "verdict": "insufficient_sample"}
    ic = spearman_rank_ic([d for d, _ in pairs], [a for _, a in pairs])
    return {"date": today, "n": n, "n_directional": n_dir, "ic": ic,
            "verdict": "measured" if ic is not None else "insufficient_sample"}


def record_rankic_snapshot(snap: dict, log_path: Path = LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")


def rankic_decay_verdict(log_path: Path = LOG_PATH, k: int = K_CONSECUTIVE,
                         threshold: float = IC_THRESHOLD) -> dict:
    """读月度序列，判方向排序力是否衰减。只把 measured 月计入；可测月<k → insufficient_history。"""
    rows = []
    if log_path.exists():
        for ln in log_path.read_text().splitlines():
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    measured = [r for r in sorted(rows, key=lambda r: r["date"])
                if r.get("verdict") == "measured" and r.get("ic") is not None]
    if len(measured) < k:
        return {"status": "insufficient_history", "n_measured": len(measured),
                "k_needed": k, "current_ic": measured[-1]["ic"] if measured else None}
    recent = measured[-k:]
    status = "decaying" if all(r["ic"] < threshold for r in recent) else "healthy"
    return {"status": status, "n_measured": len(measured), "k_needed": k,
            "current_ic": measured[-1]["ic"], "recent_ics": [r["ic"] for r in recent]}


def run_rankic_monitor(today: str | None = None, log_path: Path = LOG_PATH,
                       rows_fn=None, db_path=None) -> dict:
    """月度自检：同月幂等 → 快照 → 记录 → 出衰减裁决。"""
    today = today or _date.today().isoformat()
    if log_path.exists():
        ym = today[:7]
        for ln in log_path.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("date", "")[:7] == ym:
                return {"skipped": "already_recorded_this_month",
                        "decay": rankic_decay_verdict(log_path)}
    snap = monthly_rankic_snapshot(today, rows_fn=rows_fn, db_path=db_path)
    record_rankic_snapshot(snap, log_path)
    return {"snapshot": snap, "decay": rankic_decay_verdict(log_path)}


def build_rankic_alert_card(verdict: dict) -> dict:
    """构建 RankIC 衰减告警飞书卡（v1 interactive，与减仓卡同款结构）。仅 decaying 时用。"""
    ics = "、".join(f"{v:+.3f}" for v in verdict.get("recent_ics", []))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🌡️ 策略复核提醒：建议方向排序力可疑"}},
        "elements": [{
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": (f"连续 {verdict['k_needed']} 个可测月 RankIC 低于 {IC_THRESHOLD}"
                                 f"（近期 IC：{ics}），我们的建议方向与后续超额收益的秩相关偏弱——"
                                 f"**建议人工复核策略是否失效**（纯观测提醒，不自动改任何建议）。")},
        }],
    }


async def notify_if_decaying(verdict: dict, skip_notify: bool = False) -> bool:
    """仅当 decaying 才推飞书卡；healthy/insufficient 静默不刷屏。"""
    if verdict.get("status") != "decaying":
        return False
    card = build_rankic_alert_card(verdict)
    if skip_notify:
        return True
    from finance_agent.notifications.feishu import send_feishu_card
    await send_feishu_card(card, fallback_text="RankIC 衰减：建议人工复核策略")
    return True
