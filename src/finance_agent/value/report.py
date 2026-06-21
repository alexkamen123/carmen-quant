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
    """能力总评——三问：让你买的赚多少 / 让你拿住的赚还是亏 / 让你卖的没做会怎样。
    个股是真本事，定投单列只放收益供横向比较（用户设计：评整体能力 + 调风投/定投比例）。
    全大白话，不用 alpha/命中率/CI 黑话；口径不变只换说法。"""
    hr, ba, hq = m["hit_rate"], m["buy_alpha"], m.get("hold_quality") or {}
    avp = m.get("active_vs_passive") or {}
    lines = ["**📊 卡门智投能力总评**　_这些是「我们的建议」战绩，不是你的操作次数_",
             "**【个股·风险类】这才是真本事**"]

    # 1️⃣ 让你买的——选股眼光
    if ba["status"] == "no_sample":
        lines.append("1️⃣ **让你买的**：还没有满 7 天的买入建议可打分")
    else:
        verb = "多赚" if ba["avg"] >= 0 else "少赚"
        few = f"（仅 {ba['n']} 次，太少先参考）" if ba["n"] < 5 else f"（{ba['n']} 次）"
        lines.append(f"1️⃣ **让你买的**：买入后 7 天平均比大盘**{verb} {abs(ba['avg']):.1f}%**{few}")

    # 2️⃣ 让你拿住的——持有判断（原单列的 hold_quality 并进来）
    if hq.get("n"):
        avg = hq.get("avg_alpha")
        tail = (f"，整体{'跑赢' if avg >= 0 else '跑输'}大盘 {abs(avg):.1f}%") if avg is not None else ""
        lines.append(
            f"2️⃣ **让你拿住的**：{hq['n']} 次「持有别动」里，"
            f"幸亏拿住(跑赢) **{hq['right']}** 次、该卖没卖(明显跑输) **{hq['wrong']}** 次{tail}"
        )
    else:
        lines.append("2️⃣ **让你拿住的**：暂无可评估的持有建议")

    # 3️⃣ 让你卖的——反事实：没听我们卖，7 天后会怎样（仅卖/减，不含买入）
    sr, sw = hr.get("sell_right", 0), hr.get("sell_wrong", 0)
    if sr + sw:
        warn = "　⚠️ 样本还少，先别当真" if (sr + sw) < 30 else ""
        lines.append(
            f"3️⃣ **让你卖/减的**：{sr + sw} 次减仓建议里，"
            f"**卖对(后续真跌) {sr} 次、卖飞(没卖能多赚) {sw} 次**{warn}\n"
            f"　_口径=若当时不卖、7 天后涨=卖飞、跌=卖对；早期系统性卖飞、近期转好_"
        )
    else:
        lines.append("3️⃣ **让你卖/减的**：暂无可判对错的卖出建议")

    # 综合：个股整体 vs 躺平
    ca = m["combined_alpha"]
    if ca["status"] == "ok":
        if ca["avg"] >= 0:
            lines.append(f"　**▶ 我们的买卖信号·7天平均超额：+{ca['avg']:.1f}%** ✅　_（信号表现，非你账户累计）_")
        else:
            lines.append(f"　**▶ 我们的买卖信号·7天平均超额：{ca['avg']:.1f}%** ❌"
                         f"　_（信号7天表现，非你账户累计；样本少，不代表长期）_")

    # 分窗口超额（7/30/90 天）：7天噪声大，30/90天才见价值兑现
    abw = m.get("alpha_by_window") or {}
    parts = []
    for w, label in (("7d", "7天"), ("30d", "30天"), ("90d", "90天")):
        s = abw.get(w)
        if s and s.get("alpha_avg") is not None:
            parts.append(f"{label} {s['alpha_avg']:+.1f}%（n={s['alpha_n']}）")
        else:
            parts.append(f"{label} 未到")
    if any("未到" not in p for p in parts):
        lines.append("　_信号超额·分窗口_：" + " ／ ".join(parts)
                     + "　_（30/90天才是价值兑现窗口，7天多为噪声）_")

    # 【ETF·定投类】不评判断，只放收益供横向比较（调风投/定投比例用）
    if avp.get("dca_avg") is not None or avp.get("active_avg") is not None:
        lines.append("**【ETF·定投类】不评对错，只看收益（定投本就不择时）**")
        dca_s = f"{avp['dca_avg']:+.1f}%（{avp['dca_n']} 次）" if avp.get("dca_avg") is not None else "—"
        act_s = (f"{avp['active_avg']:+.1f}%（{avp['active_n']} 次）"
                 if avp.get("active_avg") is not None else "—")
        lines.append(f"• 定投标的 7 日平均收益：**{dca_s}**")
        lines.append(f"• 你主动买入 7 日平均收益：**{act_s}**")
        if avp.get("dca_avg") is not None and avp.get("active_avg") is not None:
            winner = "你的主动选股更赚" if avp["active_avg"] > avp["dca_avg"] else "定投/躺平更稳赚"
            lines.append(f"　_横向比：目前 **{winner}** → 可据此调整风投/定投的钱分多少_")

    lines.append("_“比大盘多赚/少赚”=同期你的票涨跌 减去 大盘指数(美股SPY/港股恒指)涨跌_")
    return "\n".join(lines)


