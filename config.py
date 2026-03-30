# =============================================================
#  CRYPTO AGENT — CONFIG
#  Las secrets vienen de variables de entorno (.env local o Railway vars)
# =============================================================

import os
from dotenv import load_dotenv

load_dotenv()  # carga .env si existe (local); en Railway usa env vars directas

# --- Binance ---
BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_API_SECRET = os.environ["BINANCE_API_SECRET"]
BINANCE_TESTNET    = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

# --- Anthropic ---
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]

# --- Telegram ---
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- GitHub Gist (dashboard remoto) ---
GITHUB_GIST_TOKEN = os.getenv("GITHUB_GIST_TOKEN", "")
GITHUB_GIST_ID    = os.getenv("GITHUB_GIST_ID", "")

# --- Pares que el agente monitorea ---
SYMBOLS = ["ETH/USDT", "SOL/USDT"]

# --- Reglas de riesgo (hardcoded, el agente no las puede cambiar) ---
MAX_TRADE_USD         = 10     # máximo por operación en USD
STOP_LOSS_PCT         = 0.04   # 4%
MAX_DAILY_LOSS_USD    = 15     # pérdida diaria máxima antes de detenerse
MAX_OPEN_POSITIONS    = 2      # máximo de posiciones abiertas simultáneas
MIN_SIGNAL_CONVICTION = 7      # convicción mínima para ejecutar

# --- Intervalo de análisis ---
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "15"))
# TEST=15, producción=240
