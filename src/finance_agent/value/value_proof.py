"""价值证明·多维体温计：把 4 个孤儿读数只读聚进价值卡一个面板。

铁则：① 只读（用各读数的只读裁决·不触发月度记录/联网）；② 绝不相加（4 口径各异·只并列）；
③ 每读数 guarded（任一失败→该行"暂无数据"·不拖垮价值卡）；④ 样本不足诚实标注。
纯观测渲染层·不碰任何 recommendation/仓位/权重。
"""
from __future__ import annotations

_HEADER = "_以下是不同维度的体温计，各看各的口径，**不合成一个总分**；样本不足会诚实标注。_"


# ---- 只读读数（各自 guarded 由 gather 兜） ----
def _read_rankic():
    from finance_agent.value.rankic_monitor import rankic_decay_verdict
    return rankic_decay_verdict()             # 读 jsonl·无写；{status,n_measured,k_needed,current_ic}


def _read_oos():
    from finance_agent.backtest.oos_monitor import oos_decay_verdict
    return oos_decay_verdict()                # 读 jsonl·无 fetch；默认 horizon=7；{status,horizon,n_testable,current_edge}


def _read_sue(db_path):
    from finance_agent.value.sue_edge import sue_edge_reading
    return sue_edge_reading(db_path=db_path)  # 只读 DB


def _read_shadow():
    from finance_agent.value.shadow_ab import report_shadow_ab
    return report_shadow_ab()                 # 只读 jsonl 日志·非 sweep（不联网回填）；无 db_path 参数


def gather_thermometer_readings(db_path=None) -> dict:
    """4 读数各自 guarded 聚成 dict；任一失败该键 None。"""
    out = {}
    for key, fn in (("rankic", lambda: _read_rankic()),
                    ("oos", lambda: _read_oos()),
                    ("sue", lambda: _read_sue(db_path)),
                    ("shadow", lambda: _read_shadow())):
        try:
            out[key] = fn()
        except Exception:
            out[key] = None
    return out


# ---- 各维度渲染（verdict→中文 label·各套词表各写） ----
def _rankic_line(d):
    if not d:
        return "• 建议方向排序力：⚪ 暂无数据"
    lab = {"healthy": "✅ 方向仍有排序力", "decaying": "🚨 连续衰减·建议复核策略",
           "insufficient_history": "⚪ 可测月不足·暂不下结论"}.get(d.get("status"), d.get("status"))
    ic = d.get("current_ic")
    return (f"• 建议方向排序力（RankIC·方向vs7日超额）：{lab}"
            f"（可测月 {d.get('n_measured', 0)}/{d.get('k_needed', 2)}·当前 IC {ic}）")


def _oos_line(d):
    if not d:
        return "• 方案A 还有效吗：⚪ 暂无数据"
    lab = {"healthy": "✅ regime 增量仍在", "decaying": "🚨 衰减·建议复核护栏",
           "insufficient_regime_data": "⚪ 跌市样本不足·暂不下结论"}.get(d.get("status"), d.get("status"))
    return (f"• 方案A 还有效吗（walk-forward OOS·{d.get('horizon', 7)}日）：{lab}"
            f"（可测 {d.get('n_testable', 0)}·当前 edge {d.get('current_edge')}）")


def _sue_line(d):
    if not d:
        return "• 盈余惊喜漂移 edge：⚪ 暂无数据"
    b = d.get("beat", {}) or {}
    lab = {"edge_present": "✅ 漂移兑现", "no_edge": "❌ 无边际",
           "insufficient": "⚪ 样本不足·暂不下结论"}.get(b.get("verdict"), b.get("verdict"))
    return (f"• 盈余惊喜漂移 edge（beat 后30日vs基准）：{lab}"
            f"（beat n={b.get('n', 0)}·命中率 {b.get('hit_rate')}·均超额 {b.get('mean_excess')}）")


def _shadow_line(d):
    if not d:
        return "• 护栏 A/B 反事实：⚪ 暂无数据"
    lab = {"guardrail_helps": "✅ 护栏有帮助", "guardrail_hurts": "🚨 护栏有害·建议复核",
           "neutral": "➖ 中性", "insufficient": "⚪ 分歧样本不足·暂不下结论"}.get(d.get("verdict"), d.get("verdict"))
    return (f"• 护栏 A/B 反事实（开vs关·7日超额）：{lab}"
            f"（分歧 n={d.get('n', 0)}·on {d.get('on_mean')} / off {d.get('off_mean')}）")


def thermometer_section(thermo: dict) -> str:
    """渲染面板正文（markdown 多行·绝不相加·各行 guarded label）。"""
    thermo = thermo or {}
    lines = [_HEADER, "",
             _rankic_line(thermo.get("rankic")),
             _oos_line(thermo.get("oos")),
             _sue_line(thermo.get("sue")),
             _shadow_line(thermo.get("shadow"))]
    return "\n".join(lines)
