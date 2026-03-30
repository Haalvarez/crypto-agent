# =============================================================
#  CRYPTO AGENT — EXECUTOR
#  Ejecuta órdenes en Binance Testnet cuando hay señal accionable
# =============================================================

import sqlite3
import ccxt
from datetime import datetime
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    MAX_TRADE_USD, MAX_OPEN_POSITIONS
)


# ── Conexión al exchange ──────────────────────────────────────

def get_exchange():
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'options': {
            'defaultType': 'spot',
            'adjustForTimeDifference': True,
        },
    })
    if BINANCE_TESTNET:
        exchange.set_sandbox_mode(True)
    return exchange


# ── Base de datos SQLite ──────────────────────────────────────

def init_db():
    conn = sqlite3.connect('trades.db')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT,
            direction   TEXT,
            conviction  INTEGER,
            entry_price REAL,
            stop_loss   REAL,
            take_profit REAL,
            quantity    REAL,
            usd_value   REAL,
            order_id    TEXT,
            status      TEXT DEFAULT 'OPEN',
            exit_price  REAL,
            pnl_usd     REAL,
            opened_at   TEXT,
            closed_at   TEXT
        )
    ''')
    conn.commit()
    conn.close()


def save_trade(trade: dict) -> int:
    conn = sqlite3.connect('trades.db')
    cur = conn.execute('''
        INSERT INTO trades
        (symbol, direction, conviction, entry_price, stop_loss, take_profit,
         quantity, usd_value, order_id, status, opened_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    ''', (
        trade['symbol'], trade['direction'], trade['conviction'],
        trade['entry_price'], trade['stop_loss'], trade['take_profit'],
        trade['quantity'], trade['usd_value'], trade['order_id'],
        datetime.now().isoformat()
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def get_open_trades() -> list:
    conn = sqlite3.connect('trades.db')
    cur = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    trades = cur.fetchall()
    conn.close()
    return trades


def count_open_trades() -> int:
    conn = sqlite3.connect('trades.db')
    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    count = cur.fetchone()[0]
    conn.close()
    return count


# ── Parsing de precios desde señal ───────────────────────────

def parse_price(value: str) -> float:
    """Extrae el primer número de strings como '$66,400 (en retroceso)'"""
    import re
    if not value or value == 'N/A':
        return 0.0
    nums = re.findall(r'[\d,]+\.?\d*', value.replace(',', ''))
    return float(nums[0]) if nums else 0.0


# ── Ejecución principal ───────────────────────────────────────

def execute_signal(signal: dict, market_data: dict) -> dict | None:
    """
    Ejecuta una señal accionable en Binance.
    Retorna dict con resultado o None si no se ejecutó.
    """
    init_db()

    symbol    = signal['symbol']
    direction = signal['direction']

    # Verificar límite de posiciones abiertas
    open_count = count_open_trades()
    if open_count >= MAX_OPEN_POSITIONS:
        print(f"  [executor] Límite de {MAX_OPEN_POSITIONS} posiciones abiertas — saltando {symbol}")
        return None

    # Precio actual
    current_price = market_data.get(symbol, {}).get('price', 0)
    if not current_price:
        print(f"  [executor] Sin precio para {symbol} — abortando")
        return None

    # Calcular cantidad a comprar (máximo MAX_TRADE_USD)
    quantity_raw = MAX_TRADE_USD / current_price

    # Parsear stop y target desde la señal
    stop_loss   = parse_price(signal.get('stop_loss', ''))
    take_profit = parse_price(signal.get('take_profit', ''))

    if not stop_loss or not take_profit:
        print(f"  [executor] Stop o target inválido para {symbol} — abortando")
        return None

    try:
        exchange = get_exchange()

        # Redondear quantity según las reglas del par
        exchange.load_markets()
        market    = exchange.market(symbol)
        precision = market['precision']['amount']
        quantity  = exchange.amount_to_precision(symbol, quantity_raw)

        print(f"  [executor] Ejecutando {direction} {symbol} | qty: {quantity} | precio: ${current_price}")

        # Orden de mercado
        side  = 'buy' if direction == 'LONG' else 'sell'
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=float(quantity),
        )

        entry_price = float(order.get('average') or order.get('price') or current_price)
        order_id    = str(order['id'])
        usd_value   = float(quantity) * entry_price

        # Guardar en DB
        trade_data = {
            'symbol':      symbol,
            'direction':   direction,
            'conviction':  signal['conviction'],
            'entry_price': entry_price,
            'stop_loss':   stop_loss,
            'take_profit': take_profit,
            'quantity':    float(quantity),
            'usd_value':   usd_value,
            'order_id':    order_id,
        }
        trade_id = save_trade(trade_data)

        print(f"  [executor] Orden ejecutada — ID: {order_id} | Trade DB ID: {trade_id}")

        return {
            'trade_id':    trade_id,
            'order_id':    order_id,
            'symbol':      symbol,
            'direction':   direction,
            'entry_price': entry_price,
            'stop_loss':   stop_loss,
            'take_profit': take_profit,
            'quantity':    float(quantity),
            'usd_value':   usd_value,
        }

    except Exception as e:
        print(f"  [executor] ERROR ejecutando {symbol}: {e}")
        return None


def get_balance_usdt() -> float:
    """Retorna el balance de USDT disponible."""
    try:
        exchange = get_exchange()
        balance  = exchange.fetch_balance()
        return float(balance['free'].get('USDT', 0))
    except Exception as e:
        print(f"  [executor] ERROR obteniendo balance: {e}")
        return 0.0


def close_trade(trade_id: int, exit_price: float, result: str) -> None:
    """Marca un trade como cerrado en la DB con PnL calculado."""
    conn = sqlite3.connect('trades.db')
    trade = conn.execute(
        "SELECT direction, entry_price, quantity FROM trades WHERE id = ?", (trade_id,)
    ).fetchone()
    if trade:
        direction, entry_price, quantity = trade
        if direction == 'LONG':
            pnl_usd = (exit_price - entry_price) * quantity
        else:
            pnl_usd = (entry_price - exit_price) * quantity
        conn.execute(
            """UPDATE trades SET status=?, exit_price=?, pnl_usd=?, closed_at=?
               WHERE id=?""",
            (result, exit_price, round(pnl_usd, 4), datetime.now().isoformat(), trade_id)
        )
        conn.commit()
    conn.close()


def check_open_positions(market_data: dict) -> list[dict]:
    """
    Revisa todas las posiciones OPEN contra el precio actual.
    Cierra las que tocaron stop-loss o take-profit.
    Retorna lista de trades cerrados en este ciclo.
    """
    conn   = sqlite3.connect('trades.db')
    trades = conn.execute(
        "SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity FROM trades WHERE status='OPEN'"
    ).fetchall()
    conn.close()

    closed = []
    for trade in trades:
        trade_id, symbol, direction, entry, stop, target, qty = trade
        price = market_data.get(symbol, {}).get('price', 0)
        if not price:
            continue

        result     = None
        exit_price = None

        if direction == 'LONG':
            if price <= stop:
                result, exit_price = 'LOSS', stop
            elif price >= target:
                result, exit_price = 'WIN', target
        else:  # SHORT
            if price >= stop:
                result, exit_price = 'LOSS', stop
            elif price <= target:
                result, exit_price = 'WIN', target

        if result:
            close_trade(trade_id, exit_price, result)
            pnl = (exit_price - entry) * qty if direction == 'LONG' else (entry - exit_price) * qty
            closed.append({
                'trade_id':    trade_id,
                'symbol':      symbol,
                'direction':   direction,
                'result':      result,
                'entry_price': entry,
                'exit_price':  exit_price,
                'pnl_usd':     round(pnl, 4),
            })
            print(f"  [executor] Trade #{trade_id} cerrado: {result} | {symbol} | PnL ${pnl:.2f}")

    return closed


def get_all_trades_stats() -> dict:
    """Retorna estadísticas globales de todos los trades."""
    conn  = sqlite3.connect('trades.db')
    rows  = conn.execute("SELECT status, pnl_usd FROM trades").fetchall()
    open_ = conn.execute("SELECT id, symbol, direction, entry_price, stop_loss, take_profit, opened_at FROM trades WHERE status='OPEN'").fetchall()
    conn.close()

    wins   = [r for r in rows if r[0] == 'WIN']
    losses = [r for r in rows if r[0] == 'LOSS']
    total  = len(wins) + len(losses)

    return {
        "total_closed": total,
        "wins":         len(wins),
        "losses":       len(losses),
        "open_count":   len(open_),
        "win_rate":     round(len(wins) / total * 100, 1) if total else 0,
        "total_pnl":    round(sum(r[1] or 0 for r in rows if r[0] in ('WIN', 'LOSS')), 2),
        "open_trades":  [
            {
                "id":          t[0], "symbol": t[1], "direction": t[2],
                "entry_price": t[3], "stop_loss": t[4], "take_profit": t[5],
                "opened_at":   t[6],
            }
            for t in open_
        ],
    }
