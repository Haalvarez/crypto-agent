# =============================================================
#  CRYPTO AGENT — EXECUTOR
#  Ejecuta órdenes en Binance Testnet cuando hay señal accionable
# =============================================================

import json
import os
import sqlite3
import ccxt
from datetime import datetime, timezone
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    MAX_TRADE_USD, MAX_OPEN_POSITIONS
)

# Railway Volume en /data, fallback a directorio local
DB_PATH = os.path.join(os.getenv('DATA_DIR', '.'), 'trades.db')


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
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol               TEXT,
            direction            TEXT,
            conviction           INTEGER,
            entry_price          REAL,
            stop_loss            REAL,
            take_profit          REAL,
            quantity             REAL,
            usd_value            REAL,
            order_id             TEXT,
            status               TEXT DEFAULT 'OPEN',
            exit_price           REAL,
            pnl_usd              REAL,
            opened_at            TEXT,
            closed_at            TEXT,
            group_name           TEXT DEFAULT 'A',
            strategy             TEXT DEFAULT 'TREND',
            trailing_stop_price  REAL,
            atr_value            REAL
        )
    ''')
    # Migraciones para DBs preexistentes — idempotentes
    for col, typ in [
        ("group_name",          "TEXT DEFAULT 'A'"),
        ("strategy",             "TEXT DEFAULT 'TREND'"),
        ("trailing_stop_price", "REAL"),
        ("atr_value",           "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        except Exception:
            pass

    # Backfill: trades viejos sin strategy → marcar según group_name
    try:
        conn.execute(
            "UPDATE trades SET strategy='MOMENTUM' WHERE strategy IS NULL AND group_name='B'"
        )
        conn.execute(
            "UPDATE trades SET strategy='TREND' WHERE strategy IS NULL"
        )
    except Exception:
        pass

    conn.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT    NOT NULL,
            type       TEXT    NOT NULL,
            symbol     TEXT,
            group_name TEXT,
            level      TEXT    DEFAULT 'INFO',
            title      TEXT    NOT NULL,
            details    TEXT
        )
    ''')
    conn.commit()
    conn.close()


def log_event(type: str, title: str, symbol: str = None, group: str = None,
              level: str = 'INFO', details: dict = None) -> None:
    """Registra un evento en la tabla events."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """INSERT INTO events (timestamp, type, symbol, group_name, level, title, details)
               VALUES (?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), type, symbol, group, level, title,
             json.dumps(details, default=str) if details else None)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [executor] log_event ERROR: {e}")


def get_events(limit: int = 100, offset: int = 0,
               type_filter: str = None, symbol_filter: str = None) -> list[dict]:
    """Retorna eventos ordenados por timestamp descendente."""
    conn  = sqlite3.connect(DB_PATH)
    where = []
    args  = []
    if type_filter:
        where.append("type = ?");   args.append(type_filter)
    if symbol_filter:
        where.append("symbol = ?"); args.append(symbol_filter)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id,timestamp,type,symbol,group_name,level,title,details "
        f"FROM events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [limit, offset]
    ).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM events {clause}", args).fetchone()[0]
    conn.close()
    result = []
    for r in rows:
        d = {'id':r[0],'timestamp':r[1],'type':r[2],'symbol':r[3],
             'group':r[4],'level':r[5],'title':r[6]}
        try:
            d['details'] = json.loads(r[7]) if r[7] else None
        except Exception:
            d['details'] = r[7]
        result.append(d)
    return result, total


