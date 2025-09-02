import os
import logging
import json
import time
import math
import threading
import coloredlogs # type: ignore

from decimal import Decimal, ROUND_UP, ROUND_DOWN, getcontext

from fastapi import FastAPI, BackgroundTasks # type: ignore
from swarm import Agent, Swarm # type: ignore
from src.clients.hyperliquid import HyperliquidClient

from typing import Dict, Tuple, Optional #for helpers

# ---------- Logging ----------
coloredlogs.install()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Decimal-Genauigkeit ----------
getcontext().prec = 28

# ---------- FastAPI ----------
app = FastAPI(title="Hyperliquid Trading Bot API")

# ---------- Clients ----------
hyperliquid = HyperliquidClient(testnet=False)
swarm_client = Swarm()

# ---------- Zustände ----------
running = False        # nur für /status
stop_event = threading.Event()

watchlist = set(["BTC/USDC:USDC", "ETH/USDC:USDC", "SOL/USDC:USDC"])
executed_trades_log = []

# ---------- Konfiguration / Parameter ----------
# Gebühren (ohne Rebate; bei dir 0,0432% Maker / 0,0864% Taker; bei Bedarf anpassen)
MAKER_BPS = Decimal("0.0432")
TAKER_BPS = Decimal("0.0864")
MAKER_RATE = MAKER_BPS / Decimal("100")
TAKER_RATE = TAKER_BPS / Decimal("100")

# Ziel & Risiko
TARGET_PROFIT_USDC = Decimal("0.50")      # fester Netto-TP je Trade
PER_TRADE_EQUITY_RISK = Decimal("0.02")   # max. 2% von Gesamt-Equity pro Trade

# LLM-Kosten / Kadenz
PAUSE_UTILIZATION = Decimal("0.80")       # ab 80% Kapitalbindung: LLM Calls pausieren
IDLE_ACTIVE_SEC = 5                       # Poll-Intervall wenn aktiv
IDLE_PAUSED_SEC = 30                      # Poll-Intervall bei Pause

# ---------- Swarm-Agents ----------
risk_assessment_agent = Agent(
    name="Risk-Assessor",
    instructions="Analyze risk scores for given crypto assets. Score from 0-100 (lower is better).",
)

trade_execution_agent = Agent(
    name="Trade-Executor",
    instructions="Decide trades based on risk scores. Buy if <40, Sell if >60, Hold otherwise.",
)

# === ADD: helpers (z.B. direkt unter den Agent-Definitionen) =================

def get_equity_and_free_collateral(hyperliquid) -> Tuple[Optional[float], Optional[float], str]:
    """
    Versucht echte Werte aus fetch_balance() zu ziehen.
    Fallback: konservative Approximation aus offenen Positionen.
    Returns: (equity, free_collateral, source)
    """
    # 1) Primärquelle: fetch_balance().info hat je nach CCXT-Version diese Keys:
    #    - 'accountValue' (≈ Equity)
    #    - 'freeCollateral'
    try:
        bal = hyperliquid.exchange.fetch_balance()
        info = bal.get("info", {}) or {}
        # Hyperliquid-typische Felder (können je nach CCXT-Version variieren)
        equity = (
            float(info.get("accountValue"))
            if info.get("accountValue") is not None
            else float(bal.get("total", {}).get("USDC", 0.0))  # Fallback
        )
        free_coll = (
            float(info.get("freeCollateral"))
            if info.get("freeCollateral") is not None
            else float(bal.get("free", {}).get("USDC", 0.0))   # Fallback
        )
        if equity and equity > 0:
            return equity, max(free_coll, 0.0), "balance"
    except Exception as e:
        logger.debug(f"[util] fetch_balance fallback, reason: {e}")

    # 2) Fallback: aus offenen Positionen konservativ abschätzen.
    #    Idee: verwendete Margin ≈ Sum(|contracts * entryPrice| / max(leverage,1)).
    #    Equity unbekannt → nehmen used_margin * 1.25 als grobe Obergrenze,
    #    damit Utilization nicht zu optimistisch ist.
    try:
        open_positions = hyperliquid.get_open_positions()
        used_margin = 0.0
        for p in open_positions.values():
            qty = float(p.get("contracts", 0) or 0)
            entry = float(p.get("entryPrice", 0) or 0)
            lev = float(p.get("leverage", 1) or 1) or 1
            notional = abs(qty * entry)
            used_margin += (notional / max(lev, 1.0))
        if used_margin > 0:
            equity = used_margin * 1.25   # konservativ
            free_coll = max(equity - used_margin, 0.0)
            return equity, free_coll, "approx"
    except Exception as e:
        logger.debug(f"[util] approx fallback failed: {e}")

    return None, None, "none"


