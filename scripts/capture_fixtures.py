#!/usr/bin/env python3
"""
capture_fixtures.py
-------------------
捕获当前市场数据作为测试 fixtures（快照），保存到 tests/fixtures/price_snapshots/。

用法：
    cd ~/Projects/personal/finance-agent
    source .venv/bin/activate
    python scripts/capture_fixtures.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

# ── 路径 ──────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
_PORTFOLIO_YAML = _ROOT / "config" / "portfolio.yaml"
_FIXTURES_DIR = _ROOT / "tests" / "fixtures" / "price_snapshots"

# ── 北京时间 ──────────────────────────────────────────────────────────────────
_BJT = timezone(timedelta(hours=8))


def _yf_ticker(ticker: str, market: str) -> str:
    """将 portfolio 格式 ticker 转成 yfinance 格式。"""
    if market == "hk":
        # 港股：00700 → 0700.HK  /  03032 → 3032.HK
        numeric = str(int(ticker))          # 去掉前导 0
        return f"{numeric}.HK"
    return ticker


def fetch_ohlcv(ticker: str, market: str, days: int = 5) -> pd.DataFrame:
    """
    拉取最近 days 天的 5min OHLCV，返回 DataFrame。
    列名统一为小写：open/high/low/close/volume。
    """
    yf_sym = _yf_ticker(ticker, market)
    df = yf.download(
        yf_sym,
        period=f"{days}d",
        interval="5m",
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        print(f"  [WARN] {ticker} ({yf_sym}) 无数据，跳过", flush=True)
        return pd.DataFrame()

    # 处理 MultiIndex 列（yfinance >= 0.2 在多 ticker 时会产生）
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]

    # 保留核心列
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].copy()

    # 把 DatetimeIndex 转为 UTC ISO 字符串（JSON 可序列化）
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "datetime"
    return df


def df_to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → JSON 可序列化的记录列表。"""
    records = []
    for idx, row in df.iterrows():
        rec = {"datetime": idx.isoformat()}
        for col in df.columns:
            val = row[col]
            # numpy 类型 → Python 原生
            rec[col] = float(val) if pd.notna(val) else None
        records.append(rec)
    return records


def main() -> None:
    # 加载持仓
    with open(_PORTFOLIO_YAML) as f:
        portfolio = yaml.safe_load(f)

    holdings: list[dict] = portfolio.get("holdings", []) + portfolio.get("watchlist", [])
    if not holdings:
        print("[ERROR] portfolio.yaml 中没有 holdings，退出", flush=True)
        sys.exit(1)

    _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    today_str = datetime.now(_BJT).strftime("%Y-%m-%d")
    manifest: dict = {
        "captured_at": datetime.now(_BJT).isoformat(),
        "date": today_str,
        "tickers": [],
    }

    print(f"[Capture] 目标目录: {_FIXTURES_DIR}", flush=True)
    print(f"[Capture] 持仓数量: {len(holdings)}", flush=True)

    for item in holdings:
        ticker: str = item["ticker"]
        market: str = item.get("market", "us")

        print(f"[Capture] 拉取 {ticker} ({market}) ...", end=" ", flush=True)
        df = fetch_ohlcv(ticker, market, days=5)
        if df.empty:
            continue

        records = df_to_records(df)
        out_file = _FIXTURES_DIR / f"{ticker}_{today_str}.json"
        payload = {
            "ticker": ticker,
            "market": market,
            "yf_symbol": _yf_ticker(ticker, market),
            "captured_at": datetime.now(_BJT).isoformat(),
            "interval": "5m",
            "rows": len(records),
            "data": records,
        }
        out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"OK ({len(records)} 行) → {out_file.name}", flush=True)

        manifest["tickers"].append({
            "ticker": ticker,
            "market": market,
            "rows": len(records),
            "file": out_file.name,
        })

    # 写 manifest
    manifest_file = _FIXTURES_DIR / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n[Capture] 完成！共保存 {len(manifest['tickers'])} 只标的", flush=True)
    print(f"[Capture] manifest: {manifest_file}", flush=True)


if __name__ == "__main__":
    main()