def save_trade(trade: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute('''
        INSERT INTO trades
        (symbol, direction, conviction, entry_price, stop_loss, take_profit,
         quantity, usd_value, order_id, status, opened_at, group_name, strategy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
    ''', (
        trade['symbol'], trade['direction'], trade['conviction'],
        trade['entry_price'], trade['stop_loss'], trade['take_profit'],
        trade['quantity'], trade['usd_value'], trade['order_id'],
        datetime.now().isoformat(),
        trade.get('group_name', 'A'),
        trade.get('strategy',   'TREND'),
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def get_strategy_stats() -> dict:
    """
    Retorna estadísticas agregadas por estrategia: {strategy: {win_rate, profit_factor, ...}}
    Solo cuenta trades cerrados (WIN/LOSS).
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT COALESCE(strategy,'TREND'), status, pnl_usd FROM trades WHERE status IN ('WIN','LOSS')"
    ).fetchall()
    open_rows = conn.execute(
        "SELECT COALESCE(strategy,'TREND') FROM trades WHERE status='OPEN'"
    ).fetchall()
    conn.close()

    by_strat: dict = {}
    for strat, status, pnl in rows:
        s = by_strat.setdefault(strat, {"wins": 0, "losses": 0, "pnl_total": 0.0,
                                        "gross_win": 0.0, "gross_loss": 0.0, "open": 0})
        if status == 'WIN':
            s["wins"]      += 1
            s["gross_win"] += float(pnl or 0)
        else:
            s["losses"]     += 1
            s["gross_loss"] += abs(float(pnl or 0))
        s["pnl_total"] += float(pnl or 0)

    for strat, in open_rows:
        s = by_strat.setdefault(strat, {"wins": 0, "losses": 0, "pnl_total": 0.0,
                                        "gross_win": 0.0, "gross_loss": 0.0, "open": 0})
        s["open"] += 1

    # Computar métricas derivadas
    for strat, s in by_strat.items():
        total = s["wins"] + s["losses"]
        s["total_closed"]  = total
        s["win_rate"]      = round(s["wins"] / total * 100, 1) if total else 0
        s["profit_factor"] = round(s["gross_win"] / s["gross_loss"], 2) if s["gross_loss"] else None
        s["avg_win"]       = round(s["gross_win"]  / s["wins"],   2) if s["wins"]   else 0
        s["avg_loss"]      = round(s["gross_loss"] / s["losses"], 2) if s["losses"] else 0
        s["pnl_total"]     = round(s["pnl_total"], 2)
        s["gross_win"]     = round(s["gross_win"],  2)
        s["gross_loss"]    = round(s["gross_loss"], 2)

    return by_strat


def get_open_trades() -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    trades = cur.fetchall()
    conn.close()
    return trades


def count_open_trades() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    count = cur.fetchone()[0]
    conn.close()
    return count


def has_open_position(symbol: str) -> bool:
    """Retorna True si el par ya tiene una posición abierta."""
    conn  = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND symbol=?", (symbol,)
    ).fetchone()[0]
    conn.close()
    return count > 0


def get_open_position(symbol: str) -> dict | None:
    """Retorna la posición abierta de un par, o None si no hay."""
    conn  = sqlite3.connect(DB_PATH)
    row   = conn.execute(
        """SELECT id, symbol, direction, entry_price, stop_loss, take_profit,
                  quantity, opened_at
           FROM trades WHERE status='OPEN' AND symbol=? LIMIT 1""",
        (symbol,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "symbol": row[1], "direction": row[2],
        "entry_price": row[3], "stop_loss": row[4], "take_profit": row[5],
        "quantity": row[6], "opened_at": row[7],
    }


def get_trade_by_id(trade_id: int) -> dict | None:
    """Retorna un trade OPEN por ID, o None si no existe o ya está cerrado."""
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute(
        """SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity, opened_at
           FROM trades WHERE id=? AND status='OPEN'""",
        (trade_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "symbol": row[1], "direction": row[2],
        "entry_price": row[3], "stop_loss": row[4], "take_profit": row[5],
        "quantity": row[6], "opened_at": row[7],
    }


def market_close_trade(trade: dict, current_price: float, reason: str) -> dict:
    """
    Cierra un trade al precio de mercado (no espera stop/target).
    Usado para salidas por cambio de régimen u otras condiciones externas.
    """
    try:
        exchange = get_exchange()
        exchange.load_markets()
        quantity = exchange.amount_to_precision(trade["symbol"], trade["quantity"])
        # Para cerrar un LONG vendemos, para cerrar un SHORT compramos
        side  = 'sell' if trade["direction"] == 'LONG' else 'buy'
        order = exchange.create_order(
            symbol=trade["symbol"], type='market', side=side, amount=float(quantity)
        )
        exit_price = float(order.get('average') or order.get('price') or current_price)
    except Exception as e:
        print(f"  [executor] ERROR cerrando mercado {trade['symbol']}: {e}")
        exit_price = current_price  # fallback: registrar al precio actual

    if trade["direction"] == 'LONG':
        pnl = (exit_price - trade["entry_price"]) * trade["quantity"]
    else:
        pnl = (trade["entry_price"] - exit_price) * trade["quantity"]

    result = 'WIN' if pnl >= 0 else 'LOSS'
    close_trade(trade["id"], exit_price, result)

    print(f"  [executor] Trade #{trade['id']} cerrado por {reason} | {result} | PnL ${pnl:.2f}")
    return {
        "trade_id":    trade["id"],
        "symbol":      trade["symbol"],
        "direction":   trade["direction"],
        "result":      result,
        "entry_price": trade["entry_price"],
        "exit_price":  exit_price,
        "pnl_usd":     round(pnl, 4),
        "reason":      reason,
    }


# ── Parsing de precios desde señal ───────────────────────────

def parse_price(value: str) -> float:
    """Extrae el primer número de strings como '$66,400 (en retroceso)'"""
    import re
    if not value or value == 'N/A':
        return 0.0
    nums = re.findall(r'[\d,]+\.?\d*', value.replace(',', ''))
    return float(nums[0]) if nums else 0.0


# ── Correlación ───────────────────────────────────────────────

def _has_correlated_position(symbol: str, direction: str) -> bool:
    """
    Verifica si ya hay una posición abierta en un par correlacionado
    con la misma dirección. Ej: LONG BTC abierto bloquea LONG ETH.
    Direcciones opuestas se permiten (hedge implícito).
    """
    from config import CORRELATED_GROUPS

    # Buscar el grupo de correlación del símbolo
    my_group = None
    for group in CORRELATED_GROUPS:
        if symbol in group:
            my_group = group
            break
    if not my_group:
        return False

    # Buscar posiciones abiertas en el mismo grupo con misma dirección
    correlated_peers = my_group - {symbol}
    conn = sqlite3.connect(DB_PATH)
    for peer in correlated_peers:
        row = conn.execute(
            "SELECT direction FROM trades WHERE status='OPEN' AND symbol=? LIMIT 1",
            (peer,)
        ).fetchone()
        if row and row[0] == direction:
            conn.close()
            print(f"  [executor] Correlación: {peer} ya tiene {direction} abierto")
            return True
    conn.close()
    return False


# ── Ejecución principal ───────────────────────────────────────

def _calc_sl_tp(symbol: str, direction: str, entry: float,
                stop_pct: float, take_profit_signal: float) -> tuple[float, float]:
    """
    Calcula SL basado en ATR(14) 4h (mismo timeframe que la señal de entrada).
    Fallback a porcentaje fijo si ATR falla.
    TP: usa el sugerido por Claude si es válido; si no, 2× el riesgo ATR (R:R 1:2).
    """
    from strategies.trailing_stop import _calc_atr_sync, ATR_MULT, ATR_INTERVAL_ENTRY

    atr = _calc_atr_sync(symbol, period=14, interval=ATR_INTERVAL_ENTRY)
    if atr and atr > 0:
        if direction == 'LONG':
            sl = entry - atr * ATR_MULT
            tp = take_profit_signal if take_profit_signal > entry else entry + atr * ATR_MULT * 2
        else:
            sl = entry + atr * ATR_MULT
            tp = take_profit_signal if 0 < take_profit_signal < entry else entry - atr * ATR_MULT * 2
        print(f"  [executor] ATR={atr:.4f} → SL={sl:.4f} TP={tp:.4f}")
    else:
        # Fallback a porcentaje fijo
        pct = stop_pct or 0.04
        if direction == 'LONG':
            sl = entry * (1 - pct)
            tp = take_profit_signal if take_profit_signal > entry else entry * (1 + pct * 2)
        else:
            sl = entry * (1 + pct)
            tp = take_profit_signal if 0 < take_profit_signal < entry else entry * (1 - pct * 2)
        print(f"  [executor] ATR no disponible — usando pct={pct:.1%} → SL={sl:.4f} TP={tp:.4f}")

    return round(sl, 8), round(tp, 8)


def execute_signal(signal: dict, market_data: dict, stop_pct: float = None) -> dict | None:
    """
    Ejecuta una señal accionable en Binance.
    SL calculado con ATR(14) 4h (fallback a % fijo si ATR no disponible).
    Retorna dict con resultado o None si no se ejecutó.
    """
    init_db()

    symbol    = signal['symbol']
    direction = signal['direction']

    # Verificar que no haya posición abierta en este par específico
    if has_open_position(symbol):
        print(f"  [executor] Ya hay posición abierta en {symbol} — saltando")
        return None

    # Verificar correlación — no abrir misma dirección en pares correlacionados
    if _has_correlated_position(symbol, direction):
        print(f"  [executor] Posición correlacionada ya abierta ({symbol} {direction}) — saltando")
        return None

    # Precio actual
    current_price = market_data.get(symbol, {}).get('price', 0)
    if not current_price:
        print(f"  [executor] Sin precio para {symbol} — abortando")
        return None

    # Calcular cantidad a comprar (máximo MAX_TRADE_USD)
    quantity_raw = MAX_TRADE_USD / current_price

    # TP sugerido por Claude (referencia; puede ser reemplazado por ATR)
    take_profit_signal = parse_price(signal.get('take_profit', ''))

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

        # SL/TP basado en ATR (se calcula con el entry_price real de la orden)
        stop_loss, take_profit = _calc_sl_tp(
            symbol, direction, entry_price, stop_pct, take_profit_signal
        )

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
            'group_name':  signal.get('group_name', 'A'),
            'strategy':    signal.get('strategy',   'TREND'),
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
    conn = sqlite3.connect(DB_PATH)
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

    SL fijo y TP fijo siempre activos.
    El trailing stop (main_async) es una capa adicional — no deshabilita el TP.
    """
    conn   = sqlite3.connect(DB_PATH)
    trades = conn.execute(
        "SELECT id, symbol, direction, entry_price, stop_loss, take_profit, quantity "
        "FROM trades WHERE status='OPEN'"
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
    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute("SELECT status, pnl_usd FROM trades").fetchall()
    open_ = conn.execute(
        "SELECT id, symbol, direction, entry_price, stop_loss, take_profit, opened_at, usd_value, quantity, COALESCE(strategy,'TREND') FROM trades WHERE status='OPEN'"
    ).fetchall()
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
                "opened_at":   t[6], "usd_value": t[7], "quantity": t[8],
                "strategy":    t[9],
            }
            for t in open_
        ],
    }