def _behavior_section(m: dict) -> str:
    beh = m["behavior"]
    trades = beh["trades"]
    lines = [f"**💰 你的操作**（每笔=操作后**第7天定格**快照，非最新价）· 已回填 {beh['n']} 笔"]
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
    # 观察期提示：未满 7 天的操作还没结果（含首笔系统荐股 AVGO），给个交代
    pend = beh.get("pending") or []
    if pend:
        names = "、".join(f"{p['ticker']}({p['date'][5:]})" for p in pend[:4])
        lines.append(f"⏳ 还在 7 天观察期、结果待揭晓：{names}")
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


def _glossary_section() -> str:
    """名词小课堂：复用全局术语库（glossary.strategy_glossary_md），不再单独维护一份。"""
    from finance_agent.notifications.glossary import strategy_glossary_md
    return strategy_glossary_md()


def _panel(title: str, content: str, expanded: bool = False) -> dict:
    """schema 2.0 折叠面板：主屏只显标题，点开看 content（C 端瘦身，用户选型）。"""
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined",
                     "size": "16px 16px"},
            "icon_position": "right",
            "icon_expanded_angle": 180,
        },
        "elements": [{"tag": "markdown", "content": content}],
    }


def build_value_card(m: dict) -> dict:
    """schema 2.0：主屏只留「结论 + 能力总评」，详情全部折叠（C 端阅读体验）。"""
    v = m["verdict"]
    # 选股套路体检 + 名词小课堂合并进一个折叠面板
    strat_block = _strategy_edge_text() + "\n\n" + _glossary_section()
    body_elements = [
        {"tag": "markdown", "content": f"{v['text']}\n\n`{v['badge']}`"},
        {"tag": "hr"},
        {"tag": "markdown", "content": _adv_section(m)},   # 能力总评——主屏常驻
        {"tag": "hr"},
        _panel("🧪 选股套路体检 + 名词小课堂（点开）", strat_block),
        *([_panel("🔭 试用选股名单（点开）", _shadow_section(m))] if _shadow_section(m) else []),
        _panel("💰 你的逐笔操作（点开）", _behavior_section(m)),
        _panel("🛡️ 暴跌预警准不准（点开）", _dip_section(m)),
        _panel("📎 把话说在前头 · 诚实附录（点开）", _meta_section(m)),
        {"tag": "markdown", "content":
            "_仅统计已过 7 日、已回填的记录；过往表现不代表未来收益；样本越小越不可靠；不构成投资建议_"},
    ]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🏆 卡门智投 · 价值体检 {m['data_through'] or ''}"},
            "template": _TEMPLATE.get(v["color"], "grey"),
        },
        "body": {"elements": body_elements},
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
