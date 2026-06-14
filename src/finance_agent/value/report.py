# src/finance_agent/value/report.py
"""
价值证明报告：把 compute_value_metrics 的结果渲染成飞书卡片 + 纯文本。
置顶三态结论（灰/橙/绿）由数据确定性生成，零 LLM 叙事。
"""
from finance_agent.db.tracker import (
    _resolve_db, fill_7d_returns, fill_long_returns, backfill_action_returns,
    backfill_dip_outcomes, backfill_market,
)
from finance_agent.value.metrics import (
    compute_value_metrics,
    DIP_BUCKET_OPPORTUNITY, DIP_BUCKET_BROKEN, DIP_BUCKET_WATCH,
)
from finance_agent.value.strategy_scorecard import compute_strategy_edge, format_strategy_edge_section

_TEMPLATE = {"grey": "grey", "orange": "orange", "green": "green"}


def _adv_section(m: dict) -> str:
    """建议价值段——全大白话，不用 alpha/命中率/CI 等黑话（用户反馈看不懂）。
    口径不变，只换说法：alpha→比大盘多赚/少赚；命中率→说对几次。"""
    hr, ba, ca = m["hit_rate"], m["buy_alpha"], m["combined_alpha"]
    lines = ["**🎯 我们的建议准不准**"]

    # 1. 买卖建议说对几次（原"方向命中率"）
    if hr["n_judged"]:
        warn = (f"　⚠️ 但只有 {hr['n_tickers']} 只票、样本太少，这个数字还会大幅波动，先别当真"
                if hr["n_judged"] < 30 else "")
        lines.append(
            f"• **买卖建议**：{hr['n_judged']} 次明确的买/卖建议里，"
            f"事后看**说对 {hr['correct']} 次、说错 {hr['wrong']} 次**（约 {hr['win_rate']:.0f}%）{warn}"
        )
    else:
        lines.append("• **买卖建议**：暂时没有可以判对错的买/卖建议")

    # 2. 选股能力（原"选股 alpha"，只看买入类）
    if ba["status"] == "no_sample":
        lines.append("• **选股眼光**：还没有能打分的买入建议（买入后要满 7 天才知道结果）")
    else:
        verb = "多赚" if ba["avg"] >= 0 else "还少赚了"
        few = "（只有 {} 次，参考意义有限）".format(ba["n"]) if ba["n"] < 5 else f"（{ba['n']} 次）"
        lines.append(
            f"• **选股眼光**：我们让你「买」的票，7 天后平均比大盘**{verb} {abs(ba['avg']):.1f}%**{few}"
        )

    # 3. 整体 vs 躺平买大盘（原"组合超额收益 alpha"——这是北极星指标的人话版）
    if ca["status"] == "ok":
        if ca["avg"] >= 0:
            lines.append(f"• **跟我们做 vs 躺平买大盘指数**：目前**领先大盘 +{ca['avg']:.1f}%** ✅")
        else:
            lines.append(
                f"• **跟我们做 vs 躺平买大盘指数**：目前**落后大盘 {abs(ca['avg']):.1f}%** ❌"
                f"（样本少、且受早期追高拖累，不代表长期；这正是我们要改进的）"
            )
    elif ca["status"] == "low_coverage":
        lines.append("• **跟我们做 vs 躺平买大盘指数**：能跟大盘对比的样本还不够，暂不下结论")
    else:
        lines.append("• **跟我们做 vs 躺平买大盘指数**：暂无可对比的样本")

    lines.append("_“比大盘多赚/少赚”=同期你的票涨跌 减去 大盘指数(美股SPY/港股恒指)涨跌_")
    return "\n".join(lines)


def _behavior_section(m: dict) -> str:
    beh = m["behavior"]
    trades = beh["trades"]
    lines = [f"**💰 你的操作（交易者的行为对不对）** · 已回填 {beh['n']} 笔"]
    if not trades:
        lines.append("暂无已回填的实操记录")
        return "\n".join(lines)

    # 大白话总结：把"红多"拆成 真亏 / 卖早(没亏) / 赚，避免误读
    real_loss = sum(1 for t in trades if t["verdict"] == "亏")
    sold_early = sum(1 for t in trades if t["verdict"] == "踏空")
    good = sum(1 for t in trades if t["verdict"] in ("赚", "躲跌✓"))
    lines.append(
        f"📊 _{beh['n']} 笔里：真亏 **{real_loss}** 笔、卖早(踏空，没亏只少赚) **{sold_early}** 笔、"
        f"赚/卖对 **{good}** 笔。红≠全做错，🟡是卖早。_"
    )

    for t in trades:
        sign = "+" if t["ret"] >= 0 else ""
        if t["verdict"] in ("赚", "躲跌✓"):
            icon = "🟢"
        elif t["verdict"] == "亏":
            icon = "🔴"
        elif t["verdict"] == "踏空":
            icon = "🟡"   # 卖早=少赚，不是亏，单独黄色，不与真亏混
        else:
            icon = "➖"
        lines.append(f"{icon} {t['date'][5:]} **{t['ticker']}** {t['action']} → 7日 {sign}{t['ret']}% · {t['verdict']}")
    lines.append(f"_{beh['symbol_note']}_")
    return "\n".join(lines)


