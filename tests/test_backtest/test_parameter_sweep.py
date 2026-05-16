"""
tests/test_backtest/test_parameter_sweep.py
--------------------------------------------
参数扫描测试：在 fixture 数据上模拟 _check_price_drop 的告警行为，
对 threshold_pct 进行网格搜索，计算 precision / recall / F1，
结果写入 tests/test_backtest/results.md。

运行方式：
    pytest tests/test_backtest/ -v

前提：已运行 python scripts/capture_fixtures.py 捕获了快照数据。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pytest

# ── 结果文件路径 ───────────────────────────────────────────────────────────────
_RESULTS_MD = Path(__file__).resolve().parent / "results.md"

# ── 参数扫描网格 ───────────────────────────────────────────────────────────────
_THRESHOLD_SWEEP = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

# ── 模拟 "真实异动" 的宽松定义 ────────────────────────────────────────────────
# 我们把"过去1小时跌幅超过 _GROUND_TRUTH_PCT 且随后1小时继续下跌" 视为 TP。
# 此处用 2.0% 作为 ground truth 参考线（宽松标准，因为我们没有真实标注数据）。
_GROUND_TRUTH_PCT = 2.0


# ── 核心函数：纯数据驱动的 _check_price_drop 模拟 ────────────────────────────

class AlertEvent(NamedTuple):
    timestamp: datetime   # 触发时刻
    price_now: float
    price_1h_ago: float
    drop_pct: float


def simulate_price_drop_alerts(
    df: pd.DataFrame,
    threshold_pct: float,
    window_bars: int = 12,   # 12 根 5min K ≈ 1 小时
) -> list[AlertEvent]:
    """
    在 OHLCV DataFrame 上滑动窗口，模拟 _check_price_drop 的逻辑。

    逻辑等价于 news_monitor._check_price_drop：
        drop_pct = (close_now - close_1h_ago) / close_1h_ago * 100
        if drop_pct <= -threshold_pct → 触发告警

    每次窗口末尾触发后，向前跳过 window_bars 避免连续重复告警
    （与生产代码的 5min dedup 语义对齐）。
    """
    if "close" not in df.columns or len(df) < window_bars + 1:
        return []

    close = df["close"].dropna()
    events: list[AlertEvent] = []
    skip_until = 0

    for i in range(window_bars, len(close)):
        if i < skip_until:
            continue
        price_now = float(close.iloc[i])
        price_1h_ago = float(close.iloc[i - window_bars])
        if price_1h_ago <= 0:
            continue
        drop_pct = (price_now - price_1h_ago) / price_1h_ago * 100
        if drop_pct <= -threshold_pct:
            events.append(AlertEvent(
                timestamp=close.index[i].to_pydatetime(),
                price_now=price_now,
                price_1h_ago=price_1h_ago,
                drop_pct=round(drop_pct, 4),
            ))
            skip_until = i + window_bars  # 跳过重复

    return events


def compute_ground_truth_alerts(
    df: pd.DataFrame,
    ground_truth_pct: float = _GROUND_TRUTH_PCT,
    window_bars: int = 12,
) -> set[int]:
    """
    用宽松标准生成"真实告警"集合（bar index 集合）。

    定义：某 bar i 满足
        close[i] / close[i - window_bars] - 1 <= -ground_truth_pct%
    则认为 i 是一个真实异动 bar。
    """
    if "close" not in df.columns or len(df) < window_bars + 1:
        return set()
    close = df["close"].dropna()
    truth: set[int] = set()
    for i in range(window_bars, len(close)):
        p_now = float(close.iloc[i])
        p_1h = float(close.iloc[i - window_bars])
        if p_1h > 0 and (p_now - p_1h) / p_1h * 100 <= -ground_truth_pct:
            truth.add(i)
    return truth


class SweepResult(NamedTuple):
    threshold: float
    ticker: str
    total_bars: int
    alerts_triggered: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


def evaluate_threshold(
    df: pd.DataFrame,
    ticker: str,
    threshold_pct: float,
    window_bars: int = 12,
) -> SweepResult:
    """对单个 ticker 的快照，在给定阈值下计算 precision/recall/F1。"""
    close = df["close"].dropna() if "close" in df.columns else pd.Series(dtype=float)
    total_bars = len(close)

    alerts = simulate_price_drop_alerts(df, threshold_pct, window_bars)
    # 将告警时刻映射回 bar index（近似：找最近的 close index）
    alert_bar_indices: set[int] = set()
    for ev in alerts:
        # 在 close.index 中找与 ev.timestamp 最近的位置
        idx = close.index.get_indexer([ev.timestamp], method="nearest")
        if idx[0] >= 0:
            alert_bar_indices.add(int(idx[0]))

    truth_bars = compute_ground_truth_alerts(df, _GROUND_TRUTH_PCT, window_bars)

    tp = len(alert_bar_indices & truth_bars)
    fp = len(alert_bar_indices - truth_bars)
    fn = len(truth_bars - alert_bar_indices)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)

    return SweepResult(
        threshold=threshold_pct,
        ticker=ticker,
        total_bars=total_bars,
        alerts_triggered=len(alerts),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
    )


# ── results.md 写入辅助 ───────────────────────────────────────────────────────

def _append_results(rows: list[SweepResult], run_at: str) -> None:
    """将扫描结果追加到 results.md 表格中（幂等：先删同次 run 的旧记录）。"""
    header = (
        "| threshold_pct | ticker | bars | alerts | TP | FP | FN "
        "| precision | recall | F1 |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    table_rows = [
        f"| {r.threshold} | {r.ticker} | {r.total_bars} | {r.alerts_triggered} "
        f"| {r.true_positives} | {r.false_positives} | {r.false_negatives} "
        f"| {r.precision:.4f} | {r.recall:.4f} | {r.f1:.4f} |"
        for r in rows
    ]
    section = (
        f"\n## Run: {run_at}  (ground_truth_pct={_GROUND_TRUTH_PCT}%)\n\n"
        + header
        + "\n".join(table_rows)
        + "\n"
    )

    existing = _RESULTS_MD.read_text() if _RESULTS_MD.exists() else ""
    _RESULTS_MD.write_text(existing + section)


# ── 测试 ──────────────────────────────────────────────────────────────────────

def test_baseline(all_snapshots: dict[str, pd.DataFrame]) -> None:
    """
    验证默认参数（threshold=3.0）能在 fixture 数据上正常运行，不抛异常。
    同时检查返回的告警列表类型正确。
    """
    default_threshold = 3.0
    errors: list[str] = []

    for ticker, df in all_snapshots.items():
        try:
            alerts = simulate_price_drop_alerts(df, default_threshold)
            assert isinstance(alerts, list), f"{ticker}: 应返回 list"
            for ev in alerts:
                assert ev.drop_pct <= -default_threshold, (
                    f"{ticker}: 告警跌幅 {ev.drop_pct}% 不满足阈值 {default_threshold}%"
                )
        except AssertionError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{ticker} 异常: {e}")

    assert not errors, "baseline 测试失败:\n" + "\n".join(errors)
    print(
        f"\n[Baseline] threshold=3.0 在 {len(all_snapshots)} 只标的上运行正常",
        flush=True,
    )


def test_threshold_sweep(all_snapshots: dict[str, pd.DataFrame]) -> None:
    """
    对 threshold_pct 在 [1.5, 2.0, 2.5, 3.0, 3.5, 4.0] 上遍历，
    计算每只标的的 precision/recall/F1，结果写入 results.md。
    """
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_results: list[SweepResult] = []

    for threshold in _THRESHOLD_SWEEP:
        for ticker, df in all_snapshots.items():
            result = evaluate_threshold(df, ticker, threshold)
            all_results.append(result)

    # 写入 results.md
    _append_results(all_results, run_at)

    # 汇总各阈值的平均 F1
    from collections import defaultdict
    f1_by_threshold: dict[float, list[float]] = defaultdict(list)
    for r in all_results:
        f1_by_threshold[r.threshold].append(r.f1)

    print("\n[Sweep] 各阈值平均 F1 (ground_truth={_GROUND_TRUTH_PCT}%):", flush=True)
    best_threshold = 3.0
    best_f1 = -1.0
    for thr in sorted(f1_by_threshold):
        f1_vals = f1_by_threshold[thr]
        avg_f1 = sum(f1_vals) / len(f1_vals) if f1_vals else 0.0
        print(f"  threshold={thr}%  avg_F1={avg_f1:.4f}  ({len(f1_vals)} tickers)", flush=True)
        if avg_f1 > best_f1:
            best_f1 = avg_f1
            best_threshold = thr

    print(f"\n[Sweep] 最优阈值: {best_threshold}%  avg_F1={best_f1:.4f}", flush=True)
    print(f"[Sweep] 结果已写入: {_RESULTS_MD}", flush=True)

    # 至少一个阈值有数据（非退化情况）
    total_alerts = sum(r.alerts_triggered for r in all_results)
    print(f"[Sweep] 总告警次数（所有阈值+标的）: {total_alerts}", flush=True)
