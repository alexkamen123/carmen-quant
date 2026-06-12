#!/usr/bin/env python3
"""一次性脚本：拉实时价 → 算当前市值/板块占比 → 算把超配标的削到 <=15% 需减几股。
仅本地运行，不写任何远程。"""
import os, sys, yaml
from pathlib import Path

# Yahoo 在海外，走代理
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:8118")
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:8118")

import yfinance as yf

ROOT = Path(__file__).parents[1]
cfg = yaml.safe_load(open(ROOT / "config" / "portfolio.yaml"))
holdings = cfg["holdings"]

# 港股代码 → yfinance 格式（去前导零补 .HK），并记录币种
def yf_symbol(h):
    t, m = h["ticker"], h["market"]
    if m == "hk":
        return f"{int(t):04d}.HK"   # 00700 -> 0700.HK
    return t

# 简易汇率（仅用于折算总览，精度足够决策）
FX = {"usd": 7.20, "hkd": 0.92, "cn": 1.0}  # 1 USD≈7.20 CNY, 1 HKD≈0.92 CNY

def last_price(sym):
    try:
        fi = yf.Ticker(sym).fast_info
        p = fi.get("last_price") or fi.get("lastPrice")
        if p:
            return float(p)
    except Exception:
        pass
    # 回退：取最近收盘
    df = yf.Ticker(sym).history(period="5d")
    if not df.empty:
        return float(df["Close"].dropna().iloc[-1])
    return None

rows = []
for h in holdings:
    sym = yf_symbol(h)
    price = last_price(sym)
    cur = h["market"]  # 计价币种 us=USD hk=HKD
    ccy = {"us": "usd", "hk": "hkd", "cn": "cn"}[cur]
    mv_local = price * h["shares"] if price else None
    mv_cny = mv_local * FX[ccy] if mv_local is not None else None
    cost_local = h["cost_basis"] * h["shares"]
    pnl_pct = (price / h["cost_basis"] - 1) * 100 if price else None
    rows.append({
        "ticker": h["ticker"], "sym": sym, "sector": h.get("sector", ""),
        "shares": h["shares"], "cost": h["cost_basis"], "price": price,
        "ccy": ccy, "mv_local": mv_local, "mv_cny": mv_cny,
        "cost_cny": cost_local * FX[ccy], "pnl_pct": pnl_pct,
    })

total_cny = sum(r["mv_cny"] for r in rows if r["mv_cny"] is not None)

print("=" * 92)
print(f"{'标的':<8}{'板块':<14}{'股数':>8}{'成本':>9}{'现价':>9}{'盈亏%':>8}{'市值(本币)':>13}{'≈CNY':>10}{'占比':>7}")
print("-" * 92)
for r in rows:
    if r["price"] is None:
        print(f"{r['ticker']:<8}{r['sector']:<14}  价格拉取失败")
        continue
    w = r["mv_cny"] / total_cny * 100
    print(f"{r['ticker']:<8}{r['sector']:<14}{r['shares']:>8.4g}{r['cost']:>9.2f}"
          f"{r['price']:>9.2f}{r['pnl_pct']:>+7.1f}%{r['mv_local']:>12.1f}{r['mv_cny']:>10.0f}{w:>6.1f}%")
print("-" * 92)
print(f"组合总市值 ≈ ¥{total_cny:,.0f} CNY（汇率假设 USD={FX['usd']} HKD={FX['hkd']}）")

# 板块占比
print("\n=== 板块占比 ===")
sec = {}
for r in rows:
    if r["mv_cny"] is None: continue
    sec[r["sector"]] = sec.get(r["sector"], 0) + r["mv_cny"]
# 半导体主题合计
semi = sum(v for k, v in sec.items() if "半导体" in k)
for k, v in sorted(sec.items(), key=lambda x: -x[1]):
    print(f"  {k:<16}{v/total_cny*100:>5.1f}%   (¥{v:,.0f})")
print(f"  >> 半导体/AI算力主题合计：{semi/total_cny*100:.1f}%  (红线 45%)")

# 减仓计算：把单只 > 15% 削到 15%；半导体主题 > 45% 削到 45%
print("\n=== 减仓建议（单只≤15%，半导体主题≤45%）===")
CAP_SINGLE = 0.15
for r in rows:
    if r["mv_cny"] is None: continue
    w = r["mv_cny"] / total_cny
    if w > CAP_SINGLE:
        # 目标市值（CNY）→ 目标股数。注意削仓会降低 total，这里用静态 total 近似（小账户误差可接受）
        target_cny = CAP_SINGLE * total_cny
        excess_cny = r["mv_cny"] - target_cny
        price_cny = r["price"] * FX[r["ccy"]]
        sell_sh = excess_cny / price_cny
        print(f"  {r['ticker']}: 当前 {w*100:.1f}% → 减约 {sell_sh:.2f} 股 "
              f"(≈¥{excess_cny:,.0f})，剩 {r['shares']-sell_sh:.2f} 股")

print(f"\n半导体主题当前 {semi/total_cny*100:.1f}%，红线 45% → "
      f"需再减 ≈¥{max(0, semi-0.45*total_cny):,.0f} 的半导体敞口")
