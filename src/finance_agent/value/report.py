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
    g, hr, ba, ca = m["gate"], m["hit_rate"], m["buy_alpha"], m["combined_alpha"]
    lines = ["**🎯 建议价值（我们的建议对不对）**", "　_减仓择时 ≠ 选股能力，分开看_"]
    if hr["n_judged"]:
        warn = "　⚠️样本偏少仅参考" if hr["n_judged"] < 30 else ""
        lines.append(
            f"• 方向命中率：**{hr['win_rate']}%**"
            f"（95%CI {hr['ci_low']}%~{hr['ci_high']}%，n={hr['n_judged']}，{hr['n_tickers']}票）{warn}"
        )
        tail = "　·　当前全为减仓信号" if hr.get("buckets_note") else ""
        lines.append(f"　{hr['correct']}对 / {hr['wrong']}错（中性{hr['neutral']}条不计入）{tail}")
    else:
        lines.append("• 方向命中率：—（暂无可裁决方向性信号）")

    if ba["status"] == "no_sample":
        lines.append(f"• 选股(买入/加仓) alpha：**尚无可评估样本**（N=0，{ba.get('note', '回测窗口未到')}）")
    else:
        sign = "+" if ba["avg"] >= 0 else ""
        lines.append(f"• 选股 alpha：{sign}{ba['avg']}%（n={ba['n']}）")

    if ca["status"] == "ok":
        sign = "+" if ca["avg"] >= 0 else ""
        lines.append(f"• 组合超额收益 alpha：{sign}{ca['avg']}%（n={ca['n']}）")
    elif ca["status"] == "low_coverage":
        lines.append(
            f"• 组合 alpha：⬜ 暂不结论（benchmark 覆盖率仅 {int(g['bm_cov_dir'] * 100)}%，"
            f"有效独立样本远小于名义条数）"
        )
    else:
        lines.append("• 组合 alpha：—（无 benchmark 样本）")
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
    """影子选股段：watchlist 为空/无记录时返回 ""（不占卡片空间）。"""
    s = m.get("shadow_picks") or {}
    if not s.get("n"):
        return ""
    n_judged = s["correct"] + s["wrong"]
    rate = f"{s['correct']}/{n_judged}" if n_judged else "—"
    alpha = f"{s['avg_alpha']:+.1f}%" if s.get("avg_alpha") is not None else "—"
    return (f"**🔭 影子选股（watchlist，无真实仓位，纯测量）** · {s['n']} 条 / {s['n_tickers']} 票\n"
            f"方向判对 {rate} · 平均超额 alpha {alpha}\n"
            f"_与持仓操作命中率分栏统计；影子建议不投入真金，只考选股能力_")


def _hold_quality_section(m: dict) -> str:
    """持有判断质量——与方向命中率分栏呈现，绝不混算（防灌水）。"""
    h = m.get("hold_quality") or {}
    n = h.get("n", 0)
    lines = [f"**🤝 持有判断质量（与命中率分栏，不混算）** · {n} 条持有/观望"]
    if not n:
        lines.append("暂无可评估的持有记录")
        return "\n".join(lines)
    thr = h.get("bad_alpha_threshold", -5.0)
    avg = h.get("avg_alpha")
    avg_str = f"{avg:+.1f}%" if avg is not None else "—"
    lines.append(
        f"跑赢基准(持有对) **{h['right']}** · 大幅跑输≤{thr:g}%(该减没减) **{h['wrong']}** · "
        f"区间内中性 **{h['neutral']}**；持有期平均 alpha **{avg_str}**"
    )
    if h.get("wrong_cases"):
        worst = "、".join(f"{c['ticker']}({c['date'][5:]} α{c['alpha']:+.1f}%)"
                          for c in h["wrong_cases"][:3])
        lines.append(f"最该减没减：{worst}")
    lines.append("_口径：持有期个股 vs 基准的 7 日 alpha；跑赢=幸亏没卖，深度跑输=该减仓。"
                 "与「操作建议命中率」分开统计，定投除外。_")
    return "\n".join(lines)


def _dip_section(m: dict) -> str:
    """风险预警段：规则分级（thesis 完好性 × 下跌归因），硬性预算 ≤4 行。
    注：opportunity 字段已死（DIP_SYSTEM 不再输出），呈现一律用 bucket 替代。"""
    dip = m["dip"]
    if dip["n"] == 0:
        return "**🛡️ 风险预警**：暂无已回填的暴跌预警记录"
    if dip["n"] < 5:
        cases = []
        for c in dip["cases"][:5]:
            r7 = c.get("return_7d")
            r7s = (("+" if r7 >= 0 else "") + str(r7) + "%") if r7 is not None else "待回填"
            cases.append(f"{c['ticker']}（{c.get('bucket', '—')}，7日 {r7s}）")
        return (f"**🛡️ 风险预警** · 仅 {dip['n']} 条（样本不足，作个案不作命中率）\n"
                + "；".join(cases))
    lines = [f"**🛡️ 风险预警** · {dip['n']} 条（规则分级：thesis 完好性 × 下跌归因）"]
    notes = {DIP_BUCKET_BROKEN: "（续跌=预警对）", DIP_BUCKET_WATCH: "（矛盾/史前记录，不计结论）"}
    for bucket in (DIP_BUCKET_OPPORTUNITY, DIP_BUCKET_BROKEN, DIP_BUCKET_WATCH):
        st = dip["buckets"].get(bucket)
        if not st:
            continue   # 空桶不显示
        note = notes.get(bucket, "")
        if st["filled"]:
            sign = "+" if st["avg_ret7"] >= 0 else ""
            lines.append(f"　{bucket} {st['n']} 条：事后7日已回填 {st['filled']}，"
                         f"{st['up']} 涨 / 平均 {sign}{st['avg_ret7']}%{note}")
        else:
            lines.append(f"　{bucket} {st['n']} 条：事后7日均待回填{note}")
    return "\n".join(lines)


def _meta_section(m: dict) -> str:
    g, comp = m["gate"], m["composition"]
    th = g["thresholds"]
    return "\n".join([
        "**📎 元信息 / 已知局限（诚实附录）**",
        f"• 证据等级：{'未过闸门 → 数据不足' if not g['gate_passed'] else '已过闸门'}"
        f"（下结论需 n≥{th['min_n']} 且 ≥{th['min_tickers']}票 且 benchmark≥{int(th['min_bm_cov'] * 100)}%，阈值写死防粉饰）",
        f"• 建议构成：已回填 {comp['filled']} 条中仅 {comp['directional']} 条可裁决"
        f"（占 {int(comp['actionable_ratio'] * 100)}%），其余 {comp['neutral_or_passive']} 条为持有/定投，**不计入命中率**",
        "• 方法学口径：alpha 两腿已配对窗口对齐（个股与基准同起点同终点，历史已全量重算，"
        "2026-06-10 迁移）；仍知缺陷：① 同票多日推荐样本重叠、自相关 "
        "② 30/90 日窗口（=21/63 个交易日）回填中、样本未成熟，verdict 仍以 7 日为准",
        f"• 数据截至 {m['data_through'] or '—'}；纯数据计算，无 AI 叙事。",
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
