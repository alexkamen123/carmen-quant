# src/finance_agent/monthly/review.py
"""
月度投资回顾：每月第一个交易日自动运行。

内容：
  1. 上月推荐准确率（按股票拆分）
  2. 哪种投资视角（价值/成长/动量）正确率最高
  3. 哪些 thesis 本月受到了挑战
  4. Claude 生成 3-5 句月度总结与下月重点关注

输出：飞书卡片（indigo 配色）
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.db.tracker import (
    _resolve_db, _CREATE_SQL, feedback_summary, backfill_action_returns,
    guidance_month_summary, check_guidance_adherence,
)
from finance_agent.monthly.scorecard import (
    build_trade_review, trade_review_summary, generate_scorecard,
)
from finance_agent.notifications.cards import collapsible_panel


def _trade_review_panel(tr_lines: list[str]) -> dict:
    """逐笔复盘收进折叠：tr_lines[0]=标题进 header、其余 per-trade 明细进 content（减冗余）。
    主屏已留总评/行为评分/准确率，逐笔细节点开再看。"""
    title = tr_lines[0] if tr_lines else "**🔍 逐笔复盘**"
    body = "\n".join(tr_lines[1:]) if len(tr_lines) > 1 else "本月无已回填收益的操作"
    return collapsible_panel(f"{title}（点开看每笔）", body)


REVIEW_SYSTEM = """你是一位家庭投资组合的月度复盘顾问。
根据上月的推荐记录和准确率数据，给出简洁的月度总结。

要求：
- 纯文字，3-5 句话，不超过 150 字
- 禁止使用 markdown 表格
- 第1句：量化总结（准确率数字 + 哪些建议正确/错误）
- 第2-3句：最值得反思的判断失误（或值得延续的判断亮点）
- 最后1句：下月最需要关注的 1-2 个风险点或机会"""

REVIEW_USER = """上月（{month}）推荐统计：

总体准确率：{overall_acc}
各股票准确率：
{ticker_acc}

准确的推荐（样本）：
{correct_samples}

错误的推荐（样本）：
{wrong_samples}

【用户实际操作反馈】：
{feedback_summary}

当前持仓的 Thesis 状态：
{thesis_notes}

