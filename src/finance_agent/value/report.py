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
from finance_agent.value.cumulative import (
    compute_cumulative_value, compute_live_action_returns, compute_live_rec_returns,
)

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
            f"（持有建议到今天跑赢大盘 +{hold_a:.1f}%）；而**买卖建议到今天其实在亏**"
            f"（平均超额 {ca['avg']:.1f}%）。给你的提示：**守住好仓、少折腾**。")


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
    return " ／ ".join(parts)   # 口径标题与免责由调用方承担，这里只给数字


def _summary_section(m: dict) -> str:
    """主屏极简结论：三类建议各一句话(都到今天)+ 行动建议。定性为主、只带一个锚点数；
    combined_alpha(-38%)/分窗口/主动vs定投 这些会正负打架的精确数全部下沉到折叠（用户：看不懂）。"""
    hq = m.get("hold_quality") or {}
    hr = m.get("hit_rate") or {}
    ba = m.get("buy_alpha") or {}
    lines = ["**📊 我们的建议准不准**　_都看「到今天」的结果，不是某天的快照_"]
    # 让你拿住的（持有判断——通常是账户赚钱主力）。✅/❌ 放行首，扫一眼就知对错
    a = hq.get("avg_alpha")
    if hq.get("n") and a is not None:
        tag = "✅" if a >= 0 else "❌"
        verb = "跑赢" if a >= 0 else "跑输"
        extra = "（你账户赚钱主力）" if a >= 0 else ""
        lines.append(f"{tag} **让你拿住的**：{hq['n']} 次，整体{verb}大盘 **{abs(a):.1f}%**{extra}")
    # 让你减仓/卖的（定性：大多卖飞还是减对）
    sr, sw = hr.get("sell_right", 0), hr.get("sell_wrong", 0)
    if sr + sw:
        if sw > sr:
            lines.append(f"❌ **让你减仓的**：{sr + sw} 次，大多**卖飞了**（减完又涨）")
        elif sr > sw:
            lines.append(f"✅ **让你减仓的**：{sr + sw} 次，大多**减对了**（躲过下跌）")
        else:
            lines.append(f"➖ **让你减仓的**：{sr + sw} 次，对错各半")
    # 让你买的（选股——样本通常少，弱化用 🤔）
    if ba.get("status") == "no_sample" or not ba.get("n"):
        lines.append("🤔 **让你买的**：还太少，先观望")
    else:
        verb = "多赚" if ba["avg"] >= 0 else "少赚"
        if ba["n"] < 5:
            lines.append(f"🤔 **让你买的**：才 {ba['n']} 次（太少先参考），平均比大盘{verb} {abs(ba['avg']):.1f}%")
        else:
            tag = "✅" if ba["avg"] >= 0 else "❌"
            lines.append(f"{tag} **让你买的**：{ba['n']} 次，平均比大盘**{verb} {abs(ba['avg']):.1f}%**")
    # 行动建议：仅"账户赢 ∧ 持有正 ∧ 买卖负"这一'赚靠拿住'模式给（与 _takeaway 同判据）
    c = m.get("cumulative") or {}
    ca = m.get("combined_alpha") or {}
    ca_avg = ca.get("avg")
    if ((c.get("excess_pct") or 0) >= 0 and (a or 0) > 0
            and ca.get("status") == "ok" and ca_avg is not None and ca_avg < 0):
        lines.append("👉 **给你的话**：你赚钱靠拿住、瞎减仓在亏 → **守住好仓、别频繁折腾**")
    return "\n".join(lines)


