# src/finance_agent/monthly/scorecard.py
"""
月度行为复盘：逐笔反事实复盘 + 5 维行为打分。

数据全部来自 P1/P2 已落地的真实记录：
  - 逐笔复盘读 user_actions.actual_return（P1 回填）
  - 行为打分喂入推荐准确率 + 操作反馈 + 指导执行率（P2）+ 三桶漂移（P2）
"""
import json

from finance_agent.agents.claude_client import claude_cli_chat, has_claude_cli, strip_markdown
from finance_agent.agents.bull_agent import deepseek_chat
from finance_agent.db.tracker import get_period_actions


# ── 1. 逐笔反事实复盘（规则化）────────────────────────────────

_GOOD, _BAD, _FLAT = "✅", "❌", "⚠️"


def _grade_trade(action: str, ret: float) -> tuple[str, str]:
    """根据操作方向 + 远期收益给评级 + 反事实文案。"""
    act = (action or "").upper()
    if act == "BUY":
        if ret > 2.0:
            return _GOOD, f"买对了，赚 {ret:+.1f}%"
        if ret < -2.0:
            return _BAD, f"买亏了，不买可省 {abs(ret):.1f}%"
        return _FLAT, "基本打平"
    if act in ("SELL", "TRIM"):
        # SELL 的 actual_return 是卖出后远期收益：负=躲过下跌（卖得好）
        if ret < -2.0:
            return _GOOD, f"卖对了，躲过 {abs(ret):.1f}% 下跌"
        if ret > 2.0:
            return _BAD, f"卖早了，不卖多赚 {ret:+.1f}%"
        return _FLAT, "基本打平"
    return _FLAT, ""


def build_trade_review(since: str, until: str,
                       db_path: str | None = None) -> list[dict]:
    """
    拉 [since, until] 内已回填收益的操作，逐笔评级 + 反事实。
    返回 [{date, ticker, action, shares, price, actual_return, grade, note}]。
    """
    rows = get_period_actions(since, until, db_path=db_path)
    reviewed = []
    for r in rows:
        ret = r.get("actual_return")
        if ret is None:
            continue
        grade, note = _grade_trade(r["action"], ret)
        reviewed.append({
            "date": r["date"], "ticker": r["ticker"], "action": r["action"],
            "shares": r["shares"], "price": r["price"],
            "actual_return": ret, "grade": grade, "note": note,
        })
    return reviewed


def trade_review_summary(reviewed: list[dict]) -> str:
    """把逐笔复盘压成一段文字，供打分 LLM 参考。"""
    if not reviewed:
        return "本月无已回填的操作记录"
    lines = []
    for t in reviewed:
        lines.append(
            f"{t['grade']} {t['ticker']} {t['action']} → 7日{t['actual_return']:+.1f}%（{t['note']}）"
        )
    return "\n".join(lines)


# ── 2. 5 维行为打分（LLM）─────────────────────────────────────

SCORECARD_SYSTEM = """你是一位严格但建设性的投资行为复盘教练。
根据用户上月的真实数据，给 5 个固定维度打分（0-10 整数）并各给一句理由。

5 个维度（顺序固定）：
1. 执行纪律 —— 是否按既定指导/计划执行，有无频繁变向、来回折腾
2. 风险管理 —— 对冲是否到位、是否触碰集中度红线
3. 择时能力 —— 买卖点位的实际效果（买入后涨跌、卖出是否躲跌）
4. 仓位管理 —— 单票/板块集中度、三桶是否均衡
5. 策略一致性 —— 实际操作与既定三桶策略的吻合度

评分锚点：8-10 优秀，5-7 及格，2-4 偏差较大，0-1 严重失控。
理由要具体、点名数据或标的，像教练当面讲，不超过 30 字。

只输出 JSON（不要 markdown 代码块、不要前后缀）：
{
  "dimensions": [
    {"name": "执行纪律", "score": 4, "reason": "..."},
    {"name": "风险管理", "score": 3, "reason": "..."},
    {"name": "择时能力", "score": 5, "reason": "..."},
    {"name": "仓位管理", "score": 4, "reason": "..."},
    {"name": "策略一致性", "score": 2, "reason": "..."}
  ],
  "overall": "一句话总评，点出最该改的一点"
}"""

SCORECARD_USER = """上月（{month}）行为数据：

【推荐准确率】{overall_acc}

【你的操作反馈】{feedback}

【指导执行情况】已照做 {g_followed} 条 / 未执行过期 {g_expired} 条 / 进行中 {g_open} 条

【三桶配置漂移】{drift}

【逐笔操作复盘】
{trade_review}

请据此给 5 维行为打分。"""

_FALLBACK_SCORECARD = {
    "dimensions": [
        {"name": n, "score": 5, "reason": "数据不足，给中性分"}
        for n in ("执行纪律", "风险管理", "择时能力", "仓位管理", "策略一致性")
    ],
    "overall": "数据不足，本月评分仅供参考",
}


def _parse_scorecard(raw: str) -> dict:
    text = strip_markdown(raw)
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return dict(_FALLBACK_SCORECARD)
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return dict(_FALLBACK_SCORECARD)
    # 结构校验：dimensions 必须是非空 list；逐元素清洗，保证返回结构始终合法
    dims = data.get("dimensions") if isinstance(data, dict) else None
    if not isinstance(dims, list) or not dims:
        return dict(_FALLBACK_SCORECARD)
    clean = []
    for d in dims:
        if not isinstance(d, dict):
            continue  # 跳过畸形元素（如字符串/数字）
        d.setdefault("name", "未知维度")
        d.setdefault("reason", "")
        try:
            d["score"] = max(0, min(10, int(round(float(d.get("score", 5))))))
        except (TypeError, ValueError):
            d["score"] = 5
        clean.append(d)
    if not clean:
        return dict(_FALLBACK_SCORECARD)
    data["dimensions"] = clean
    return data


async def generate_scorecard(context: dict) -> dict:
    """
    context: {month, overall_acc, feedback, g_followed, g_expired, g_open, drift, trade_review}
    返回 {dimensions:[{name,score,reason}], overall}。LLM 失败则返回中性兜底。
    """
    user_msg = SCORECARD_USER.format(
        month=context.get("month", ""),
        overall_acc=context.get("overall_acc", "暂无"),
        feedback=context.get("feedback", "暂无"),
        g_followed=context.get("g_followed", 0),
        g_expired=context.get("g_expired", 0),
        g_open=context.get("g_open", 0),
        drift=context.get("drift", "暂无（本月未跑周报）"),
        trade_review=context.get("trade_review", "暂无"),
    )
    try:
        if has_claude_cli():
            raw = await claude_cli_chat(SCORECARD_SYSTEM, user_msg, timeout=90)
        else:
            raw = await deepseek_chat(SCORECARD_SYSTEM, user_msg)
    except Exception as e:
        print(f"[Scorecard] LLM 失败，用中性兜底: {e}")
        return dict(_FALLBACK_SCORECARD)
    return _parse_scorecard(raw)