def calc_utilization(equity: Optional[float], free_coll: Optional[float]) -> Optional[float]:
    if equity is None or equity <= 0 or free_coll is None:
        return None
    used = max(equity - free_coll, 0.0)
    return min(max(used / equity, 0.0), 1.0)


def summarize_positions(open_positions: Dict[str, Dict]) -> Dict[str, float]:
    """
    Loggt eine kompakte Übersicht und liefert Totals zurück.
    Totals: positions, gross_notional, used_margin(est), unrealized_pnl(est)
    """
    totals = {
        "positions": 0,
        "gross_notional": 0.0,
        "used_margin_est": 0.0,
        "unrealized_pnl_est": 0.0,
    }
    if not open_positions:
        logger.info("📭 No open positions.")
        return totals

    logger.info("— Positions Summary —")
    logger.info(f"{'SYMBOL':<18} {'SIDE':<6} {'QTY':>10} {'ENTRY':>12} {'LEV':>6} {'NOTIONAL':>14} {'PnL':>10}")
    for sym, p in open_positions.items():
        side = (p.get("side") or "").upper()
        qty = float(p.get("contracts") or 0)
        entry = float(p.get("entryPrice") or 0)
        lev = float(p.get("leverage") or 1)
        pnl = float(p.get("unrealizedPnl") or 0)

        notional = abs(qty * entry)
        used_margin = notional / max(lev, 1.0)

        totals["positions"] += 1
        totals["gross_notional"] += notional
        totals["used_margin_est"] += used_margin
        totals["unrealized_pnl_est"] += pnl

        logger.info(f"{sym:<18} {side:<6} {qty:>10.6f} {entry:>12.2f} {lev:>6.1f} {notional:>14.2f} {pnl:>10.2f}")

    logger.info(
        f"TOTALS → Positions: {totals['positions']} | "
        f"Gross Notional: {totals['gross_notional']:.2f} | "
        f"Used Margin(est): {totals['used_margin_est']:.2f} | "
        f"Unrealized PnL(est): {totals['unrealized_pnl_est']:.2f}"
    )
    return totals
# =============================================================================

# ---------- Helfer ----------

def round_price_to_cent(p: Decimal) -> Decimal:
    return p.quantize(Decimal("0.01"), rounding=ROUND_UP)

def get_equity_and_free_collateral() -> tuple[Decimal, Decimal]:
    """
    Versucht Equity & Free Collateral aus ccxt Balance zu lesen.
    Fällt robust zurück, falls Schema anders ist.
    """
    try:
        bal = hyperliquid.exchange.fetch_balance()
        total = bal.get("total", {})
        free = bal.get("free", {})
        # bevorzugt USDC / USD / info-Felder
        equity = Decimal(str(
            total.get("USDC") or total.get("USD")
            or bal.get("info", {}).get("equity") or "0"
        ))
        free_collateral = Decimal(str(
            free.get("USDC") or free.get("USD")
            or bal.get("info", {}).get("freeCollateral") or "0"
        ))
        if equity <= 0:
            # letzter Fallback: nicht pausieren
            return Decimal("1"), Decimal("1")
        return equity, free_collateral
    except Exception as e:
        logger.warning(f"⚠️ fetch_balance failed: {e}")
        return Decimal("1"), Decimal("1")

def utilization(equity: Decimal, free_collateral: Decimal) -> Decimal:
    used = equity - free_collateral
    if equity <= 0:
        return Decimal("0")
    u = used / equity
    return max(Decimal("0"), min(Decimal("1"), u))

