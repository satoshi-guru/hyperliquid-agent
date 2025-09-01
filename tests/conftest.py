import pytest
from unittest.mock import patch

@pytest.fixture(autouse=True)
def mock_hyperliquid_all():
    """
    Stubbt den globalen Hyperliquid-Client (src.api.main.hyperliquid)
    für alle Tests → keine echten ccxt/Netzwerk-Aufrufe.
    """
    with patch("src.api.main.hyperliquid") as mock:
        # Exchange-Methoden
        mock.exchange.fetch_positions.return_value = []
        mock.exchange.fetch_open_orders.return_value = []
        mock.exchange.fetch_ticker.return_value = {"last": "100.0"}

        # High-Level Client-Methoden
        mock.get_market_data.return_value = [[1700000000, 100, 101, 99, 100.5, 123.45]]
        mock.get_open_positions.return_value = {}
        mock.place_order.return_value = {"status": "ok", "order_id": "123"}

        yield mock
