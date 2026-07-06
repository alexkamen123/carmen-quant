# P2c 失效触发器 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 给每只持仓的论点(thesis)在生成时逐票声明"什么事件=论点失效"（固定词表·结构化进死列），已有扫描器（earnings-check/news-scan/price-scan）命中该票自己声明的破坏条件时 → 落独立事件表 + 即时飞书"止损复核"告警（纯提醒·去重·默认 on）。

**架构：** 纯判定函数（signals/thesis_invalidation.py）+ 事件独立表 thesis_invalidation_events（db/tracker.py·照 earnings_surprise_alerts）+ thesis 生成结构化触发器授权（db/thesis_generator.py）+ 季度财报取数（data 层新增）+ 扫描器挂已有 earnings/news/price 车（alerts/thesis_invalidation_trigger.py）。告警纯提醒不改建议/仓位，flag 默认 on；off 时扫描器逐字节不变。

**技术栈：** Python / yfinance quarterly_financials / sqlite3 / pytest（tmp_path + AGENT_DB_PATH·脱网 mock）

---

## ⚠️ 计划对 spec 的两处偏差说明（实现遵此）

1. **`news_negative` 判据简化**：spec §2 写"事件类型∈利空集"，但 `_classify_stock_news`（news_monitor.py:445）**不返 event_type**，只返 `impact`(1-10) + `sentiment`(中文 `"利好"|"利空"|"中性"`)。故 `check_news_negative` = `impact≥impact_min 且 sentiment=="利空"`。真正的"防刷屏收窄"靠**该票 thesis 必须声明了 news_negative 触发器**这层（未声明的票永不告）+ impact 阈。事件词表白名单（`_extract_dedup_seed`）留作后续可选增强，MVP 不做。
2. **季度营收/毛利需新增取数**：无现成 quarterly 序列取数（只有年度 `.financials` 首列 + `.info` 标量）。`revenue_decline`/`margin_break` 新增 `fetch_quarterly_rev_margin(ticker)`（仿 `fetch_fundamental_factors` 的 `run_in_executor`+`_YF_SEM`+try/except→None）。数据脆弱→None 静默不误报。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| 创建 `src/finance_agent/signals/thesis_invalidation.py` | flag 读取 + 5 类纯判定函数 + match_triggers + format_invalidation_alert |
| 改 `src/finance_agent/db/tracker.py` | thesis_invalidation_events 表 DDL+迁移+CRUD + load_thesis_triggers |
| 改 `src/finance_agent/data/yfinance_provider.py` | fetch_quarterly_rev_margin 季度营收/毛利取数 + 纯解析 helper |
| 改 `src/finance_agent/db/thesis_generator.py` | THESIS_SYSTEM 追加结构化 JSON + generate_thesis_for 解析落列 |
| 创建 `src/finance_agent/alerts/thesis_invalidation_trigger.py` | 3 个扫描器（earnings/price/news 回调）+ 去重 + guarded |
| 改 `src/finance_agent/alerts/news_monitor.py` | news-scan 分类后挂失效扫描回调 |
| 改 `src/finance_agent/main.py` | check-invalidation CLI + earnings-check/price-scan 挂载 |
| 改 `config/settings.yaml` | thesis_invalidation flag 块（默认 on） |
| 创建 `tests/test_signals/test_thesis_invalidation.py` | 纯函数测试 |
| 创建 `tests/test_db/test_invalidation.py` | 表+CRUD+load_thesis_triggers 测试 |
| 创建 `tests/test_alerts/test_invalidation_trigger.py` | 扫描器挂载测试（mock 检测器+mock 告警） |
| 改 `tests/test_db/test_thesis_gen.py`（或新建） | thesis 生成结构化落列测试（mock LLM） |

---

## 任务 1：flag 读取 + 5 类纯判定函数 + match_triggers + 告警文案

