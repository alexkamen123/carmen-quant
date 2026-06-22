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
from finance_agent.value.cumulative import compute_cumulative_value, compute_live_action_returns

_TEMPLATE = {"grey": "grey", "orange": "orange", "green": "green"}


def _takeaway(m: dict) -> str | None:
    """一句话串场：仅在「账户跑赢躺平 ∧ 持有判断为正 ∧ 买卖信号可比且为负」这一
    经典『赚靠拿住、短线买卖在亏』模式命中时点破，给行为指导。其余返回 None，不强行串场。
    确定性判定、零 LLM——治「绿头条 vs 红明细」看似自相矛盾的观感。"""
    c = m.get("cumulative")
    if not c or c.get("excess_pct") is None or c["excess_pct"] < 0:
        return None
    hold_a = (m.get("hold_quality") or {}).get("avg_alpha")
    if hold_a is None or hold_a <= 0:
        return None
    ca = m.get("combined_alpha") or {}
    if ca.get("status") != "ok" or ca.get("avg") is None or ca["avg"] >= 0:
        return None
    return (f"💡 **一句话看懂**：你账户能跑赢躺平，主要靠**拿住没瞎动**"
            f"（持有判断跑赢大盘 +{hold_a:.1f}%）；而**短线买卖其实在亏**"
            f"（买卖信号 7 天 {ca['avg']:.1f}%）。给你的提示：**守住好仓、少折腾**。")


_MIN_WINDOW_ALPHA_N = 5   # 分窗口超额低于此样本量不报数值（防「30天 -50.4%(n=4)」噪声吓人）


def _format_window_alpha(abw: dict) -> str | None:
    """分窗口超额一行：样本够(n≥5)才报数值，小样本诚实标「样本少」，无任一够样本则返回 None。"""
    parts = []
    has_number = False
    for w, label in (("7d", "7天"), ("30d", "30天"), ("90d", "90天")):
        s = abw.get(w)
        if s and s.get("alpha_avg") is not None and s.get("alpha_n", 0) >= _MIN_WINDOW_ALPHA_N:
            parts.append(f"{label} {s['alpha_avg']:+.1f}%（n={s['alpha_n']}）")
            has_number = True
        elif s and s.get("alpha_n"):
            parts.append(f"{label} 样本少(n={s['alpha_n']})暂不下结论")
        else:
            parts.append(f"{label} 未到")
    if not has_number:
        return None
    return ("　_信号超额·分窗口_：" + " ／ ".join(parts)
            + "　_（30/90天才是价值兑现窗口，7天多为噪声）_")


