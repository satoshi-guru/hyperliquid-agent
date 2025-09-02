import ccxt # type: ignore
import time
import math
import logging
import json
from config.settings import settings
from decimal import Decimal, ROUND_UP, ROUND_DOWN

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """Handles spot trading for BTC, ETH, and SOL using CCXT with Hyperliquid."""

    def __init__(self, testnet=False):
        self.wallet = settings.HYPERLIQUID_WALLET_ADDRESS
        self.secret = settings.HYPERLIQUID_PRIVATE_KEY
        self.testnet = testnet
        self.exchange = ccxt.hyperliquid(
            {
                "walletAddress": self.wallet,
                "privateKey": self.secret,
            }
        )

        if self.testnet:
            self.exchange.set_sandbox_mode(True)
            # self.exchange.urls["api"] = "https://api.hyperliquid-testnet.xyz"

        self.assets = ["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC"]

    def get_open_positions(self):
        """Fetch open positions for all assets in a concise, human-readable format."""
        try:
            positions = self.exchange.fetch_positions()
            open_positions = {}

            for pos in positions:
                if float(pos.get("contracts", 0)) == 0:
                    continue  # Skip if no active position

                # Dynamically include only available fields
                filtered_details = {
                    key: pos[key]
                    for key in [
                        "symbol",
                        "side",
                        "contracts",
                        "entryPrice",
                        "leverage",
                        "unrealizedPnl",
                        "liquidationPrice",
                    ]
                    if key in pos
                }

                open_positions[pos["symbol"]] = filtered_details

            # ✅ Log formatted output
            if open_positions:
                logger.info("\n📌 **Open Positions:**")
                for asset, details in open_positions.items():
                    logger.info(
                        f"{asset} | {details.get('side', 'N/A').upper()} | "
                        f"Size: {details.get('contracts', 'N/A')} | "
                        f"Entry: {details.get('entryPrice', 'N/A')} | "
                        f"Lev: {details.get('leverage', 'N/A')}x | "
                        f"PnL: {details.get('unrealizedPnl', 'N/A')} | "
                        f"Liquidation: {details.get('liquidationPrice', 'N/A')}"
                    )
            else:
                logger.info("📭 No open positions found.")

            return open_positions

        except Exception as e:
            logger.error(f"❌ Error fetching open positions: {e}")
            return {}

    def get_market_data(self, asset):
        """Retrieve latest OHLCV data (Open, High, Low, Close, Volume)."""
        try:
            data = self.exchange.fetch_ohlcv(asset, timeframe="1m", limit=1)
            logger.info(f"Fetched market data for {asset}: {data}")
            return data  # Latest candle
        except Exception as e:
            logger.error(f"Error fetching market data for {asset}: {e}")
            return None

    def place_order(
        self, asset, side, amount, price=None, take_profit=None, stop_loss=None
    ):
        """
        Places a market/limit order and then attaches TP/SL as separate reduce-only triggers.
        - respektiert das übergebene `amount`
        - garantiert ≥ 20 USDC Nominalwert
        - rundet auf Tick/Lot
        - TP/SL: reduce-only + triggerAbove je Richtung
        """
        try:
            markets = self.exchange.load_markets()
            m = markets[asset]

            # Tick/Lot aus Markt-Infos robust ermitteln
            price_step = Decimal(
                str(
                    m.get("precision", {}).get("price")
                    or m.get("limits", {}).get("price", {}).get("min")
                    or m.get("info", {}).get("priceIncrement")
                    or "0.01"
                )
            )
            size_step = Decimal(
                str(
                    m.get("precision", {}).get("amount")
                    or m.get("limits", {}).get("amount", {}).get("min")
                    or m.get("info", {}).get("sizeIncrement")
                    or "0.000001"
                )
            )

            def round_price(p: Decimal) -> Decimal:
                step = price_step if price_step > 0 else Decimal("0.01")
                return (Decimal(p) / step).to_integral_value(rounding=ROUND_UP) * step

            def round_size(s: Decimal) -> Decimal:
                step = size_step if size_step > 0 else Decimal("0.000001")
                return (Decimal(s) / step).to_integral_value(rounding=ROUND_UP) * step

            # Preis/Order-Typ bestimmen
            order_type = "limit" if price is not None else "market"
            if order_type == "market":
                last = Decimal(str(self.exchange.fetch_ticker(asset)["last"]))
                slippage = Decimal("0.001")  # 0.1 %
                eff_price = (
                    last * (Decimal("1") + slippage)
                    if side == "buy"
                    else last * (Decimal("1") - slippage)
                )
                eff_price = round_price(eff_price)
            else:
                eff_price = round_price(Decimal(str(price)))

            # Mindest-Nominalwert 12 USDC
            min_trade_size = (Decimal("12") / eff_price).quantize(
                Decimal("0.000001"), rounding=ROUND_UP
            )
            requested = Decimal(str(amount))
            eff_size = max(requested, min_trade_size)
            eff_size = round_size(eff_size)

            logger.info(
                f"🛠️ Placing {side.upper()} {order_type} for {asset} at {eff_price} (Size: {eff_size})"
            )
            main_order = self.exchange.create_order(
                asset,
                order_type,
                side,
                float(eff_size),
                None if order_type == "market" else float(eff_price),
            )
            logger.info(
                f"✅ Placed {side.upper()} order for {eff_size} {asset} at {eff_price}"
            )

            # Gegenrichtung für Exit-Orders
            exit_side = "sell" if side == "buy" else "buy"

            # TP (reduce-only; triggerAbove True bei Long, False bei Short)
            if take_profit is not None:
                tp_px = round_price(Decimal(str(take_profit)))
                tp_params = {
                    "triggerPrice": float(tp_px),
                    "tpsl": "tp",
                    "reduceOnly": True,
                    "triggerAbove": True if side == "buy" else False,
                }
                tp_order = self.exchange.create_order(
                    asset,
                    "trigger",
                    exit_side,
                    float(eff_size),
                    float(tp_px),
                    params=tp_params,
                )
                logger.info(f"🎯 Take Profit Order: {json.dumps(tp_order, indent=4)}")

            # SL (reduce-only; triggerAbove False bei Long, True bei Short)
            if stop_loss is not None:
                sl_px = round_price(Decimal(str(stop_loss)))
                sl_params = {
                    "triggerPrice": float(sl_px),
                    "tpsl": "sl",
                    "reduceOnly": True,
                    "triggerAbove": False if side == "buy" else True,
                }
                sl_order = self.exchange.create_order(
                    asset,
                    "trigger",
                    exit_side,
                    float(eff_size),
                    float(sl_px),
                    params=sl_params,
                )
                logger.info(f"🛑 Stop Loss Order: {json.dumps(sl_order, indent=4)}")

            return main_order

        except Exception as e:
            logger.error(f"❌ Error placing order for {asset}: {e}")
            return None

    def cancel_all_orders(self, asset):
        """Cancel all open orders for a specific asset."""
        try:
            orders = self.exchange.fetch_open_orders(asset)
            for order in orders:
                self.exchange.cancel_order(order["id"], asset)
            logger.info(f"Canceled all orders for {asset}")
        except Exception as e:
            logger.error(f"Error canceling orders for {asset}: {e}")
