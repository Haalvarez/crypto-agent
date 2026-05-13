"""
reconcile_positions.py
──────────────────────
Reconcilia el estado entre Binance Testnet y trades.db.

Diagnostica y opcionalmente corrige 3 escenarios de desincronización:

  1. HUERFANO_BINANCE: trade marcado WIN/LOSS en DB pero el activo sigue
     en el wallet de Binance (causado por el bug histórico de
     check_open_positions que solo actualizaba la DB sin emitir sell).

  2. HUERFANO_DB: posición OPEN en DB pero sin balance en Binance
     (raro — implica cierre manual fuera del agente).

  3. EXTRA_BINANCE: balance en Binance que no corresponde a ningún
     trade en DB (depósito manual u operación externa).

Uso:
    python reconcile_positions.py --dry-run     # solo diagnostica
    python reconcile_positions.py --execute     # cierra huérfanos vendiendo
    python reconcile_positions.py --execute --confirm  # sin prompt

Salida:
    - Reporte por símbolo: qty en Binance vs qty implícita por trades OPEN
    - Lista de cierres pendientes con PnL estimado
    - Si --execute: ejecuta market sells y backfilla DB con exit_price real
"""

import sys
import sqlite3
from datetime import datetime
from collections import defaultdict

import executor as exc


def get_binance_balances() -> dict:
    """Retorna {asset: free_qty} de Binance Testnet (solo balances > 0)."""
    exchange = exc.get_exchange()
    bal      = exchange.fetch_balance()
    return {asset: float(qty) for asset, qty in bal['free'].items()
            if float(qty) > 0 and asset != 'USDT'}


