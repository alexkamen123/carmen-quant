# scripts/backtest_l2b.py
"""
L2b 实盘反馈注入的历史重放回测（task #27）。

对每个测试日 D：
  1. git 恢复 D 当日 portfolio.yaml（点时间仓位）
  2. yfinance 重建截至 D-1 的技术信号（绝不用 D 之后的数据）
  3. DeepSeek 重生成多空辩论（新闻留空——无法点时间重建，但 A/B 共享同一份，内部公平）
  4. PM 批量裁决 3 次：A=含 as-of 实盘反馈注入，B/B2=不含（B vs B2 = 温度噪声地板）
  5. 按 D 之后 7 交易日真实价格路径计分

诚实边界（写进报告）：
  - thesis 用当前版本（点时间 thesis 没存档；A/B 共享，公平性不受影响）
  - 新闻/基本面留空、量化策略证据两臂都不注入（隔离 live_feedback 单变量）
  - in-sample：卖早签名提取自同段历史，本回测证明"机制按预期工作+历史上值多少钱"，
    不证明未来收益（未来靠真实运行 out-of-sample）
用法：uv run python scripts/backtest_l2b.py [--days 2026-06-01,2026-06-02] [--smoke]
"""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parents[1]
load_dotenv(ROOT / ".env")

from openai import AsyncOpenAI  # noqa: E402
import os  # noqa: E402

from finance_agent.agents.prompts import (BEAR_SYSTEM, BEAR_USER, BULL_SYSTEM,  # noqa: E402
                                          BULL_USER, PM_BATCH_STOCK_TEMPLATE,
                                          PM_BATCH_SYSTEM, PM_BATCH_USER)
from finance_agent.db.tracker import (_conn, _fetch_paired_window,  # noqa: E402
                                      format_live_feedback)
from finance_agent.signals.technical import calculate_signals  # noqa: E402
import yfinance as yf  # noqa: E402

