# tests/test_db/test_guards.py
"""防追高护栏（L3a）测试：触发正确 + 误报无害（只降级、不激进）。"""
import datetime
from types import SimpleNamespace

from finance_agent.db import tracker
from finance_agent.db.tracker import save_guidance, check_guidance_adherence
from finance_agent.agents import guards
from finance_agent.agents.guards import (
    apply_anti_chase, parse_limits, exempt_tickers, recent_buy_velocity,
    _overbought_hits, _is_bullish,
)


def _stock(ticker="NVDA", sector="半导体/AI算力", sw="normal", rsi=50, bb=0.5, pvm=0.0):
    return SimpleNamespace(
        ticker=ticker, sector=sector, signal_weight=sw,
        signals=SimpleNamespace(rsi=rsi, bb_position=bb, price_vs_ma20=pvm),
    )


def _buy(pc="小加1"):
    return {"recommendation": "买入", "position_change": pc,
            "short_term_action": "立即执行", "key_risk": "原风险", "one_line": "原结论"}


# ── 解析/辅助 ─────────────────────────────────────────────────

def test_parse_limits():
    pf = {"strategy": {"risk_limits": [
        {"rule": "sector:半导体/AI算力 <= 45%"},
        {"rule": "single_ticker <= 15%"},
    ]}}
    sl, single = parse_limits(pf)
    assert sl["半导体/AI算力"] == 45.0 and single == 15.0


def test_exempt_tickers():
    pf = {"holdings": [
        {"ticker": "QQQM", "sector": "宽基ETF", "is_dca": True},
        {"ticker": "SCHD", "sector": "股息ETF"},
        {"ticker": "NVDA", "sector": "半导体/AI算力"},
    ]}
    ex = exempt_tickers(pf)
    assert "QQQM" in ex and "SCHD" in ex and "IAU" in ex and "SGOV" in ex  # ETF+对冲桶
    assert "NVDA" not in ex


def test_overbought_hits():
    assert _overbought_hits(SimpleNamespace(rsi=72, bb_position=0.95, price_vs_ma20=15)) == 3
    assert _overbought_hits(SimpleNamespace(rsi=50, bb_position=0.5, price_vs_ma20=2)) == 0
    assert _is_bullish(_buy()) and not _is_bullish({"recommendation": "减仓", "position_change": "减仓1"})


# ── G1 扎堆追高 ───────────────────────────────────────────────

def test_g1_chase_clamps():
    s = _stock(rsi=72, bb=0.95, pvm=15)  # overbought=3
    out, hits = apply_anti_chase(s, _buy(), n_distinct=5, sector_pct={}, ticker_pct={},
                                 sector_limits={}, single_limit=15, exempt=set())
    assert hits and "追高" in hits[0]
    assert out["position_change"] == "维持" and out["recommendation"] == "观望"
    assert "护栏" in out["key_risk"] and "原风险" in out["key_risk"]  # 保留原文+理由


def test_g1_skips_when_not_clustered():
    s = _stock(rsi=80, bb=1.0, pvm=20)  # 超买但近窗只买了 2 只（未扎堆）
    out, hits = apply_anti_chase(s, _buy(), n_distinct=2, sector_pct={}, ticker_pct={},
                                 sector_limits={}, single_limit=15, exempt=set())
    assert hits == [] and out == _buy()


def test_g1_skips_low_signal_weight():
    # signal_weight=='low'（技术信号无效）→ 不用技术因子硬拦
    s = _stock(sw="low", rsi=85, bb=1.0, pvm=25)
    out, hits = apply_anti_chase(s, _buy(), n_distinct=9, sector_pct={}, ticker_pct={},
                                 sector_limits={}, single_limit=15, exempt=set())
    assert hits == [] and out == _buy()


# ── G2 板块/单票红线 ──────────────────────────────────────────

def test_g2_sector_redline_clamps():
    s = _stock(rsi=50, bb=0.5, pvm=0)  # 不超买，但板块已破红线
    out, hits = apply_anti_chase(s, _buy(), n_distinct=1,
                                 sector_pct={"半导体/AI算力": 48}, ticker_pct={},
                                 sector_limits={"半导体/AI算力": 45}, single_limit=15, exempt=set())
    assert any("板块红线" in h for h in hits) and out["position_change"] == "维持"


def test_g2_single_ticker_redline_clamps():
    s = _stock(ticker="NVDA", rsi=50, bb=0.5, pvm=0)
    out, hits = apply_anti_chase(s, _buy(), n_distinct=1, sector_pct={},
                                 ticker_pct={"NVDA": 16.0}, sector_limits={}, single_limit=15, exempt=set())
    assert any("单票红线" in h for h in hits)


