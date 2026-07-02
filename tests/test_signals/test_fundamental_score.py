# tests/test_signals/test_fundamental_score.py
"""P1a 基本面量化因子(纯计算核心)：FCF Yield / GP·A毛利资产比 / 12-1月动量 / 分析师上行空间
→ 合成 0-10 fundamental_score，作辩手+PM 的数字证据(替代 LLM 自由发挥)。
铁律：0-10 映射公式是 v1 占位 heuristic(非拟合)，≥60条outcome RankIC 校准前禁当已验证alpha；
可用因子<2 → score=None(不单因子硬撑)；缺字段→None(不猜、不写'None'泄漏进 prompt)。"""
from finance_agent.signals import fundamental_score as fs


def test_fcf_yield():
    """(OCF-CapEx 近似用 freeCashflow)/marketCap * 100。"""
    assert fs._fcf_yield({"freeCashflow": 8e9, "marketCap": 1e11}) == 8.0
    assert fs._fcf_yield({"marketCap": 1e11}) is None       # 缺 fcf
    assert fs._fcf_yield({"freeCashflow": 8e9, "marketCap": 0}) is None  # 市值0不除


def test_gp_to_assets():
    """Gross Profit / Total Assets * 100。"""
    assert fs._gp_to_assets({"Gross Profit": 30e9}, {"Total Assets": 100e9}) == 30.0
    assert fs._gp_to_assets({}, {"Total Assets": 100e9}) is None   # 缺 GP
    assert fs._gp_to_assets({"Gross Profit": 30e9}, {"Total Assets": 0}) is None


def test_momentum_12_1():
    """(close[-22]/close[-252]-1)*100；历史不足 252 → None。"""
    close = [100.0] * 230 + [float(100 + i) for i in range(30)]   # 260 个点
    # close[-252] 与 close[-22]
    exp = round((close[-22] / close[-252] - 1) * 100, 2)
    assert fs._momentum_12_1(close) == exp
    assert fs._momentum_12_1([100.0] * 100) is None    # 不足 252


def test_analyst_upside():
    """(目标价-现价)/现价*100；分析师覆盖 <3 → None(太薄不可信)。"""
    assert fs._analyst_upside({"targetMeanPrice": 120, "currentPrice": 100,
                               "numberOfAnalystOpinions": 10}) == 20.0
    assert fs._analyst_upside({"targetMeanPrice": 120, "currentPrice": 100,
                               "numberOfAnalystOpinions": 2}) is None   # 覆盖太薄
    assert fs._analyst_upside({"currentPrice": 100, "numberOfAnalystOpinions": 10}) is None


def test_clip_bounds():
    """0-10 映射不越界。"""
    assert fs._clip(-5) == 0.0 and fs._clip(99) == 10.0 and fs._clip(7.3) == 7.3


def test_composite_needs_two_factors():
    """可用因子 <2 → score=None(不单因子硬撑误导 PM)。"""
    ff = fs.calculate_fundamental_factors({"freeCashflow": 5e9, "marketCap": 1e11})  # 仅 FCF
    assert ff.fcf_yield == 5.0 and ff.score is None


def test_composite_averages_available():
    """≥2 因子 → 综合分=可用子分等权均值。"""
    info = {"freeCashflow": 5e9, "marketCap": 1e11,          # FCF 5% → 子分 clip(5+5)=10
            "targetMeanPrice": 100, "currentPrice": 100,     # 上行 0% → 子分 clip(5+0)=5
            "numberOfAnalystOpinions": 8}
    ff = fs.calculate_fundamental_factors(info)
    assert ff.fcf_yield == 5.0 and ff.analyst_upside == 0.0
    assert ff.score == 7.5   # (10+5)/2


def test_flag_default_off():
    """改核心建议→flag fundamental_factors.enabled 默认 off。"""
    assert fs.fundamental_factors_enabled() is False


def test_attach_factors_sets_fields_and_appends_view():
    """attach：因子原始值落 earnings 字段(供埋点)+ to_prompt_str 追加进 fundamental_view(供PM)。"""
    from finance_agent.graph.state import StockAnalysis, EarningsSummary
    from finance_agent.signals.models import TechnicalSignals
    sig = TechnicalSignals(ticker="NVDA", close=100, change_pct=0, rsi=50, rsi_signal="neutral",
                           ma5=99, ma20=98, ma60=95, ma_signal="neutral", price_vs_ma20=2,
                           macd=0.1, macd_signal=0.1, macd_hist=0, macd_trend="neutral",
                           bb_upper=110, bb_lower=90, bb_position=0.5, volume_ratio=1, composite_score=0)
    a = StockAnalysis(ticker="NVDA", market="us", signals=sig,
                      earnings=EarningsSummary(fundamental_view="原有基本面分析"))
    ff = fs.calculate_fundamental_factors({"freeCashflow": 5e9, "marketCap": 1e11,
                                           "targetMeanPrice": 120, "currentPrice": 100,
                                           "numberOfAnalystOpinions": 8})
    out = fs.attach_factors(a, ff)
    assert out.earnings.fcf_yield == 5.0 and out.earnings.fundamental_score == ff.score
    assert "原有基本面分析" in out.earnings.fundamental_view       # 不覆盖原文
    assert "FCF收益率" in out.earnings.fundamental_view           # 追加了因子行


def test_attach_factors_none_score_no_view_change():
    """因子不足(score None)但仍可有单因子文本；无任何因子→view 不变。"""
    from finance_agent.graph.state import StockAnalysis, EarningsSummary
    from finance_agent.signals.models import TechnicalSignals
    sig = TechnicalSignals(ticker="X", close=100, change_pct=0, rsi=50, rsi_signal="neutral",
                           ma5=99, ma20=98, ma60=95, ma_signal="neutral", price_vs_ma20=2,
                           macd=0.1, macd_signal=0.1, macd_hist=0, macd_trend="neutral",
                           bb_upper=110, bb_lower=90, bb_position=0.5, volume_ratio=1, composite_score=0)
    a = StockAnalysis(ticker="X", market="us", signals=sig,
                      earnings=EarningsSummary(fundamental_view="原文"))
    out = fs.attach_factors(a, fs.calculate_fundamental_factors({}))   # 全 None
    assert out.earnings.fundamental_view == "原文"   # 无因子 → view 逐字不变


def test_to_prompt_str_skips_missing():
    """渲染跳过缺失因子、绝不写 'None' 泄漏进 prompt。"""
    ff = fs.calculate_fundamental_factors({"freeCashflow": 5e9, "marketCap": 1e11,
                                           "targetMeanPrice": 120, "currentPrice": 100,
                                           "numberOfAnalystOpinions": 8})
    txt = ff.to_prompt_str()
    assert "None" not in txt and "FCF" in txt and "分析师" in txt
    # 无任何因子 → 空串
    assert fs.calculate_fundamental_factors({}).to_prompt_str() == ""
