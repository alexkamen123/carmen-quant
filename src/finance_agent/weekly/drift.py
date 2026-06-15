# src/finance_agent/weekly/drift.py
"""
三桶配置漂移计算 + 规则化纠偏指导生成。

三桶模型：
  passive 被动Beta桶（宽基/股息ETF）
  hedge   对冲桶（黄金/短债）
  ammo    弹药/战术桶（单股 + 板块ETF）

漂移表回答"离计划偏了多远、往哪纠"；derive_guidance 把红线突破转成可追踪的指导项。
"""
from datetime import datetime, timedelta

HEDGE_TICKERS = {"IAU", "BIL", "SGOV", "GLD", "SLV"}

BUCKET_LABEL = {
    "passive": "被动Beta桶（宽基/股息ETF）",
    "hedge":   "对冲桶（黄金/短债）",
    "ammo":    "弹药桶（单股/战术）",
}

# 目标比例缺省值（来自 portfolio.yaml strategy.monthly_dca）
# 注：monthly_dca 原义是「月定投资金分配」(flow)，此处复用作组合配置参照，
# 与 client-review 报告口径一致；属有意简化，漂移表是方向性指导而非精确再平衡。
_DEFAULT_TARGET = {"passive": 50, "hedge": 25, "ammo": 25}

DRIFT_BAND = 5.0  # |漂移| 在此带内算达标 ✅


def bucket_of(holding: dict) -> str:
    """把一只持仓归入三桶之一（ticker/sector 启发式）。"""
    ticker = str(holding.get("ticker", "")).upper()
    if ticker in HEDGE_TICKERS:
        return "hedge"
    sector = holding.get("sector", "") or ""
    if holding.get("is_dca") or "宽基" in sector or "股息" in sector:
        return "passive"
    return "ammo"


def compute_drift(bucket_value: dict, total_value: float, strategy: dict) -> list[dict]:
    """
    目标 vs 当前，返回三桶漂移行。
    bucket_value: {bucket: usd}；目标取 strategy.monthly_dca，缺失用默认 50/25/25。
    """
    dca = (strategy or {}).get("monthly_dca", {})
    target = {
        "passive": dca.get("passive_beta_pct", _DEFAULT_TARGET["passive"]),
        "hedge":   dca.get("hedge_pct", _DEFAULT_TARGET["hedge"]),
        "ammo":    dca.get("ammo_pct", _DEFAULT_TARGET["ammo"]),
    }
    rows = []
    for bucket in ("passive", "hedge", "ammo"):
        cur_pct = (bucket_value.get(bucket, 0.0) / total_value * 100) if total_value > 0 else 0.0
        tgt = float(target[bucket])
        drift = cur_pct - tgt
        rows.append({
            "bucket": bucket,
            "label": BUCKET_LABEL[bucket],
            "target_pct": round(tgt, 1),
            "current_pct": round(cur_pct, 1),
            "drift": round(drift, 1),
            # 三档（修 P2：原只有 ✅/🔴，+5.1% 与 +20% 同显红灯，缺过渡）：
            # 带内 ✅ / 带外~2倍带 🟡 提示注意 / 超 2 倍带 🔴 需纠偏
            "status": ("✅" if abs(drift) <= DRIFT_BAND
                       else "🟡" if abs(drift) <= 2 * DRIFT_BAND else "🔴"),
        })
    return rows


def derive_guidance(drift_rows: list[dict], holding_value: dict, total_value: float,
                    strategy: dict, due_days: int = 14) -> list[dict]:
    """
    基于漂移 + 风险红线，确定性生成纠偏指导项（每项可被 user_actions 检验执行）。
    holding_value: {ticker: usd_value}，用于单票集中度判定。
    只生成可映射到 BUY/SELL 的可追踪项；「停止加仓半导体」这类约束由卡片的集中度警告承担。
    """
    due_by = (datetime.today() + timedelta(days=due_days)).strftime("%Y-%m-%d")
    strategy = strategy or {}
    drift_by_bucket = {r["bucket"]: r for r in drift_rows}
    hedge_floor = float(strategy.get("hedge_bucket", {}).get("target_pct", 12.0))

    items: list[dict] = []

    # 1) 对冲桶低于下限 → 建仓
    hedge_row = drift_by_bucket.get("hedge")
    if hedge_row and hedge_row["current_pct"] < hedge_floor:
        items.append({
            "ticker": "IAU,SGOV", "action": "建仓",
            "target": f"补对冲桶至≥{hedge_floor:.0f}%（当前{hedge_row['current_pct']:.0f}%）",
            "rationale": "对冲桶低于下限，缺乏下跌缓冲", "due_by": due_by,
        })

    # 2) 单票超 15% 红线 → 减仓
    if total_value > 0:
        for ticker, val in sorted(holding_value.items()):
            pct = val / total_value * 100
            if pct > 15.0:
                items.append({
                    "ticker": ticker, "action": "减仓",
                    "target": f"减至<15%（当前{pct:.0f}%）",
                    "rationale": "单票超集中度红线15%", "due_by": due_by,
                })

    # 3) 被动桶严重低配（< 目标×0.6）→ 补仓
    passive_row = drift_by_bucket.get("passive")
    if passive_row and passive_row["current_pct"] < passive_row["target_pct"] * 0.6:
        items.append({
            "ticker": "QQQM,VOO", "action": "补仓",
            "target": f"逐月定投补被动桶至{passive_row['target_pct']:.0f}%"
                      f"（当前{passive_row['current_pct']:.0f}%）",
            "rationale": "被动Beta桶严重低配", "due_by": due_by,
        })

    return items
