import pytest #type:ignore
from urllib.parse import quote
from fastapi.testclient import TestClient #type:ignore
from src.api.main import app, execute_trades
from unittest.mock import patch

# FastAPI TestClient
client = TestClient(app)

# ✅ API ENDPOINTS
@pytest.mark.parametrize("endpoint", [
    "/status",
    "/watchlist",
    "/trades",
    "/open-positions",
    "/open-orders",
])
def test_get_endpoints(endpoint):
    """Alle GET-Endpunkte liefern 200 + JSON."""
    r = client.get(endpoint)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")

def test_stop_trading():
    """POST /stop beendet Trading Bot oder gibt 'not running'."""
    r = client.post("/stop")
    assert r.status_code == 200
    assert r.json()["status"] in ["Trading bot stopped", "Trading bot is not running"]

def test_add_remove_asset():
    """
    Asset zur Watchlist hinzufügen und wieder entfernen.
    Mit {asset:path} KEIN URL-Encoding nötig (und auch nicht gewünscht).
    """
    base = "DOGE"
    # add -> erstellt "DOGE/USDC:USDC" in der Watchlist
    r = client.post(f"/add-asset/{base}")
    assert r.status_code == 200
    assert "Added" in r.json().get("status", "")

    # remove -> exakt der gleiche String mit echtem Slash
    full_symbol = f"{base}/USDC:USDC"
    r = client.post(f"/remove-asset/{full_symbol}")
    assert r.status_code == 200


# ✅ TRADE EXECUTION
def test_execute_trades_buy_and_sell():
    """
    Trade-Logik mit garantiert gemocktem hyperliquid
    (expliziter Patch hier → unabhängig von globaler Fixture).
    """
    with patch("src.api.main.hyperliquid") as mock:
        mock.exchange.fetch_ticker.return_value = {"last": "100.0"}
        mock.place_order.return_value = {"status": "ok", "order_id": "123"}

        trade_decisions = {"ETH/USDC:USDC": "buy", "BTC/USDC:USDC": "sell"}
        open_positions = {
            "ETH/USDC:USDC": {"side": "long", "contracts": "1.0", "entryPrice": "2000"},
            "BTC/USDC:USDC": {"side": "short", "contracts": "0.5", "entryPrice": "80000"},
        }

        result = execute_trades(trade_decisions, open_positions)
        assert isinstance(result, list)
        assert len(result) > 0  # mindestens 1 Order platziert

def test_execute_trades_with_no_open_positions():
    """Trade-Logik ohne bestehende Positionen."""
    with patch("src.api.main.hyperliquid") as mock:
        mock.exchange.fetch_ticker.return_value = {"last": "100.0"}
        mock.place_order.return_value = {"status": "ok", "order_id": "123"}

        trade_decisions = {"ETH/USDC:USDC": "buy"}
        open_positions = {}

        result = execute_trades(trade_decisions, open_positions)
        assert isinstance(result, list)
        assert len(result) > 0
