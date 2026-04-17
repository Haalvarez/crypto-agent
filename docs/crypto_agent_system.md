# Crypto Agent â€” DocumentaciÃ³n Completa del Sistema

## 1. VisiÃ³n General

Crypto Agent es un sistema de trading algorÃ­tmico autÃ³nomo que opera criptomonedas en Binance. Combina anÃ¡lisis tÃ©cnico mecÃ¡nico, clasificaciÃ³n de rÃ©gimen de mercado con Machine Learning (Hidden Markov Model), y validaciÃ³n de seÃ±ales con inteligencia artificial (Claude de Anthropic).

El sistema corre 24/7 en Railway (cloud), monitorea precios cada 15 minutos, analiza oportunidades cada 4 horas, y ejecuta Ã³rdenes automÃ¡ticamente cuando todas las condiciones se cumplen.

### Stack tecnolÃ³gico
- **Lenguaje**: Python 3.11+
- **Exchange**: Binance (Testnet o Mainnet) via librerÃ­a `ccxt`
- **IA**: Anthropic Claude (Sonnet para anÃ¡lisis, Haiku para vetos rÃ¡pidos)
- **ML**: GaussianHMM de `hmmlearn` para clasificaciÃ³n de rÃ©gimen
- **Base de datos**: SQLite (`trades.db`) para trades y eventos
- **Datos de mercado**: Binance Public API (sin autenticaciÃ³n) + WebSocket en tiempo real
- **Alertas**: Telegram Bot API
- **Dashboard**: HTML/JS servido por HTTPServer embebido
- **Deploy**: Railway con volumen persistente en `/data`

### Pares monitoreados
- **Grupo A** (permanentes): BTC/USDT, ETH/USDT, SOL/USDT â€” tienen modelo HMM entrenado
- **Grupo B** (dinÃ¡micos): top movers diarios con >5% cambio y >$50M volumen â€” sin modelo HMM, analizados por Claude con criterios de momentum

---

## 2. Arquitectura de Archivos

```
crypto-agent/
â”œâ”€â”€ main.py                  # Ciclo principal â€” orquesta todo
â”œâ”€â”€ main_async.py            # Motor async paralelo (WebSocket + TrailingStop)
â”œâ”€â”€ config.py                # ConfiguraciÃ³n: keys, sÃ­mbolos, parÃ¡metros de riesgo
â”œâ”€â”€ data.py                  # Fetch de datos de mercado + indicadores tÃ©cnicos
â”œâ”€â”€ regime.py                # Clasificador HMM de rÃ©gimen de mercado
â”œâ”€â”€ brain.py                 # Interfaz con Claude (prompts, parseo de seÃ±ales)
â”œâ”€â”€ executor.py              # EjecuciÃ³n de Ã³rdenes + gestiÃ³n de DB
â”œâ”€â”€ telegram_alerts.py       # Notificaciones formateadas a Telegram
â”œâ”€â”€ server.py                # Servidor HTTP standalone (legacy)
â”œâ”€â”€ core/
â”‚   â””â”€â”€ binance_ws.py        # WebSocket client para precios en tiempo real
â”œâ”€â”€ strategies/
â”‚   â””â”€â”€ trailing_stop.py     # TrailingStop ATR(14). SL/TP inicial: 4h, trailing: 1h
â”œâ”€â”€ persistence/
â”‚   â””â”€â”€ db_manager.py        # Migraciones y helpers async para SQLite
â”œâ”€â”€ integrations/
â”‚   â””â”€â”€ claude_client_async.py  # Cliente Claude async (futuro)
â”œâ”€â”€ backtest/
â”‚   â”œâ”€â”€ downloader.py        # Descarga datos histÃ³ricos de Binance
â”‚   â”œâ”€â”€ regime_trainer.py    # Entrena el modelo HMM
â”‚   â”œâ”€â”€ simulator.py         # Simulador de estrategias
â”‚   â”œâ”€â”€ optimizer.py         # Grid search con walk-forward
â”‚   â””â”€â”€ report.py            # Generador de reportes HTML
â”œâ”€â”€ models/
â”‚   â””â”€â”€ hmm_*.pkl            # Modelos HMM serializados (uno por par)
â”œâ”€â”€ dashboard.html           # Dashboard PWA principal
â”œâ”€â”€ timeline.html            # Vista cronolÃ³gica de eventos
â”œâ”€â”€ system_explainer.html    # DocumentaciÃ³n visual del sistema
â””â”€â”€ trades.db                # Base de datos SQLite (en /data en Railway)
```