**文件：**
- 创建：`src/finance_agent/signals/thesis_invalidation.py`
- 测试：`tests/test_signals/test_thesis_invalidation.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_signals/test_thesis_invalidation.py
from finance_agent.signals import thesis_invalidation as ti


def test_check_earnings_miss():
    assert ti.check_earnings_miss(-2.0, sigma=1.5) is True
    assert ti.check_earnings_miss(-1.0, sigma=1.5) is False
    assert ti.check_earnings_miss(None) is False


def test_check_news_negative():
    assert ti.check_news_negative(8, "利空", impact_min=7) is True
    assert ti.check_news_negative(8, "利好", impact_min=7) is False   # 正面不算
    assert ti.check_news_negative(5, "利空", impact_min=7) is False   # 冲击不足
    assert ti.check_news_negative(None, "利空") is False


def test_check_price_break():
    assert ti.check_price_break(140.0, 150.0) is True
    assert ti.check_price_break(150.0, 150.0) is False               # 等于不算
    assert ti.check_price_break(160.0, 150.0) is False
    assert ti.check_price_break(140.0, None) is False                # 没声明止损价
    assert ti.check_price_break(None, 150.0) is False


def test_check_revenue_decline():
    assert ti.check_revenue_decline([-3.0, -1.0]) is True            # 连续 2 季同比负
    assert ti.check_revenue_decline([-3.0, 2.0]) is False            # 最近一季转正
    assert ti.check_revenue_decline([-1.0]) is False                 # 样本<2
    assert ti.check_revenue_decline([]) is False


def test_check_margin_break():
    assert ti.check_margin_break(35.0, 40.0) is True
    assert ti.check_margin_break(45.0, 40.0) is False
    assert ti.check_margin_break(35.0, None) is False                # 没声明阈值
    assert ti.check_margin_break(None, 40.0) is False


def test_match_triggers():
    pillars = [
        {"pillar": "毛利护城河", "trigger_type": "margin_break", "threshold": 40.0},
        {"pillar": "增长故事", "trigger_type": "revenue_decline", "threshold": None},
        {"pillar": "AI 需求", "trigger_type": "news_negative", "threshold": None},  # 本次事件没测这个
    ]
    measured = {"margin_break": 35.0, "revenue_decline": [-3.0, -1.0]}
    hits = ti.match_triggers(pillars, measured, sigma=1.5, impact_min=7)
    got = {(h["pillar"], h["trigger_type"]) for h in hits}
    assert got == {("毛利护城河", "margin_break"), ("增长故事", "revenue_decline")}


def test_format_invalidation_alert_wording():
    note = ti.format_invalidation_alert("NVDA", "毛利护城河", "margin_break", "毛利率跌破 40%（当前 35%）")
    assert "止损复核" in note and "毛利护城河" in note and "NVDA" in note
    assert "加仓" not in note and "买入" not in note


def test_flag_default_off(monkeypatch, tmp_path):
    monkeypatch.setattr(ti, "_CONFIG_DIR", tmp_path)   # 无 settings.yaml
    assert ti.thesis_invalidation_enabled() is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_signals/test_thesis_invalidation.py -v`
预期：FAIL，`ModuleNotFoundError: ... thesis_invalidation`

- [ ] **步骤 3：编写最少实现代码**

```python
# src/finance_agent/signals/thesis_invalidation.py
"""P2c 失效触发器：纯判定函数 + 告警文案（确定性·零 LLM/网络）。

thesis 逐票声明的破坏条件(固定词表)命中已披露事件 → 论点失效 → 止损复核告警。
纯提醒·不改建议/仓位·绝无加仓字样。flag 默认关时调用方不扫（扫描器逐字节不变）。
"""
from __future__ import annotations

from pathlib import Path

_CONFIG_DIR = Path(__file__).parents[3] / "config"
_TI_DEFAULTS = {"enabled": False, "sigma": 1.5, "impact_min": 7}

# 固定触发词表（唯一真相源）——thesis 的 trigger_type 只能取这 5 值
TRIGGER_TYPES = ("earnings_miss", "news_negative", "price_break",
                 "revenue_decline", "margin_break")


def _load_block(name: str, defaults: dict) -> dict:
    try:
        import yaml
        p = _CONFIG_DIR / "settings.yaml"
        if p.exists():
            with open(p) as f:
                s = yaml.safe_load(f) or {}
            return {**defaults, **(s.get(name, {}) or {})}
    except Exception:
        pass
    return dict(defaults)


def thesis_invalidation_enabled() -> bool:
    return bool(_load_block("thesis_invalidation", _TI_DEFAULTS)["enabled"])


def _ti_cfg() -> dict:
    return _load_block("thesis_invalidation", _TI_DEFAULTS)


def _num(x):
    return x if isinstance(x, (int, float)) and not (isinstance(x, float) and x != x) else None


def check_earnings_miss(sue_score, sigma: float = 1.5) -> bool:
    s = _num(sue_score)
    return s is not None and s <= -sigma


def check_news_negative(impact, sentiment, impact_min: int = 7) -> bool:
    i = _num(impact)
    return i is not None and i >= impact_min and sentiment == "利空"


def check_price_break(close, stop_price) -> bool:
    c, sp = _num(close), _num(stop_price)
    return c is not None and sp is not None and c < sp


def check_revenue_decline(yoy_growths) -> bool:
    vals = [_num(v) for v in (yoy_growths or [])]
    if len(vals) < 2 or any(v is None for v in vals[-2:]):
        return False
    return all(v < 0 for v in vals[-2:])


def check_margin_break(gross_margin, margin_floor) -> bool:
    m, fl = _num(gross_margin), _num(margin_floor)
    return m is not None and fl is not None and m < fl


def match_triggers(pillars, measured: dict, sigma: float = 1.5,
                   impact_min: int = 7) -> list[dict]:
    """pillars=该票声明的触发器列表；measured=trigger_type→实测值。返回命中的 pillar。

    measured 例：{"earnings_miss": sue, "revenue_decline": [yoy...],
                 "margin_break": gross_margin, "price_break": close,
                 "news_negative": (impact, sentiment)}
    """
    hits = []
    for p in pillars or []:
        tt = p.get("trigger_type")
        if tt not in measured:
            continue
        v, thr = measured[tt], p.get("threshold")
        ok = False
        if tt == "earnings_miss":
            ok = check_earnings_miss(v, sigma)
        elif tt == "news_negative":
            ok = check_news_negative(v[0], v[1], impact_min) if isinstance(v, (tuple, list)) else False
        elif tt == "price_break":
            ok = check_price_break(v, thr)
        elif tt == "revenue_decline":
            ok = check_revenue_decline(v)
        elif tt == "margin_break":
            ok = check_margin_break(v, thr)
        if ok:
            hits.append(p)
    return hits


def format_invalidation_alert(ticker: str, pillar: str, trigger_type: str, detail: str) -> str:
    """确定性告警文案·纯提醒·绝无加仓/仓位数值字样（抄 format_sue_note 风格）。"""
    return (f"⚠️【论点失效】{ticker} 触发你写的失效条件『{pillar}』——{detail}。"
            f"建议止损复核（此为提醒·非加仓/仓位指令）。")
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_signals/test_thesis_invalidation.py -v`
预期：8 项全 PASS

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/signals/thesis_invalidation.py tests/test_signals/test_thesis_invalidation.py
git commit -m "feat(P2c): 失效触发5类纯判定+match_triggers+告警文案+flag（TDD）"
```

---

## 任务 2：事件表 + CRUD + load_thesis_triggers

**文件：**
- 修改：`src/finance_agent/db/tracker.py`（DDL 进 `_CREATE_SQL`·迁移仿 `_migrate_sue_table:140-153`·CRUD 仿 `save_sue_alert:552`/`get_sue_alerts:565`）
- 测试：`tests/test_db/test_invalidation.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_db/test_invalidation.py
import json
from finance_agent.db import tracker


