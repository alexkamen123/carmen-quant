# src/finance_agent/weekly/report_card.py
"""
将 allocation_advisor 的结构化结果渲染成飞书卡片。
"""

URGENCY_EMOJI = {"高": "🔴", "中": "🟡", "低": "🟢"}
SIGNAL_EMOJI = {"强": "🔥", "中": "✨"}


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
            fit = op.get("portfolio_fit", "")
            fit_tag = f"　⚠️ **{fit}**" if "叠加" in fit else (f"　✅ {fit}" if fit else "")
            opp_lines.append(
                f"{sig_emoji} **{op['ticker']}** — {op.get('reason', '')}\n"
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
            "title": {"tag": "plain_text", "content": "📊 卡门智投 · 周度配置建议"},
            "template": "indigo",
        },
        "elements": elements,
    }
