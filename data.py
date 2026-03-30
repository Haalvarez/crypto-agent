# =============================================================
#  CRYPTO AGENT — DATA
#  Obtiene precios e indicadores desde Binance public API
#  (sin key, sin rate limit estricto)
# =============================================================

import requests
import pandas as pd
from datetime import datetime


def get_prices_and_indicators(symbols: list[str]) -> dict:
    results = {}

    for symbol in symbols:
        binance_symbol = symbol.replace("/", "")  # BTC/USDT → BTCUSDT
        try:
            # Precio actual + cambio 24h
            ticker_url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={binance_symbol}"
            tr = requests.get(ticker_url, timeout=10).json()
            price      = float(tr["lastPrice"])
            change_24h = float(tr["priceChangePercent"])

            # Velas 4h — últimas 100 para EMA50, RSI14 y volumen
            klines_url = (
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={binance_symbol}&interval=4h&limit=100"
            )
            klines = requests.get(klines_url, timeout=10).json()
            closes  = pd.Series([float(k[4]) for k in klines])
            volumes = pd.Series([float(k[5]) for k in klines])

            rsi        = _calc_rsi(closes, period=14)
            ema20      = closes.ewm(span=20).mean().iloc[-1]
            ema50      = closes.ewm(span=50).mean().iloc[-1]
            trend      = "ALCISTA" if ema20 > ema50 else "BAJISTA"
            vol_ratio  = round(float(volumes.iloc[-1] / volumes.rolling(20).mean().iloc[-1]), 2)

            # Cambio de la última vela 4h
            change_4h  = round((closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100, 2)

            results[symbol] = {
                "price":      round(price, 2),
                "change_24h": round(change_24h, 2),
                "change_4h":  change_4h,
                "rsi":        round(rsi, 1),
                "ema20":      round(ema20, 2),
                "ema50":      round(ema50, 2),
                "trend":      trend,
                "vol_ratio":  vol_ratio,
            }
            print(f"  [data] {symbol}: ${price:,.2f} | RSI {rsi:.1f} | {trend} | vol {vol_ratio}x")

        except Exception as e:
            print(f"  [data] ERROR {symbol}: {e}")
            results[symbol] = {"error": str(e)}

    return results


def get_fear_and_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        print(f"  [data] Fear&Greed ERROR: {e}")
        return {"value": 50, "label": "Neutral"}


def _calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def format_market_context(market_data: dict, fng: dict) -> str:
    lines = [
        f"=== CONTEXTO DE MERCADO — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===",
        f"Fear & Greed Index: {fng['value']}/100 ({fng['label']})",
        "",
    ]
    for symbol, d in market_data.items():
        if "error" in d:
            lines.append(f"{symbol}: ERROR — {d['error']}")
            continue
        lines += [
            f"--- {symbol} ---",
            f"  Precio:       ${d['price']:,.2f}",
            f"  Cambio 4h:    {d['change_4h']:+.2f}%",
            f"  Cambio 24h:   {d['change_24h']:+.2f}%",
            f"  RSI (14):     {d['rsi']}",
            f"  EMA20/50:     {d['ema20']} / {d['ema50']}  →  Tendencia {d['trend']}",
            f"  Volumen 4h:   {d['vol_ratio']}x promedio",
            "",
        ]
    return "\n".join(lines)