def test_save_invalidation_idempotent(tmp_path):
    db = tmp_path / "t.db"; tracker.init_db(db)
    for _ in range(2):
        tracker.save_invalidation_event(ticker="NVDA", market="us",
            trigger_type="margin_break", pillar="毛利护城河",
            triggered_at="2026-07-06", detail="毛利跌破40%", price_at_event=150.0, db_path=db)
    rows = tracker.get_invalidation_events("NVDA", db_path=db)
    assert len(rows) == 1 and rows[0]["trigger_type"] == "margin_break"
    assert rows[0]["return_30d"] is None


def test_get_invalidation_matured_only(tmp_path):
    db = tmp_path / "t.db"; tracker.init_db(db)
    tracker.save_invalidation_event("MU", "us", "earnings_miss", "周期", "2026-05-01",
                                    "SUE-2σ", 80.0, db_path=db)
    _set_outcome(db, "MU", "2026-05-01", -8.0, -1.0)
    tracker.save_invalidation_event("NV", "us", "price_break", "止损", "2026-06-20",
                                    "跌破150", 140.0, db_path=db)  # 距 asof<30 天
    matured = tracker.get_invalidation_events(matured_only=True, asof="2026-06-10", db_path=db)
    assert {r["ticker"] for r in matured} == {"MU"}


def test_load_thesis_triggers(tmp_path):
    db = tmp_path / "t.db"; tracker.init_db(db)
    pillars = [{"pillar": "毛利", "trigger_type": "margin_break", "threshold": 40.0}]
    tracker.save_thesis("NVDA", "us", "论点正文", pillars=pillars,
                        stop_conditions="毛利跌破40%", db_path=db)
    trig = tracker.load_thesis_triggers("NVDA", db_path=db)
    assert trig["pillars"][0]["trigger_type"] == "margin_break"
    assert trig["stop_conditions"] == "毛利跌破40%"
    # 无结构化触发器 → None
    tracker.save_thesis("AAPL", "us", "只有正文", db_path=db)
    assert tracker.load_thesis_triggers("AAPL", db_path=db) is None


def _set_outcome(db, ticker, triggered_at, return_30d, benchmark_return_30d):
    from finance_agent.db.tracker import _conn, _resolve_db
    with _conn(_resolve_db(db)) as con:
        con.execute("UPDATE thesis_invalidation_events SET price_30d=?, return_30d=?, "
                    "benchmark_return_30d=? WHERE ticker=? AND triggered_at=?",
                    (100.0, return_30d, benchmark_return_30d, ticker, triggered_at))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_db/test_invalidation.py -v`
预期：FAIL，`AttributeError: ... save_invalidation_event`（或建表缺失）

- [ ] **步骤 3：编写最少实现代码**

3a. `_CREATE_SQL` 加表（紧随 earnings_surprise_alerts）：
```sql
CREATE TABLE IF NOT EXISTS thesis_invalidation_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT NOT NULL,
    market                TEXT NOT NULL DEFAULT 'us',
    trigger_type          TEXT NOT NULL,
    pillar                TEXT,
    triggered_at          TEXT NOT NULL,
    detail                TEXT,
    price_at_event        REAL,
    price_30d             REAL,
    return_30d            REAL,
    benchmark_return_30d  REAL,
    created_at            TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_inval_ticker ON thesis_invalidation_events(ticker);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inval_uniq
    ON thesis_invalidation_events(ticker, trigger_type, triggered_at);