---

## 3. Ciclo de Vida Diario Completo

### 3.1 Arranque del sistema (`main.py â†’ main()`)

Al iniciar, el agente ejecuta esta secuencia:

1. **Inicia el servidor HTTP** (`APIHandler` en el puerto configurado, default 8080)
   - Sirve dashboard.html, timeline.html y los archivos estÃ¡ticos
   - Expone la API REST: `/api/events` (GET) y `/api/close` (POST)
   - Railway necesita que el puerto estÃ© escuchando para marcar el proceso como healthy

2. **Inicia el motor async** (TrailingEngine en hilo separado)
   - Corre `main_async.py` en un thread daemon
   - Conecta WebSocket a Binance para recibir precios en tiempo real
   - Carga posiciones abiertas de la DB y restaura sus trailing stops
   - Suscribe a los streams de todos los sÃ­mbolos monitoreados

3. **Inicializa la base de datos** (`executor.init_db()`)
   - Crea las tablas `trades` y `events` si no existen
   - Migra esquema si es necesario (agrega columnas nuevas)

4. **Registra evento STARTUP** en la tabla `events`
   - Incluye sÃ­mbolos monitoreados, estado del testnet, intervalos configurados

5. **EnvÃ­a notificaciÃ³n de arranque por Telegram**
   - Confirma pares monitoreados, intervalo de monitoreo y de anÃ¡lisis

6. **Entra al loop principal** â€” corre `run_cycle()` cada 15 minutos indefinidamente

### 3.2 Cada ciclo de monitoreo (cada 15 minutos)

El ciclo de monitoreo (`run_cycle()`) ejecuta 10 pasos en secuencia:

#### Paso 1 â€” Scan de Grupo B (una vez por dÃ­a)
- `_scan_group_b()` consulta `GET /api/v3/ticker/24hr` de Binance (todos los pares)
- Filtra: pares USDT, excluye stablecoins y wrapped tokens, excluye Grupo A
- Selecciona los 2 con mayor |cambio_24h| que superen 5% y $50M de volumen
- Si encuentra movers, registra evento `MOVER_DETECTED` por cada uno
- Se ejecuta solo una vez por dÃ­a (guarda fecha del Ãºltimo scan)

#### Paso 2 â€” Datos de mercado
- `data.get_prices_and_indicators()` descarga para cada sÃ­mbolo (Grupo A + B):
  - Precio actual y cambio 24h via `GET /api/v3/ticker/24hr`
  - 100 velas de 4h via `GET /api/v3/klines?interval=4h&limit=100`
- Calcula indicadores tÃ©cnicos:
  - **RSI(14)**: Relative Strength Index con Wilder smoothing
  - **EMA20 y EMA50**: medias mÃ³viles exponenciales â†’ tendencia ALCISTA/BAJISTA
  - **vol_ratio**: volumen actual / promedio rolling de 20 perÃ­odos
  - **change_4h**: cambio porcentual de la Ãºltima vela de 4h
  - **ema_cross_up/down**: cruce de EMA20 sobre EMA50 en las Ãºltimas 4 velas
  - **rsi_recovery**: RSI estuvo <35 en Ãºltimas 6 velas y ahora >40
  - **rsi_rejection**: RSI estuvo >65 en Ãºltimas 6 velas y ahora <60
- `data.get_fear_and_greed()` consulta alternative.me â†’ Ã­ndice 0-100 de sentimiento

#### Paso 3 â€” Monitor de posiciones abiertas
- `executor.check_open_positions()` revisa cada trade OPEN contra el precio actual:
  - **LONG**: si precio â‰¤ stop_loss â†’ LOSS; si precio â‰¥ take_profit â†’ WIN
  - **SHORT**: si precio â‰¥ stop_loss â†’ LOSS; si precio â‰¤ take_profit â†’ WIN
- Los trades cerrados se registran como evento `TRADE_CLOSE`
- Se notifica por Telegram cada cierre
- Si el resultado es LOSS, se suma al acumulador de pÃ©rdida diaria
- Si la pÃ©rdida diaria supera MAX_DAILY_LOSS_USD ($30), el agente se **detiene** hasta el dÃ­a siguiente (evento `DAILY_HALT`)