def _shadow_section(m: dict) -> str:
    """影子选股段（试用名单，不花真钱只考眼光）：空则不占卡片。"""
    s = m.get("shadow_picks") or {}
    if not s.get("n"):
        return ""
    n_judged = s["correct"] + s["wrong"]
    rate = f"说对 {s['correct']}/{n_judged} 次" if n_judged else "暂无结果"
    if s.get("avg_alpha") is not None:
        verb = "多赚" if s["avg_alpha"] >= 0 else "少赚"
        extra = f"，平均比大盘{verb} {abs(s['avg_alpha']):.1f}%"
    else:
        extra = ""
    return (f"**🔭 试用选股名单**（你没买，只用来考我们的选股眼光）· {s['n']} 条 / {s['n_tickers']} 票\n"
            f"{rate}{extra}\n"
            f"_这些票不花你的钱，纯粹用来攒「我们挑的票准不准」的实战记录_")


def _hold_quality_section(m: dict) -> str:
    """持有判断质量——人话版：劝你拿住的票，是幸亏没卖还是该卖没卖。"""
    h = m.get("hold_quality") or {}
    n = h.get("n", 0)
    lines = [f"**🤝 “拿住别动”的建议准不准** · 共 {n} 次"]
    if not n:
        lines.append("暂无可评估的持有记录")
        return "\n".join(lines)
    avg = h.get("avg_alpha")
    if avg is not None:
        verb = "跑赢大盘" if avg >= 0 else "跑输大盘"
        avg_str = f"，整体{verb} {abs(avg):.1f}%"
    else:
        avg_str = ""
    lines.append(
        f"幸亏拿住(跑赢大盘) **{h['right']}** 次 · 该卖没卖(明显跑输) **{h['wrong']}** 次 · "
        f"不好不坏 **{h['neutral']}** 次{avg_str}"
    )
    if h.get("wrong_cases"):
        worst = "、".join(f"{c['ticker']}({c['date'][5:]} 比大盘差{abs(c['alpha']):.0f}%)"
                          for c in h["wrong_cases"][:3])
        lines.append(f"最该减没减：{worst}")
    lines.append("_“拿住”也是一种判断：拿住后跑赢大盘=对，明显跑输=本该减仓。"
                 "和买卖建议分开算，定投不计。_")
    return "\n".join(lines)


# 暴跌分桶的人话注解（bucket 常量名是 metrics 的 key，此处只管显示）
_DIP_GLOSS = {
    DIP_BUCKET_OPPORTUNITY: "跌了但逻辑没破，可能是机会",
    DIP_BUCKET_BROKEN: "公司基本面真出问题了",
    DIP_BUCKET_WATCH: "看不清/数据不全，先观望",
}


def _dip_section(m: dict) -> str:
    """风险预警段（暴跌时我们的判断准不准）——人话版。"""
    dip = m["dip"]
    if dip["n"] == 0:
        return "**🛡️ 暴跌预警准不准**：暂无已满 7 天的暴跌预警记录"
    if dip["n"] < 5:
        cases = []
        for c in dip["cases"][:5]:
            r7 = c.get("return_7d")
            r7s = (("+" if r7 >= 0 else "") + str(r7) + "%") if r7 is not None else "还没满7天"
            cases.append(f"{c['ticker']}（{c.get('bucket', '—')}，7日后 {r7s}）")
        return (f"**🛡️ 暴跌预警准不准** · 才 {dip['n']} 条（太少，只当个案看）\n"
                + "；".join(cases))
    lines = [f"**🛡️ 暴跌预警准不准** · 共 {dip['n']} 次（按“跌的原因”分三类）"]
    notes = {DIP_BUCKET_BROKEN: "（继续跌=我们预警对了）",
             DIP_BUCKET_WATCH: "（证据矛盾/早期记录，不算分）"}
    for bucket in (DIP_BUCKET_OPPORTUNITY, DIP_BUCKET_BROKEN, DIP_BUCKET_WATCH):
        st = dip["buckets"].get(bucket)
        if not st:
            continue   # 空桶不显示
        gloss = f"（{_DIP_GLOSS.get(bucket, '')}）"
        note = notes.get(bucket, "")
        if st["filled"]:
            sign = "+" if st["avg_ret7"] >= 0 else ""
            lines.append(f"　{bucket}{gloss} {st['n']} 次：已满7天 {st['filled']} 次，"
                         f"{st['up']} 次后续上涨 / 平均 {sign}{st['avg_ret7']}%{note}")
        else:
            lines.append(f"　{bucket}{gloss} {st['n']} 次：暂都没满 7 天{note}")
    return "\n".join(lines)


