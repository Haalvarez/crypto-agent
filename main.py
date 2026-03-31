# =============================================================
#  CRYPTO AGENT — MAIN v3 (régimen HMM + monitor posiciones + dashboard)
# =============================================================

import json
import os
import time
import logging
from datetime import datetime

import config
import data as market_data_module
import brain
import regime as regime_module
import telegram_alerts as tg
import executor as exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "dashboard_state.json")

state = {
    "daily_loss_usd":  0.0,
    "halted":          False,
    "cycles_run":      0,
    "analysis_cycles": 0,
    "last_reset_date": datetime.now().date(),
    # Cuándo fue el último análisis Claude por par
    "last_analysis":   {},   # {symbol: datetime}
    # Contadores por par — persisten mientras el proceso corre
    "pair_stats":      {sym: {"queries": 0, "actionable": 0, "discarded": 0, "last_signal": None,
                              "last_conviction": 0, "last_regime": None, "last_price": 0,
                              "last_rsi": 0, "last_trend": None}
                        for sym in config.SYMBOLS},
}


def needs_analysis(symbol: str) -> bool:
    """True si pasaron ANALYSIS_INTERVAL_MINUTES desde el último análisis Claude de este par."""
    last = state["last_analysis"].get(symbol)
    if last is None:
        return True
    return (datetime.now() - last).total_seconds() >= config.ANALYSIS_INTERVAL_MINUTES * 60


# ── Helpers ───────────────────────────────────────────────────

def reset_daily_state_if_needed():
    today = datetime.now().date()
    if today != state["last_reset_date"]:
        log.info("Nuevo día — reseteando pérdida diaria.")
        state["daily_loss_usd"]  = 0.0
        state["halted"]          = False
        state["last_reset_date"] = today


def _check_regime_exits(regimes: dict, mkt: dict) -> list[dict]:
    """
    Cierra posiciones al mercado si el régimen cambió a uno incompatible con la dirección.

    Lógica:
      - LONG abierto + régimen ahora es BEAR_TREND → salir (la tesis alcista ya no aplica)
      - SHORT abierto + régimen ahora es BULL_TREND → salir
      - Otros cambios de régimen: no forzar salida (SIDEWAYS/REVERSAL son ambiguos)
    """
    closed = []
    for sym in config.SYMBOLS:
        trade = exc.get_open_position(sym)
        if not trade:
            continue

        regime_info = regimes.get(sym, {})
        if not regime_info.get("available"):
            continue

        regime    = regime_info.get("regime")
        price     = mkt.get(sym, {}).get("price", 0)
        direction = trade["direction"]

        should_exit = (
            (direction == "LONG"  and regime == "BEAR_TREND") or
            (direction == "SHORT" and regime == "BULL_TREND")
        )

        if should_exit:
            log.info(f"  [regime exit] {sym}: {direction} cerrado — régimen cambió a {regime}")
            ct = exc.market_close_trade(trade, price, f"cambio de régimen a {regime}")
            closed.append(ct)

    return closed


def _log_regimes(regimes: dict) -> None:
    for sym, info in regimes.items():
        if info.get("available"):
            log.info(
                f"  [régimen] {sym}: {info['regime']} "
                f"({info['bars_in_regime']} barras · {info['hours_in_regime']}h)"
            )


def _update_pair_stats(signals: list[dict], mkt: dict, regimes: dict) -> None:
    """Actualiza contadores por par en el state en memoria."""
    sig_map = {s["symbol"]: s for s in signals}

    for sym in config.SYMBOLS:
        ps = state["pair_stats"].setdefault(sym, {
            "queries": 0, "actionable": 0, "discarded": 0,
            "last_signal": None, "last_conviction": 0,
            "last_regime": None, "last_price": 0,
            "last_rsi": 0, "last_trend": None,
        })
        ps["queries"] += 1

        sig = sig_map.get(sym)
        if sig:
            if sig.get("actionable"):
                ps["actionable"] += 1
            else:
                ps["discarded"] += 1
            ps["last_signal"]     = sig.get("direction")
            ps["last_conviction"] = sig.get("conviction", 0)

        d = mkt.get(sym, {})
        if not d.get("error"):
            ps["last_price"] = d.get("price", 0)
            ps["last_rsi"]   = d.get("rsi", 0)
            ps["last_trend"] = d.get("trend")

        r = regimes.get(sym, {})
        if r.get("available"):
            ps["last_regime"]       = r.get("regime")
            ps["last_regime_hours"] = r.get("hours_in_regime", 0)