def _adv_section(m: dict) -> str:
    """能力总评·明细（折叠）——给精确数字，但按「三把尺子」分块：
    ① 个股能力·到今天超额  ② 定投vs主动·7日绝对收益  ③ 短线/长线·固定窗口定格。
    口径写进 ▍小标题、所有消歧/免责合并成底部唯一一条 ⓘ 脚注，
    避免行内括号注解把数据淹成一堵墙（用户反馈：巨乱）。"""
    hr, ba, hq = m["hit_rate"], m["buy_alpha"], m.get("hold_quality") or {}
    avp = m.get("active_vs_passive") or {}
    ca = m["combined_alpha"]
    blocks = ["**📊 能力总评·明细**"]

    # ── 尺①：只看个股，口径「到今天比大盘多赚多少」──
    b1 = ["**▍只看个股 · 口径「到今天比大盘多赚多少」**"]
    if ba["status"] == "no_sample":
        b1.append("· 让你买的：_还没满 7 天的买入建议可打分_")
    else:
        few = f"仅 {ba['n']} 次，太少先参考" if ba["n"] < 5 else f"{ba['n']} 次"
        b1.append(f"· 让你买的：**{ba['avg']:+.1f}%**　_{few}_")
    if hq.get("n"):
        a = hq.get("avg_alpha")
        head = f"**{a:+.1f}%**" if a is not None else "_暂无_"
        b1.append(f"· 让你拿住的：{head}　_拿对 {hq['right']}、拿错 {hq['wrong']}_")
    sr, sw = hr.get("sell_right", 0), hr.get("sell_wrong", 0)
    if sr + sw:
        note = "样本少但近期转好" if (sr + sw) < 30 else "近期转好"
        b1.append(f"· 让你卖减的：_卖对 {sr}、卖飞 {sw}，{note}_")
    if ca["status"] == "ok":
        b1.append(f"· **合计买卖超额：{ca['avg']:+.1f}%** {'✅' if ca['avg'] >= 0 else '❌'}"
                  f"　_每条到今天的终局，非账户加权_")
    wc = hq.get("wrong_cases") or []
    if wc:
        worst = "、".join(f"{c['ticker']}（{c['date'][5:]} 差{abs(c['alpha']):.0f}%）" for c in wc[:3])
        b1.append(f"· 最该减没减：{worst}")
    blocks.append("\n".join(b1))

    # ── 尺②：定投 vs 主动，口径「7 日绝对收益」（另一把尺，别和①比）──
    if avp.get("dca_avg") is not None or avp.get("active_avg") is not None:
        dca_s = f"**{avp['dca_avg']:+.1f}%**（{avp['dca_n']} 次）" if avp.get("dca_avg") is not None else "—"
        act_s = f"**{avp['active_avg']:+.1f}%**（{avp['active_n']} 次）" if avp.get("active_avg") is not None else "—"
        tail = ""
        if avp.get("dca_avg") is not None and avp.get("active_avg") is not None:
            tail = "　→　" + ("主动选股更赚" if avp["active_avg"] > avp["dca_avg"] else "定投更稳")
        blocks.append("**▍定投 vs 主动 · 口径「7 日绝对收益」**\n"
                      f"· 定投 {dca_s}　·　你主动买入 {act_s}{tail}")

    # ── 尺③：短线还是长线，口径「固定窗口定格」──
    abw_line = _format_window_alpha(m.get("alpha_by_window") or {})
    if abw_line:
        blocks.append("**▍短线还是长线 · 口径「固定窗口定格」**\n"
                      f"· {abw_line}　_30/90 天才见价值兑现，7 天多噪声_")

    # 统一脚注：超额定义 + 拿错/卖飞释义 + 主动vs信号消歧 + 三尺别横比，一次说清
    blocks.append("_ⓘ 超额 = 你的票涨跌 − 大盘(SPY/恒指)。拿错 = 该减没减后续跑输；卖飞 = 减仓后续还涨。"
                  "「你主动买入」是你实操下单、「让你买的」是我们的信号，不是一回事。三把尺口径不同，别横着比。_")
    return "\n\n".join(blocks)


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
        "• 怎么算的：**判对错看「到今天」的终局结果**——拿建议发出时的价比今天的价(减同期大盘涨跌"
        "=跑赢/跑输多少)，操作满 5 天才算、太新的不下结论；同票连推按 (票, 周) 去重。"
        "下方「信号超额·分窗口(7/30/90天)」是另一码事：那是固定窗口定格，专门看建议是只灵几天的短线、"
        "还是越拿越值的长线，跟主口径「到今天」互补、别混看。",
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
    money = f"{'+' if ex >= 0 else '−'}${abs(c['excess_amount_usd']):,.0f}"
    basis_tag = "" if c["basis"] == "real" else "　_（⚠️ 纸面模拟·非真实账户）_"
    lines = [
        # 结论先行：最该看的「多赚 $X」放第一行、最显眼
        f"**{icon} 听我们的，到今天比躺平{verb} {abs(ex):.1f}%（约 {money}）**{basis_tag}",
        f"_你按建议持有这 {c['n_positions']} 只，账面 {sp:+.1f}%；同期躺平买指数才 {pp:+.1f}%_",
    ]
    # 口径砍成一句（完整解释在「诚实附录」折叠里）
    note = "_算法：真实持仓×今日价、已折美元——这是「你账户」的结果，和下方「每条建议准不准」是两回事_"
    part = c.get("partial") or []
    if part:
        note += f"；另有 {len(part)} 只缺数据未纳入（{'、'.join(p['ticker'] for p in part[:4])}）"
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
    """schema 2.0：主屏只留「头条(你vs躺平) + 一句话结论(三类建议准不准+行动)」，
    所有精确数字/命中率/分窗口/逐笔/明细全部折叠（用户反馈：信息过载、正负数打架看不懂）。"""
    strat_block = _strategy_edge_text() + "\n\n" + _glossary_section()
    head = _cumulative_headline(m)   # 累计「你 vs 躺平」头条，常驻、不受闸门控制
    body_elements = [
        *([{"tag": "markdown", "content": head}, {"tag": "hr"}] if head else []),
        {"tag": "markdown", "content": _summary_section(m)},   # 主屏唯一结论块（极简三句+行动）
        {"tag": "hr"},
        _panel("📊 建议明细 · 命中率 / 各类超额 / 分窗口", _adv_section(m)),
        _panel("💰 你的逐笔操作 · 每笔到今天赚亏", _behavior_section(m)),
        *([_panel("🔭 试用选股名单 · 不花钱考眼光", _shadow_section(m))] if _shadow_section(m) else []),
        _panel("🛡️ 暴跌预警准不准", _dip_section(m)),
        _panel("🧪 名词小课堂 + 选股套路体检", strat_block),
        _panel("📎 诚实附录 · 数据怎么来的", _meta_section(m)),
        {"tag": "markdown", "content":
            "_仅统计已回填记录；过往表现不代表未来收益；样本越小越不可靠；不构成投资建议_"},
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
    head = _cumulative_headline(m)
    return "\n".join([
        f"🏆 卡门智投 · 价值体检 {m['data_through'] or ''}",
        *([head, ""] if head else []),
        _summary_section(m), "",
        "—————— 以下为明细（想深究再看）——————", "",
        _adv_section(m), "", _behavior_section(m), "",
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

    # 终局口径：实时拉每条推荐的「至今」涨跌/基准(满5天才纳入)，作命中率/alpha 主口径；
    # 拉取失败或无满足项则回落 None → compute_value_metrics 自动沿用第7天定格，保证卡不空。
    live_recs = None
    try:
        lr = compute_live_rec_returns(p, min_days=5)
        if lr:
            live_recs = lr
    except Exception as e:
        print(f"[ValueReport] 终局口径拉取跳过(回落第7天定格)：{e}")
    m = compute_value_metrics(p, live_overrides=live_recs)
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