def _adv_section(m: dict) -> str:
    """能力总评——三问：让你买的赚多少 / 让你拿住的赚还是亏 / 让你卖的没做会怎样。
    个股是真本事，定投单列只放收益供横向比较（用户设计：评整体能力 + 调风投/定投比例）。
    全大白话，不用 alpha/命中率/CI 黑话；口径不变只换说法。"""
    hr, ba, hq = m["hit_rate"], m["buy_alpha"], m.get("hold_quality") or {}
    avp = m.get("active_vs_passive") or {}
    lines = []
    tk = _takeaway(m)
    if tk:
        lines += [tk, ""]   # 串场置顶，统领下方分项明细
    lines += ["**📊 卡门智投能力总评**　_这些是「我们的建议」战绩，不是你的操作次数_",
              "**【个股·风险类】这才是真本事**"]

    # 1️⃣ 让你买的——选股眼光
    if ba["status"] == "no_sample":
        lines.append("1️⃣ **让你买的**：还没有满 7 天的买入建议可打分")
    else:
        verb = "多赚" if ba["avg"] >= 0 else "少赚"
        few = f"（仅 {ba['n']} 次，太少先参考）" if ba["n"] < 5 else f"（{ba['n']} 次）"
        lines.append(f"1️⃣ **让你买的**：买入后 7 天平均比大盘**{verb} {abs(ba['avg']):.1f}%**{few}")

    # 2️⃣ 让你拿住的——持有判断（持有也是立场，与买卖同级，不降级）
    if hq.get("n"):
        avg = hq.get("avg_alpha")
        tail = (f"，整体{'跑赢' if avg >= 0 else '跑输'}大盘 {abs(avg):.1f}%") if avg is not None else ""
        lines.append(
            f"2️⃣ **让你拿住的**：{hq['n']} 次「持有别动」里，"
            f"幸亏拿住(跑赢) **{hq['right']}** 次、该卖没卖(明显跑输) **{hq['wrong']}** 次{tail}"
        )
        # 最该减没减——可直接行动的减仓提示（原埋在死代码里，提到主屏）
        wc = hq.get("wrong_cases") or []
        if wc:
            worst = "、".join(f"{c['ticker']}({c['date'][5:]} 差{abs(c['alpha']):.0f}%)"
                              for c in wc[:3])
            lines.append(f"　↳ 最该减没减：{worst}")
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

    # 分窗口超额（7/30/90 天）：7天噪声大，30/90天才见价值兑现；小样本不报数值
    abw_line = _format_window_alpha(m.get("alpha_by_window") or {})
    if abw_line:
        lines.append(abw_line)

    # 【ETF·定投类】不评判断，只放收益供横向比较（调风投/定投比例用）
    if avp.get("dca_avg") is not None or avp.get("active_avg") is not None:
        lines.append("**【ETF·定投类】不评对错，只看收益（定投本就不择时）**")
        dca_s = f"{avp['dca_avg']:+.1f}%（{avp['dca_n']} 次）" if avp.get("dca_avg") is not None else "—"
        act_s = (f"{avp['active_avg']:+.1f}%（{avp['active_n']} 次）"
                 if avp.get("active_avg") is not None else "—")
        lines.append(f"• 定投标的 7 日平均收益：**{dca_s}**")
        lines.append(f"• 你主动买入(你实操下单的个股) 7 日平均收益：**{act_s}**"
                     f"\n　_注：这是你真金白银买的(含没按建议的)、看的是绝对涨跌；"
                     f"和最上面「让你买的」(只算我们建议的信号、比的是跑赢大盘多少)不是一回事，别直接比_")
        if avp.get("dca_avg") is not None and avp.get("active_avg") is not None:
            winner = "你的主动选股更赚" if avp["active_avg"] > avp["dca_avg"] else "定投/躺平更稳赚"
            lines.append(f"　_横向比：目前 **{winner}** → 可据此调整风投/定投的钱分多少_")

    lines.append("_“比大盘多赚/少赚”=同期你的票涨跌 减去 大盘指数(美股SPY/港股恒指)涨跌_")
    return "\n".join(lines)


def _verdict_for(action: str, ret: float, kind: str) -> str:
    """按涨跌判这笔操作对错——live 与 7 日同一判据，只是 ret 取值不同。"""
    if kind == "cash":
        return "现金管理"
    if action == "BUY":
        return "赚" if ret > 0 else ("亏" if ret < 0 else "平")
    if action in ("SELL", "TRIM"):
        return "躲跌✓" if ret < 0 else ("踏空" if ret > 0 else "平")
    return "—"


def _verdict_icon(v: str) -> str:
    return {"赚": "🟢", "躲跌✓": "🟢", "亏": "🔴", "踏空": "🟡"}.get(v, "➖")


def _behavior_section(m: dict) -> str:
    beh = m["behavior"]
    trades = beh["trades"]
    # 三层隔离：个股(评选股对错) / 现金对冲(不评胜负)
    stock = [t for t in trades if t.get("kind") != "cash"]
    cash = [t for t in trades if t.get("kind") == "cash"]
    lines = [f"**💰 你的操作**（每笔=这笔操作**到今天**的真实涨跌，按实时价算；"
             f"个别拉价失败的退显第7天定格）· 已回填 {beh['n']} 笔"]
    if not trades:
        lines.append("暂无已回填的实操记录")
        return "\n".join(lines)

    # 每笔取「到今天」优先、回落 7 日定格；统计与判对错都用这个口径（第7天定格会骗人：
    # 7天亏可能至今已赚、反之亦然，逐笔对账该看你现在到底赚没赚）
    def _eff(t):
        live = t.get("live_return")
        if live is not None:
            return live, _verdict_for(t["action"], live, t.get("kind", "stock")), True
        return t["ret"], t["verdict"], False

    real_loss = sold_early = good = 0
    for t in stock:
        _, v, _ = _eff(t)
        real_loss += int(v == "亏")
        sold_early += int(v == "踏空")
        good += int(v in ("赚", "躲跌✓"))
    lines.append(
        f"📊 _{len(stock)} 笔个股操作里：真亏 **{real_loss}** 笔、卖早(踏空，没亏只少赚) **{sold_early}** 笔、"
        f"赚/卖对 **{good}** 笔。红≠全做错，🟡是卖早。_"
    )

    for t in stock:
        ret, v, is_live = _eff(t)
        icon = _verdict_icon(v)
        sign = "+" if ret >= 0 else ""
        if is_live:
            r7 = t.get("ret")
            ref = f"（7日时 {'+' if (r7 or 0) >= 0 else ''}{r7}%）" if r7 is not None else ""
            lines.append(f"{icon} {t['date'][5:]} **{t['ticker']}** {t['action']} → 至今 {sign}{ret}% · {v}{ref}")
        else:
            lines.append(f"{icon} {t['date'][5:]} **{t['ticker']}** {t['action']} → 第7天 {sign}{ret}% · {v}（缺实时价）")
    # 现金/对冲单列——不计选股胜负（否则 SGOV 近 0 收益会稀释、冒充选股战绩）
    if cash:
        cs = "、".join(f"{t['ticker']}({t['date'][5:]} {'+' if t['ret'] >= 0 else ''}{t['ret']}%)"
                      for t in cash)
        lines.append(f"💵 现金管理/对冲(不算选股对错)：{cs}")
    # 观察期提示：未满 7 天的操作还没结果（含首笔系统荐股 AVGO），给个交代
    pend = beh.get("pending") or []
    if pend:
        names = "、".join(f"{p['ticker']}({p['date'][5:]})" for p in pend[:4])
        lines.append(f"⏳ 还在 7 天观察期、结果待揭晓：{names}")
    lines.append(f"_{beh['symbol_note']}_")
    return "\n".join(lines)