```

3b. 迁移（仿 `_migrate_sue_table`）+ 在 `init_db` 注册 `_migrate_invalidation_table(con)`：
```python
def _migrate_invalidation_table(con: sqlite3.Connection) -> None:
    try:
        existing = {r[1] for r in con.execute(
            "PRAGMA table_info(thesis_invalidation_events)").fetchall()}
    except sqlite3.OperationalError:
        return
    for col, typ in (("price_at_event", "REAL"), ("price_30d", "REAL"),
                     ("return_30d", "REAL"), ("benchmark_return_30d", "REAL")):
        if col not in existing:
            try:
                con.execute(f"ALTER TABLE thesis_invalidation_events ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
```

3c. CRUD + loader（放 SUE CRUD 附近）：
```python
def save_invalidation_event(ticker, market, trigger_type, pillar, triggered_at,
                            detail, price_at_event=None, db_path=None):
    """写失效事件·(ticker,trigger_type,triggered_at) 幂等（UNIQUE + INSERT OR IGNORE）。"""
    p = _resolve_db(db_path)
    with _conn(p) as con:
        con.execute(
            "INSERT OR IGNORE INTO thesis_invalidation_events "
            "(ticker, market, trigger_type, pillar, triggered_at, detail, price_at_event) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker.upper(), market, trigger_type, pillar, triggered_at, detail, price_at_event))


def get_invalidation_events(ticker=None, db_path=None, matured_only=False, asof=None):
    p = _resolve_db(db_path)
    where, params = [], []
    if ticker:
        where.append("ticker = ?"); params.append(ticker.upper())
    if matured_only:
        where.append("return_30d IS NOT NULL")
        where.append("julianday(?) - julianday(triggered_at) >= 30")
        params.append(asof or _today().strftime("%Y-%m-%d"))
    sql = "SELECT * FROM thesis_invalidation_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY triggered_at DESC"
    with _conn(p) as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def load_thesis_triggers(ticker, db_path=None):
    """取该票结构化触发器 {pillars:[...], stop_conditions:str}；无/解析失败 → None。"""
    p = _resolve_db(db_path)
    with _conn(p) as con:
        row = con.execute("SELECT pillars, stop_conditions FROM theses WHERE ticker=?",
                          (ticker.upper(),)).fetchone()
    if not row or not row["pillars"]:
        return None
    try:
        pillars = json.loads(row["pillars"])
    except (ValueError, TypeError):
        return None
    if not pillars:
        return None
    return {"pillars": pillars, "stop_conditions": row["stop_conditions"] or ""}
```
> 依赖确认：`_today()`（P2b 已加）、`_conn`（row_factory 已设）、`json` 已在 tracker 顶部导入（save_thesis 用了 json.dumps）。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_db/test_invalidation.py -v`
预期：3 项 PASS

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/db/tracker.py tests/test_db/test_invalidation.py
git commit -m "feat(P2c): thesis_invalidation_events 表+CRUD+load_thesis_triggers（TDD）"
```

---

## 任务 3：季度营收/毛利取数（revenue_decline / margin_break 数据源）

**文件：**
- 修改：`src/finance_agent/data/yfinance_provider.py`（新增取数 + 纯解析 helper）
- 测试：`tests/test_data/test_quarterly_rev_margin.py`（只测纯解析·不联网）

- [ ] **步骤 1：编写失败的测试（纯解析·脱网构造 DataFrame）**

```python
# tests/test_data/test_quarterly_rev_margin.py
import pandas as pd
from finance_agent.data import yfinance_provider as yp


def _qdf(cols):
    # cols: [(date, total_revenue, gross_profit)]，列=季度（最新在左·yfinance 惯例）
    idx = ["Total Revenue", "Gross Profit"]
    data = {pd.Timestamp(d): [rev, gp] for d, rev, gp in cols}
    return pd.DataFrame(data, index=idx)


def test_extract_rev_margin_yoy_and_margin():
    # 5 季（够算最近 2 季的同比：本季 vs 去年同季）
    df = _qdf([("2026-03-31", 90, 30), ("2025-12-31", 100, 45), ("2025-09-30", 110, 44),
               ("2025-06-30", 120, 48), ("2025-03-31", 100, 40)])
    out = yp.extract_rev_margin(df)
    # 最近季营收 90 vs 去年同季(2025-03-31)100 → yoy=-10%
    assert out["yoy_growths"][-1] < 0
    assert abs(out["gross_margin"] - (30 / 90 * 100)) < 0.01   # 最近季毛利率


def test_extract_rev_margin_insufficient_or_none():
    assert yp.extract_rev_margin(None) is None
    assert yp.extract_rev_margin(pd.DataFrame()) is None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_data/test_quarterly_rev_margin.py -v`
预期：FAIL，`AttributeError: ... extract_rev_margin`

- [ ] **步骤 3：编写最少实现代码**

在 `yfinance_provider.py` 追加（纯解析 + 网络取数分离）：
```python
def extract_rev_margin(df):
    """从 quarterly_financials DataFrame 抽最近若干季的同比营收增速 + 最新季毛利率。

    df 列=季度(最新在左)、行含 'Total Revenue'/'Gross Profit'。
    返回 {"yoy_growths": [...], "gross_margin": float} 或 None（数据不足/缺行）。
    yoy = (本季营收 − 去年同季营收)/|去年同季| ·%；至少要 5 季才能算最近 2 季的同比。
    """
    if df is None or getattr(df, "empty", True):
        return None
    try:
        cols = list(df.columns)  # 已按最新在左；升序排一下便于同比
        cols_sorted = sorted(cols)
        rev = {c: df.at["Total Revenue", c] for c in cols_sorted if "Total Revenue" in df.index}
        if len(cols_sorted) < 5 or "Total Revenue" not in df.index:
            gm = None
            if "Gross Profit" in df.index and "Total Revenue" in df.index:
                latest = cols_sorted[-1]
                r0 = df.at["Total Revenue", latest]
                g0 = df.at["Gross Profit", latest]
                gm = float(g0) / float(r0) * 100 if r0 else None
            return {"yoy_growths": [], "gross_margin": gm} if gm is not None else None
        seq = [float(rev[c]) for c in cols_sorted]
        yoy = []
        for i in range(4, len(seq)):
            base = seq[i - 4]
            if base:
                yoy.append(round((seq[i] - base) / abs(base) * 100, 2))
        latest = cols_sorted[-1]
        r0, g0 = df.at["Total Revenue", latest], df.at["Gross Profit", latest] if "Gross Profit" in df.index else None
        gm = float(g0) / float(r0) * 100 if (g0 is not None and r0) else None
        return {"yoy_growths": yoy, "gross_margin": gm}
    except Exception:
        return None


def fetch_quarterly_rev_margin(ticker):
    """同步取 yf.Ticker(t).quarterly_financials → extract_rev_margin。失败静默 None。

    仿 fetch_fundamental_factors：由调用方用 run_in_executor 包装 + _YF_SEM 保护。
    """
    try:
        import yfinance as yf
        return extract_rev_margin(yf.Ticker(ticker).quarterly_financials)
    except Exception:
        return None
```
> 实现首步核实（spec §7）：`quarterly_financials` 行名是否确为 `Total Revenue`/`Gross Profit`（跨版本可能是 `Gross Profit`/`TotalRevenue`）；不符则调整行名或改用 `.quarterly_income_stmt`。列方向（最新在左/右）用 `sorted` 已规避。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_data/test_quarterly_rev_margin.py -v`
预期：2 项 PASS

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/data/yfinance_provider.py tests/test_data/test_quarterly_rev_margin.py
git commit -m "feat(P2c): 季度营收同比+毛利率取数（脱网解析 TDD·取数层降级None）"
```

---

## 任务 4：thesis 生成结构化触发器授权

**文件：**
- 修改：`src/finance_agent/db/thesis_generator.py`（THESIS_SYSTEM:18-36 + generate_thesis_for:72-106）
- 测试：`tests/test_db/test_thesis_gen.py`（新建·mock LLM）

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_db/test_thesis_gen.py
import json
import pytest
from finance_agent.db import tracker, thesis_generator as tg


def test_generate_parses_structured_triggers(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    monkeypatch.setattr(tracker, "_resolve_db", lambda p=None: db)  # 若需
    fake = ("核心论点...\n支撑理由...\n破坏条件...\n当前阶段...\n"
            '```json\n{"pillars":[{"pillar":"毛利","trigger_type":"margin_break","threshold":40.0,"status":"intact"}],'
            '"stop_conditions":"毛利跌破40%"}\n```')
    monkeypatch.setattr(tg, "has_claude_cli", lambda: True)
    async def _fake_cli(sys, user, timeout=90): return fake
    monkeypatch.setattr(tg, "claude_cli_chat", _fake_cli)
    import asyncio
    asyncio.run(tg.generate_thesis_for("NVDA", "us", 100.0, 10, "", force=True, db_path=db))
    trig = tracker.load_thesis_triggers("NVDA", db_path=db)
    assert trig is not None and trig["pillars"][0]["trigger_type"] == "margin_break"


def test_generate_parse_failure_degrades(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    monkeypatch.setattr(tg, "has_claude_cli", lambda: True)
    async def _fake_cli(sys, user, timeout=90): return "只有自由文本没有JSON"
    monkeypatch.setattr(tg, "claude_cli_chat", _fake_cli)
    import asyncio
    asyncio.run(tg.generate_thesis_for("AAPL", "us", 100.0, 10, "", force=True, db_path=db))
    # thesis_text 照存、结构化触发器 None（降级不崩）
    assert tracker.load_thesis("AAPL", db_path=db)  # 正文在
    assert tracker.load_thesis_triggers("AAPL", db_path=db) is None
```
> 注：`generate_thesis_for` 现签名可能没有 `db_path` 参数——步骤 3 需加一个可选 `db_path=None` 透传给 `save_thesis`（便于测试注入），或测试改用 `AGENT_DB_PATH` env + monkeypatch。实现时二选一并保持与任务 2 一致。

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_db/test_thesis_gen.py -v`
预期：FAIL（未解析结构化触发器 / db_path 参数缺失）

- [ ] **步骤 3：编写最少实现代码**

3a. `THESIS_SYSTEM` 末尾追加（在"总字数不超过 200 字"之后）：
```
另外，在正文之后附一段 JSON 代码块（```json ... ```），声明这只票的"论点失效条件"，供机器监控：
{"pillars": [{"pillar": "<支柱名>", "trigger_type": "<从下列固定值选>", "threshold": <数值或null>, "status": "intact"}],
 "stop_conditions": "<给人看的一句话止损条件摘要>"}
trigger_type 只能取：earnings_miss(财报大幅低于预期) / news_negative(重大利空新闻) / price_break(跌破止损价) / revenue_decline(营收连续两季转负) / margin_break(毛利率跌破阈值)。
price_break 与 margin_break 必须给数值 threshold（止损价 / 毛利率百分比）；其余 threshold 填 null。最多 4 条，只列真正会证伪核心逻辑的。
```

3b. `generate_thesis_for` 落库处（L105 `save_thesis(ticker, market, thesis)`）改为解析 + 传参：
```python
    pillars, stop_conditions = _parse_thesis_triggers(thesis)
    save_thesis(ticker, market, thesis, pillars=pillars,
                stop_conditions=stop_conditions, db_path=db_path)
```
并新增解析纯函数（放模块内）：
```python
import json
import re

_VALID_TRIGGERS = {"earnings_miss", "news_negative", "price_break",
                   "revenue_decline", "margin_break"}


def _parse_thesis_triggers(text):
    """从 thesis 文本抽 ```json``` 块 → (pillars|None, stop_conditions)。解析失败→(None, "")。"""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)
    if not m:
        return None, ""
    try:
        obj = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None, ""
    pillars = obj.get("pillars")
    if not isinstance(pillars, list):
        return None, ""
    clean = [p for p in pillars if isinstance(p, dict)
             and p.get("trigger_type") in _VALID_TRIGGERS]
    return (clean or None), (obj.get("stop_conditions") or "")
```
并给 `generate_thesis_for` 加 `db_path=None` 参数透传给 `save_thesis`（与任务 2 loader 测试一致）。

> **护栏**：`thesis`（thesis_text·PM 读的自由文本）仍原样存入 save_thesis 第 3 参，结构化 JSON 只额外抽进 pillars/stop_conditions 列 → PM 材料零影响。

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_db/test_thesis_gen.py -v`
预期：2 项 PASS

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/db/thesis_generator.py tests/test_db/test_thesis_gen.py
git commit -m "feat(P2c): thesis 生成结构化失效触发器授权+解析降级（TDD·PM正文不变）"
```

---

## 任务 5：失效扫描器（earnings/price/news 挂载 + 去重 + guarded）

**文件：**
- 创建：`src/finance_agent/alerts/thesis_invalidation_trigger.py`
- 测试：`tests/test_alerts/test_invalidation_trigger.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_alerts/test_invalidation_trigger.py
import asyncio
import finance_agent.alerts.thesis_invalidation_trigger as it
from finance_agent.db import tracker


def _seed_thesis(db, ticker, pillars):
    tracker.save_thesis(ticker, "us", "正文", pillars=pillars, stop_conditions="", db_path=db)


def test_earnings_scan_hits_declared_trigger(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "MU", [{"pillar": "周期", "trigger_type": "earnings_miss", "threshold": None}])
    monkeypatch.setattr(it, "_load_us_holdings", lambda: [{"ticker": "MU", "market": "us"}])
    # mock：最近 SUE=-2σ（爆雷），无季度财报
    monkeypatch.setattr(it, "_latest_sue", lambda t, db_path: -2.0)
    monkeypatch.setattr(it, "_fetch_rev_margin", lambda t: None)
    sent = []
    async def _fake_alert(ticker, pillar, tt, detail): sent.append((ticker, tt))
    monkeypatch.setattr(it, "_push_invalidation", _fake_alert)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_earnings_invalidation(db_path=db))
    assert ("MU", "earnings_miss") in sent
    assert len(tracker.get_invalidation_events("MU", db_path=db)) == 1


def test_no_declared_trigger_no_alert(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "MU", [{"pillar": "毛利", "trigger_type": "margin_break", "threshold": 40.0}])
    monkeypatch.setattr(it, "_load_us_holdings", lambda: [{"ticker": "MU", "market": "us"}])
    monkeypatch.setattr(it, "_latest_sue", lambda t, db_path: -2.0)  # 爆雷但没声明 earnings_miss
    monkeypatch.setattr(it, "_fetch_rev_margin", lambda t: None)     # 毛利数据缺失→静默
    sent = []
    async def _fake_alert(*a): sent.append(a)
    monkeypatch.setattr(it, "_push_invalidation", _fake_alert)
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_earnings_invalidation(db_path=db))
    assert sent == []


def test_single_ticker_failure_isolated(tmp_path, monkeypatch):
    db = tmp_path / "t.db"; tracker.init_db(db)
    _seed_thesis(db, "MU", [{"pillar": "周期", "trigger_type": "earnings_miss", "threshold": None}])
    _seed_thesis(db, "NV", [{"pillar": "周期", "trigger_type": "earnings_miss", "threshold": None}])
    monkeypatch.setattr(it, "_load_us_holdings",
                        lambda: [{"ticker": "MU", "market": "us"}, {"ticker": "NV", "market": "us"}])
    def _sue(t, db_path):
        if t == "MU": raise RuntimeError("boom")
        return -2.0
    monkeypatch.setattr(it, "_latest_sue", _sue)
    monkeypatch.setattr(it, "_fetch_rev_margin", lambda t: None)
    monkeypatch.setattr(it, "_push_invalidation", lambda *a: asyncio.sleep(0))
    monkeypatch.setattr(it, "_today_str", lambda: "2026-07-06")
    asyncio.run(it.scan_earnings_invalidation(db_path=db))  # MU 抛错不拖垮 NV
    assert len(tracker.get_invalidation_events("NV", db_path=db)) == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest tests/test_alerts/test_invalidation_trigger.py -v`
预期：FAIL，模块/函数缺失

- [ ] **步骤 3：编写最少实现代码**

```python
# src/finance_agent/alerts/thesis_invalidation_trigger.py
"""P2c 失效扫描器：挂已有 earnings/price/news 车，命中声明的失效条件→落库+飞书告警。

受 thesis_invalidation_enabled() 门控（off→调用方不调，扫描器逐字节不变）。
单票 try/except continue 降级；去重复用 news_alerted。纯提醒·不改建议/仓位。
"""
import asyncio
import datetime

from finance_agent.data.yfinance_provider import fetch_quarterly_rev_margin
from finance_agent.db.tracker import (get_sue_alerts, load_thesis_triggers,
                                      save_invalidation_event)
from finance_agent.signals.thesis_invalidation import (_ti_cfg, format_invalidation_alert,
                                                       match_triggers)
from finance_agent.alerts.earnings_trigger import _load_us_holdings


def _today_str():
    return datetime.date.today().isoformat()


def _latest_sue(ticker, db_path):
    rows = get_sue_alerts(ticker, db_path=db_path)
    return rows[0]["sue_score"] if rows else None


def _fetch_rev_margin(ticker):
    return fetch_quarterly_rev_margin(ticker)


async def _push_invalidation(ticker, pillar, trigger_type, detail):
    """飞书告警（复用 news_monitor 卡片）+ 去重。失败不抛。"""
    from finance_agent.alerts.news_monitor import _send_stock_alert, _save_alerted, _key_exists
    key = f"invalidation:{ticker}:{trigger_type}:{_today_str()}"
    if _key_exists(key):
        return
    text = format_invalidation_alert(ticker, pillar, trigger_type, detail)
    try:
        await _send_stock_alert(ticker, "us", "论点失效提醒", _today_str(),
                                impact=8, sentiment="利空", reason=text)
        _save_alerted(key, _today_str())
    except Exception:
        pass


async def scan_earnings_invalidation(db_path=None):
    """对每只美股持仓：命中 earnings_miss / revenue_decline / margin_break → 落库+告警。"""
    cfg = _ti_cfg()
    loop = asyncio.get_event_loop()
    for h in _load_us_holdings():
        ticker = h["ticker"]
        try:
            trig = load_thesis_triggers(ticker, db_path=db_path)
            if not trig:
                continue
            measured = {}
            types = {p.get("trigger_type") for p in trig["pillars"]}
            if "earnings_miss" in types:
                sue = _latest_sue(ticker, db_path)
                if sue is not None:
                    measured["earnings_miss"] = sue
            if {"revenue_decline", "margin_break"} & types:
                rm = await loop.run_in_executor(None, lambda t=ticker: _fetch_rev_margin(t))
                if rm:
                    if "revenue_decline" in types:
                        measured["revenue_decline"] = rm.get("yoy_growths", [])
                    if "margin_break" in types and rm.get("gross_margin") is not None:
                        measured["margin_break"] = rm["gross_margin"]
            hits = match_triggers(trig["pillars"], measured,
                                  sigma=float(cfg["sigma"]), impact_min=int(cfg["impact_min"]))
            for p in hits:
                detail = _detail_for(p, measured)
                save_invalidation_event(ticker, "us", p["trigger_type"], p.get("pillar", ""),
                                        _today_str(), detail, db_path=db_path)
                await _push_invalidation(ticker, p.get("pillar", ""), p["trigger_type"], detail)
        except Exception:
            continue


def _detail_for(pillar, measured):
    tt = pillar.get("trigger_type")
    if tt == "earnings_miss":
        return f"财报大幅低于预期（SUE={measured.get('earnings_miss'):+.1f}σ）"
    if tt == "revenue_decline":
        return f"营收连续两季同比转负（{measured.get('revenue_decline')}）"
    if tt == "margin_break":
        return f"毛利率跌破 {pillar.get('threshold')}%（当前 {measured.get('margin_break'):.1f}%）"
    return "触发失效条件"
```
> `scan_price_invalidation` / `scan_news_invalidation` 结构同构，可本任务一并加（price：对声明 price_break 的票取 last close 比 threshold；news：回调签名 `scan_news_invalidation(ticker, impact, sentiment, db_path)`，命中声明的 news_negative 则落库+告警）。为控本步规模，先实现 earnings 扫描 + 通过其 3 测试；price/news 扫描在步骤 3b 补同构代码 + 各 1 测试。

- [ ] **步骤 3b：补 price/news 扫描器 + 各 1 测试**（同构·此处略展开，实现时照 earnings 版写 `scan_price_invalidation(db_path)` 与 `scan_news_invalidation(ticker, impact, sentiment, db_path)`，测试仿 test_earnings_scan_hits_declared_trigger）

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run pytest tests/test_alerts/test_invalidation_trigger.py -v`
预期：全 PASS（earnings 3 + price/news 各 1）

- [ ] **步骤 5：Commit**

```bash
git add src/finance_agent/alerts/thesis_invalidation_trigger.py tests/test_alerts/test_invalidation_trigger.py
git commit -m "feat(P2c): 失效扫描器 earnings/price/news 挂载+去重+降级（TDD）"
```

---

## 任务 6：CLI 挂载 + news-scan 回调 + settings flag

**文件：**
- 修改：`src/finance_agent/main.py`、`src/finance_agent/alerts/news_monitor.py`、`config/settings.yaml`

- [ ] **步骤 1：settings.yaml 加 flag 块（默认 on）**

```yaml
# ── P2c 失效触发器（提醒复核·不改建议·默认 on）──────────────────
# thesis 逐票声明失效条件(结构化词表)；扫描器命中→落 thesis_invalidation_events 表 + 飞书"止损复核"告警。
# 纯提醒·不碰任何 recommendation/仓位/权重(同 sell_guard/news 告警治理级)，默认 on；严格去重防刷屏。
# off 则扫描器逐字节不变、不扫不告。
thesis_invalidation:
  enabled: true
  sigma: 1.5
  impact_min: 7
```
校验：`uv run python -c "import yaml; yaml.safe_load(open('config/settings.yaml')); print('yaml ok')"`

- [ ] **步骤 2：main.py earnings-check 挂载（`_earnings_check` L669 旁·仿 SUE）**

```python
    from finance_agent.signals.thesis_invalidation import thesis_invalidation_enabled
    if thesis_invalidation_enabled():
        try:
            from finance_agent.alerts.thesis_invalidation_trigger import scan_earnings_invalidation
            await scan_earnings_invalidation()
        except Exception as e:
            console.print(f"⚠️ 失效扫描(earnings)失败，跳过：{e}")
```

- [ ] **步骤 3：main.py price-scan 挂载 + check-invalidation CLI**

price-scan（L155-161）内 `run_price_scan` 之后加 guarded `scan_price_invalidation`；新增命令：
```python
@app.command("check-invalidation")
def check_invalidation_cmd(skip_notify: bool = typer.Option(False, "--skip-notify")):
    """P2c 手动跑一次全持仓失效扫描（命中声明的失效条件→止损复核告警）。"""
    from finance_agent.alerts.thesis_invalidation_trigger import scan_earnings_invalidation
    asyncio.run(scan_earnings_invalidation())
```

- [ ] **步骤 4：news_monitor.py 挂回调（L1880 分类后、L1893 推送前·仿 L1884 情绪落库 guarded 块）**

```python
        from finance_agent.signals.thesis_invalidation import thesis_invalidation_enabled
        if thesis_invalidation_enabled():
            try:
                from finance_agent.alerts.thesis_invalidation_trigger import scan_news_invalidation
                await scan_news_invalidation(scan_ticker, result.get("impact"),
                                             result.get("sentiment"), db_path=None)
            except Exception:
                pass
```

- [ ] **步骤 5：冒烟 + Commit**

```bash
uv run python -c "import finance_agent.main; print('ok')"
uv run finance-agent check-invalidation --skip-notify 2>&1 | tail -5   # worktree 缺 portfolio 走 except 即可
git add src/finance_agent/main.py src/finance_agent/alerts/news_monitor.py config/settings.yaml
git commit -m "feat(P2c): earnings/price/news 挂失效扫描+check-invalidation CLI+flag（guarded·默认on）"
```

---

## 任务 7：flag-off 验证 + 全量回归 + 收口

- [ ] **步骤 1：全量零回归** `uv run pytest tests/ -x -q`（唯一允许失败=已知正交 test_guards portfolio.yaml）
- [ ] **步骤 2：flag-off 逐字节** —— settings 临时 `thesis_invalidation.enabled: false`，确认 `thesis_invalidation_enabled()` 返 False → 三扫描器 guarded 块早退、earnings-check/news-scan/price-scan 行为不变（跑一次干跑核对无【论点失效】告警）。改回 true。
- [ ] **步骤 3：更新 进展.md**（现在段 P2c 落地 + 决策史 + 素材台账 + flag 现状"默认 on·测量伏笔留后续"）
- [ ] **步骤 4：Commit** `docs(P2c): 失效触发器实现落地留痕`

---

## 自检结果（对照 spec）

**规格覆盖度：** §2 词表→任务1(5 check+match)；§3 thesis 授权+向后兼容→任务4；§4.1 load_thesis_triggers→任务2；§4.2 事件表→任务2；§4.3 纯函数+告警文案→任务1；§4.4 扫描器挂载+去重+降级→任务5+6；§4.5 CLI→任务6；§4.6 flag→任务6；§5/§6 反过拟合/未来函数（触发条件持仓期声明·季度只用已披露）→任务1/3/5 判据；§8 测试→任务1-5；§10 验收→任务7；§11 测量伏笔（事件表留 outcome 列·回填留后续）→任务2 表结构。数据取数缺口→任务3。

**占位符扫描：** 任务5 步骤3b 的 price/news 扫描器标"同构照 earnings 版写"——给了签名与判据、非空占位（earnings 版是完整可照抄范本）。其余步骤均含可运行代码。任务3/4 含"实现首步核实"（quarterly 行名/generate_thesis_for db_path 参数）为诚实标注非占位。

**类型一致性：** `save_invalidation_event`/`get_invalidation_events`/`load_thesis_triggers`/`match_triggers`/`format_invalidation_alert`/`thesis_invalidation_enabled`/`_ti_cfg`/`fetch_quarterly_rev_margin`/`extract_rev_margin`/`_parse_thesis_triggers` 签名跨任务一致；事件表列名（trigger_type/triggered_at/pillar/return_30d）DDL→CRUD→测试全程一致；trigger_type 5 值词表在 §2/任务1/任务4 三处一致。

---

## 执行交接

**计划已保存到 `docs/superpowers/plans/2026-07-06-p2c-invalidation.md`。两种执行方式：**

1. **子代理驱动（推荐）** — 每任务一个新子代理 + 两阶段审查（superpowers:subagent-driven-development）
2. **内联执行** — 当前会话批量执行并设检查点（superpowers:executing-plans）

**选哪种方式？**