def tp_price_for_target_profit_usdc(entry_px: Decimal, size: Decimal, target_usdc: Decimal, side: str) -> Decimal:
    """
    Ziel: Netto-TP in USDC erreichen, inkl. Fees.
    LONG:
      (tp - entry)*size - taker*entry*size - maker*tp*size = target
      => tp = [ entry*(1 + taker) + target/size ] / (1 - maker)
    SHORT:
      (entry - tp)*size - taker*entry*size - maker*tp*size = target
      => tp = [ entry*(1 - taker) - target/size ] / (1 + maker)
    """
    if size <= 0:
        return entry_px

    maker = MAKER_RATE
    taker = TAKER_RATE

    if side == "buy":  # LONG
        tp_raw = (entry_px * (Decimal("1") + taker) + (target_usdc / size)) / (Decimal("1") - maker)
    else:              # "sell" = SHORT
        tp_raw = (entry_px * (Decimal("1") - taker) - (target_usdc / size)) / (Decimal("1") + maker)

    return tp_raw.quantize(Decimal("0.01"), rounding=ROUND_UP)

def sl_price_for_equity_risk(entry_px: Decimal, size: Decimal, equity_usdc: Decimal, side: str) -> Decimal:
    """
    Cap: max PER_TRADE_EQUITY_RISK * equity_usdc netto Verlust, inkl. Taker-Fees.
    LONG (SL < entry):
      loss = (entry - sl)*size + taker*entry*size + taker*sl*size <= cap
      => sl = [ entry*size*(1 + taker) - cap ] / [ size*(1 - taker) ]
    SHORT (SL > entry):
      loss = (sl - entry)*size + taker*entry*size + taker*sl*size <= cap
      => sl = [ cap + entry*size*(1 - taker) ] / [ size*(1 + taker) ]
    """
    taker = TAKER_RATE
    cap = PER_TRADE_EQUITY_RISK * equity_usdc

    if size <= 0:
        return entry_px

    if side == "buy":  # LONG
        den = size * (Decimal("1") - taker)
        if den <= 0:
            return (entry_px * Decimal("0.99")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        sl = (entry_px * size * (Decimal("1") + taker) - cap) / den
        return sl.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    else:              # "sell" = SHORT
        den = size * (Decimal("1") + taker)
        if den <= 0:
            return (entry_px * Decimal("1.01")).quantize(Decimal("0.01"), rounding=ROUND_UP)
        sl = (cap + entry_px * size * (Decimal("1") - taker)) / den
        return sl.quantize(Decimal("0.01"), rounding=ROUND_UP)

# ---------- Trading ----------

def execute_trades(trade_decisions, open_positions):
    """
    Führt Trades aus:
      - respektiert $20 Mindest-Nominal
      - nutzt TP=+0.50 USDC (netto) und SL=max 2% Equity
      - schließt gegensätzliche Positionen, öffnet neue mit Brackets
    """
    global executed_trades_log
    executed_trades = []

    for asset, decision in trade_decisions.items():
        if asset not in watchlist:
            logger.info(f"Skipping {asset}: Not in watchlist")
            continue

        if decision not in ["buy", "sell"]:
            logger.info(f"Skipping {asset}: No action needed ({decision})")
            continue

        # Preis holen
        try:
            ticker = hyperliquid.exchange.fetch_ticker(asset)
            latest_price = Decimal(str(ticker["last"]))
        except Exception as e:
            logger.error(f"❌ Error fetching price for {asset}: {e}")
            continue

        # Mindestgröße (Nominal ≥ 20 USDC)
        min_trade_size = (Decimal("20") / latest_price).quantize(Decimal("0.000001"), rounding=ROUND_UP)
        logger.info(f"ℹ️ Calculated min trade size for {asset}: {min_trade_size} (latest price: {latest_price})")

        # Positionserkennung
        if asset in open_positions:
            position = open_positions[asset]
            position_side = position.get("side", "none")
            position_size = Decimal(str(position.get("contracts", "0")))
            entry_price = Decimal(str(position.get("entryPrice", latest_price)))
        else:
            position_side = "none"
            position_size = min_trade_size
            entry_price = latest_price

        # TP/SL nach Ziel/Risiko
        equity, free_coll = get_equity_and_free_collateral()
        # TP/SL nach Ziel/Risiko – abhängig von der Seite (buy=LONG, sell=SHORT)
        take_profit_price = tp_price_for_target_profit_usdc(entry_price, position_size, TARGET_PROFIT_USDC, decision)
        stop_loss_price   = sl_price_for_equity_risk(entry_price, position_size, equity, decision)
        logger.info(f"📊 Setting TP: {take_profit_price}, SL: {stop_loss_price} for {asset} ({decision.upper()})")

                # Sicherheitskorridor: min. 5 Cent Abstand zum Entry
        cent = Decimal("0.05")
        if decision == "buy":  # LONG
            if take_profit_price <= entry_price:
                take_profit_price = (entry_price + cent).quantize(cent)
            if stop_loss_price >= entry_price:
                stop_loss_price = (entry_price - cent).quantize(cent)
        else:                  # SHORT
            if take_profit_price >= entry_price:
                take_profit_price = (entry_price - cent).quantize(cent)
            if stop_loss_price <= entry_price:
                stop_loss_price = (entry_price + cent).quantize(cent)

        # Gegensätzliche Position schließen
        if decision == "sell" and position_side == "long":
            logger.info(f"⚠️ Closing LONG on {asset}.")
            order = hyperliquid.place_order(asset, "sell", float(position_size), float(latest_price),
                                            take_profit=take_profit_price, stop_loss=stop_loss_price)
            executed_trades.append(order)
            continue

        if decision == "buy" and position_side == "short":
            logger.info(f"⚠️ Closing SHORT on {asset}.")
            order = hyperliquid.place_order(asset, "buy", float(position_size), float(latest_price),
                                            take_profit=take_profit_price, stop_loss=stop_loss_price)
            executed_trades.append(order)
            continue

        # Neue Short-/Long-Position, falls keine offen
        if decision == "sell" and position_side == "none":
            logger.info(f"🛑 Opening new SHORT on {asset}.")
            order = hyperliquid.place_order(asset, "sell", float(min_trade_size), float(latest_price),
                                            take_profit=take_profit_price, stop_loss=stop_loss_price)
            executed_trades.append(order)
            continue

        if decision == "buy" and position_side == "none":
            logger.info(f"📈 Opening new LONG on {asset}.")
            order = hyperliquid.place_order(asset, "buy", float(min_trade_size), float(latest_price),
                                            take_profit=take_profit_price, stop_loss=stop_loss_price)
            executed_trades.append(order)
            continue

        # Falls bereits Position existiert und Entscheidung gleichgerichtet → optional skalieren
        order = hyperliquid.place_order(asset, decision, float(min_trade_size), float(latest_price),
                                        take_profit=take_profit_price, stop_loss=stop_loss_price)
        if order:
            logger.info(f"✅ Executed {decision.upper()} order for {min_trade_size} {asset} at {latest_price}")
            executed_trades.append(order)
            executed_trades_log.append(order)
        else:
            logger.error(f"❌ Failed to place order for {asset}")

    return executed_trades

def trading_loop():
    """Main trading loop: fetch market data, assess risk (including open positions), and execute trades."""
    global running

    CYCLE_SEC = 16 * 60  # 16 Minuten zwischen den Zyklen

    while running:
        logger.info("\n---- Running Trading Cycle ----")

        # 1) Daten holen
        market_data = {asset: hyperliquid.get_market_data(asset) for asset in watchlist}
        open_positions = hyperliquid.get_open_positions()

        # 2) Summary & Utilization
        try:
            summarize_positions(open_positions)
        except Exception as e:
            logger.warning(f"⚠️ summarize_positions failed: {e}")

        equity, free_coll, source = get_equity_and_free_collateral(hyperliquid)
        util = calc_utilization(equity, free_coll)

        if equity is not None and free_coll is not None and util is not None:
            logger.info(
                f"💼 Equity: {equity:.2f} | Free Collateral: {free_coll:.2f} | "
                f"Utilization: {util*100:.1f}% (source={source})"
            )
        else:
            logger.warning("⚠️ Utilization unknown (no balance data). New entries will be allowed cautiously.")

        # 3) Wenn Auslastung ≥ 80% → nur Exits erlauben
        pause_opens = (util is not None and util >= 0.80)
        if pause_opens:
            logger.info("⏸️ Utilization ≥ 80% → Pause (nur Exits)")
            # Optional: hier könntest du z.B. TPs enger nachziehen etc.

        # 4) Risk Assessment (unverändert)
        risk_input = {"market_data": market_data, "open_positions": open_positions}
        risk_response = swarm_client.run(
            agent=risk_assessment_agent,
            messages=[{"role": "user", "content": f"Analyze risk for {json.dumps(risk_input)}"}],
        )
        risk_scores = risk_response.messages[-1]["content"]
        logger.info(f"Risk Scores: {risk_scores}")

        trade_response = swarm_client.run(
            agent=trade_execution_agent,
            messages=[{"role": "user", "content": f"Make trade decisions for risk: {risk_scores}"}],
        )
        try:
            trade_decisions = json.loads(trade_response.messages[-1]["content"])
        except json.JSONDecodeError:
            logger.warning("⚠️ Model response was not valid JSON. Falling back to manual parsing.")
            trade_decisions = {
                asset: "buy" if asset.split("/")[0] in trade_response.messages[-1]["content"] else "hold"
                for asset in watchlist
            }

        logger.info(f"Trade Decisions (Parsed): {trade_decisions}")

        # 5) Wenn pausiert, filtern wir Neueinträge raus (nur Exits durchlassen)
        if pause_opens:
            filtered = {}
            for asset, decision in trade_decisions.items():
                pos = open_positions.get(asset)
                if not pos:
                    # keine offene Position → skip (würde neuen Entry erzeugen)
                    filtered[asset] = "hold"
                else:
                    # Exits erlauben: wenn Entscheidung gegen die aktuelle Richtung geht
                    side = (pos.get("side") or "").lower()
                    if (side == "long" and decision == "sell") or (side == "short" and decision == "buy"):
                        filtered[asset] = decision
                    else:
                        filtered[asset] = "hold"
            trade_decisions = filtered
            logger.info(f"🔒 Openings blocked (≥80%). Decisions now: {trade_decisions}")

        # 6) Trades ausführen
        execute_trades(trade_decisions, open_positions)

        # 7) Sleep (16 Minuten)
        logger.info(f"Waiting {CYCLE_SEC} seconds before next cycle...\n")
        time.sleep(CYCLE_SEC)

# ---------- API ----------

@app.post("/start")
def start_trading(background_tasks: BackgroundTasks):
    """Startet den Trading-Bot im Hintergrund-Thread."""
    if not running:
        stop_event.clear()
        background_tasks.add_task(trading_loop)
        return {"status": "Trading bot started"}
    return {"status": "Trading bot already running"}

@app.post("/stop")
def stop_trading():
    """Stoppt den Trading-Bot sauber."""
    if running:
        stop_event.set()
        return {"status": "Trading bot stopping"}
    return {"status": "Trading bot is not running"}

@app.post("/add-asset/{asset}")
async def add_asset(asset: str):
    """Fügt ein Asset der Watchlist hinzu, wenn es auf Hyperliquid existiert."""
    global watchlist
    formatted_asset = f"{asset.upper()}/USDC:USDC"
    market_data = hyperliquid.get_market_data(formatted_asset)
    if market_data:
        watchlist.add(formatted_asset)
        return {"status": f"Added {formatted_asset} to watchlist"}
    return {"error": f"Asset {formatted_asset} is not tradable on Hyperliquid"}

@app.post("/remove-asset/{asset:path}")
async def remove_asset(asset: str):
    """Entfernt ein Asset aus der Watchlist."""
    global watchlist
    if asset in watchlist:
        watchlist.remove(asset)
        return {"status": f"Removed {asset} from watchlist"}
    return {"error": "Asset not in watchlist"}

@app.get("/watchlist")
async def get_watchlist():
    return {"watchlist": list(watchlist)}

@app.get("/trades")
async def get_trades():
    return {"executed_trades": executed_trades_log}

@app.get("/status")
async def get_status():
    return {"running": running, "watchlist": list(watchlist)}

@app.get("/open-positions")
def get_open_positions():
    """Positions-API (kompakt)."""
    try:
        positions = hyperliquid.exchange.fetch_positions()
        formatted_positions = {p["symbol"]: p for p in positions if p.get("contracts", 0) > 0}
        return {"open_positions": formatted_positions}
    except Exception as e:
        logger.error(f"Error fetching open positions: {e}")
        return {"error": "Failed to fetch open positions"}

@app.get("/open-orders")
def get_open_orders():
    """Offene Orders abrufen."""
    try:
        orders = hyperliquid.exchange.fetch_open_orders()
        return {"open_orders": orders}
    except Exception as e:
        logger.error(f"Error fetching open orders: {e}")
        return {"error": "Failed to fetch open orders"}