请给出 3-5 句月度复盘总结。"""


@contextmanager
def _conn(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _get_last_month_stats(db_path: Path) -> dict:
    """从 recommendations 表获取上月统计数据"""
    today = datetime.today()
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    since = last_month_start.strftime("%Y-%m-%d")
    until = last_month_end.strftime("%Y-%m-%d")
    month_label = last_month_start.strftime("%Y年%m月")

    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT ticker, recommendation, position_change, outcome, return_7d "
            "FROM recommendations "
            "WHERE date >= ? AND date <= ? AND outcome IS NOT NULL",
            (since, until),
        ).fetchall()

    if not rows:
        return {"month": month_label, "since": since, "until": until, "empty": True}

    rows = [dict(r) for r in rows]
    correct = [r for r in rows if r["outcome"] == "正确"]
    wrong   = [r for r in rows if r["outcome"] == "错误"]
    total   = len([r for r in rows if r["outcome"] in ("正确", "错误")])
    acc_pct = round(len(correct) / total * 100) if total > 0 else 0

    # 按 ticker 分组准确率
    by_ticker: dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        if t not in by_ticker:
            by_ticker[t] = {"correct": 0, "wrong": 0}
        if r["outcome"] == "正确":
            by_ticker[t]["correct"] += 1
        elif r["outcome"] == "错误":
            by_ticker[t]["wrong"] += 1

    ticker_acc_lines = []
    for t, v in sorted(by_ticker.items()):
        ttl = v["correct"] + v["wrong"]
        pct = round(v["correct"] / ttl * 100) if ttl > 0 else 0
        ticker_acc_lines.append(f"  {t}: {pct}%（{v['correct']}✅ {v['wrong']}❌）")

    # 样本
    correct_samples = "\n".join(
        f"  {r['ticker']} {r['recommendation']}（7日涨跌={r['return_7d']:+.1f}%）"
        for r in correct[:3]
    ) or "无"
    wrong_samples = "\n".join(
        f"  {r['ticker']} {r['recommendation']}（7日涨跌={r['return_7d']:+.1f}%）"
        for r in wrong[:3]
    ) or "无"

    return {
        "month": month_label,
        "since": since,
        "until": until,
        "overall_acc": f"{acc_pct}%（{len(correct)}✅ {len(wrong)}❌ / 共{total}条）",
        "ticker_acc": "\n".join(ticker_acc_lines),
        "correct_samples": correct_samples,
        "wrong_samples": wrong_samples,
        "empty": False,
    }


def _get_thesis_notes(db_path: Path) -> str:
    """列出当前各持仓 thesis 的前 60 字（概况），供 Claude 参考"""
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT ticker, SUBSTR(thesis_text, 1, 60) AS preview FROM theses ORDER BY ticker"
        ).fetchall()
    if not rows:
        return "暂无持仓逻辑记录"
    return "\n".join(f"  {r['ticker']}: {r['preview']}..." for r in rows)


def _load_cached_drift() -> str:
    """读 data/weekly_latest.json 的 drift_rows，压成一行；无则返回空串。"""
    try:
        path = Path("data/weekly_latest.json")
        if not path.exists():
            return ""
        rows = json.loads(path.read_text()).get("drift_rows", [])
        if not rows:
            return ""
        return " | ".join(
            f"{r['bucket']} {r['current_pct']:.0f}%/{r['target_pct']:.0f}%{r['status']}"
            for r in rows
        )
    except Exception:
        return ""


def _scorecard_bar(score: int) -> str:
    """0-10 分 → 进度条，如 4 → ████░░░░░░"""
    s = max(0, min(10, int(score)))
    return "█" * s + "░" * (10 - s)


async def run_monthly_review(db_path_str: str = "data/agent.db") -> tuple[dict, str, dict, dict] | None:
    """
    生成月度回顾，返回 (飞书卡片 dict, AI复盘文本, stats dict, scorecard dict)。
    若上月无数据则返回 None。
    """
    db_path = _resolve_db(db_path_str)

    # 先回填本月内 BUY 操作的 7 日涨跌（异步，给反馈统计提供数据）
    await backfill_action_returns(db_path)

    stats = _get_last_month_stats(db_path)
    if stats.get("empty"):
        print(f"[MonthlyReview] 上月（{stats.get('month', '?')}）无已回填推荐数据，跳过")
        return None

    thesis_notes = _get_thesis_notes(db_path)
    fb_summary = feedback_summary(db_path) or "暂无足够操作记录"

    user_msg = REVIEW_USER.format(
        month=stats["month"],
        overall_acc=stats["overall_acc"],
        ticker_acc=stats["ticker_acc"],
        correct_samples=stats["correct_samples"],
        wrong_samples=stats["wrong_samples"],
        feedback_summary=fb_summary,
        thesis_notes=thesis_notes,
    )

    try:
        if has_claude_cli():
            summary = await claude_cli_chat(REVIEW_SYSTEM, user_msg, timeout=90)
        else:
            raise RuntimeError("无 Claude CLI")
    except Exception as e:
        print(f"[MonthlyReview] Claude 失败，降级 DeepSeek: {e}")
        summary = await deepseek_chat(REVIEW_SYSTEM, user_msg)

    # ── P3: 逐笔反事实复盘 + 5 维行为打分 ──
    reviewed = build_trade_review(stats["since"], stats["until"], db_path=str(db_path))
    # 先刷新历史指导执行状态（月报可能晚于下次周报，否则计数陈旧），再读计数
    check_guidance_adherence(db_path=str(db_path))
    g_counts = guidance_month_summary(stats["since"], stats["until"], db_path=str(db_path))
    drift_str = _load_cached_drift() or "暂无（本月未跑周报）"
    scorecard = await generate_scorecard({
        "month": stats["month"],
        "overall_acc": stats["overall_acc"],
        "feedback": fb_summary,
        "g_followed": g_counts["followed"],
        "g_expired": g_counts["expired"],
        "g_open": g_counts["open"],
        "drift": drift_str,
        "trade_review": trade_review_summary(reviewed),
    })

    # 行为评分区块
    sc_lines = ["**🎯 行为评分**"]
    for d in scorecard.get("dimensions", []):
        sc_lines.append(
            f"{d['name']}　{_scorecard_bar(d['score'])} {d['score']}/10\n　{d.get('reason', '')}"
        )
    if scorecard.get("overall"):
        sc_lines.append(f"\n**总评：** {scorecard['overall']}")

    # 逐笔复盘区块
    if reviewed:
        tr_lines = ["**🔍 逐笔复盘**（已回填收益的操作）"]
        for t in reviewed:
            price = f"@{t['price']:g}" if t.get("price") else ""
            sh = f"{t['shares']:g}股" if t.get("shares") is not None else ""
            tr_lines.append(
                f"{t['grade']} **{t['ticker']}** {t['action']} {sh}{price}"
                f" → 7日 {t['actual_return']:+.1f}% · {t['note']}"
            )
    else:
        tr_lines = ["**🔍 逐笔复盘**", "本月无已回填收益的操作"]

    # ── P2a Shadow Account 操作剧本（纯观测·guarded·样本不足自动闭嘴不出面板）──
    playbook_el = None
    try:
        from finance_agent.db.tracker import get_actions_for_pairing
        from finance_agent.monthly.playbook import build_shadow_playbook, playbook_panel
        playbook_el = playbook_panel(
            build_shadow_playbook(get_actions_for_pairing(db_path=str(db_path))))
        if playbook_el:
            print("[Playbook] 操作剧本已生成（FIFO 已平仓复盘）")
    except Exception as e:
        print(f"[Playbook] 操作剧本生成失败（跳过，不影响月报）: {e}")

    # ── 本月一句话 TL;DR（置顶：总评 + 行为均分）──
    _dims = scorecard.get("dimensions", [])
    _avg = round(sum(d["score"] for d in _dims) / len(_dims), 1) if _dims else 0
    tldr_content = f"**📋 本月一句话**\n{scorecard.get('overall', '')}（行为均分 {_avg}/10）"

    # ── 构建飞书卡片 ──
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📆 卡门智投 · {stats['month']}月度回顾"},
            "template": "indigo",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": tldr_content}},
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**整体准确率：** {stats['overall_acc']}\n\n"
                        f"**各股票：**\n{stats['ticker_acc']}"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🔁 **你的操作 vs 模型**\n\n{fb_summary}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(sc_lines)},
            },
            {"tag": "hr"},
            _trade_review_panel(tr_lines),
            # P2a 操作剧本折叠面板（None=样本不足闭嘴，列表推导自动滤掉）
            *([{"tag": "hr"}, playbook_el] if playbook_el else []),
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"💡 **月度复盘**\n\n{summary.strip()}"},
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text",
                               "content": "以上为系统依据当前数据给出的明确建议，仓位调整请结合自身风险承受能力执行"}],
            },
        ],
    }
    return card, summary, stats, scorecard
