# src/finance_agent/weekly/report_card.py
"""
将 allocation_advisor 的结构化结果渲染成飞书卡片。
"""
from finance_agent.notifications.glossary import build_glossary_element
from finance_agent.notifications.cards import collapsible_panel

URGENCY_EMOJI = {"高": "🔴", "中": "🟡", "低": "🟢"}
SIGNAL_EMOJI = {"强": "🔥", "中": "✨"}
TYPE_TAG = {"防御/对冲型": "🛡️ 防御/对冲", "成长型": "📈 成长", "ETF/宽基": "📦 ETF/宽基"}


def build_weekly_card(result: dict) -> dict:
    """
    根据三步流程结果构建飞书交互式卡片。
    result 来自 allocation_advisor.run_allocation_advisor() 的返回值。
    """
    diagnosis = result.get("diagnosis", {})
    hedge_instruments = result.get("hedge_instruments", [])
    opportunities = result.get("opportunities", [])
    sector_summary = result.get("sector_summary", "")
    candidates_screened = result.get("candidates_screened", 0)

    concentration_risk = diagnosis.get("concentration_risk", "")
    macro_risk = diagnosis.get("macro_risk", "")
    hedge_directions = diagnosis.get("hedge_directions", [])

    elements = []

    # ── 本周速览 TL;DR（置顶：漂移 + 指导待办状态）──────────────────────────────
    drift_rows = result.get("drift_rows", [])
    adherence = result.get("guidance_adherence", {})
    tldr_bits = []
    reds = [r for r in drift_rows if r.get("status") == "🔴"]
    if reds:
        worst = max(reds, key=lambda r: abs(r["drift"]))
        tldr_bits.append(
            f"⚠️ {worst['label'].split('（')[0]}偏离最大"
            f"（当前{worst['current_pct']:.0f}%/目标{worst['target_pct']:.0f}%）"
        )
    n_open = len(adherence.get("open", []))
    n_exp = len(adherence.get("expired", []))
    if n_open or n_exp:
        g = f"指导待办 {n_open} 条"
        if n_exp:
            g += f"、过期未做 {n_exp} 条"
        tldr_bits.append(g)
    if tldr_bits:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**📋 本周速览**\n" + "　·　".join(tldr_bits)},
        })
        elements.append({"tag": "hr"})

    # ── 诊断摘要 ──────────────────────────────────────────────────────────────
    diag_lines = []
    if concentration_risk:
        diag_lines.append(f"**集中风险：** {concentration_risk}")
    if macro_risk:
        diag_lines.append(f"**宏观风险：** {macro_risk}")
    if sector_summary:
        diag_lines.append(f"**持仓分布：** {sector_summary}")

    if diag_lines:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(diag_lines)},
        })
        elements.append({"tag": "hr"})

    # ── 三桶配置漂移（P2）：精确表收进折叠，主屏速览已点出偏离最大的桶（减冗余）──
    drift_rows = result.get("drift_rows", [])
    if drift_rows:
        drift_lines = []
        for r in drift_rows:
            sign = "+" if r["drift"] >= 0 else ""
            drift_lines.append(
                f"{r['status']} {r['label']}：{r['target_pct']:.0f}% → "
                f"**{r['current_pct']:.0f}%**（漂移 {sign}{r['drift']:.0f}%）"
            )
        elements.append(collapsible_panel(
            "**📐 三桶配置漂移**（目标 → 当前·点开看全部）", "\n".join(drift_lines)))
        elements.append({"tag": "hr"})

    # ── 上期指导执行情况（P2）─────────────────────────────────────────────────
    adherence = result.get("guidance_adherence", {})
    if adherence and any(adherence.get(k) for k in ("followed", "expired", "open")):
        g_lines = ["**📋 指导执行情况**"]
        for it in adherence.get("followed", []):
            g_lines.append(f"✅ 已照做：{it['action']} **{it['ticker']}**")
        for it in adherence.get("expired", []):
            g_lines.append(f"❌ 未执行（已过期）：{it['action']} **{it['ticker']}** — {it.get('target', '')}")
        for it in adherence.get("open", []):
            g_lines.append(
                f"⏳ 进行中：{it['action']} **{it['ticker']}** — {it.get('target', '')}"
                f"（截止 {it.get('due_by', '')}）"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(g_lines)},
        })
        elements.append({"tag": "hr"})

    # ── Step 1: 对冲方向 ──────────────────────────────────────────────────────
    if hedge_directions:
        direction_lines = ["**🧭 建议对冲方向**"]
        for d in hedge_directions:
            emoji = URGENCY_EMOJI.get(d.get("urgency", "中"), "🟡")
            direction_lines.append(
                f"{emoji} **{d['direction']}**（{d.get('urgency', '')}优先级）\n"
                f"　　{d.get('rationale', '')}"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(direction_lines)},
        })
        elements.append({"tag": "hr"})

    # ── Step 2: 对冲品种 ──────────────────────────────────────────────────────
    if hedge_instruments:
        instr_lines = ["**📦 推荐对冲品种**"]
        for block in hedge_instruments:
            direction_name = block.get("direction", "")
            instr_lines.append(f"**{direction_name}**")
            for ins in block.get("instruments", []):
                market_label = {"us": "美股", "hk": "港股"}.get(ins.get("market", "us"), "")
                instr_lines.append(
                    f"　· **{ins['ticker']}** {ins.get('name', '')}（{market_label}）\n"
                    f"　　{ins.get('rationale', '')}  →  {ins.get('entry_hint', '')}"
                )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(instr_lines)},
        })
        elements.append({"tag": "hr"})

    # ── Step 3: 机会筛选 ──────────────────────────────────────────────────────
    if opportunities:
        opp_lines = [f"**🎯 当前机会（从 {candidates_screened} 只候选中筛出）**"]
        for op in opportunities:
            sig_emoji = SIGNAL_EMOJI.get(op.get("signal_strength", "中"), "✨")
            type_tag = TYPE_TAG.get(op.get("type", ""), "")
            fit = op.get("portfolio_fit", "")
            fit_tag = f"　⚠️ **{fit}**" if "叠加" in fit else (f"　✅ {fit}" if fit else "")
            opp_lines.append(
                f"{sig_emoji} **{op['ticker']}**　{type_tag} — {op.get('reason', '')}\n"
                f"　　建议仓位：{op.get('suggested_position', '')}{fit_tag}"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(opp_lines)},
        })
        elements.append({"tag": "hr"})
    elif candidates_screened > 0:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"🎯 **机会筛选**：初筛通过 {candidates_screened} 只，当前无明显高质量机会",
            },
        })
        elements.append({"tag": "hr"})

    # ── 近期复盘 ──────────────────────────────────────────────────────────────
    weekly_stats = result.get("weekly_stats", {})
    if weekly_stats.get("available"):
        ws = weekly_stats
        REC_EMOJI = {"买入": "🟢", "持有": "🟡", "观望": "🟡",
                     "减仓": "🟠", "卖出": "🔴", "按计划定投": "⬜"}
        recap_lines = [
            f"**📊 近期复盘**（{ws['period']}）",
            f"方向性建议命中率 **{ws['win_rate']}%**（近14天口径·模型判断对错）　✅ {ws['correct']} 正确　❌ {ws['wrong']} 错误　共 {ws['total']} 条"
            + (f"　（另 {ws['hold_n']} 条持有/定投不计方向，其中 {ws['hold_ok']} 条 7 日未大跌）"
               if ws.get("hold_n") else ""),
        ]
        if ws.get("best"):
            b = ws["best"]
            sign = "+" if b["ret"] > 0 else ""
            recap_lines.append(
                f"✅ 最准：**{b['ticker']}** {REC_EMOJI.get(b['rec'], '⬜')}{b['rec']}"
                f" → 实际 {sign}{b['ret']:.1f}%"
            )
        if ws.get("worst"):
            w = ws["worst"]
            sign = "+" if w["ret"] > 0 else ""
            recap_lines.append(
                f"❌ 最偏：**{w['ticker']}** {REC_EMOJI.get(w['rec'], '⬜')}{w['rec']}"
                f" → 实际 {sign}{w['ret']:.1f}%"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(recap_lines)},
        })
        elements.append({"tag": "hr"})

    # ── 名词解释 ──────────────────────────────────────────────────────────────
    full_text = (
        sector_summary + " " + concentration_risk + " " + macro_risk + " "
        + " ".join(d.get("direction", "") + " " + d.get("rationale", "") for d in hedge_directions)
        + " ".join(
            ins.get("rationale", "") + " " + ins.get("entry_hint", "")
            for block in hedge_instruments for ins in block.get("instruments", [])
        )
        + " ".join(
            op.get("reason", "") + " " + op.get("portfolio_fit", "")
            for op in opportunities
        )
    )
    glossary_el = build_glossary_element(full_text, max_terms=4)
    if glossary_el:
        elements.append(glossary_el)

    # ── 免责声明 ──────────────────────────────────────────────────────────────
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": "以上为 AI 辅助配置建议，仅供参考，不构成投资建议，请结合自身风险偏好决策"}
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"📊 卡门智投 · 周度配置建议 · {__import__('datetime').date.today():%m-%d}"},
            "template": "indigo",
        },
        "elements": elements,
    }
