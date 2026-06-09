# src/finance_agent/value/report.py
"""
价值证明报告：把 compute_value_metrics 的结果渲染成飞书卡片 + 纯文本。
置顶三态结论（灰/橙/绿）由数据确定性生成，零 LLM 叙事。
"""
from finance_agent.db.tracker import (
    _resolve_db, fill_7d_returns, backfill_action_returns, backfill_dip_outcomes,
    backfill_market,
)
from finance_agent.value.metrics import compute_value_metrics

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


def _dip_section(m: dict) -> str:
    dip = m["dip"]
    if dip["n"] == 0:
        return "**🛡️ 风险预警**：暂无已回填的暴跌预警记录"
    if dip["n"] < 5:
        cases = []
        for c in dip["cases"][:5]:
            r7 = c.get("return_7d")
            r7s = (("+" if r7 >= 0 else "") + str(r7) + "%") if r7 is not None else "待回填"
            cases.append(f"{c['ticker']}（{c.get('opportunity') or '—'}机会，7日 {r7s}）")
        return (f"**🛡️ 风险预警** · 仅 {dip['n']} 条（样本不足，作个案不作命中率）\n"
                + "；".join(cases))
    hits = sum(1 for c in dip["cases"]
               if c.get("opportunity") in ("高", "中") and (c.get("return_7d") or 0) > 0)
    return f"**🛡️ 风险预警** · {dip['n']} 条，抄底机会事后上涨 {hits} 次"


def _meta_section(m: dict) -> str:
    g, comp = m["gate"], m["composition"]
    th = g["thresholds"]
    return "\n".join([
        "**📎 元信息 / 已知局限（诚实附录）**",
        f"• 证据等级：{'未过闸门 → 数据不足' if not g['gate_passed'] else '已过闸门'}"
        f"（下结论需 n≥{th['min_n']} 且 ≥{th['min_tickers']}票 且 benchmark≥{int(th['min_bm_cov'] * 100)}%，阈值写死防粉饰）",
        f"• 建议构成：已回填 {comp['filled']} 条中仅 {comp['directional']} 条可裁决"
        f"（占 {int(comp['actionable_ratio'] * 100)}%），其余 {comp['neutral_or_passive']} 条为持有/定投，**不计入命中率**",
        "• 已知方法学缺陷（团队已知，L1b 修）：① alpha 两条腿窗口可能不对齐"
        "（个股用回填时最新价 vs 基准取 rec 日起 7 交易日）② market 已按 ticker 推断回填（港股归恒指），"
        "但历史已回填的 benchmark 仍为旧口径（部分港股曾按 SPY 算），重算留 L1b ③ 同票多日推荐样本重叠、自相关",
        f"• 数据截至 {m['data_through'] or '—'}；纯数据计算，无 AI 叙事。",
    ])


def build_value_card(m: dict) -> dict:
    v = m["verdict"]
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"{v['text']}\n\n`{v['badge']}`"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _adv_section(m)}},
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
        _adv_section(m), "", _behavior_section(m), "", _dip_section(m), "", _meta_section(m),
    ])


async def run_value_report(db_path: str = "data/agent.db") -> tuple[dict, str, dict]:
    """先确保数据回填最新，再算价值记分牌。返回 (飞书卡片, 纯文本, metrics)。"""
    p = _resolve_db(db_path)
    for label, coro in (("7日回填", fill_7d_returns(p)), ("操作回填", backfill_action_returns(p))):
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
