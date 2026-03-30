# =============================================================
#  CRYPTO AGENT — BRAIN
#  Llama a Claude con el contexto de mercado y parsea la señal
# =============================================================

import re
import anthropic
from config import ANTHROPIC_API_KEY, MIN_SIGNAL_CONVICTION

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Sos un analista cuantitativo senior con 20 años de experiencia en crypto y mercados globales.
Tu función es evaluar el contexto de mercado provisto y emitir señales de trading con rigor institucional.

PROCESO OBLIGATORIO:
1. Leé el RÉGIMEN DE MERCADO (HMM 4h) — es la capa más importante del análisis
2. Evaluá el Fear & Greed Index como termómetro de sentiment general
3. Para cada activo analizá: tendencia (EMA 4h), momentum (RSI 4h), volumen relativo
4. Cruzá los activos para detectar correlaciones o divergencias

CÓMO USAR EL RÉGIMEN HMM:
- BULL_TREND: sesgo LONG. Volatilidad baja → stops estándar son eficientes.
- BEAR_TREND: volatilidad 2x normal → stops fijos se triggean por ruido. Reducir tamaño o NEUTRAL.
- SIDEWAYS: deriva bajista gradual. Sin trades de trend-following, esperar ruptura.
- REVERSAL: recuperación post-bear. LONGs tempranos posibles con RSI no sobrecomprado (< 60).
- Si el régimen lleva pocas barras (< 6), puede ser una transición — ser más conservador.

FORMATO DE RESPUESTA — siempre exactamente este bloque para cada activo:
ACTIVO: [símbolo]
DIRECCIÓN: LONG | SHORT | NEUTRAL
CONVICCIÓN: [1-10]
ENTRADA: [precio sugerido o MERCADO]
STOP-LOSS: [precio]
TAKE-PROFIT: [precio]
RATIO R/B: [x:1]
TESIS: [máximo 2 líneas: señal + qué la invalidaría + cómo el régimen la refuerza o debilita]
---

REGLAS ABSOLUTAS:
- Solo emitir LONG o SHORT si convicción >= 7
- RSI > 75 → no entrar long. RSI < 25 → no entrar short
- En BEAR_TREND: convicción mínima 8 para operar (entorno desfavorable)
- Si Fear & Greed < 20 (Extreme Fear) → solo considerar LONG de largo plazo
- Si Fear & Greed > 80 (Extreme Greed) → sesgo bajista, ser cauteloso con longs
- Ratio riesgo/beneficio mínimo 2:1 para cualquier señal no-NEUTRAL
- Respondé siempre en español, sé directo y cuantitativo"""


def analyze(market_context: str, regime_context: str = "") -> dict:
    """
    Envía el contexto a Claude y retorna:
    - raw_response: texto completo de Claude
    - signals: lista de señales parseadas por activo
    """
    full_context = f"{regime_context}\n\n{market_context}" if regime_context else market_context

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": full_context}
        ]
    )

    raw     = response.content[0].text
    signals = _parse_signals(raw)

    return {
        "raw_response":  raw,
        "signals":       signals,
        "input_tokens":  response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def _parse_signals(text: str) -> list[dict]:
    """Extrae señales estructuradas del texto de Claude."""
    # Limpiar markdown bold que Claude a veces agrega
    clean = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    
    signals = []
    blocks = re.split(r'\n---\n', clean)

    for block in blocks:
        signal = {}

        patterns = {
            "symbol":     r"ACTIVO:\s*(.+)",
            "direction":  r"DIRECCIÓN:\s*(LONG|SHORT|NEUTRAL)",
            "conviction": r"CONVICCIÓN:\s*(\d+)",
            "entry":      r"ENTRADA:\s*(.+)",
            "stop_loss":  r"STOP-LOSS:\s*(.+)",
            "take_profit":r"TAKE-PROFIT:\s*(.+)",
            "ratio":      r"RATIO R/B:\s*(.+)",
            "thesis":     r"TESIS:\s*(.+)",
        }

        for key, pattern in patterns.items():
            m = re.search(pattern, block, re.IGNORECASE)
            if m:
                signal[key] = m.group(1).strip()

        if "symbol" in signal and "direction" in signal:
            conviction = int(signal.get("conviction", 0))
            signal["conviction"] = conviction
            signal["actionable"] = (
                signal["direction"] != "NEUTRAL"
                and conviction >= MIN_SIGNAL_CONVICTION
            )
            signals.append(signal)

    return signals