def _shadow_section(m: dict) -> str:
    """影子选股段（试用名单，不花真钱只考眼光）：空则不占卡片。
    本轮口径：只展示明细 + Wilson CI，措辞固定「样本不足不下结论」，绝不开「证明了选股能力」头条
    （样本小且无自动来源时设头条=复刻 p-hacking）。与真实账户口径物理隔离。"""
    s = m.get("shadow_picks") or {}
    if not s.get("n"):
        return ""
    njud = s.get("n_judged") or 0
    target = s.get("target") or 0
    short = max(0, target - njud)
    head = (f"**🔭 选股眼光·影子轨**（没花你一分钱，纯考「我们挑的票准不准」）· "
            f"{s['n']} 条 / {s['n_tickers']} 票，已满7天可判 {njud} 条")
    if njud:
        wr = s.get("win_rate")
        ci = (f"（95%CI {s['ci_low']}%~{s['ci_high']}%）"
              if s.get("ci_low") is not None else "")
        body = f"　说对 {s['correct']}/{njud} 次"
        if wr is not None:
            body += f"（命中率 {wr}%{ci}）"
        if s.get("avg_alpha") is not None:
            verb = "多赚" if s["avg_alpha"] >= 0 else "少赚"
            body += f"，平均比大盘{verb} {abs(s['avg_alpha']):.1f}%"
    else:
        body = "　暂无已满 7 天、方向明确的可判记录"
    status = (f"　⏳ **样本不足，只记录不下结论**（已攒 {njud} 条 / 目标 {target} 条，"
              f"还差 {short} 条才考虑评估；何时升格为结论须单独评审）")
    lines = [head, body, status]
    picks = s.get("picks") or []
    if picks:
        icon = {"对": "🟢", "错": "🔴", "中性": "➖", "—": "·"}
        detail = "；".join(
            f"{icon.get(p['verdict'], '·')}{p['date'][5:]} {p['ticker']} {p['rec']}"
            + (f"({'+' if (p['ret'] or 0) >= 0 else ''}{p['ret']}%)" if p['ret'] is not None else "")
            for p in picks)
        lines.append("　" + detail)
    lines.append("_⚠️ 这些票来自人工/bot 自选关注名单、非预注册完整 universe，存在挑票偏差；"
                 "纸面胜率系上界、不可外推为真金白银能赚。影子轨绝不并入「你 vs 躺平」累计与操作轨超额。_")
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
        cred = "方向性样本已够，结论可信度较高"
    else:
        cred = (f"现在 {comp['directional']} 条明确买卖建议够给「初步判断」了，"
                f"样本越多结论越硬，攒到 {th['min_n']} 条、{th['min_tickers']} 只票就很可靠"
                f"（账户到今天累计赚没赚不受此限，见上方头条）")
    return "\n".join([
        "**📎 把话说在前头（诚实附录）**",
        f"• 结论可信度：{cred}",
        f"• 数据构成：{comp['filled']} 条已满 7 天的记录拆开看——"
        f"**{comp['passive']} 条**是“持有/定投/观望”(按兵不动，不算谁对谁错)、"
        f"**{comp['directional_raw']} 条**是明确买卖建议"
        + (f"(同一只票一周内反复推荐的只算一次，去重后 {comp['directional']} 条独立信号)"
           if comp['dedup_dropped'] else f"({comp['directional']} 条独立信号)")
        + (f"、**{comp['shadow']} 条**是试用选股名单(没花你的钱)" if comp['shadow'] else "")
        + "。",
        "• 怎么算的：跟大盘比时，你的票和大盘用**同一个起止日期**算涨跌，避免时间错位造假数。"
        "同票连推已按 (票, 周) 去重、不再伪重复计入；7/30/90 天超额已并列展示（90 天样本未满）。",
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


def _cumulative_headline(m: dict) -> str | None:
    """头条：截至今天，听我们的真实持仓 vs 同期躺平，账户累计多赚/少赚多少（钱视角）。
    不受样本闸门控制——灰态也常驻。无累计数据返回 None（不占卡片）。"""
    c = m.get("cumulative")
    if not c:
        return None
    sp, pp, ex = c["strategy_cum_pct"], c["passive_cum_pct"], c["excess_pct"]
    verb = "多赚" if ex >= 0 else "少赚"
    icon = "🟢" if ex >= 0 else "🔴"
    basis_tag = "" if c["basis"] == "real" else "（⚠️ 纸面模拟·非你真实账户）"
    lines = [
        f"**{icon} 截至 {c['as_of']}：听我们的 vs 躺平买指数**{basis_tag}",
        f"你按建议持有的这些钱（{c['n_positions']} 只仓位）账面累计 **{sp:+.1f}%**；"
        f"同期这笔钱躺平买指数 **{pp:+.1f}%**",
        f"　**▶ 你比躺平 {verb} {abs(ex):.1f}%（约 ${abs(c['excess_amount_usd']):,.0f}）**",
    ]
    note = ("_↑ 按你真实持仓×成本×今日最新价算、跨币种已折美元（HKD÷7.8 / CNY÷7.2）；"
            "躺平=同一笔本金在各仓入场日买对应市场指数(美股SPY/港股恒指)持有至今。"
            "这是「你账户到今天的累计」，与下方「信号7天命中」是两回事。_")
    part = c.get("partial") or []
    if part:
        names = "、".join(p["ticker"] for p in part[:6])
        note += f"\n_（{len(part)} 只仓位暂未纳入对比：{names}——缺现价或基准数据，金额为已纳入部分）_"
    lines.append(note)
    return "\n".join(lines)


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


def _header_template(m: dict) -> str:
    """整卡 header 颜色 = 北极星朴素问题「你 vs 躺平」的答案：
    有真实账户累计(cumulative.excess)就由它定色(≥0绿/<0橙)，永不被样本闸门焊成永久灰；
    cumulative 缺失(实时拉价失败/无持仓)才回落 verdict.color。"""
    c = m.get("cumulative")
    if c and c.get("excess_pct") is not None:
        return "green" if c["excess_pct"] >= 0 else "orange"
    return _TEMPLATE.get(m["verdict"]["color"], "grey")


def build_value_card(m: dict) -> dict:
    """schema 2.0：主屏只留「结论 + 能力总评」，详情全部折叠（C 端阅读体验）。"""
    v = m["verdict"]
    # 选股套路体检 + 名词小课堂合并进一个折叠面板
    strat_block = _strategy_edge_text() + "\n\n" + _glossary_section()
    head = _cumulative_headline(m)   # 累计「你 vs 躺平」头条，常驻、不受闸门控制
    body_elements = [
        *([{"tag": "markdown", "content": head}, {"tag": "hr"}] if head else []),
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
            "template": _header_template(m),
        },
        "body": {"elements": body_elements},
    }


def _build_text(m: dict) -> str:
    v = m["verdict"]
    head = _cumulative_headline(m)
    return "\n".join([
        f"🏆 卡门智投 · 价值体检 {m['data_through'] or ''}",
        *([head, ""] if head else []),
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
    # 累计「你 vs 躺平·截至今天」头条——实时拉价，失败兜底 None 绝不崩卡
    try:
        m["cumulative"] = compute_cumulative_value(p)
    except Exception as e:
        print(f"[ValueReport] 累计头条计算跳过：{e}")
        m["cumulative"] = None
    # 逐笔「到今天」真实涨跌——实时拉价注入 trades，失败的笔自动回落第7天定格（绝不崩）
    try:
        live = compute_live_action_returns(p)
        for t in m["behavior"]["trades"]:
            t["live_return"] = live.get(t["id"])
    except Exception as e:
        print(f"[ValueReport] 逐笔到今天涨跌跳过：{e}")
    return build_value_card(m), _build_text(m), m