def _upload_to_gist(content: str) -> None:
    """Sube el dashboard_state.json a GitHub Gist si están configurados token y gist_id."""
    token    = config.GITHUB_GIST_TOKEN
    gist_id  = config.GITHUB_GIST_ID
    if not token or not gist_id:
        return
    try:
        import requests as req
        r = req.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
            json={"files": {"dashboard_state.json": {"content": content}}},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"[gist] Upload falló: {r.status_code}")
    except Exception as e:
        log.warning(f"[gist] Error: {e}")


def write_dashboard_state(mkt: dict, fng: dict, regimes: dict,
                          signals: list[dict], balance: float) -> None:
    """Escribe dashboard_state.json — leído por el dashboard PWA."""
    trade_stats = exc.get_all_trades_stats()

    # Enriquecer open_trades con PnL flotante actual
    for t in trade_stats["open_trades"]:
        price = mkt.get(t["symbol"], {}).get("price", 0)
        if price and t["entry_price"]:
            if t["direction"] == "LONG":
                t["pnl_pct"] = round((price - t["entry_price"]) / t["entry_price"] * 100, 2)
            else:
                t["pnl_pct"] = round((t["entry_price"] - price) / t["entry_price"] * 100, 2)
            t["current_price"] = price
        else:
            t["pnl_pct"] = 0

    payload = {
        "last_update":      datetime.now().isoformat(),
        "cycle":            state["cycles_run"],
        "monitor_interval_minutes":  config.MONITOR_INTERVAL_MINUTES,
        "analysis_interval_minutes": config.ANALYSIS_INTERVAL_MINUTES,
        "halted":           state["halted"],
        "fear_greed":       fng,
        "balance_usdt":     balance,
        "pair_stats":       state["pair_stats"],
        "open_trades":      trade_stats["open_trades"],
        "trade_summary": {
            "total_closed": trade_stats["total_closed"],
            "wins":         trade_stats["wins"],
            "losses":       trade_stats["losses"],
            "open_count":   trade_stats["open_count"],
            "win_rate":     trade_stats["win_rate"],
            "total_pnl":    trade_stats["total_pnl"],
        },
        "regimes": {
            sym: {
                "regime":         info.get("regime", "UNKNOWN"),
                "hours":          info.get("hours_in_regime", 0),
                "persist_prob":   info.get("persist_prob", 0),
                "prev_regime":    info.get("prev_regime"),
                "available":      info.get("available", False),
            }
            for sym, info in regimes.items()
        },
    }

    content = json.dumps(payload, indent=2, default=str)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    _upload_to_gist(content)


# ── Ciclo principal ───────────────────────────────────────────