# ── 误报无害化 ────────────────────────────────────────────────

def test_bearish_never_clamped():
    """空头/减仓决策绝不被改动（护栏只更保守，不会把减仓改成买入）。"""
    s = _stock(rsi=85, bb=1.0, pvm=25)
    dec = {"recommendation": "减仓", "position_change": "减仓1", "one_line": "z"}
    out, hits = apply_anti_chase(s, dec, n_distinct=9, sector_pct={"半导体/AI算力": 99},
                                 ticker_pct={"NVDA": 99}, sector_limits={"半导体/AI算力": 45},
                                 single_limit=15, exempt=set())
    assert hits == [] and out == dec


def test_exempt_ticker_skipped():
    s = _stock(ticker="IAU", sector="对冲", rsi=85, bb=1.0, pvm=25)
    out, hits = apply_anti_chase(s, _buy(), n_distinct=9, sector_pct={}, ticker_pct={},
                                 sector_limits={}, single_limit=15, exempt={"IAU"})
    assert hits == [] and out == _buy()


def test_no_trigger_passes_through():
    s = _stock(rsi=50, bb=0.5, pvm=0)
    out, hits = apply_anti_chase(s, _buy(), n_distinct=2, sector_pct={"半导体/AI算力": 30},
                                 ticker_pct={"NVDA": 5}, sector_limits={"半导体/AI算力": 45},
                                 single_limit=15, exempt=set())
    assert hits == [] and out == _buy()


# ── 速率统计（带 db）──────────────────────────────────────────

def test_recent_buy_velocity(tmp_path):
    db = tmp_path / "t.db"
    tracker.init_db(db)
    today = datetime.date.today().isoformat()
    with tracker._conn(db) as con:
        for t in ["NVDA", "TSM", "FUTU", "AAPL", "QQQM", "IAU"]:
            con.execute("INSERT INTO user_actions(date,ticker,action,source) VALUES(?,?,?,?)",
                        (today, t, "BUY", "auto"))
        con.execute("INSERT INTO user_actions(date,ticker,action,source) VALUES(?,?,?,?)",
                    (today, "MU", "SELL", "auto"))
    n, tks = recent_buy_velocity(exempt={"QQQM", "IAU"}, db_path=db)
    assert n == 4 and "MU" not in tks  # QQQM/IAU 豁免、MU 是卖出


# ── 对抗审查回归 ──────────────────────────────────────────────

def test_config_dir_resolves_and_loads():
    """回归 P0：_CONFIG_DIR 必须指向真实 config，否则 G2 板块红线/豁免全失效。"""
    assert guards._CONFIG_DIR.exists(), guards._CONFIG_DIR
    pf = guards.load_portfolio()
    assert len(pf.get("holdings", [])) > 0           # 真能读到持仓
    sl, single = parse_limits(pf)
    assert any("半导体" in k for k in sl) and single == 15.0  # 板块红线真解析到


def test_clamp_clears_entry_hint_and_text():
    """回归 P2：clamp 后清掉买入入场点、措辞不写死'降级观望'。"""
    s = _stock(rsi=72, bb=0.95, pvm=15)
    dec = _buy()
    dec["entry_hint"] = "回调到180买入"
    out, hits = apply_anti_chase(s, dec, n_distinct=5, sector_pct={}, ticker_pct={},
                                 sector_limits={}, single_limit=15, exempt=set())
    assert hits
    assert "买入" not in out["entry_hint"] and "护栏期" in out["entry_hint"]
    assert "护栏拦截" in out["one_line"] and "降级观望" not in out["one_line"]


def test_guardrail_adherence_inverted(tmp_path):
    """回归 P1：护栏 guidance 反向语义——忍住没买=resisted，买了=ignored。"""
    db = tmp_path / "t.db"
    # 忍住(过期未买) → resisted
    save_guidance([{"ticker": "NVDA", "action": "观望", "target": "护栏", "due_by": "2020-01-01"}],
                  source="guardrail", db_path=db)
    res = check_guidance_adherence(db_path=db)
    assert len(res["guardrail_resisted"]) == 1 and res["expired"] == []
    # 另一条：买了 → ignored
    today = datetime.date.today().isoformat()
    save_guidance([{"ticker": "TSM", "action": "观望", "target": "护栏"}],
                  source="guardrail", db_path=db)
    with tracker._conn(db) as con:
        con.execute("INSERT INTO user_actions(date,ticker,action,source) VALUES(?,?,?,?)",
                    (today, "TSM", "BUY", "manual"))
    res2 = check_guidance_adherence(db_path=db)
    assert len(res2["guardrail_ignored"]) == 1
