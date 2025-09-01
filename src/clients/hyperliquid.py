def place_order(self, asset, side, amount, price=None, take_profit=None, stop_loss=None):
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
        from decimal import Decimal, ROUND_UP

        # Tick/Lot aus Markt-Infos robust ermitteln
        price_step = Decimal(str(
            m.get("precision", {}).get("price")
            or m.get("limits", {}).get("price", {}).get("min")
            or m.get("info", {}).get("priceIncrement")
            or "0.01"
        ))
        size_step = Decimal(str(
            m.get("precision", {}).get("amount")
            or m.get("limits", {}).get("amount", {}).get("min")
            or m.get("info", {}).get("sizeIncrement")
            or "0.000001"
        ))

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
            eff_price = last * (Decimal("1") + slippage) if side == "buy" else last * (Decimal("1") - slippage)
            eff_price = round_price(eff_price)
        else:
            eff_price = round_price(Decimal(str(price)))

        # Mindest-Nominalwert 20 USDC
        min_trade_size = (Decimal("20") / eff_price).quantize(Decimal("0.000001"), rounding=ROUND_UP)
        requested = Decimal(str(amount))
        eff_size = max(requested, min_trade_size)
        eff_size = round_size(eff_size)

        logger.info(f"🛠️ Placing {side.upper()} {order_type} for {asset} at {eff_price} (Size: {eff_size})")
        main_order = self.exchange.create_order(
            asset,
            order_type,
            side,
            float(eff_size),
            None if order_type == "market" else float(eff_price),
        )
        logger.info(f"✅ Placed {side.upper()} order for {eff_size} {asset} at {eff_price}")

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
                asset, "trigger", exit_side, float(eff_size), float(tp_px), params=tp_params
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
                asset, "trigger", exit_side, float(eff_size), float(sl_px), params=sl_params
            )
            logger.info(f"🛑 Stop Loss Order: {json.dumps(sl_order, indent=4)}")

        return main_order

    except Exception as e:
        logger.error(f"❌ Error placing order for {asset}: {e}")
        return None