#### Paso 4 â€” ClasificaciÃ³n de rÃ©gimen HMM
- `regime.classify_all()` para cada par del Grupo A:
  1. Carga el modelo HMM pre-entrenado desde `models/hmm_{SYMBOL}.pkl` (cache en memoria)
  2. Descarga 200 velas 4h de Binance
  3. Calcula 5 features: log_return, volatilidad rolling 20, vol_ratio, RSI centrado, pendiente EMA50
  4. Normaliza con el StandardScaler del entrenamiento
  5. `model.predict()` vÃ­a algoritmo de Viterbi â†’ secuencia de estados
  6. El estado de la Ãºltima barra es el rÃ©gimen actual
- Resultado por par: rÃ©gimen actual, barras consecutivas, horas en rÃ©gimen, rÃ©gimen anterior, probabilidad de persistencia, volatilidad del estado
- **Detecta cambios de rÃ©gimen**: si el rÃ©gimen cambiÃ³ respecto al ciclo anterior, registra evento `REGIME_CHANGE`

#### Paso 5 â€” Salida por cambio de rÃ©gimen (sin Claude)
- Para cada posiciÃ³n abierta, verifica si el rÃ©gimen es incompatible:
  - **LONG abierto + rÃ©gimen BEAR_TREND** â†’ cierre inmediato al mercado
  - **SHORT abierto + rÃ©gimen BULL_TREND** â†’ cierre inmediato al mercado
  - Otros cambios (SIDEWAYS, REVERSAL) â†’ no se fuerza salida
- Registra evento `REGIME_EXIT` con PnL

#### Paso 6a â€” AnÃ¡lisis Grupo A (filtro mecÃ¡nico + veto Claude)

Solo se analiza un par si:
- No tiene posiciÃ³n abierta
- Pasaron â‰¥ 4 horas (ANALYSIS_INTERVAL_MINUTES) desde el Ãºltimo anÃ¡lisis de ese par

Para cada par que califica:

**Filtro mecÃ¡nico** (`data.check_entry_conditions()`):
1. **RÃ©gimen operable**: solo BULL_TREND (â†’ LONG) o BEAR_TREND (â†’ SHORT). SIDEWAYS y REVERSAL bloquean la entrada.
2. **Tipo de seÃ±al**: detecta si hay una seÃ±al fuerte:
   - `EMA_CROSS`: EMA20 cruzÃ³ EMA50 en las Ãºltimas 4 velas
   - `RSI_RECOVERY`: RSI saliÃ³ de oversold (<35) a >40
   - `RSI_REJECTION`: RSI saliÃ³ de overbought (>65) a <60
   - `ALIGNMENT`: alineaciÃ³n simple de indicadores (sin seÃ±al fuerte)
3. **EMA alineada**: EMA20 > EMA50 para LONG, EMA20 < EMA50 para SHORT (excepto EMA_CROSS que es el cruce mismo)
4. **RSI en zona neutral**: rangos dinÃ¡micos segÃºn tipo de seÃ±al:
   - LONG normal: RSI 42-65 / con EMA_CROSS: 42-72 / con RSI_RECOVERY: 35-65
   - SHORT normal: RSI 35-58 / con EMA_CROSS: 28-58 / con RSI_REJECTION: 35-65
5. **Volumen mÃ­nimo**: â‰¥1.3x promedio (â‰¥1.2x si hay seÃ±al fuerte)

Si el filtro mecÃ¡nico rechaza â†’ registra evento `ENTRY_CHECK` y pasa al siguiente par.

**Veto Claude Haiku** (`brain.analyze_veto()`):
- Si el filtro mecÃ¡nico aprueba, Claude Haiku (rÃ¡pido y barato) hace un chequeo de Ãºltima instancia
- Solo puede vetar por razones objetivas: evento macro inminente, divergencia RSI obvia, BTC en caÃ­da libre, sobreextensiÃ³n extrema
- Si veta â†’ registra evento `CLAUDE_VETO`
- Si aprueba â†’ la seÃ±al pasa a ejecuciÃ³n con convicciÃ³n fija de 9/10

#### Paso 6b â€” AnÃ¡lisis Grupo B (Claude Sonnet)
- Para movers del Grupo B sin posiciÃ³n abierta y con anÃ¡lisis pendiente
- MÃ¡ximo de posiciones Grupo B: 1 (configurable)
- Claude Sonnet analiza con prompt especializado en momentum:
  - Criterios mÃ¡s estrictos: stop 3%, target mÃ­nimo 15% (ratio 5:1)
  - RSI >72 o <28 â†’ NEUTRAL obligatorio
- Las seÃ±ales se suman al pipeline de ejecuciÃ³n

