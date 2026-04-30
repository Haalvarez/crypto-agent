"""
strategies/grid_trader.py
─────────────────────────
Grid Trading mean-reversion (Fase 1 — single-position).

Lógica:
  1. Calcula N niveles geométricos en el rango percentil 10-90 de los últimos
     LOOKBACK_DAYS (velas 4h).
  2. Cuando el precio toca un nivel inferior y NO hay posición grid abierta,
     emite una señal LONG con TP en el siguiente nivel y SL en el piso del rango.
  3. La posición se gestiona con check_open_positions() como cualquier otra:
     cierra al tocar TP (gana ~grid_step) o SL (pérdida limitada).

No predice tendencia — explota el rango. Funciona mejor en mercados laterales.
Si BTC rompe el rango definitivamente, el SL limita la pérdida y los próximos
recálculos ajustan los niveles al nuevo régimen.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


class GridTrader:
    """
    Genera señales de entrada Grid sobre un símbolo.
    Mantiene los niveles en memoria y los recalcula cada 24 horas.
    """

    def __init__(self, symbol: str, n_levels: int = 8,
                 lookback_days: int = 30, grid_step_pct: float = 0.025):
        self.symbol         = symbol
        self.n_levels       = n_levels
        self.lookback_days  = lookback_days
        self.grid_step_pct  = grid_step_pct
        self.levels: list[float] = []
        self.range_low:  float   = 0.0
        self.range_high: float   = 0.0
        self.last_recalc: Optional[datetime] = None

    # ── API pública ──────────────────────────────────────────

    def calculate_levels(self) -> list[float]:
        """Recalcula niveles geométricos sobre el rango percentil 10-90."""
        binance_sym = self.symbol.replace("/", "")
        try:
            klines = requests.get(BINANCE_KLINES, params={
                "symbol":   binance_sym,
                "interval": "4h",
                "limit":    self.lookback_days * 6,   # 4h × 6 = 24h
            }, timeout=10).json()
        except Exception as e:
            log.warning(f"[GRID] {self.symbol} fetch error: {e}")
            return self.levels

        if not klines or isinstance(klines, dict):
            log.warning(f"[GRID] {self.symbol} sin klines")
            return self.levels

        lows  = sorted(float(k[3]) for k in klines)
        highs = sorted(float(k[2]) for k in klines)
        floor   = lows[int(len(lows)   * 0.10)]
        ceiling = highs[int(len(highs) * 0.90)]

        median  = (floor * ceiling) ** 0.5  # geométrico
        half    = self.n_levels // 2

        levels = []
        for i in range(-half, half + 1):
            level = median * (1 + self.grid_step_pct) ** i
            if floor <= level <= ceiling:
                levels.append(round(level, 2))

        levels.sort()
        self.levels      = levels
        self.range_low   = round(floor,   2)
        self.range_high  = round(ceiling, 2)
        self.last_recalc = datetime.now()
        log.info(f"[GRID] {self.symbol} | rango ${floor:,.0f}–${ceiling:,.0f} | {len(levels)} niveles | step {self.grid_step_pct:.1%}")
        return levels

    def get_buy_signal(self, current_price: float,
                       open_grid_trades: list[dict]) -> Optional[dict]:
        """
        Si el precio está cerca de un nivel inferior y no hay grid posicionado
        en ese nivel, retorna un dict de señal listo para execute_signal().
        Retorna None si no hay setup válido.
        """
        # Recalcular niveles cada 24h (o si nunca se calcularon)
        if not self.levels or not self.last_recalc or \
           (datetime.now() - self.last_recalc) > timedelta(hours=24):
            self.calculate_levels()

        if not self.levels:
            return None

        # Validar que el precio esté DENTRO del rango (si rompió, no operar)
        if current_price < self.range_low or current_price > self.range_high:
            log.debug(f"[GRID] {self.symbol} ${current_price:,.2f} fuera de rango")
            return None

        # Niveles ya ocupados por trades grid abiertos (tolerancia 1%)
        occupied_prices = [t['entry_price'] for t in open_grid_trades]

        # Buscar nivel inferior cercano al precio actual (tolerancia 0.5%)
        for i, level in enumerate(self.levels):
            if i >= len(self.levels) - 1:
                continue   # último nivel no tiene siguiente para TP

            tolerance = 0.005   # 0.5% de proximidad al nivel
            within = abs(current_price - level) / level < tolerance
            if not within:
                continue

            already_open = any(
                abs(level - ep) / ep < 0.01 for ep in occupied_prices
            )
            if already_open:
                continue

            next_level = self.levels[i + 1]
            sl_price   = round(self.range_low * 0.97, 2)   # 3% bajo el piso del rango

            return {
                "symbol":            self.symbol,
                "direction":         "LONG",
                "conviction":        9,
                "actionable":        True,
                "thesis":            f"Grid level ${level:,.0f} → TP ${next_level:,.0f} (step {self.grid_step_pct:.1%})",
                "group":             "A",
                "group_name":        "A",
                "strategy":          "GRID",
                "stop_loss_price":   sl_price,
                "take_profit_price": next_level,
                "grid_level":        level,
                "grid_next":         next_level,
                "take_profit":       "",   # vacío para que parse_price no lo use
                "stop_loss":         "",
            }

        return None

    def status(self) -> dict:
        """Estado del grid para el dashboard."""
        return {
            "symbol":      self.symbol,
            "range_low":   self.range_low,
            "range_high":  self.range_high,
            "levels":      self.levels,
            "last_recalc": self.last_recalc.isoformat() if self.last_recalc else None,
            "step_pct":    self.grid_step_pct,
        }
