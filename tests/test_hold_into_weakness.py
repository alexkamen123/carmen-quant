# tests/test_hold_into_weakness.py
"""
TDD 测试：该减没减守门函数 flag_hold_into_weakness

守门条件：
  - 持有立场：recommendation 不是 买入/减仓/卖出（其余立场都算持有）
  - 且技术面明显走弱：rsi < 50 且 price < ma20 < ma60（空头排列）且 macd < 0
→ 返回含"该减没减"的警示文案；任一条件不满足 → None

历史背景：7 例「该减没减」——对技术面已走弱的票仍建议持有/维持，结果显著跑输。

注：ETF/定投标的的排除发生在 workflow.format_report_node 集成层（逆势继续买是
策略本意），见文件末尾 TestEtfDcaExcludedAtIntegration。
"""

import pytest
from finance_agent.signals.sell_guard import flag_hold_into_weakness


class TestHoldWithWeakTechnicalsReturnsWarning:
    """持有立场 + 技术面走弱 → 必须产出警示"""

    def test_hold_weak_all_conditions(self):
        """持有 + rsi=40 + price=90<ma20=100<ma60=110 + macd=-1.2 → 含该减没减"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is not None
        assert "该减没减" in result

    def test_guanwang_weak_technicals(self):
        """观望建议 + 技术面走弱 -> 警示"""
        result = flag_hold_into_weakness(
            recommendation="观望",
            rsi=45.0,
            price=80.0,
            ma20=90.0,
            ma60=100.0,
            macd=-0.5,
        )
        assert result is not None
        assert "该减没减" in result

    def test_warning_contains_historical_context(self):
        """警示文案应包含历史背景（7例、持有判断错/跑输）"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is not None
        # 应提及历史案例数或"跑输"/"跑输"
        assert "7例" in result or "7 例" in result or "跑输" in result
        # 应包含确认继续持有的引导
        assert "止损" in result or "减仓" in result or "确认" in result

    def test_other_non_directional_stance_treated_as_hold(self):
        """非买入/减仓/卖出的其它立场（如空字符串）也视为持有立场 → 警示"""
        result = flag_hold_into_weakness(
            recommendation="",   # 既非买入也非减仓/卖出 → 当持有处理
            rsi=35.0,
            price=70.0,
            ma20=85.0,
            ma60=95.0,
            macd=-2.0,
        )
        assert result is not None
        assert "该减没减" in result


class TestStrictBoundaryRsi:
    """RSI 边界：rsi == 50 不触发（需严格 <50）"""

    def test_rsi_exactly_50_no_trigger(self):
        """rsi==50 不触发（严格小于）"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=50.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_rsi_55_not_weak(self):
        """rsi=55 不弱 → None"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=55.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None


