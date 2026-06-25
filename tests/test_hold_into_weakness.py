# tests/test_hold_into_weakness.py
"""
TDD 测试：该减没减守门函数 flag_hold_into_weakness

守门条件：
  - 持有立场：recommendation ∈ {"持有","观望"} 或 position_change 为空/"维持"且非买入/减仓/卖出
  - 且技术面明显走弱：rsi < 50 且 price < ma20 < ma60（空头排列）且 macd < 0
→ 返回含"该减没减"的警示文案；任一条件不满足 → None

历史背景：7 例「该减没减」——对技术面已走弱的票仍建议持有/维持，结果显著跑输。
"""

import pytest
from finance_agent.signals.sell_guard import flag_hold_into_weakness


class TestHoldWithWeakTechnicalsReturnsWarning:
    """持有立场 + 技术面走弱 → 必须产出警示"""

    def test_hold_weak_all_conditions(self):
        """持有 + rsi=40 + price=90<ma20=100<ma60=110 + macd=-1.2 → 含该减没减"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            position_change="维持",
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
            position_change="",
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
            position_change="维持",
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

    def test_empty_position_change_hold_stance(self):
        """position_change 为空字符串 + recommendation=持有 → 视为持有立场"""
        result = flag_hold_into_weakness(
            recommendation="持有",
            position_change="",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="大加",
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
            position_change="减仓20%",
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
            position_change="卖出",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
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
            position_change="维持",
            rsi=None,
            price=None,
            ma20=None,
            ma60=None,
            macd=None,
        )
        assert result is None
