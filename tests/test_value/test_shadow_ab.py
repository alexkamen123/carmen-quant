# tests/test_value/test_shadow_ab.py
"""方案A 影子 A/B 对照：每天把"开护栏(regime压反指族) vs 关护栏"两套机会排序都记下来，
7日后回填各自篮子的真实超额，攒够样本直接对比——给方案A装铁证级体温计。
纯记账、不影响线上建议。"""
import json

import pandas as pd

from finance_agent.value import shadow_ab as sab


def _w(strategy, regime):
    """测试桩权重：跌市把 mom 压到 0.7，其余恒 1.0（模拟方案A护栏）。"""
    if regime == "down" and strategy.startswith("mom"):
        return 0.7
    return 1.0


def _opps():
    return [
        {"ticker": "XXX", "signals": [{"strategy": "mom_20", "t_stat": 3.0}]},
        {"ticker": "YYY", "signals": [{"strategy": "rsi_14_30", "t_stat": 2.6}]},
        {"ticker": "ZZZ", "signals": [{"strategy": "boll_20_2", "t_stat": 2.4}]},
    ]


def test_divergent_in_down_regime():
    """跌市：mom 龙头 XXX 被压到 0.7*3.0=2.1，跌到 YYY/ZZZ 之后 → 两套排序分歧。"""
    r = sab.compute_ab_picks(_opps(), regime="down", weight_fn=_w, top_k=3)
    assert r["on"] == ["YYY", "ZZZ", "XXX"]    # 护栏开：mom 被降权后置
    assert r["off"] == ["XXX", "YYY", "ZZZ"]   # 护栏关：按原始 t 值
    assert r["divergent"] is True


def test_no_divergence_in_up_regime():
    """涨市：mom 不被压 → 两套完全一致，divergent=False（无对照价值，仍记录）。"""
    r = sab.compute_ab_picks(_opps(), regime="up", weight_fn=_w, top_k=3)
    assert r["on"] == r["off"] == ["XXX", "YYY", "ZZZ"]
    assert r["divergent"] is False


def test_record_appends_jsonl(tmp_path):
    """记账：一行 JSONL 含日期/行情/两套篮子/分歧标记/未回填标记。"""
    log = tmp_path / "shadow_ab.jsonl"
    sab.record_shadow_ab(_opps(), regime="down", today="2026-06-30",
                         log_path=log, weight_fn=_w, top_k=3)
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-06-30"
    assert row["regime"] == "down"
    assert row["divergent"] is True
    assert row["on"] == ["YYY", "ZZZ", "XXX"]
    assert row["filled"] is False
    assert row["on_alpha"] is None


def test_record_skips_when_regime_none(tmp_path):
    """取数失败 regime=None：无对照意义，不记账（不污染样本）。"""
    log = tmp_path / "shadow_ab.jsonl"
    sab.record_shadow_ab(_opps(), regime=None, today="2026-06-30",
                         log_path=log, weight_fn=_w)
    assert not log.exists()


def _write_rows(log, rows):
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def test_backfill_only_matured(tmp_path):
    """回填：只填"已满7日且未填"的行；未到期的保持 filled=False。"""
    log = tmp_path / "shadow_ab.jsonl"
    _write_rows(log, [
        {"date": "2026-06-01", "regime": "down", "on": ["A"], "off": ["B"],
         "divergent": True, "filled": False, "on_alpha": None, "off_alpha": None},
        {"date": "2026-06-28", "regime": "down", "on": ["A"], "off": ["B"],
         "divergent": True, "filled": False, "on_alpha": None, "off_alpha": None},
    ])
    # alpha 桩：A=+2.0%、B=-1.0%（on 篮子赢）
    alpha = {"A": 2.0, "B": -1.0}
    sab.backfill_shadow_ab(log, alpha_fn=lambda tk, d: alpha[tk], today="2026-06-12")
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert rows[0]["filled"] is True       # 06-01 已满 7 日
    assert rows[0]["on_alpha"] == 2.0
    assert rows[0]["off_alpha"] == -1.0
    assert rows[1]["filled"] is False      # 06-28 未到期，不动


def test_backfill_averages_basket(tmp_path):
    """篮子超额=各票超额均值；取数失败的票跳过，按可用票求均值。"""
    log = tmp_path / "shadow_ab.jsonl"
    _write_rows(log, [
        {"date": "2026-06-01", "regime": "down", "on": ["A", "C"], "off": ["B"],
         "divergent": True, "filled": False, "on_alpha": None, "off_alpha": None},
    ])
    alpha = {"A": 3.0, "C": 1.0, "B": -2.0}   # on 均值=(3+1)/2=2.0
    sab.backfill_shadow_ab(log, alpha_fn=lambda tk, d: alpha[tk], today="2026-07-01")
    row = json.loads(log.read_text().splitlines()[0])
    assert row["on_alpha"] == 2.0
    assert row["off_alpha"] == -2.0


def test_backfill_retries_when_window_incomplete(tmp_path):
    """口径对齐：到期但交易日窗口没长够(alpha_fn全返None) → 不标 filled，留待重试。
    否则该分歧样本会被永久丢失（filled后不再回填）。"""
    log = tmp_path / "shadow_ab.jsonl"
    _write_rows(log, [
        {"date": "2026-06-01", "regime": "down", "on": ["A"], "off": ["B"],
         "divergent": True, "filled": False, "on_alpha": None, "off_alpha": None},
    ])
    # 数据还没长够：alpha_fn 全返 None（窗口未完整）
    sab.backfill_shadow_ab(log, alpha_fn=lambda tk, d: None, today="2026-06-10")
    row = json.loads(log.read_text().splitlines()[0])
    assert row["filled"] is False    # 没算出来就别标完成，下轮再试