class TestStrictBoundaryPrice:
    """价格均线边界：price == ma20 不触发（需严格 <ma20）"""

    def test_price_equals_ma20_no_trigger(self):
        """price==ma20 不触发（需严格 price < ma20）"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=100.0,
            ma20=100.0,   # price == ma20，不满足严格 <
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_price_above_ma20_no_trigger(self):
        """price=105 > ma20=100（没空头排列）→ None"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=105.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_ma20_equals_ma60_no_trigger(self):
        """ma20==ma60 不触发（需严格 ma20 < ma60）"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=100.0,   # ma20 == ma60，不满足严格 <
            macd=-1.2,
        )
        assert result is None


class TestStrictBoundaryMacd:
    """MACD 边界：macd == 0 不触发（需严格 <0）"""

    def test_macd_zero_no_trigger(self):
        """macd=0 不触发（严格小于）"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=0.0,
        )
        assert result is None

    def test_macd_positive_no_trigger(self):
        """macd=0.5 正值 → None"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=0.5,
        )
        assert result is None


class TestNonHoldStance:
    """非持有立场（买入/减仓/卖出）→ 一律 None（即使技术面走弱）"""

    def test_buy_recommendation(self):
        """买入建议 → None"""
        result = flag_hold_into_weakness(
            recommendation="买入",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_jian_cang_recommendation(self):
        """减仓建议 → None（已在减了，守门无需重复）"""
        result = flag_hold_into_weakness(
            recommendation="减仓",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_mai_chu_recommendation(self):
        """卖出建议 → None（已在卖了，守门无需重复）"""
        result = flag_hold_into_weakness(
            recommendation="卖出",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None


class TestNoneInputsGraceful:
    """任一技术指标为 None → graceful 返回 None（数据不全不瞎警示）"""

    def test_rsi_none(self):
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=None,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_price_none(self):
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=None,
            ma20=100.0,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_ma20_none(self):
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=None,
            ma60=110.0,
            macd=-1.2,
        )
        assert result is None

    def test_ma60_none(self):
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=None,
            macd=-1.2,
        )
        assert result is None

    def test_macd_none(self):
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=40.0,
            price=90.0,
            ma20=100.0,
            ma60=110.0,
            macd=None,
        )
        assert result is None

    def test_all_none(self):
        result = flag_hold_into_weakness(
            recommendation="持有",
            rsi=None,
            price=None,
            ma20=None,
            ma60=None,
            macd=None,
        )
        assert result is None


def _weak_signals():
    """构造一个「技术面明显走弱」的 TechnicalSignals（满足守门触发条件）。"""
    from finance_agent.signals.models import TechnicalSignals
    return TechnicalSignals(
        close=90.0, change_pct=-2.0,
        rsi=40.0, rsi_signal="neutral",
        ma5=92.0, ma20=100.0, ma60=110.0, ma_signal="death_cross",
        price_vs_ma20=-10.0,
        macd=-1.2, macd_signal=-0.8, macd_hist=-0.4, macd_trend="bearish",
        bb_upper=120.0, bb_lower=80.0, bb_position=0.25,
        volume_ratio=1.0, composite_score=30.0,
    )


def _stock(ticker, *, is_etf=False, recommendation="持有", shares=10.0):
    from finance_agent.graph.state import StockAnalysis
    return StockAnalysis(
        ticker=ticker, market="us", signals=_weak_signals(),
        shares=shares, is_etf=is_etf, recommendation=recommendation,
        position_change="维持", confidence="中", one_line="测试用",
    )


class TestEtfDcaExcludedAtIntegration:
    """集成层：ETF/定投标的即使技术面走弱也不打「该减没减」警示（逆势买是策略本意）。
    普通持仓股则照常警示，证明排除是定向的、没把守门整体关掉。"""

    async def _run(self, stock):
        from finance_agent.graph.state import AgentState
        from finance_agent.graph.workflow import format_report_node
        state = AgentState(date="2026-06-25", stocks=[stock])
        out = await format_report_node(state)
        # 把卡片里所有 div 文本拼起来，检查是否出现警示
        texts = []
        for el in out.report_card.get("elements", []):
            t = el.get("text", {})
            if isinstance(t, dict) and t.get("content"):
                texts.append(t["content"])
        return " ".join(texts)

    async def test_etf_weak_no_warning(self):
        """ETF（is_etf=True）+ 技术面走弱 → 卡片里不出现「该减没减」"""
        text = await self._run(_stock("VOO", is_etf=True))
        assert "该减没减" not in text

    async def test_dca_weak_no_warning(self):
        """定投立场（按计划定投）+ 技术面走弱 → 不出现「该减没减」"""
        text = await self._run(_stock("QQQM", recommendation="按计划定投"))
        assert "该减没减" not in text

    async def test_normal_hold_weak_still_warns(self):
        """普通持仓股（非ETF·持有）+ 技术面走弱 → 仍照常警示（排除是定向的）"""
        text = await self._run(_stock("NVDA", recommendation="持有"))
        assert "该减没减" in text
