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
    "last_reset_date": datetime.now().date(),
    # Contadores por par — persisten mientras el proceso corre
    "pair_stats":      {sym: {"queries": 0, "actionable": 0, "discarded": 0, "last_signal": None,
                              "last_conviction": 0, "last_regime": None, "last_price": 0,
                              "last_rsi": 0, "last_trend": None}
                        for sym in config.SYMBOLS},
}


# ── Helpers ───────────────────────────────────────────────────

def reset_daily_state_if_needed():
    today = datetime.now().date()
    if today != state["last_reset_date"]:
        log.info("Nuevo día — reseteando pérdida diaria.")
        state["daily_loss_usd"]  = 0.0
        state["halted"]          = False
        state["last_reset_date"] = today


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
        "interval_minutes": config.INTERVAL_MINUTES,
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

    # 4. Análisis Claude
    try:
        result  = brain.analyze(context_text, regime_context)
        signals = result["signals"]
        tokens  = result["input_tokens"] + result["output_tokens"]
        log.info(f"Claude OK — {len(signals)} señales — {tokens} tokens")
    except Exception as e:
        log.error(f"Error brain: {e}")
        tg.send_error("análisis Claude", str(e))
        return

    # 5. Ejecutar señales accionables
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

    # 8. Resumen Telegram silencioso solo cada 3 ciclos (o si hay algo que reportar)
    if cycle_num % 3 == 0 or closed_trades:
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

        log.info(f"Próximo ciclo en {config.INTERVAL_MINUTES} min...")
        time.sleep(config.INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