def run_cycle():
    cycle_num = state["cycles_run"] + 1
    log.info(f"── Ciclo #{cycle_num} iniciando ──")

    # 1. Datos de mercado
    try:
        mkt          = market_data_module.get_prices_and_indicators(config.SYMBOLS)
        fng          = market_data_module.get_fear_and_greed()
        context_text = market_data_module.format_market_context(mkt, fng)
        log.info("Datos de mercado OK")
    except Exception as e:
        log.error(f"Error datos: {e}")
        tg.send_error("fetch datos", str(e))
        return

    # 2. Monitor posiciones abiertas — cerrar las que tocaron stop/target
    try:
        closed_trades = exc.check_open_positions(mkt)
        for ct in closed_trades:
            tg.send_trade_closed(ct)
            # Trade cerrado → resetear last_analysis para que el par se analice en el próximo ciclo
            state["last_analysis"].pop(ct["symbol"], None)
            if ct["result"] == "LOSS":
                state["daily_loss_usd"] += abs(ct["pnl_usd"])
                if state["daily_loss_usd"] >= config.MAX_DAILY_LOSS_USD:
                    state["halted"] = True
                    tg.send_daily_limit_hit(state["daily_loss_usd"])
                    log.warning("Límite de pérdida diaria alcanzado — agente detenido.")
    except Exception as e:
        log.error(f"Error monitor posiciones: {e}")

    # 3. Régimen HMM
    try:
        regimes        = regime_module.classify_all(config.SYMBOLS)
        regime_context = regime_module.format_regime_context(regimes)
        _log_regimes(regimes)
    except Exception as e:
        log.warning(f"Régimen no disponible: {e}")
        regimes        = {}
        regime_context = ""

    # 4. Salida por cambio de régimen (sin Claude)
    try:
        regime_closed = _check_regime_exits(regimes, mkt)
        for ct in regime_closed:
            tg.send_trade_closed(ct)
            state["last_analysis"].pop(ct["symbol"], None)
            closed_trades.append(ct)
            if ct["result"] == "LOSS":
                state["daily_loss_usd"] += abs(ct["pnl_usd"])
                if state["daily_loss_usd"] >= config.MAX_DAILY_LOSS_USD:
                    state["halted"] = True
                    tg.send_daily_limit_hit(state["daily_loss_usd"])
    except Exception as e:
        log.error(f"Error regime exits: {e}")

    # 5. Análisis Claude — solo pares SIN posición abierta Y con análisis vencido (>4h)
    free_symbols    = [s for s in config.SYMBOLS if not exc.has_open_position(s)]
    blocked_symbols = [s for s in config.SYMBOLS if exc.has_open_position(s)]
    due_symbols     = [s for s in free_symbols if needs_analysis(s)]

    if blocked_symbols:
        log.info(f"  Skip Claude (posición abierta): {', '.join(blocked_symbols)}")
    not_due = [s for s in free_symbols if s not in due_symbols]
    if not_due:
        mins_each = {s: int((datetime.now() - state["last_analysis"][s]).total_seconds() / 60)
                     for s in not_due}
        log.info(f"  Skip Claude (análisis reciente): " +
                 ", ".join(f"{s} ({config.ANALYSIS_INTERVAL_MINUTES - m}min restantes)"
                           for s, m in mins_each.items()))

    signals = []
    tokens  = 0

    if due_symbols and not state["halted"]:
        try:
            due_mkt     = {s: mkt[s] for s in due_symbols if s in mkt}
            due_context = market_data_module.format_market_context(due_mkt, fng)
            due_regime  = regime_module.format_regime_context(
                {s: regimes[s] for s in due_symbols if s in regimes}
            )
            result  = brain.analyze(due_context, due_regime)
            signals = result["signals"]
            tokens  = result["input_tokens"] + result["output_tokens"]
            log.info(f"Claude OK — {len(signals)} señales — {tokens} tokens ({len(due_symbols)} pares)")
            # Registrar timestamp de análisis para cada par consultado
            for s in due_symbols:
                state["last_analysis"][s] = datetime.now()
            state["analysis_cycles"] += 1
        except Exception as e:
            log.error(f"Error brain: {e}")
            tg.send_error("análisis Claude", str(e))
            return
    elif not free_symbols:
        log.info("Todos los pares tienen posición abierta — skip Claude este ciclo")
    elif not due_symbols:
        log.info("Sin pares vencidos para análisis — ciclo de monitoreo")

    # 6. Ejecutar señales accionables
    for signal in signals:
        if signal.get("actionable") and not state["halted"]:
            log.info(f"Ejecutando: {signal['symbol']} {signal['direction']}")
            res = exc.execute_signal(signal, mkt)
            if res:
                tg.send_execution_confirmation(res)
            else:
                log.warning(f"No ejecutado: {signal['symbol']}")

    # Notificar solo si hay algo accionable — silencio si todo es NEUTRAL
    actionable = [s for s in signals if s.get("actionable")]
    if actionable:
        for sig in actionable:
            tg.send_signal(sig, mkt)

    # 6. Actualizar contadores y estado
    _update_pair_stats(signals, mkt, regimes)

    # 7. Dashboard state
    balance = exc.get_balance_usdt()
    write_dashboard_state(mkt, fng, regimes, signals, balance)

    # 8. Resumen Telegram cada 3 ciclos de análisis (≈12h) o si hubo cierres
    if state["analysis_cycles"] % 3 == 0 and signals or closed_trades:
        tg.send_cycle_summary(signals, fng, tokens, balance, regimes)

    state["cycles_run"] += 1
    log.info(f"── Ciclo #{cycle_num} completado ──\n")


def main():
    log.info("=== CRYPTO AGENT v3 ARRANCANDO ===")
    exc.init_db()
    tg.send_startup()

    while True:
        reset_daily_state_if_needed()
        if state["halted"]:
            log.info("Agente detenido por límite diario. Esperando reseteo.")
            time.sleep(3600)
            continue
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Detenido manualmente.")
            tg.send("🛑 Agente detenido manualmente.")
            break
        except Exception as e:
            log.error(f"Error loop: {e}")
            tg.send_error("loop principal", str(e))

        log.info(f"Próximo ciclo en {config.MONITOR_INTERVAL_MINUTES} min...")
        time.sleep(config.MONITOR_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