CACHE = ROOT / "data" / "backtest_cache"
# 方向性样本 as-of 累积决定弹药期：06-04 起 07709/MU 过闸门（见 task #27 侦察）。
# 05-15/06-01 留作对照日（闸门应静默）；06-04 起为实测日。
# 注意 06-04 之后的 7td 窗口在 2026-06-15 前未走满，先按"至今收益"计分，窗口补全后重评。
DEFAULT_DAYS = ["2026-05-15", "2026-06-01",
                "2026-06-04", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10"]
ETF_SECTORS = {"宽基ETF", "股息ETF"}
FWD_TD = 7
_SEM = asyncio.Semaphore(3)

# 激进度刻度：rank(A) < rank(B) = A 更保守
_REC_RANK = {"买入": 2, "持有": 0, "观望": 0, "减仓": -1, "卖出": -2, "按计划定投": 0}
_POS_RANK_PREFIX = (("大加", 2), ("小加", 1), ("维持", 0), ("减仓", -1))


def _agg_rank(verdict: dict) -> int:
    r = _REC_RANK.get(verdict.get("recommendation", ""), 0)
    pos = verdict.get("position_change") or ""
    p = next((v for k, v in _POS_RANK_PREFIX if pos.startswith(k)), 0)
    return r + p


async def _deepseek(system: str, user: str, max_tokens: int, temperature: float) -> str:
    client = AsyncOpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                         base_url="https://api.deepseek.com", timeout=180.0)
    async with _SEM:
        resp = await client.chat.completions.create(
            model="deepseek-chat", temperature=temperature, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
    return resp.choices[0].message.content.strip()


def portfolio_asof(day: str) -> dict:
    """git 恢复 D 当日（含当天）最后一次提交的 portfolio.yaml。"""
    sha = subprocess.run(
        ["git", "rev-list", "-1", f"--before={day}T23:59:59+08:00", "main",
         "--", "config/portfolio.yaml"],
        capture_output=True, text=True, cwd=ROOT).stdout.strip()
    if not sha:
        raise RuntimeError(f"{day} 之前没有 portfolio.yaml 提交记录")
    raw = subprocess.run(["git", "show", f"{sha}:config/portfolio.yaml"],
                         capture_output=True, text=True, cwd=ROOT).stdout
    return yaml.safe_load(raw)


def _yf_symbol(ticker: str, market: str) -> str:
    return f"{int(ticker):04d}.HK" if market == "hk" else ticker


def signals_asof(ticker: str, market: str, day: str):
    """截至 D-1 收盘的技术信号（end=day 在 yf 语义里不含 day 当日）。"""
    df = yf.download(_yf_symbol(ticker, market), period="6mo", interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df = df[df.index < pd.Timestamp(day)]          # 硬切：D 当日及之后的 bar 全部丢弃
    if len(df) < 20:
        return None
    return calculate_signals(df, ticker=ticker)


def thesis_for(ticker: str) -> str:
    with _conn(ROOT / "data" / "agent.db") as con:
        row = con.execute("SELECT thesis_text FROM theses WHERE ticker = ?",
                          (ticker.upper(),)).fetchone()
    return (row["thesis_text"] if row else "") or "暂未记录持仓逻辑"


async def debates_for(day: str, stocks: list[dict]) -> dict:
    """重生成多空辩论（缓存到磁盘，重跑免费）。新闻留空，A/B 共享。"""
    cache_f = CACHE / f"debates_{day}.json"
    if cache_f.exists():
        return json.loads(cache_f.read_text())
    out = {}

    async def _one(s):
        sig_str = s["signals"].to_prompt_str()
        common = dict(signals_str=sig_str, fundamental_view="暂无基本面数据",
                      news_str="暂无新闻")
        bull, bear = await asyncio.gather(
            _deepseek(BULL_SYSTEM, BULL_USER.format(**common), 400, 0.3),
            _deepseek(BEAR_SYSTEM, BEAR_USER.format(**common), 400, 0.3))
        out[s["ticker"]] = {"bull": bull, "bear": bear}
        print(f"  [debate] {day} {s['ticker']} done", flush=True)

    await asyncio.gather(*[_one(s) for s in stocks])
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps(out, ensure_ascii=False))
    return out


def build_stock_block(s: dict, debates: dict, evidence: str) -> str:
    cost = s.get("cost_basis")
    px = s["signals"].close
    cost_str = (f"均价 {cost}，浮盈 {((px - cost) / cost * 100):+.1f}%"
                if cost else "未记录")
    d = debates.get(s["ticker"], {})
    return PM_BATCH_STOCK_TEMPLATE.format(
        ticker=s["ticker"], market={"us": "美股", "hk": "港股"}.get(s["market"], s["market"]),
        position_pct=s["position_pct"], shares=s.get("shares", 0),
        current_price=round(px, 2), cost_basis_str=cost_str,
        thesis=s["thesis"], signals_str=s["signals"].to_prompt_str(),
        strategy_evidence=evidence,
        fundamental_view="暂无基本面数据（回测重放）",
        bull_thesis=d.get("bull", "无"), bear_thesis=d.get("bear", "无"),
        next_earnings_date="未知")


async def pm_verdict(day: str, stocks: list[dict], debates: dict,
                     inject: bool, label: str) -> dict:
    blocks = []
    for s in stocks:
        evidence = (format_live_feedback(s["ticker"], asof=day) if inject else "")
        blocks.append(build_stock_block(s, debates, evidence))
    user_msg = PM_BATCH_USER.format(
        macro_summary=f"（回测重放 {day}：宏观区块省略，请按个股材料独立判断）",
        strategy_limits="", exposure_note="回测重放：机制信号省略",
        sector_summary="（回测重放：集中度区块省略）",
        n=len(stocks), stocks_block="\n\n".join(blocks))
    raw = await _deepseek(PM_BATCH_SYSTEM, user_msg, 3000, 0.2)
    start, end = raw.find("["), raw.rfind("]") + 1
    decisions = json.loads(raw[start:end])
    print(f"  [pm:{label}] {day} {len(decisions)} 只裁决完成", flush=True)
    return {d.get("ticker", "").upper(): d for d in decisions}


def forward_return(ticker: str, market: str, day: str) -> float | None:
    win = _fetch_paired_window(ticker, market, day, fwd_td=FWD_TD)
    if win is None:
        return None
    p0, p_exit, *_ = win
    return round((p_exit - p0) / p0 * 100, 2)


async def run_day(day: str) -> dict:
    print(f"\n=== {day} ===", flush=True)
    pf = portfolio_asof(day)
    holdings = [h for h in pf.get("holdings", [])
                if h.get("sector", "") not in ETF_SECTORS and not h.get("is_dca", False)]
    total_usd = 0.0
    stocks = []
    for h in holdings:
        sig = signals_asof(h["ticker"], h.get("market", "us"), day)
        if sig is None:
            print(f"  [skip] {h['ticker']} 无点时间行情", flush=True)
            continue
        fx = 7.8 if h.get("market") == "hk" else 1.0
        usd = sig.close * h.get("shares", 0) / fx
        total_usd += usd
        stocks.append({"ticker": h["ticker"].upper(), "market": h.get("market", "us"),
                       "shares": h.get("shares", 0), "cost_basis": h.get("cost_basis"),
                       "signals": sig, "usd": usd, "thesis": thesis_for(h["ticker"])})
    for s in stocks:
        s["position_pct"] = round(s["usd"] / total_usd * 100, 1) if total_usd else 0

    injected = {s["ticker"]: format_live_feedback(s["ticker"], asof=day) for s in stocks}
    n_inj = sum(1 for v in injected.values() if v)
    print(f"  as-of 注入覆盖：{n_inj}/{len(stocks)} 只", flush=True)
    if n_inj == 0:
        # 两臂 prompt 完全相同，跑 PM 是浪费——记录闸门验证结果即可
        return {"day": day, "n_stocks": len(stocks), "n_injected": 0,
                "note": "as-of 反馈全静默（对照时代），两臂 prompt 相同，跳过 PM"}

    debates = await debates_for(day, stocks)
    arm_a, arm_b, arm_b2 = await asyncio.gather(
        pm_verdict(day, stocks, debates, inject=True, label="A 注入"),
        pm_verdict(day, stocks, debates, inject=False, label="B 无注入"),
        pm_verdict(day, stocks, debates, inject=False, label="B2 噪声基线"))

    rows = []
    for s in stocks:
        t = s["ticker"]
        va, vb, vb2 = arm_a.get(t, {}), arm_b.get(t, {}), arm_b2.get(t, {})
        fwd = forward_return(t, s["market"], day)
        rows.append({
            "ticker": t, "injected": bool(injected[t]), "fwd_7td": fwd,
            "A": {k: va.get(k) for k in ("recommendation", "position_change")},
            "B": {k: vb.get(k) for k in ("recommendation", "position_change")},
            "B2": {k: vb2.get(k) for k in ("recommendation", "position_change")},
            "rank_A": _agg_rank(va), "rank_B": _agg_rank(vb), "rank_B2": _agg_rank(vb2),
        })
    return {"day": day, "n_stocks": len(stocks), "n_injected": n_inj, "rows": rows}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default=",".join(DEFAULT_DAYS))
    ap.add_argument("--smoke", action="store_true", help="只跑第一个有注入的日子")
    args = ap.parse_args()
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    if args.smoke:
        days = days[:2]   # 对照日 + 第一个实测日

    results = []
    for d in days:
        try:
            results.append(await run_day(d))
        except Exception as e:
            print(f"  [error] {d}: {type(e).__name__}: {e}", flush=True)
            results.append({"day": d, "error": str(e)})

    out_f = ROOT / "data" / "backtest_l2b_results.json"
    out_f.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n[done] 明细已存 {out_f}", flush=True)

    # ── 汇总 ──
    sig_days = [r for r in results if r.get("rows")]
    diff_ab = diff_noise = n_inj_rows = 0
    money_lines = []
    for r in sig_days:
        for row in r["rows"]:
            if row["rank_A"] != row["rank_B"]:
                diff_ab += 1
                direction = "保守" if row["rank_A"] < row["rank_B"] else "⚠️激进"
                hit = ""
                if row["fwd_7td"] is not None:
                    good = (row["rank_A"] < row["rank_B"]) == (row["fwd_7td"] < 0)
                    hit = f"，后续7td {row['fwd_7td']:+.1f}% → {'判断对' if good else '判断错'}"
                money_lines.append(
                    f"  {r['day']} {row['ticker']}（注入={row['injected']}）："
                    f"A={row['A']} vs B={row['B']}（A 更{direction}{hit}）")
            if row["rank_B"] != row["rank_B2"]:
                diff_noise += 1
            if row["injected"]:
                n_inj_rows += 1
    n_rows = sum(len(r["rows"]) for r in sig_days)
    print(f"\n=== 汇总 ===")
    print(f"实测 {len(sig_days)} 天 × 共 {n_rows} 股次（其中注入 {n_inj_rows} 股次）")
    print(f"A vs B 方向差异：{diff_ab}；噪声基线 B vs B2 差异：{diff_noise}")
    print("差异明细：")
    print("\n".join(money_lines) or "  （无方向级差异）")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
