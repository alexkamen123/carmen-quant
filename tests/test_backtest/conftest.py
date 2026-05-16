"""
tests/test_backtest/conftest.py
--------------------------------
pytest fixtures for backtest parameter sweep tests.

Fixtures:
    price_snapshot(ticker) — 从 fixtures 目录加载最新快照 DataFrame
    all_snapshots()        — 加载所有快照，返回 {ticker: df}

如果 fixtures 目录不存在或为空，测试将自动 skip，并提示运行 capture_fixtures.py。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pandas as pd
import pytest

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "price_snapshots"

_NO_FIXTURES_MSG = (
    "找不到 fixture 数据！请先运行：\n"
    "  cd ~/Projects/personal/finance-agent\n"
    "  source .venv/bin/activate\n"
    "  python scripts/capture_fixtures.py"
)


def _fixtures_exist() -> bool:
    """检查 fixtures 目录是否存在且含有 JSON 数据文件（排除 manifest.json）。"""
    if not _FIXTURES_DIR.exists():
        return False
    return any(
        f for f in _FIXTURES_DIR.glob("*.json")
        if f.name != "manifest.json"
    )


def _load_snapshot(path: Path) -> pd.DataFrame:
    """从 JSON 文件加载 OHLCV DataFrame。"""
    payload = json.loads(path.read_text())
    records = payload["data"]
    df = pd.DataFrame(records)
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("datetime").sort_index()
    return df


def _latest_snapshot_for(ticker: str) -> Path | None:
    """返回指定 ticker 最新日期的快照文件路径。"""
    candidates = sorted(_FIXTURES_DIR.glob(f"{ticker}_*.json"), reverse=True)
    return candidates[0] if candidates else None


@pytest.fixture
def price_snapshot() -> Callable[[str], pd.DataFrame]:
    """
    返回一个函数 f(ticker) -> pd.DataFrame。

    用法：
        def test_something(price_snapshot):
            df = price_snapshot("NVDA")
    """
    if not _fixtures_exist():
        pytest.skip(_NO_FIXTURES_MSG)

    def _get(ticker: str) -> pd.DataFrame:
        path = _latest_snapshot_for(ticker)
        if path is None:
            pytest.skip(f"没有找到 {ticker} 的 fixture 快照，请重新运行 capture_fixtures.py")
        df = _load_snapshot(path)
        if df.empty:
            pytest.skip(f"{ticker} 快照数据为空，请重新运行 capture_fixtures.py")
        return df

    return _get


@pytest.fixture
def all_snapshots() -> dict[str, pd.DataFrame]:
    """
    加载所有快照，返回 {ticker: df} 字典。

    如果 fixtures 目录为空，自动 skip。
    """
    if not _fixtures_exist():
        pytest.skip(_NO_FIXTURES_MSG)

    result: dict[str, pd.DataFrame] = {}
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        if path.name == "manifest.json":
            continue
        # 文件名格式：{ticker}_{date}.json
        # ticker 本身可能含 "." 或数字，所以按最后一个 "_YYYY-MM-DD" 分割
        stem = path.stem  # e.g. "NVDA_2026-05-16"
        # 从右往左找第一个 "_"
        sep_idx = stem.rfind("_")
        if sep_idx == -1:
            continue
        ticker = stem[:sep_idx]
        df = _load_snapshot(path)
        if not df.empty:
            result[ticker] = df

    if not result:
        pytest.skip(_NO_FIXTURES_MSG)
    return result
