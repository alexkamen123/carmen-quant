import pytest
from finance_agent.data.router import DataRouter
from finance_agent.data.yfinance_provider import YFinanceProvider
from finance_agent.data.akshare_provider import AkShareProvider

def test_router_routes_us_to_yfinance():
    router = DataRouter()
    provider = router.get_provider("NVDA", "us")
    assert isinstance(provider, YFinanceProvider)

def test_router_routes_hk_to_akshare():
    router = DataRouter()
    provider = router.get_provider("00700", "hk")
    assert isinstance(provider, AkShareProvider)

def test_router_routes_cn_to_akshare():
    router = DataRouter()
    provider = router.get_provider("600519", "cn")
    assert isinstance(provider, AkShareProvider)

def test_router_raises_on_unknown_market():
    router = DataRouter()
    with pytest.raises(ValueError, match="Unsupported market"):
        router.get_provider("NVDA", "unknown")