def _meta_section(m: dict) -> str:
    """诚实附录——人话版：把"结论可不可信、数据怎么来的、有什么没做到"说清楚。"""
    g, comp = m["gate"], m["composition"]
    th = g["thresholds"]
    if g["gate_passed"]:
        cred = "样本已够，结论可信度较高"
    else:
        cred = (f"⚠️ 样本还不够，现在的数字只能参考、不能下定论"
                f"（要攒够 {th['min_n']} 条明确买卖建议、覆盖 {th['min_tickers']} 只票才算数）")
    return "\n".join([
        "**📎 把话说在前头（诚实附录）**",
        f"• 结论可信度：{cred}",
        f"• 数据构成：{comp['filled']} 条已满 7 天的记录里，只有 {comp['directional']} 条是明确买/卖"
        f"（能打分），其余 {comp['neutral_or_passive']} 条是“持有/定投”——不算进命中率",
        "• 怎么算的：跟大盘比时，你的票和大盘用**同一个起止日期**算涨跌，避免时间错位造假数。"
        "已知还没做到：① 同一只票连续几天都推会重复计入 ② 30/90 天的长期结果还没到、目前只看 7 天",
        f"• 数据截至 {m['data_through'] or '—'}；以上全是真实记录算出来的，没有 AI 编故事。",
    ])


def _strategy_edge_text() -> str:
    """策略 edge 记分牌（锚 302 回测金矿）。读取失败时降级提示，不崩。"""
    try:
        return format_strategy_edge_section(compute_strategy_edge())
    except Exception as e:
        return f"**🧪 策略 edge（回测）**：读取失败（{e}）"


def build_value_card(m: dict) -> dict:
    v = m["verdict"]
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"{v['text']}\n\n`{v['badge']}`"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _adv_section(m)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _strategy_edge_text()}},
        {"tag": "hr"},
        *([{"tag": "div", "text": {"tag": "lark_md", "content": _shadow_section(m)}},
           {"tag": "hr"}] if _shadow_section(m) else []),
        {"tag": "div", "text": {"tag": "lark_md", "content": _hold_quality_section(m)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _behavior_section(m)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _dip_section(m)}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _meta_section(m)}},
        {"tag": "note", "elements": [{"tag": "plain_text",
            "content": "仅统计已过7日窗口、已回填的记录；过往表现不代表未来收益；样本越小结论越不可靠；不构成投资建议"}]},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🏆 卡门智投 · 价值体检 {m['data_through'] or ''}"},
            "template": _TEMPLATE.get(v["color"], "grey"),
        },
        "elements": elements,
    }


def _build_text(m: dict) -> str:
    v = m["verdict"]
    return "\n".join([
        f"🏆 卡门智投 · 价值体检 {m['data_through'] or ''}",
        v["text"], v["badge"], "",
        _adv_section(m), "", _strategy_edge_text(), "", _behavior_section(m), "",
        _dip_section(m), "", _meta_section(m),
    ])


async def run_value_report(db_path: str = "data/agent.db") -> tuple[dict, str, dict]:
    """先确保数据回填最新，再算价值记分牌。返回 (飞书卡片, 纯文本, metrics)。"""
    p = _resolve_db(db_path)
    for label, coro in (("7日回填", fill_7d_returns(p)), ("30/90日回填", fill_long_returns(p)),
                        ("操作回填", backfill_action_returns(p))):
        try:
            await coro
        except Exception as e:
            print(f"[ValueReport] {label} 跳过：{e}")
    try:
        backfill_market(p)   # 纯元数据：补齐 NULL market（港股归恒指口径），不重算历史基准
    except Exception as e:
        print(f"[ValueReport] market 回填跳过：{e}")
    try:
        backfill_dip_outcomes(db_path=p)
    except Exception as e:
        print(f"[ValueReport] 暴跌回填跳过：{e}")

    m = compute_value_metrics(p)
    return build_value_card(m), _build_text(m), m
