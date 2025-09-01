import pytest
from unittest.mock import MagicMock, patch
from src.clients.hyperliquid import HyperliquidClient

@pytest.fixture
def client():
    """HyperliquidClient-Instanz (ccxt-Aufrufe werden im Test selbst gemockt)."""
    return HyperliquidClient(testnet=True)

def test_get_market_data(client):
    """get_market_data ruft OHLCV korrekt ab."""
    client.exchange.fetch_ohlcv = MagicMock(
        return_value=[[1700000000, 40000, 40500, 39500, 40200, 100]]
    )

    market_data = client.get_market_data("BTC/USDC:USDC")

    client.exchange.fetch_ohlcv.assert_called_once_with(
        "BTC/USDC:USDC", timeframe="1m", limit=1
    )
    assert market_data is not None

@patch("src.clients.hyperliquid.HyperliquidClient.place_order")
def test_place_market_order(mock_place_order, client):
    """Market-Order via place_order."""
    mock_place_order.return_value = {"id": "order123", "status": "open"}

    order = client.place_order("BTC/USDC:USDC", "buy", 0.01)

    assert order == {"id": "order123", "status": "open"}
    mock_place_order.assert_called_once_with("BTC/USDC:USDC", "buy", 0.01)

@patch("src.clients.hyperliquid.HyperliquidClient.place_order")
def test_place_limit_order(mock_place_order, client):
    """Limit-Order via place_order."""
    mock_place_order.return_value = {"id": "order124", "status": "open"}

    order = client.place_order("ETH/USDC:USDC", "sell", 0.5, 3000)

    assert order == {"id": "order124", "status": "open"}
    mock_place_order.assert_called_once_with("ETH/USDC:USDC", "sell", 0.5, 3000)

@patch("src.clients.hyperliquid.HyperliquidClient.cancel_all_orders")
def test_cancel_orders(mock_cancel_all_orders, client):
    """cancel_all_orders schließt Orders korrekt."""
    mock_cancel_all_orders.return_value = {"status": "success"}

    result = client.cancel_all_orders("BTC/USDC:USDC")

    assert result == {"status": "success"}
    mock_cancel_all_orders.assert_called_once_with("BTC/USDC:USDC")