#### Paso 7 â€” EjecuciÃ³n de seÃ±ales
Para cada seÃ±al accionable:

1. **Filtro Fear & Greed**:
   - LONG + F&G > 80 (Extreme Greed) â†’ bloqueado
   - SHORT + F&G < 20 (Extreme Fear) â†’ bloqueado
2. **CÃ¡lculo de SL/TP** (`executor._calc_sl_tp()`):
   - Descarga velas 1h y calcula ATR(14)
   - SL = entrada Â± ATR Ã— 1.5 (multiplicador configurable)
   - TP = entrada Â± ATR Ã— 1.5 Ã— 2 (ratio R:R 1:2) o el sugerido por Claude si es vÃ¡lido
   - Fallback: SL 4% fijo si ATR no disponible
3. **Orden de mercado** via `ccxt`:
   - Calcula quantity = MAX_TRADE_USD / precio
   - `exchange.create_order(type='market', side='buy'/'sell')`
4. **Persistencia**: guarda trade en SQLite con entry, SL, TP, cantidad, order_id
5. **Notificaciones**: envÃ­a confirmaciÃ³n por Telegram
6. **Evento**: registra `TRADE_OPEN`

#### Paso 8 â€” Actualizar estadÃ­sticas por par
- Contadores: consultas, accionables, descartadas
- Ãšltimo seÃ±al, convicciÃ³n, rÃ©gimen, precio, RSI, tendencia

#### Paso 9 â€” Dashboard state
- Escribe `dashboard_state.json` con todo el estado actual
- Si hay GitHub Gist configurado, sube el JSON al gist (para acceso remoto)

#### Paso 10 â€” Resumen Telegram
- Cada 3 ciclos de anÃ¡lisis, si hubo seÃ±ales o trades cerrados:
  - Fear & Greed, rÃ©gimen de cada par, seÃ±ales accionables/neutrales, balance USDT
  - Se envÃ­a como notificaciÃ³n silenciosa

#### Dormir y repetir
- El agente espera MONITOR_INTERVAL_MINUTES (15 min) y repite el ciclo

---

## 4. Los 4 RegÃ­menes de Mercado (HMM)

El modelo GaussianHMM con 4 estados identifica automÃ¡ticamente patrones estadÃ­sticos en las velas de 4h. Los estados se etiquetan post-hoc analizando las caracterÃ­sticas estadÃ­sticas de cada cluster:

### BULL_TREND (ðŸ“ˆ)
- **DescripciÃ³n**: Tendencia alcista sostenida con baja volatilidad (~0.70%/4h)
- **Sesgo del agente**: Favorable para LONG
- **Persistencia tÃ­pica**: ~95% (se mantiene muchas barras)
- **El agente**: busca entradas LONG si los indicadores confirman

### BEAR_TREND (ðŸ“‰)
- **DescripciÃ³n**: CaÃ­da pronunciada con alta volatilidad (~1.50%/4h, 2Ã— normal)
- **Sesgo del agente**: Muy volÃ¡til, stops fijos se triggean por ruido
- **Regla especial**: convicciÃ³n mÃ­nima 8 para operar (vs 7 normal)
- **El agente**: puede buscar SHORTs pero con mayor cautela; cierra LONGs abiertos automÃ¡ticamente

### SIDEWAYS (âž¡ï¸)
- **DescripciÃ³n**: Declive gradual con la menor volatilidad de todos los estados (~0.66%/4h)
- **Sesgo del agente**: Sin tendencia clara, deriva bajista suave
- **El agente**: NO entra nuevas posiciones (filtro mecÃ¡nico bloquea)

### REVERSAL (ðŸ”„)
- **DescripciÃ³n**: RecuperaciÃ³n post-bear con volatilidad media (~1.12%/4h)
- **Sesgo del agente**: Posibles LONGs tempranos, retorno medio positivo
- **El agente**: NO entra nuevas posiciones (filtro mecÃ¡nico bloquea) â€” es un estado de transiciÃ³n

### Entrenamiento del HMM
- `backtest/regime_trainer.py` entrena sobre 2 aÃ±os de datos histÃ³ricos de velas 4h
- 5 features: log_return, volatilidad rolling 20, ratio de volumen, RSI centrado, pendiente EMA50
- 10 reinicios aleatorios, selecciona el modelo con mejor log-likelihood
- Etiquetado de estados: examina media de log_return y volatilidad de cada cluster
- Modelo serializado con `joblib` en `models/hmm_{SYMBOL}.pkl`

---

