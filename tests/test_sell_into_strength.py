# tests/test_sell_into_strength.py
"""
TDD 测试：卖飞守门函数 flag_sell_into_strength

守门条件：
  - bearish 建议（减仓/卖出，或 position_change 以"减仓"开头）
  - 且技术面强势上行：rsi < 70 且 price > ma20 > ma60 且 macd > 0
→ 返回含"卖飞风险"的警示文案；任一条件不满足 → None
"""

import pytest
from finance_agent.signals.sell_guard import flag_sell_into_strength


class TestBearishWithStrengthReturnsWarning:
    """bearish 建议 + 技术面强势 → 必须产出警示"""

    def test_jian_cang_strong_technicals(self):
        """减仓建议 + rsi=60 + price>ma20>ma60 + macd>0 → 警示"""
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓20%",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is not None
        assert "卖飞风险" in result

    def test_mai_chu_strong_technicals(self):
        """卖出建议 + 技术面强势 → 警示"""
        result = flag_sell_into_strength(
            recommendation="卖出",
            position_change="卖出",
            rsi=55.0,
            price=200.0,
            ma20=180.0,
            ma60=160.0,
            macd=2.5,
        )
        assert result is not None
        assert "卖飞风险" in result

    def test_position_change_starts_jian_cang(self):
        """position_change 以"减仓"开头（bearish）+ 技术面强势 → 警示"""
        result = flag_sell_into_strength(
            recommendation="持有",       # recommendation 非 bearish
            position_change="减仓50%",   # 但 position_change 以"减仓"开头
            rsi=62.0,
            price=150.0,
            ma20=140.0,
            ma60=130.0,
            macd=0.8,
        )
        assert result is not None
        assert "卖飞风险" in result

    def test_warning_contains_historical_data(self):
        """警示文案应包含历史数据（6/10 卖飞、7.4% 跑输）"""
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=65.0,
            price=120.0,
            ma20=110.0,
            ma60=100.0,
            macd=0.5,
        )
        assert result is not None
        # 应提及历史卖飞比例
        assert "6/10" in result or "卖飞" in result
        # 应提及跑输大盘
        assert "7.4%" in result or "7%" in result


class TestNotBearish:
    """非 bearish 建议 → 一律 None"""

    def test_buy_recommendation(self):
        result = flag_sell_into_strength(
            recommendation="买入",
            position_change="大加",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_hold_recommendation(self):
        result = flag_sell_into_strength(
            recommendation="持有",
            position_change="维持",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_dca_recommendation(self):
        result = flag_sell_into_strength(
            recommendation="按计划定投",
            position_change="",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None


class TestOverboughtRsi:
    """bearish + RSI >= 70（超买）→ None（技术面不"强势"，超买更可能回调）"""

    def test_rsi_exactly_70(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=70.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_rsi_75(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=75.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None


class TestWeakPrice:
    """bearish + price <= ma20（价格走弱）→ None"""

    def test_price_below_ma20(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=95.0,      # price < ma20
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_price_equals_ma20(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=100.0,     # price == ma20，不满足 price > ma20
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_ma20_below_ma60(self):
        """ma20 <= ma60（均线下行排列）→ None"""
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=105.0,      # ma20 < ma60，趋势向下
            macd=1.2,
        )
        assert result is None


class TestWeakMacd:
    """bearish + macd <= 0 → None"""

    def test_macd_negative(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=-0.5,
        )
        assert result is None

    def test_macd_zero(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=0.0,
        )
        assert result is None


class TestNoneInputsGraceful:
    """任一技术指标为 None → graceful 返回 None（不瞎警示）"""

    def test_rsi_none(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=None,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_price_none(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=None,
            ma20=100.0,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_ma20_none(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=110.0,
            ma20=None,
            ma60=90.0,
            macd=1.2,
        )
        assert result is None

    def test_ma60_none(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=None,
            macd=1.2,
        )
        assert result is None

    def test_macd_none(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=60.0,
            price=110.0,
            ma20=100.0,
            ma60=90.0,
            macd=None,
        )
        assert result is None

    def test_all_none(self):
        result = flag_sell_into_strength(
            recommendation="减仓",
            position_change="减仓",
            rsi=None,
            price=None,
            ma20=None,
            ma60=None,
            macd=None,
        )
        assert result is None
