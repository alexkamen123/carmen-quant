# src/finance_agent/ops/health.py
"""
调度心跳自检（task#33）：检查每个 launchd 任务是否还活着，把"静默死亡"类故障
从"等用户发现"变成"系统自己报"。

教训来源（2026-06-09→06-12）：调度整顿关停 GitHub price_alert 时没做覆盖核对，
盘中价格预警哑火 3 天才被用户问出来——同类故障必须在 24 小时内自动暴露。

两层检查：
  1. launchctl 已加载（任务根本没挂上 = 红）
  2. 日志新鲜度（mtime 超过该任务的预期最大间隔 = 红；日志不存在 = 红，
     可能重启后 /tmp 被清且任务未跑）
健康 = 完全静默（不轰炸）；任何异常 → 一张飞书卡列全部问题。
"""
import subprocess
from datetime import datetime
from pathlib import Path

# {launchd label 短名: (日志路径, 最大允许静默小时数)}
# 间隔按"调度周期 + 周末/节假日空窗 + 余量"取保守上界：
#   pricescan 每15分钟常跑（休市也会落 skip 日志）→ 1h
#   工作日任务最长空窗=周五→周一 ≈ 72h → 76h
#   followup 周二-五：周五→周二 ≈ 96h → 100h；weekly/value 周一次 → 8天；monthly → 32天
HEALTH_RULES: dict[str, tuple[str, float]] = {
    "carmen-pricescan": ("/tmp/carmen-pricescan.log", 1),
    "carmen-morning":   ("/tmp/carmen-morning.log", 76),
    "carmen-evening":   ("/tmp/carmen-evening.log", 76),
    "carmen-news":      ("/tmp/carmen-news.log", 76),
    "carmen-earnings":  ("/tmp/carmen-earnings.log", 76),
    "carmen-followup":  ("/tmp/carmen-followup.log", 100),
    "carmen-weekly":    ("/tmp/carmen-weekly.log", 8 * 24),
    "carmen-value":     ("/tmp/carmen-value.log", 8 * 24),
    "carmen-monthly":   ("/tmp/carmen-monthly.log", 32 * 24),
}


def _loaded_labels() -> set[str]:
    """launchctl list 里当前已加载的 carmen 任务短名。失败返回 None 语义由调用方处理。"""
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    return {line.rsplit(".", 1)[-1] for line in out.splitlines() if "carmen-" in line}


def check_scheduler_health(rules: dict | None = None,
                           loaded: set[str] | None = None,
                           now: datetime | None = None) -> list[dict]:
    """返回问题列表（空 = 全部健康）。每项 {job, kind, detail}。"""
    rules = rules if rules is not None else HEALTH_RULES
    if loaded is None:
        try:
            loaded = _loaded_labels()
        except Exception:
            loaded = None   # launchctl 不可用（如测试环境）→ 跳过加载检查
    now = now or datetime.now()

    problems = []
    for job, (log_path, max_hours) in rules.items():
        if loaded is not None and job not in loaded:
            problems.append({"job": job, "kind": "未加载",
                             "detail": "launchctl list 里不存在，任务可能被卸载"})
            continue
        p = Path(log_path)
        if not p.exists():
            problems.append({"job": job, "kind": "无日志",
                             "detail": f"{log_path} 不存在（重启后未跑过？）"})
            continue
        age_h = (now - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 3600
        if age_h > max_hours:
            problems.append({"job": job, "kind": "心跳超时",
                             "detail": f"日志 {age_h:.1f}h 未更新（上限 {max_hours:g}h）"})
    return problems


def build_health_card(problems: list[dict]) -> dict:
    lines = [f"· **{p['job']}**（{p['kind']}）：{p['detail']}" for p in problems]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                              "content": f"🩺 调度心跳异常 · {len(problems)} 项"},
                   "template": "red"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "排查：launchctl list | grep carmen；日志在 /tmp/carmen-*.log；"
                           "修复后无需操作，健康时本卡不出现"}]},
        ],
    }


async def run_health_check(skip_notify: bool = False) -> list[dict]:
    """CLI 入口：异常才推卡（健康=完全静默，不轰炸）。"""
    problems = check_scheduler_health()
    if not problems:
        print(f"[Health] {len(HEALTH_RULES)} 个调度任务心跳全部正常")
        return []
    for p in problems:
        print(f"[Health] ⚠️ {p['job']} {p['kind']}: {p['detail']}")
    if not skip_notify:
        from finance_agent.notifications.feishu import send_feishu_card
        await send_feishu_card(build_health_card(problems))
    return problems