## 5. Motor Async Paralelo (TrailingStop en Tiempo Real)

### Arquitectura de dos hilos
`main.py` ejecuta dos componentes en paralelo:
1. **Hilo principal**: ciclo de monitoreo cada 15 min (anÃ¡lisis, ejecuciÃ³n)
2. **Hilo daemon** (TrailingEngine): WebSocket + trailing stop en tiempo real

### Flujo del TrailingEngine (`main_async.py`)
1. Migra esquema DB (agrega columnas `trailing_stop_price`, `atr_value`)
2. Carga posiciones abiertas y restaura stops desde DB
3. Suscribe WebSocket a todos los sÃ­mbolos monitoreados + sÃ­mbolos con posiciÃ³n abierta
4. Por cada tick de precio recibido:
   - Si el trade no tiene stop inicializado â†’ calcula ATR(14) de velas 1h (trailing) â†’ stop = entry Â± ATR Ã— 1.5
   - Si ya tiene stop â†’ actualiza trailing:
     - **LONG**: si precio > peak â†’ nuevo peak, nuevo stop = peak - ATR Ã— 1.5 (sube con el precio, nunca baja)
     - **SHORT**: si precio < peak â†’ nuevo peak, nuevo stop = peak + ATR Ã— 1.5 (baja con el precio, nunca sube)
   - Si precio toca el stop â†’ cierra la posiciÃ³n al mercado

### WebSocket (`core/binance_ws.py`)
- ConexiÃ³n a `wss://stream.binance.com:9443/stream`
- SuscripciÃ³n a `{symbol}@miniTicker` para cada par (precio, high, low 24h)
- ReconexiÃ³n automÃ¡tica con backoff exponencial (2s â†’ 4s â†’ 8s â†’ ... â†’ mÃ¡x 60s)

---

## 6. Reglas de Riesgo (hardcodeadas en config.py)

| ParÃ¡metro | Valor | DescripciÃ³n |
|---|---|---|
| MAX_TRADE_USD | $50 | Monto mÃ¡ximo por operaciÃ³n |
| STOP_LOSS_PCT | 4% | Fallback si ATR no disponible |
| MAX_DAILY_LOSS_USD | $30 | LÃ­mite de pÃ©rdida diaria (~3 stops) |
| MAX_OPEN_POSITIONS | 2 | MÃ¡ximo de posiciones simultÃ¡neas |
| MIN_SIGNAL_CONVICTION | 8 | ConvicciÃ³n mÃ­nima Claude para ejecutar |
| STOP_LOSS_PCT_B | 3% | Stop loss para Grupo B |
| TAKE_PROFIT_PCT_B | 15% | Take profit para Grupo B |

### Protecciones adicionales
- **Daily halt**: si la pÃ©rdida del dÃ­a supera $30, el agente se detiene completamente hasta el dÃ­a siguiente
- **Filtro Fear & Greed**: no abre LONG en Extreme Greed (>80), no abre SHORT en Extreme Fear (<20)
- **Un par, una posiciÃ³n**: no abre una segunda posiciÃ³n en el mismo par
- **RÃ©gimen incompatible**: cierra posiciones automÃ¡ticamente si el rÃ©gimen cambia contra la direcciÃ³n

---

## 7. Ãrbol de Decisiones Completo

### Â¿Debo analizar este par?
```
Par disponible
â”œâ”€â”€ Â¿Tiene posiciÃ³n abierta? â†’ SÃ â†’ SKIP (no analizar)
â”œâ”€â”€ Â¿Pasaron 4h desde Ãºltimo anÃ¡lisis? â†’ NO â†’ SKIP
â””â”€â”€ SÃ â†’ Continuar al filtro mecÃ¡nico
```

