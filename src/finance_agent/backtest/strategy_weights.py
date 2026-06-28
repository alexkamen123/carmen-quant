# src/finance_agent/backtest/strategy_weights.py
"""L2c：策略族自适应权重表（评估→反哺参数的最后一根线）。

现状（PR 前）：策略族真实战绩（state.json 的 beat_rate/alpha）只「给 LLM 参考」，
从不修改任何排序/权重。本模块补上闭环：每轮 backfill 后，按各策略族近期真实
beat_rate 朴素升降权，下一轮 get_active_signals 读这张表给信号排序加权。

设计纪律（避免剧烈震荡、保持可解释、可关闭、可回溯）：
  - 目标权重 target = clamp(1 + sensitivity * (beat_rate - 0.5) ...) 线性、可解释；
  - 阻尼 damping：new = old + damping * (target - old)，一步只走一小段；
  - 上下限 clamp：权重锁在 [min_weight, max_weight]，绝不归零或翻倍；
  - 样本闸门：族内 reliable_combos / total_signals 不足时该族保持 1.0（不乱动）；
  - 默认 ON 但步幅极小（damping/sensitivity 都小），默认行为几乎不变；
  - feature flag：settings strategy_weight_adaptation.enabled=false 一键关；
  - 可回溯：每次调整 append 一条变更记录到 data/strategy_weights_log.jsonl。

权重 = 排序乘数（>1 该族信号在 PM 材料里更靠前，<1 更靠后）。它【不】放大/缩小
任何金额或仓位，只影响「先给 PM 看哪条证据」，因此误差最坏=排序略偏，不会乱买。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

WEIGHTS_FILE = Path("data/strategy_weights.json")
CHANGELOG_FILE = Path("data/strategy_weights_log.jsonl")

# 默认参数：保守。enabled 默认开，但 damping/sensitivity 极小 → 单轮几乎不动权重。
_DEFAULTS = {
    "enabled": True,
    "sensitivity": 0.3,      # beat_rate 偏离 0.5 → 目标权重偏离 1 的斜率（每 +0.1 beat → +0.03 target）
    "damping": 0.1,          # 每轮只向目标走 10%，避免单轮剧烈震荡
    "min_weight": 0.7,       # 权重下限（绝不归零）
    "max_weight": 1.3,       # 权重上限（绝不翻倍）
    "min_combos": 3,         # 族内有效组合 < 此值则保持 1.0（小样本不乱动）
    "max_step": 0.05,        # 单轮单族最大变动幅度（硬阻尼兜底）
    "broad_edge_guardrail": False,  # 广历史 edge 护栏（默认关，启用前须人审；见下）
    "regime_aware_guardrail": False,  # 方案A：按行情切换的护栏（默认关；OOS 已验证 cycle12，见下）
}

# 方案A regime-conditional 护栏：读权重时按【当前行情】压「此行情下反指」的信号族。
# OOS 验证(cycle12 oos_regime.py，46票×3年 5-fold)：regime-switching 在没见过的票上每折赢
# static 过滤(7d 0.482% vs 0.379%、30d 1.575% vs 1.177%，均5/5)，非过拟合、能泛化。
# 各族 7 日 alpha 分行情快照——【因果行情】(SPY vs MA50，与运行时 market_regime_from_spy
# 及 cycle12 OOS 验证同口径) 重算，refresh_regime/oos_regime.py 可刷新。
# 经济意义：下跌趋势(SPY<MA50)里趋势/动量类(动量突破/均线多头/金叉/放量)被反复打脸→跌市压；
# 只有超卖反弹(rsi/boll)在熊市仍管用(抄反弹)→放。这正是 cycle12 OOS 每折赢 static 的那个规则。
# ⚠️ 同 cycle8 教训快照会陈旧；regime 护栏优先级高于 L2c 近期 beat(反指族不许被近期 beat 上调)。
_REGIME_EDGE_SNAPSHOT_DATE = "2026-06-29"   # 因果行情·46票×3年×24809笔
_REGIME_EDGE_7D = {
    "rsi":       {"up": 0.11, "down": 0.91},    # 超卖：两行情都正（熊市抄反弹仍灵）
    "ma_align":  {"up": 0.46, "down": -1.11},   # 均线多头：跌市趋势失效→反指
    "ma":        {"up": 0.07, "down": -0.61},   # 金叉：跌市反指
    "boll":      {"up": 0.31, "down": 0.63},    # 布林下轨：两行情都正
    "mom":       {"up": 0.61, "down": -0.93},   # 动量：涨市强、跌市被打脸→反指
    "vol_surge": {"up": 0.12, "down": -0.59},   # 放量：涨市正、跌市反指
}
_REGIME_REVERSE = -0.05    # 当前行情下 alpha 低于此 = 该行情反指，压到下限

_FAMILIES = ("rsi", "ma_align", "ma", "boll", "mom", "vol_surge")

# 广历史 edge 护栏：用 backtest_signal_edge 跑出的各族 7日 edge(precision−基率)当快照。
# 护栏：广历史 edge < _REVERSE_EDGE 的族，无论近期 beat 多高，目标权重压到下限、不许上调。
# 单向（只压反指、不动正/噪声族），保守；feature flag broad_edge_guardrail 默认关。
#
# 快照演进：
#   v1（2026-06-25，8票科技股）：ma=-0.082 判反指。但 cycle8 用 6行业/48票/35808样本复跑发现
#   那是【科技偏过拟合】——金叉 ma@7d 真实 edge=+0.0031（弱正、非反指），被旧快照冤枉降权；
#   而 vol_surge@7d 真实 edge=-0.0452（< -0.03）才是被旧快照漏掉的反指。故刷新为多元 universe 值，
#   护栏行为自动对调（ma 松绑、vol_surge 被压），逻辑不变。screen_signals.py 可复现刷新。
_BROAD_EDGE_SNAPSHOT_DATE = "2026-06-26"   # 6行业/48票/35808样本（去科技偏）
_BROAD_EDGE_7D = {
    "rsi": 0.0384, "boll": 0.0379, "ma_align": 0.0116,
    "mom": 0.0091, "ma": 0.0031,        # ma=金叉，多元 universe 弱正、非反指（旧科技快照看错）
    "vol_surge": -0.0452,               # vol_surge=放量，【仅7日】反指；30日 edge=+0.0115 是正——
                                        # 本护栏只门控 7 日视角加权（系统 outcome 也以 7日锚定），
                                        # 勿把此 7日快照外推到更长持有期；多 horizon 快照留后续 cycle。
}
_REVERSE_EDGE = -0.03    # edge 低于此视为反指，触发护栏压权


def _load_settings() -> dict:
    """从 config/settings.yaml 读 strategy_weight_adaptation 块，缺失回落 _DEFAULTS。
    路径与 tracker._load_settings_block 同口径（parents[3] → 项目根）。"""
    cfg = dict(_DEFAULTS)
    try:
        import yaml
        p = Path(__file__).parents[3] / "config" / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            user = s.get("strategy_weight_adaptation") or {}
            if isinstance(user, dict):
                cfg.update({k: user[k] for k in _DEFAULTS if k in user})
    except Exception:
        pass
    return cfg


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def load_weights(path: Path | None = None) -> dict:
    """读权重表；不存在或损坏时返回空表（调用方按 1.0 兜底）。"""
    p = path or WEIGHTS_FILE
    if not p.exists():
        return {"param_version": "v0", "weights": {}, "updated_at": None}
    try:
        with open(p) as f:
            d = json.load(f)
        if not isinstance(d, dict) or "weights" not in d:
            return {"param_version": "v0", "weights": {}, "updated_at": None}
        return d
    except Exception:
        return {"param_version": "v0", "weights": {}, "updated_at": None}


def get_strategy_weight(strategy: str, path: Path | None = None,
                        regime: str | None = None) -> float:
    """返回某具体策略（如 rsi_14_30）所属族的权重乘数。

    feature flag 关 / 表缺失 / 族无记录 → 1.0（完全不影响现有行为）。
    regime ('up'/'down')：方案A regime-conditional 护栏——flag 开且当前行情下该族反指时压到下限。
    """
    cfg = _load_settings()
    if not cfg["enabled"]:
        return 1.0
    fam = _family(strategy)
    w = load_weights(path)["weights"].get(fam)
    try:
        weight = float(w) if w is not None else 1.0
    except (TypeError, ValueError):
        weight = 1.0
    # 方案A（默认关）：当前行情下反指的族压到下限——OOS 已验证有真增量(cycle12)
    if cfg.get("regime_aware_guardrail") and regime in ("up", "down"):
        edge = _REGIME_EDGE_7D.get(fam, {}).get(regime)
        if edge is not None and edge < _REGIME_REVERSE:
            return cfg["min_weight"]
    return weight


def _family(strategy: str) -> str:
    """把 rsi_14_30 → rsi 等。顺序敏感：ma_align/vol_surge 必须排在 ma 前。"""
    for prefix in ("ma_align", "vol_surge", "rsi", "boll", "mom", "ma"):
        if strategy.startswith(prefix):
            return prefix
    return strategy.split("_")[0]


def _next_param_version(prev: str) -> str:
    """v3 → v4；非法/缺失回退 v1。仅在权重真正发生变化时递增。"""
    if isinstance(prev, str) and prev.startswith("v") and prev[1:].isdigit():
        return f"v{int(prev[1:]) + 1}"
    return "v1"


def compute_targets(edge: dict, cfg: dict) -> dict[str, float]:
    """由策略族 edge（compute_strategy_edge 输出）算各族目标权重 target。

    信号源 = 各族「真实 beat_rate」的 n 加权值（这是当前唯一充足的策略级真实战绩，
    见 value/strategy_scorecard 诚实纪律）。样本不足的族 target=1.0（不动）。
    target = clamp(1 + sensitivity * (beat - 0.5), min, max)。
    """
    targets: dict[str, float] = {}
    fam_map = {f["family"]: f for f in edge.get("families", [])}
    for fam in _FAMILIES:
        d = fam_map.get(fam)
        if not d or d.get("n_combos", 0) < cfg["min_combos"]:
            targets[fam] = 1.0
            continue
        beat = _family_beat_rate(fam, edge)
        if beat is None:
            targets[fam] = 1.0
            continue
        target = 1.0 + cfg["sensitivity"] * (beat - 0.5)
        target = _clamp(target, cfg["min_weight"], cfg["max_weight"])
        # 广历史 edge 护栏（默认关）：广历史反指族压到下限，不许近期 beat 把它上调
        if cfg.get("broad_edge_guardrail") and _BROAD_EDGE_7D.get(fam, 0.0) < _REVERSE_EDGE:
            target = cfg["min_weight"]
        targets[fam] = round(target, 4)
    return targets


def _family_beat_rate(fam: str, edge: dict) -> float | None:
    """族内 n 加权 beat_rate。edge 里没带 beat 时回退到 reliable_combos 占比。"""
    raw = edge.get("_raw_families", {}).get(fam)
    if raw and raw.get("total_signals"):
        return raw["beat_w"] / raw["total_signals"]
    d = next((x for x in edge.get("families", []) if x["family"] == fam), None)
    if d and d.get("n_combos"):
        # 退化口径：reliable 占比映射到 [0, 1]，居中 0.5（无证据时不动）
        return d["reliable_combos"] / d["n_combos"]
    return None


def update_weights_from_edge(edge: dict, path: Path | None = None,
                             log_path: Path | None = None) -> dict:
    """每轮 backfill 后调用：按真实 edge 朴素更新权重表。

    返回 {"changed": bool, "param_version": str, "weights": {...}, "deltas": {...}}。
    feature flag 关时直接返回当前表、changed=False（不写盘、不递增版本）。
    """
    cfg = _load_settings()
    cur = load_weights(path)
    if not cfg["enabled"]:
        return {"changed": False, "param_version": cur.get("param_version", "v0"),
                "weights": cur.get("weights", {}), "deltas": {}}

    targets = compute_targets(edge, cfg)
    old_w = cur.get("weights", {})
    new_w: dict[str, float] = {}
    deltas: dict[str, float] = {}
    for fam, target in targets.items():
        old = float(old_w.get(fam, 1.0))
        step = cfg["damping"] * (target - old)
        # 硬阻尼兜底：单轮变动不超过 max_step
        step = _clamp(step, -cfg["max_step"], cfg["max_step"])
        nw = round(_clamp(old + step, cfg["min_weight"], cfg["max_weight"]), 4)
        new_w[fam] = nw
        if abs(nw - old) > 1e-9:
            deltas[fam] = round(nw - old, 4)

    changed = bool(deltas)
    pv = cur.get("param_version", "v0")
    if changed:
        pv = _next_param_version(pv)
        out = {
            "param_version": pv,
            "weights": new_w,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        p = path or WEIGHTS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        _append_changelog(pv, targets, new_w, deltas, log_path)

    return {"changed": changed, "param_version": pv, "weights": new_w, "deltas": deltas}


def _append_changelog(param_version: str, targets: dict, weights: dict,
                      deltas: dict, log_path: Path | None = None) -> None:
    """每次真实调整 append 一条 JSONL 变更记录（可回溯：为什么这么调）。"""
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "param_version": param_version,
        "targets": targets,
        "weights": weights,
        "deltas": deltas,
    }
    p = log_path or CHANGELOG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