def test_backfill_gives_up_when_stale(tmp_path):
    """兜底：超过 give_up_days 仍取不到数（退市等）→ 标 filled 止损，不无限重试。"""
    log = tmp_path / "shadow_ab.jsonl"
    _write_rows(log, [
        {"date": "2026-05-01", "regime": "down", "on": ["DEAD"], "off": ["B"],
         "divergent": True, "filled": False, "on_alpha": None, "off_alpha": None},
    ])
    sab.backfill_shadow_ab(log, alpha_fn=lambda tk, d: None,
                           today="2026-07-01", give_up_days=40)
    row = json.loads(log.read_text().splitlines()[0])
    assert row["filled"] is True     # 已陈旧，止损放弃重试


def test_backfill_business_day_window_not_premature(tmp_path):
    """口径铁证(评审点名补)：真实交易日序列(无周末) + 数据按today截断，
    信号日后第7个交易日要等~9-11日历日。'满7日历日但交易日不够'时不能锁死。"""
    idx = pd.bdate_range("2026-06-01", periods=30)   # 仅交易日，无周末
    close = pd.Series([100.0 + i for i in range(30)], index=idx)   # 每日+1
    bench = pd.Series([100.0] * 30, index=idx)        # 基准不动 → alpha=个股收益

    def alpha_at(asof):
        cut = pd.Timestamp(asof)
        return lambda tk, d: sab._forward_alpha(
            close[close.index <= cut], bench[bench.index <= cut], d, horizon=7)

    log = tmp_path / "shadow_ab.jsonl"
    _write_rows(log, [{"date": "2026-06-01", "regime": "down", "on": ["A"], "off": ["B"],
                       "divergent": True, "filled": False,
                       "on_alpha": None, "off_alpha": None}])
    # 满 7 日历日(06-08)但截断后交易日 bar 不足7个 → 算不出 → 不锁，留待重试
    sab.backfill_shadow_ab(log, alpha_at("2026-06-08"), today="2026-06-08")
    assert json.loads(log.read_text().splitlines()[0])["filled"] is False
    # 足够交易日后(06-20) → 算出，第7交易日个股+7、基准0 → alpha=7.0
    sab.backfill_shadow_ab(log, alpha_at("2026-06-20"), today="2026-06-20")
    row = json.loads(log.read_text().splitlines()[0])
    assert row["filled"] is True
    assert row["on_alpha"] == 7.0


def test_compute_ab_picks_empty_signals_safe():
    """纯函数健壮性(Nit3)：某票 signals 为空不应 max() 崩，跳过即可。"""
    opps = [{"ticker": "EMPTY", "signals": []},
            {"ticker": "OK", "signals": [{"strategy": "rsi_14_30", "t_stat": 2.5}]}]
    r = sab.compute_ab_picks(opps, regime="down", weight_fn=_w, top_k=5)
    assert "OK" in r["on"] and "EMPTY" not in r["on"]


def test_report_insufficient_below_gate(tmp_path):
    """裁决闸门：分歧样本 < MIN_DIVERGENT → insufficient，不下结论。"""
    log = tmp_path / "shadow_ab.jsonl"
    _write_rows(log, [
        {"date": "2026-06-01", "regime": "down", "on": ["A"], "off": ["B"],
         "divergent": True, "filled": True, "on_alpha": 2.0, "off_alpha": -1.0},
    ])
    rep = sab.report_shadow_ab(log)
    assert rep["verdict"] == "insufficient"
    assert rep["n"] == 1


def test_report_guardrail_helps(tmp_path):
    """攒够分歧样本且 on 篮子均值显著 > off → guardrail_helps。"""
    log = tmp_path / "shadow_ab.jsonl"
    rows = [{"date": f"2026-05-{d:02d}", "regime": "down", "on": ["A"], "off": ["B"],
             "divergent": True, "filled": True, "on_alpha": 2.0, "off_alpha": -1.0}
            for d in range(1, 13)]   # 12 条分歧样本 > 闸门10
    _write_rows(log, rows)
    rep = sab.report_shadow_ab(log)
    assert rep["n"] == 12
    assert rep["on_mean"] == 2.0
    assert rep["off_mean"] == -1.0
    assert rep["edge"] == 3.0
    assert rep["verdict"] == "guardrail_helps"


def _series(vals, start="2026-06-01"):
    idx = pd.date_range(start, periods=len(vals), freq="D")
    return pd.Series(vals, index=idx)


def test_forward_alpha_vs_benchmark():
    """前向超额=个股7日收益 - 基准同期收益。"""
    # 个股 100→110 (+10%)，基准 100→102 (+2%) → alpha=+8%
    close = _series([100, 101, 102, 103, 104, 105, 106, 110])   # idx0..7
    bench = _series([100, 100, 100, 100, 100, 100, 100, 102])
    a = sab._forward_alpha(close, bench, "2026-06-01", horizon=7)
    assert a == 8.0


def test_forward_alpha_none_when_window_incomplete():
    """信号日距今不足 horizon 个交易日 → None（数据没长够，不能算）。"""
    close = _series([100, 101, 102])   # 只有 3 天
    bench = _series([100, 100, 100])
    assert sab._forward_alpha(close, bench, "2026-06-01", horizon=7) is None