### Filtro mecÃ¡nico (sin IA)
```
Datos de mercado OK
â”œâ”€â”€ RÃ©gimen BULL_TREND â†’ direcciÃ³n LONG
â”œâ”€â”€ RÃ©gimen BEAR_TREND â†’ direcciÃ³n SHORT
â”œâ”€â”€ RÃ©gimen SIDEWAYS/REVERSAL/UNKNOWN â†’ BLOQUEADO
â”‚
â”œâ”€â”€ Â¿SeÃ±al fuerte? (EMA_CROSS / RSI_RECOVERY / RSI_REJECTION)
â”‚   â”œâ”€â”€ SÃ â†’ rangos RSI mÃ¡s amplios, volumen mÃ­n 1.2x
â”‚   â””â”€â”€ NO â†’ tipo ALIGNMENT, rangos estrictos, volumen mÃ­n 1.3x
â”‚
â”œâ”€â”€ Â¿EMA alineada con direcciÃ³n?
â”‚   â”œâ”€â”€ SÃ â†’ continuar
â”‚   â”œâ”€â”€ NO + tipo EMA_CROSS â†’ OK (el cruce ES la alineaciÃ³n)
â”‚   â””â”€â”€ NO + otro tipo â†’ BLOQUEADO
â”‚
â”œâ”€â”€ Â¿RSI en zona vÃ¡lida?
â”‚   â”œâ”€â”€ SÃ â†’ continuar
â”‚   â””â”€â”€ NO â†’ BLOQUEADO (sobrecomprado/sobrevendido/dÃ©bil)
â”‚
â””â”€â”€ Â¿Volumen suficiente?
    â”œâ”€â”€ SÃ â†’ CALIFICADO â†’ pasa al veto Claude
    â””â”€â”€ NO â†’ BLOQUEADO
```

### Veto Claude (Haiku)
```
SeÃ±al mecÃ¡nicamente calificada
â”œâ”€â”€ Â¿Evento macro inminente? â†’ VETO
â”œâ”€â”€ Â¿Divergencia RSI obvia? â†’ VETO
â”œâ”€â”€ Â¿BTC en caÃ­da libre (altcoin)? â†’ VETO
â”œâ”€â”€ Â¿SobreextensiÃ³n extrema? â†’ VETO
â””â”€â”€ Todo OK â†’ APROBADO (convicciÃ³n 9/10)
```

### Pre-ejecuciÃ³n
```
SeÃ±al aprobada
â”œâ”€â”€ Â¿Fear & Greed > 80 + LONG? â†’ BLOQUEADO
â”œâ”€â”€ Â¿Fear & Greed < 20 + SHORT? â†’ BLOQUEADO
â”œâ”€â”€ Â¿Agente halted (pÃ©rdida diaria)? â†’ BLOQUEADO
â”œâ”€â”€ Â¿Ya hay MAX_OPEN_POSITIONS? â†’ BLOQUEADO
â””â”€â”€ Todo OK â†’ EJECUTAR
```

### EjecuciÃ³n
```
Ejecutar seÃ±al
â”œâ”€â”€ Calcular ATR(14) de velas 4h (SL/TP inicial)
â”‚   â”œâ”€â”€ ATR disponible â†’ SL = entry Â± ATRÃ—1.5, TP = entry Â± ATRÃ—3
â”‚   â””â”€â”€ ATR no disponible â†’ SL = entry Â± 4%, TP = entry Â± 8%
â”œâ”€â”€ quantity = $50 / precio
â”œâ”€â”€ Orden de mercado en Binance (buy para LONG, sell para SHORT)
â”œâ”€â”€ Guardar en SQLite
â”œâ”€â”€ Inicializar trailing stop en motor async
â””â”€â”€ Notificar por Telegram
```

### Cierre de posiciones
```
PosiciÃ³n abierta
â”œâ”€â”€ Monitor cada 15 min:
â”‚   â”œâ”€â”€ Precio â‰¤ SL â†’ LOSS (cierre)
â”‚   â””â”€â”€ Precio â‰¥ TP â†’ WIN (cierre)
â”œâ”€â”€ Trailing Stop en tiempo real (WebSocket):
â”‚   â”œâ”€â”€ Precio sube â†’ stop sube con Ã©l (ratchet)
â”‚   â””â”€â”€ Precio toca trailing stop â†’ cierre
â”œâ”€â”€ Cambio de rÃ©gimen:
â”‚   â”œâ”€â”€ LONG + rÃ©gimen â†’ BEAR â†’ cierre inmediato
â”‚   â””â”€â”€ SHORT + rÃ©gimen â†’ BULL â†’ cierre inmediato
â”œâ”€â”€ Cierre manual (dashboard):
â”‚   â””â”€â”€ POST /api/close con trade_id + token
â””â”€â”€ Registrar resultado + PnL + notificar Telegram
```

---

## 8. API y Dashboard

### Endpoints

| MÃ©todo | Path | DescripciÃ³n |
|---|---|---|
| GET | `/` | Redirige a dashboard.html |
| GET | `/dashboard_state.json` | Estado completo del agente (JSON) |
| GET | `/api/events` | Eventos con paginaciÃ³n y filtros |
| POST | `/api/close` | Cierre manual de posiciÃ³n (requiere token) |
| GET | `/timeline.html` | Vista cronolÃ³gica de eventos |
| GET | `/system_explainer.html` | DocumentaciÃ³n visual |

