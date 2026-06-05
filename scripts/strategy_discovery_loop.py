#!/usr/bin/env python3
"""
策略发现自跑循环脚本。

每次调用完成一个「策略 × 全股票池」的评估，保存结果，
由 Claude ScheduleWakeup 驱动，无需人工干预。

状态文件：data/strategy_results/state.json
  {
    "iteration": N,
    "completed_strategies": [...],
    "all_results": [...],       # 所有迭代的 EvalResult
    "valid_strategies": [...]   # avg_alpha > 0 且 n >= 20 的汇总
  }

终止条件（由 Claude 判断）：
  - 所有 STRATEGY_REGISTRY 策略已跑完
  - 预算用完
"""
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

# 确保 src 在路径中
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from finance_agent.backtest.discovery import run_strategy_on_universe
from finance_agent.backtest.strategies import STRATEGY_REGISTRY
from finance_agent.backtest.universe import US_UNIVERSE

RESULTS_DIR   = Path("data/strategy_results")
STATE_FILE    = RESULTS_DIR / "state.json"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ── 预算控制 ──────────────────────────────────────────────────
# 最多跑 9 轮（约 60% 覆盖），之后切换为"整合模式"
MAX_ITERATIONS = 15
# 累计运行超过 10800s（3h）也触发停止（5h × 60%）
MAX_SECONDS = 10800


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "iteration": 0,
        "completed_strategies": [],
        "all_results": [],
        "valid_strategies": [],
        "start_time": time.time(),
        "total_elapsed": 0.0,
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def run_one_iteration() -> dict:
    state = load_state()
    completed = set(state["completed_strategies"])
    total_elapsed = state.get("total_elapsed", 0.0)
    iteration = state["iteration"] + 1

    # ── 预算检查 ──────────────────────────────────────────────
    if iteration > MAX_ITERATIONS:
        print(f"\n[Budget] 已达最大轮数 {MAX_ITERATIONS}，切换整合模式")
        return state
    if total_elapsed > MAX_SECONDS:
        print(f"\n[Budget] 累计用时 {total_elapsed/3600:.1f}h，超过预算上限，停止迭代")
        return state

    budget_pct = (iteration - 1) / MAX_ITERATIONS * 100
    print(f"[Budget] 进度 {budget_pct:.0f}%  已用 {total_elapsed/60:.0f}min  "
          f"（上限 {MAX_ITERATIONS} 轮 / {MAX_SECONDS//3600}h）")

    # 选下一个未跑的策略
    pending = [s for s in STRATEGY_REGISTRY if s not in completed]
    if not pending:
        print("\n[Loop] 所有策略已评估完毕！")
        return state

    strategy = pending[0]
    print(f"\n{'='*60}")
    print(f"[Loop] 第 {iteration} 轮  策略: {strategy}")
    print(f"       已完成: {len(completed)}/{len(STRATEGY_REGISTRY)}")
    print(f"       剩余:   {len(pending) - 1} 个策略")
    print(f"{'='*60}")

    t0 = time.time()
    results = run_strategy_on_universe(strategy, US_UNIVERSE, verbose=True)
    elapsed = time.time() - t0

    # 序列化结果
    results_dicts = [asdict(r) for r in results]
    state["all_results"].extend(results_dicts)
    state["completed_strategies"].append(strategy)
    state["iteration"] = iteration
    state["total_elapsed"] = total_elapsed + elapsed

    # 更新有效策略库（去重，按 avg_alpha 降序）
    all_valid = [r for r in state["all_results"] if r["passes"]]
    # 同一 (ticker, strategy) 只保留最新
    seen = set()
    deduped = []
    for r in all_valid:
        key = (r["ticker"], r["strategy"])
        if key not in seen:
            deduped.append(r)
            seen.add(key)
    deduped.sort(key=lambda r: r["avg_alpha"], reverse=True)
    state["valid_strategies"] = deduped

    save_state(state)

    # ── 本轮摘要 ──────────────────────────────────────────────
    passing = [r for r in results if r.passes]
    print(f"\n{'─'*60}")
    print(f"[Loop] 第 {iteration} 轮完成  耗时 {elapsed:.0f}s")
    print(f"  策略 {strategy}：{len(results)} 只股票  "
          f"有效（avg_alpha>0, n≥20）: {len(passing)} 只")
    if passing:
        print(f"  Top 5 有效股票:")
        for r in passing[:5]:
            print(f"    {r.ticker:6s}  α={r.avg_alpha:+.2f}%  "
                  f"beat={r.beat_rate:.0%}  n={r.n_signals}")
    print(f"\n  累计有效组合: {len(state['valid_strategies'])} 个")
    print(f"  剩余策略: {pending[1:] if len(pending) > 1 else '（无，已全部完成）'}")
    print(f"{'─'*60}")

    # 保存本轮详细结果到单独文件
    iter_file = RESULTS_DIR / f"iteration_{iteration:02d}_{strategy}.json"
    with open(iter_file, "w") as f:
        json.dump({"strategy": strategy, "results": results_dicts}, f,
                  ensure_ascii=False, indent=2)
    print(f"  详细结果 → {iter_file}")

    return state


def print_leaderboard(state: dict, top_n: int = 15) -> None:
    valid = state.get("valid_strategies", [])
    if not valid:
        print("\n[排行榜] 暂无通过门槛的策略")
        return
    print(f"\n{'='*60}")
    print(f"[排行榜] 有效策略 Top {min(top_n, len(valid))}（avg_alpha>0, n≥20）")
    print(f"{'─'*60}")
    print(f"{'策略':<20} {'股票':<8} {'α均值':>8} {'胜率':>6} {'n':>5}")
    print(f"{'─'*60}")
    for r in valid[:top_n]:
        print(f"{r['strategy']:<20} {r['ticker']:<8} "
              f"{r['avg_alpha']:>+7.2f}%  {r['beat_rate']:>5.0%}  {r['n_signals']:>4d}")
    print(f"{'='*60}")


if __name__ == "__main__":
    state = run_one_iteration()
    print_leaderboard(state)
    remaining = [s for s in STRATEGY_REGISTRY
                 if s not in state["completed_strategies"]]
    if remaining:
        print(f"\n[Loop] 下一轮将评估: {remaining[0]}")
        print(f"       还剩 {len(remaining)} 个策略未跑")
    else:
        print("\n[Loop] 全部完成！可以整合有效策略了。")