def get_db_open_by_symbol() -> dict:
    """{symbol: [trades_open]} agrupado por par."""
    conn = sqlite3.connect(exc.DB_PATH)
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_price, quantity, opened_at,
                  COALESCE(strategy,'TREND')
           FROM trades WHERE status='OPEN'"""
    ).fetchall()
    conn.close()
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r[1]].append({
            'id':         r[0],
            'symbol':     r[1],
            'direction':  r[2],
            'entry_price':r[3],
            'quantity':   r[4],
            'opened_at':  r[5],
            'strategy':   r[6],
        })
    return dict(by_sym)


def get_db_closed_recent(limit: int = 50) -> list[dict]:
    """Trades cerrados en DB recientemente — para detectar huérfanos en Binance."""
    conn = sqlite3.connect(exc.DB_PATH)
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_price, quantity, exit_price,
                  pnl_usd, status, closed_at
           FROM trades WHERE status IN ('WIN','LOSS')
           ORDER BY id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [{
        'id':r[0],'symbol':r[1],'direction':r[2],'entry_price':r[3],
        'quantity':r[4],'exit_price':r[5],'pnl_usd':r[6],'status':r[7],
        'closed_at':r[8],
    } for r in rows]


def symbol_to_asset(symbol: str) -> str:
    """ETH/USDT → ETH"""
    return symbol.split('/')[0]


def diagnose() -> dict:
    """Compara Binance vs DB y retorna reporte estructurado."""
    binance       = get_binance_balances()
    db_open       = get_db_open_by_symbol()
    db_closed     = get_db_closed_recent()

    # qty esperada por Binance = suma de OPEN LONGs por activo (asumiendo spot)
    expected_qty = defaultdict(float)
    for sym, trades in db_open.items():
        asset = symbol_to_asset(sym)
        for t in trades:
            if t['direction'] == 'LONG':
                expected_qty[asset] += t['quantity']

    # Detectar huérfanos en Binance (qty real > qty esperada por OPEN trades)
    orphans = []   # trades cerrados en DB cuyo asset sigue en wallet
    for asset, real_qty in binance.items():
        exp = expected_qty.get(asset, 0.0)
        excess = real_qty - exp
        if excess <= 0:
            continue
        # Hay excedente — buscar trades cerrados de este asset que sumen el excedente
        sym_match = f"{asset}/USDT"
        # Tomar los más recientes hasta cubrir el excess (por orden de cierre desc)
        candidates = [t for t in db_closed if t['symbol'] == sym_match]
        accumulated = 0.0
        for t in candidates:
            if accumulated >= excess - 1e-8:
                break
            orphans.append(t)
            accumulated += t['quantity']

    # Detectar OPEN en DB sin balance en Binance
    db_orphans = []
    for sym, trades in db_open.items():
        asset = symbol_to_asset(sym)
        real  = binance.get(asset, 0.0)
        for t in trades:
            if t['direction'] == 'LONG' and t['quantity'] > real + 1e-8:
                db_orphans.append(t)

    # Detectar assets en Binance sin ninguna referencia en DB
    extra = []
    for asset, qty in binance.items():
        if expected_qty.get(asset, 0) == 0 and not any(
            t['symbol'] == f"{asset}/USDT" for t in db_closed
        ):
            extra.append({'asset': asset, 'qty': qty})

    return {
        'binance_balances': binance,
        'expected_qty':     dict(expected_qty),
        'orphans_binance':  orphans,    # cerrados en DB pero vivos en Binance
        'orphans_db':       db_orphans, # OPEN en DB pero sin asset en Binance
        'extra_binance':    extra,      # assets sin referencia en DB
    }


def print_report(diag: dict) -> None:
    print("=" * 70)
    print("RECONCILIACIÓN BINANCE ↔ trades.db")
    print("=" * 70)

    print("\n📊 BALANCES BINANCE TESTNET (>0):")
    for asset, qty in diag['binance_balances'].items():
        exp = diag['expected_qty'].get(asset, 0)
        delta = qty - exp
        flag = "✅" if abs(delta) < 1e-8 else "⚠️"
        print(f"  {flag} {asset:8s} real={qty:.6f}  esperado={exp:.6f}  delta={delta:+.6f}")

    print(f"\n🔴 HUÉRFANOS BINANCE ({len(diag['orphans_binance'])}) — DB dice cerrado, Binance lo tiene vivo:")
    if not diag['orphans_binance']:
        print("  (ninguno)")
    else:
        for t in diag['orphans_binance']:
            print(f"  #{t['id']:4d} {t['symbol']:10s} qty={t['quantity']:.6f}  "
                  f"entry=${t['entry_price']:.4f}  status={t['status']}  "
                  f"closed_at={t['closed_at'][:19]}")

    print(f"\n🟡 HUÉRFANOS DB ({len(diag['orphans_db'])}) — DB dice OPEN, Binance no lo tiene:")
    if not diag['orphans_db']:
        print("  (ninguno)")
    else:
        for t in diag['orphans_db']:
            print(f"  #{t['id']:4d} {t['symbol']:10s} qty={t['quantity']:.6f}  "
                  f"entry=${t['entry_price']:.4f}  opened_at={t['opened_at'][:19]}")

    print(f"\n🔵 EXTRA BINANCE ({len(diag['extra_binance'])}) — assets sin referencia en DB:")
    if not diag['extra_binance']:
        print("  (ninguno)")
    else:
        for e in diag['extra_binance']:
            print(f"  {e['asset']:8s} qty={e['qty']:.6f}")

    print("\n" + "=" * 70)


def execute_orphan_closes(orphans: list[dict], confirm: bool = False) -> None:
    """Vende los huérfanos en Binance y actualiza DB con exit_price real."""
    if not orphans:
        print("\nNo hay huérfanos Binance para cerrar.")
        return

    print(f"\n⚠️  Se van a cerrar {len(orphans)} posiciones huérfanas en Binance Testnet.")
    if not confirm:
        resp = input("Continuar? (yes/no): ").strip().lower()
        if resp not in ('yes', 'y', 'si', 's'):
            print("Cancelado.")
            return

    exchange = exc.get_exchange()
    exchange.load_markets()

    for t in orphans:
        try:
            qty = exchange.amount_to_precision(t['symbol'], t['quantity'])
            print(f"\n→ Vendiendo #{t['id']} {t['symbol']} qty={qty}...")
            order = exchange.create_order(
                symbol=t['symbol'], type='market', side='sell', amount=float(qty)
            )
            real_exit = float(order.get('average') or order.get('price') or t['exit_price'])

            # Recalcular pnl con el precio real de salida
            real_pnl = (real_exit - t['entry_price']) * t['quantity']
            real_status = 'WIN' if real_pnl >= 0 else 'LOSS'

            # Backfill en DB con exit real
            conn = sqlite3.connect(exc.DB_PATH)
            conn.execute(
                """UPDATE trades SET exit_price=?, pnl_usd=?, status=?,
                                     closed_at=?
                   WHERE id=?""",
                (real_exit, round(real_pnl, 4), real_status,
                 datetime.now().isoformat(), t['id'])
            )
            conn.commit()
            conn.close()

            exc.log_event("RECONCILE_CLOSE",
                          f"#{t['id']} {t['symbol']} cerrado por reconciliación",
                          symbol=t['symbol'], level="WARNING",
                          details={'old_status': t['status'], 'new_status': real_status,
                                   'old_pnl': t['pnl_usd'], 'new_pnl': round(real_pnl, 4),
                                   'real_exit': real_exit, 'old_exit': t['exit_price']})

            print(f"  ✅ {real_status} | exit real ${real_exit:.4f} | "
                  f"PnL real ${real_pnl:+.2f} (DB tenía ${t['pnl_usd']:+.2f})")

        except Exception as e:
            print(f"  ❌ Error vendiendo #{t['id']} {t['symbol']}: {e}")


def main():
    args = sys.argv[1:]
    dry_run = '--dry-run' in args or '--execute' not in args
    confirm = '--confirm' in args

    diag = diagnose()
    print_report(diag)

    if dry_run:
        print("\n💡 Modo dry-run — nada modificado. Para ejecutar:")
        print("   python reconcile_positions.py --execute")
    else:
        execute_orphan_closes(diag['orphans_binance'], confirm=confirm)
        if diag['orphans_db']:
            print(f"\n⚠️  Hay {len(diag['orphans_db'])} trades OPEN en DB sin balance en Binance.")
            print("   Estos podrían cerrarse manualmente como LOSS, pero requiere revisión.")
            print("   No se tocaron automáticamente.")


if __name__ == '__main__':
    main()