### ParÃ¡metros de /api/events
- `limit` (default 100, max 500)
- `offset` (paginaciÃ³n)
- `type` (filtro: TRADE_OPEN, TRADE_CLOSE, REGIME_CHANGE, etc.)
- `symbol` (filtro por par)

### Tipos de eventos registrados
| Tipo | DescripciÃ³n |
|---|---|
| STARTUP | Agente iniciado |
| TRADE_OPEN | PosiciÃ³n abierta |
| TRADE_CLOSE | PosiciÃ³n cerrada (SL/TP/trailing/manual/rÃ©gimen) |
| REGIME_CHANGE | Cambio de rÃ©gimen HMM |
| REGIME_EXIT | PosiciÃ³n cerrada por rÃ©gimen incompatible |
| CLAUDE_SIGNAL | SeÃ±al aprobada por Claude |
| CLAUDE_VETO | SeÃ±al vetada por Claude Haiku |
| ENTRY_CHECK | Par evaluado pero no calificado |
| MOVER_DETECTED | Top mover del Grupo B detectado |
| GROUP_B_SCAN | Scan diario sin movers calificados |
| DAILY_HALT | LÃ­mite de pÃ©rdida diaria alcanzado |
| DAILY_RESET | Reset de pÃ©rdida diaria (nuevo dÃ­a) |
| MANUAL_CLOSE | PosiciÃ³n cerrada manualmente desde dashboard |

---

## 9. Notificaciones Telegram

El agente envÃ­a estos tipos de mensajes:

1. **Startup** â€” al arrancar: pares monitoreados, intervalos
2. **SeÃ±al accionable** â€” direcciÃ³n, convicciÃ³n, entrada, SL, TP, tesis
3. **ConfirmaciÃ³n de ejecuciÃ³n** â€” order ID, cantidad, precio real de entrada
4. **Trade cerrado** â€” resultado (WIN/LOSS), entrada, salida, PnL en USD
5. **Resumen de ciclo** â€” cada 3 ciclos de anÃ¡lisis: F&G, rÃ©gimenes, balance
6. **Error** â€” cualquier excepciÃ³n no capturada
7. **Daily halt** â€” cuando se alcanza el lÃ­mite de pÃ©rdida diaria

---

## 10. Sistema de Backtest

### Componentes offline (directorio `backtest/`)

1. **downloader.py** â€” Descarga 2 aÃ±os de velas 1h y 4h de Binance para los 4 pares base (730 dÃ­as Ã— 4 pares). Guarda en CSV.

2. **regime_trainer.py** â€” Entrena el modelo GaussianHMM:
   - 5 features sobre velas 4h
   - n_components=4 (4 estados)
   - 10 reinicios aleatorios, selecciona mejor log-likelihood
   - Etiqueta estados post-hoc: analiza media de retorno y volatilidad
   - Serializa con joblib: modelo, scaler, labels

3. **simulator.py** â€” Simula la estrategia EMA/RSI sobre datos histÃ³ricos:
   - Reporta: Sharpe ratio, win rate, drawdown mÃ¡ximo, PnL acumulado
   - Usa las mismas reglas de entrada que el agente en producciÃ³n

4. **optimizer.py** â€” Grid search sobre parÃ¡metros:
   - ValidaciÃ³n walk-forward 80/20 (entrena en 80%, valida en 20%)
   - Optimiza: perÃ­odos EMA, rangos RSI, multiplicador ATR, umbrales de volumen

5. **report.py** â€” Genera reportes HTML con grÃ¡ficos y mÃ©tricas visuales

---

## 11. Flujo Temporal de un DÃ­a TÃ­pico

```
00:00 â€” Reset diario (daily_loss = 0, halted = false)
00:00 â€” Ciclo #1: monitoreo de posiciones, check SL/TP
00:05 â€” Scan Grupo B: busca top movers en Binance
00:05 â€” Si hay movers calificados: registra MOVER_DETECTED
00:15 â€” Ciclo #2: monitoreo
00:30 â€” Ciclo #3: monitoreo
...
04:00 â€” Ciclo con anÃ¡lisis (4h desde arranque):
         - RÃ©gimen HMM actualizado
         - Filtro mecÃ¡nico evalÃºa cada par libre
         - Si califica â†’ veto Claude Haiku
         - Si aprobado â†’ ejecuciÃ³n en Binance
         - Notificaciones Telegram
04:15 â€” Ciclo monitoreo: revisa posiciones abiertas
...
08:00 â€” Segundo anÃ¡lisis del dÃ­a
...
12:00 â€” Tercer anÃ¡lisis
...
(mientras tanto, WebSocket + TrailingStop corren en paralelo 24/7)
(cada 15 min se verifican SL/TP estÃ¡ticos)
(en tiempo real, el trailing stop ajusta el stop dinÃ¡micamente)
```

---

## 12. Modelo de Datos (SQLite)

### Tabla `trades`
| Columna | Tipo | DescripciÃ³n |
|---|---|---|
| id | INTEGER PK | ID autoincremental |
| symbol | TEXT | Par (ej: BTC/USDT) |
| direction | TEXT | LONG o SHORT |
| conviction | INTEGER | ConvicciÃ³n 1-10 |
| entry_price | REAL | Precio de entrada |
| stop_loss | REAL | Stop loss estÃ¡tico |
| take_profit | REAL | Take profit |
| quantity | REAL | Cantidad del activo |
| usd_value | REAL | Valor en USD al abrir |
| order_id | TEXT | ID de la orden en Binance |
| status | TEXT | OPEN, WIN, LOSS |
| exit_price | REAL | Precio de salida |
| pnl_usd | REAL | Ganancia/pÃ©rdida en USD |
| opened_at | TEXT | ISO timestamp apertura |
| closed_at | TEXT | ISO timestamp cierre |
| group_name | TEXT | A o B |
| trailing_stop_price | REAL | Stop dinÃ¡mico actual |
| atr_value | REAL | ATR(14) calculado al abrir |

### Tabla `events`
| Columna | Tipo | DescripciÃ³n |
|---|---|---|
| id | INTEGER PK | ID autoincremental |
| timestamp | TEXT | ISO timestamp UTC |
| type | TEXT | Tipo de evento |
| symbol | TEXT | Par (puede ser NULL) |
| group_name | TEXT | A, B, o NULL |
| level | TEXT | INFO, WARNING, SUCCESS, ERROR |
| title | TEXT | TÃ­tulo corto del evento |
| details | TEXT | JSON con datos adicionales |

---

## 13. Prompts de Claude

### System Prompt â€” AnÃ¡lisis principal (claude-sonnet-4)
Claude actÃºa como analista cuantitativo senior. Proceso obligatorio:
1. Leer rÃ©gimen HMM (capa mÃ¡s importante)
2. Evaluar Fear & Greed como termÃ³metro de sentimiento
3. Para cada activo: tendencia EMA 4h, momentum RSI 4h, volumen relativo
4. Cruzar activos para detectar correlaciones

Reglas absolutas: convicciÃ³n â‰¥ 7 para operar, RSI > 75 no entrar long, RSI < 25 no entrar short, en BEAR_TREND convicciÃ³n mÃ­nima 8, ratio R:R mÃ­nimo 2:1.

### System Prompt â€” Grupo B (claude-sonnet-4)
Especializado en momentum de alta volatilidad. EvalÃºa si el movimiento tiene continuaciÃ³n o es agotamiento. Criterios mÃ¡s estrictos: stop 3%, target 15%, ratio 4:1 mÃ­nimo.

### System Prompt â€” Veto (claude-haiku-4)
Risk manager. Solo veta por: evento macro inminente, divergencia RSI, BTC en caÃ­da libre, sobreextensiÃ³n extrema. En caso de duda, NO vetea.

---

## 14. ConfiguraciÃ³n de Variables de Entorno

```
# Binance
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BINANCE_TESTNET=true/false

# Anthropic
ANTHROPIC_API_KEY=...

# Telegram
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...

# GitHub Gist (dashboard remoto)
GITHUB_GIST_TOKEN=...
GITHUB_GIST_ID=...

# Riesgo
MAX_TRADE_USD=50
MAX_DAILY_LOSS_USD=30

# Intervalos
MONITOR_INTERVAL_MINUTES=15
ANALYSIS_INTERVAL_MINUTES=240

# Grupo B
GROUP_B_ENABLED=true
GROUP_B_MAX_POSITIONS=1
GROUP_B_TOP_MOVERS=2
GROUP_B_MIN_CHANGE_PCT=5.0
GROUP_B_MIN_VOLUME_USD=50000000

# API
AGENT_API_TOKEN=...
PORT=8080

# Async
ASYNC_ENABLED=false
```
