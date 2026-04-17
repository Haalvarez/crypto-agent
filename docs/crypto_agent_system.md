# Crypto Agent — Documentación Completa del Sistema

## 1. Visión General

Crypto Agent es un sistema de trading algorítmico autónomo que opera criptomonedas en Binance. Combina análisis técnico mecánico, clasificación de régimen de mercado con Machine Learning (Hidden Markov Model), y validación de señales con inteligencia artificial (Claude de Anthropic).

El sistema corre 24/7 en Railway (cloud), monitorea precios cada 15 minutos, analiza oportunidades cada 4 horas, y ejecuta órdenes automáticamente cuando todas las condiciones se cumplen.

### Stack tecnológico
- **Lenguaje**: Python 3.11+
- **Exchange**: Binance (Testnet o Mainnet) via librería `ccxt`
- **IA**: Anthropic Claude (Sonnet para análisis, Haiku para vetos rápidos)
- **ML**: GaussianHMM de `hmmlearn` para clasificación de régimen
- **Base de datos**: SQLite (`trades.db`) para trades y eventos
- **Datos de mercado**: Binance Public API (sin autenticación) + WebSocket en tiempo real
- **Alertas**: Telegram Bot API
- **Dashboard**: HTML/JS servido por HTTPServer embebido
- **Deploy**: Railway con volumen persistente en `/data`

### Pares monitoreados
- **Grupo A** (permanentes): BTC/USDT, ETH/USDT, SOL/USDT — tienen modelo HMM entrenado
- **Grupo B** (dinámicos): top movers diarios con >5% cambio y >$50M volumen — sin modelo HMM, analizados por Claude con criterios de momentum

---

## 2. Arquitectura de Archivos

```
crypto-agent/
├── main.py                  # Ciclo principal — orquesta todo
├── main_async.py            # Motor async paralelo (WebSocket + TrailingStop)
├── config.py                # Configuración: keys, símbolos, parámetros de riesgo
├── data.py                  # Fetch de datos de mercado + indicadores técnicos
├── regime.py                # Clasificador HMM de régimen de mercado
├── brain.py                 # Interfaz con Claude (prompts, parseo de señales)
├── executor.py              # Ejecución de órdenes + gestión de DB
├── telegram_alerts.py       # Notificaciones formateadas a Telegram
├── server.py                # Servidor HTTP standalone (legacy)
├── core/
│   └── binance_ws.py        # WebSocket client para precios en tiempo real
├── strategies/
│   └── trailing_stop.py     # TrailingStop basado en ATR(14) de velas 1h
├── persistence/
│   └── db_manager.py        # Migraciones y helpers async para SQLite
├── integrations/
│   └── claude_client_async.py  # Cliente Claude async (futuro)
├── backtest/
│   ├── downloader.py        # Descarga datos históricos de Binance
│   ├── regime_trainer.py    # Entrena el modelo HMM
│   ├── simulator.py         # Simulador de estrategias
│   ├── optimizer.py         # Grid search con walk-forward
│   └── report.py            # Generador de reportes HTML
├── models/
│   └── hmm_*.pkl            # Modelos HMM serializados (uno por par)
├── dashboard.html           # Dashboard PWA principal
├── timeline.html            # Vista cronológica de eventos
├── system_explainer.html    # Documentación visual del sistema
└── trades.db                # Base de datos SQLite (en /data en Railway)
```

---

## 3. Ciclo de Vida Diario Completo

### 3.1 Arranque del sistema (`main.py → main()`)

Al iniciar, el agente ejecuta esta secuencia:

1. **Inicia el servidor HTTP** (`APIHandler` en el puerto configurado, default 8080)
   - Sirve dashboard.html, timeline.html y los archivos estáticos
   - Expone la API REST: `/api/events` (GET) y `/api/close` (POST)
   - Railway necesita que el puerto esté escuchando para marcar el proceso como healthy

2. **Inicia el motor async** (TrailingEngine en hilo separado)
   - Corre `main_async.py` en un thread daemon
   - Conecta WebSocket a Binance para recibir precios en tiempo real
   - Carga posiciones abiertas de la DB y restaura sus trailing stops
   - Suscribe a los streams de todos los símbolos monitoreados

3. **Inicializa la base de datos** (`executor.init_db()`)
   - Crea las tablas `trades` y `events` si no existen
   - Migra esquema si es necesario (agrega columnas nuevas)

4. **Registra evento STARTUP** en la tabla `events`
   - Incluye símbolos monitoreados, estado del testnet, intervalos configurados

5. **Envía notificación de arranque por Telegram**
   - Confirma pares monitoreados, intervalo de monitoreo y de análisis

6. **Entra al loop principal** — corre `run_cycle()` cada 15 minutos indefinidamente

### 3.2 Cada ciclo de monitoreo (cada 15 minutos)

El ciclo de monitoreo (`run_cycle()`) ejecuta 10 pasos en secuencia:

#### Paso 1 — Scan de Grupo B (una vez por día)
- `_scan_group_b()` consulta `GET /api/v3/ticker/24hr` de Binance (todos los pares)
- Filtra: pares USDT, excluye stablecoins y wrapped tokens, excluye Grupo A
- Selecciona los 2 con mayor |cambio_24h| que superen 5% y $50M de volumen
- Si encuentra movers, registra evento `MOVER_DETECTED` por cada uno
- Se ejecuta solo una vez por día (guarda fecha del último scan)

#### Paso 2 — Datos de mercado
- `data.get_prices_and_indicators()` descarga para cada símbolo (Grupo A + B):
  - Precio actual y cambio 24h via `GET /api/v3/ticker/24hr`
  - 100 velas de 4h via `GET /api/v3/klines?interval=4h&limit=100`
- Calcula indicadores técnicos:
  - **RSI(14)**: Relative Strength Index con Wilder smoothing
  - **EMA20 y EMA50**: medias móviles exponenciales → tendencia ALCISTA/BAJISTA
  - **vol_ratio**: volumen actual / promedio rolling de 20 períodos
  - **change_4h**: cambio porcentual de la última vela de 4h
  - **ema_cross_up/down**: cruce de EMA20 sobre EMA50 en las últimas 4 velas
  - **rsi_recovery**: RSI estuvo <35 en últimas 6 velas y ahora >40
  - **rsi_rejection**: RSI estuvo >65 en últimas 6 velas y ahora <60
- `data.get_fear_and_greed()` consulta alternative.me → índice 0-100 de sentimiento

#### Paso 3 — Monitor de posiciones abiertas
- `executor.check_open_positions()` revisa cada trade OPEN contra el precio actual:
  - **LONG**: si precio ≤ stop_loss → LOSS; si precio ≥ take_profit → WIN
  - **SHORT**: si precio ≥ stop_loss → LOSS; si precio ≤ take_profit → WIN
- Los trades cerrados se registran como evento `TRADE_CLOSE`
- Se notifica por Telegram cada cierre
- Si el resultado es LOSS, se suma al acumulador de pérdida diaria
- Si la pérdida diaria supera MAX_DAILY_LOSS_USD ($30), el agente se **detiene** hasta el día siguiente (evento `DAILY_HALT`)

#### Paso 4 — Clasificación de régimen HMM
- `regime.classify_all()` para cada par del Grupo A:
  1. Carga el modelo HMM pre-entrenado desde `models/hmm_{SYMBOL}.pkl` (cache en memoria)
  2. Descarga 200 velas 4h de Binance
  3. Calcula 5 features: log_return, volatilidad rolling 20, vol_ratio, RSI centrado, pendiente EMA50
  4. Normaliza con el StandardScaler del entrenamiento
  5. `model.predict()` vía algoritmo de Viterbi → secuencia de estados
  6. El estado de la última barra es el régimen actual
- Resultado por par: régimen actual, barras consecutivas, horas en régimen, régimen anterior, probabilidad de persistencia, volatilidad del estado
- **Detecta cambios de régimen**: si el régimen cambió respecto al ciclo anterior, registra evento `REGIME_CHANGE`

#### Paso 5 — Salida por cambio de régimen (sin Claude)
- Para cada posición abierta, verifica si el régimen es incompatible:
  - **LONG abierto + régimen BEAR_TREND** → cierre inmediato al mercado
  - **SHORT abierto + régimen BULL_TREND** → cierre inmediato al mercado
  - Otros cambios (SIDEWAYS, REVERSAL) → no se fuerza salida
- Registra evento `REGIME_EXIT` con PnL

#### Paso 6a — Análisis Grupo A (filtro mecánico + veto Claude)

Solo se analiza un par si:
- No tiene posición abierta
- Pasaron ≥ 4 horas (ANALYSIS_INTERVAL_MINUTES) desde el último análisis de ese par

Para cada par que califica:

**Filtro mecánico** (`data.check_entry_conditions()`):
1. **Régimen operable**: solo BULL_TREND (→ LONG) o BEAR_TREND (→ SHORT). SIDEWAYS y REVERSAL bloquean la entrada.
2. **Tipo de señal**: detecta si hay una señal fuerte:
   - `EMA_CROSS`: EMA20 cruzó EMA50 en las últimas 4 velas
   - `RSI_RECOVERY`: RSI salió de oversold (<35) a >40
   - `RSI_REJECTION`: RSI salió de overbought (>65) a <60
   - `ALIGNMENT`: alineación simple de indicadores (sin señal fuerte)
3. **EMA alineada**: EMA20 > EMA50 para LONG, EMA20 < EMA50 para SHORT (excepto EMA_CROSS que es el cruce mismo)
4. **RSI en zona neutral**: rangos dinámicos según tipo de señal:
   - LONG normal: RSI 42-65 / con EMA_CROSS: 42-72 / con RSI_RECOVERY: 35-65
   - SHORT normal: RSI 35-58 / con EMA_CROSS: 28-58 / con RSI_REJECTION: 35-65
5. **Volumen mínimo**: ≥1.3x promedio (≥1.2x si hay señal fuerte)

Si el filtro mecánico rechaza → registra evento `ENTRY_CHECK` y pasa al siguiente par.

**Veto Claude Haiku** (`brain.analyze_veto()`):
- Si el filtro mecánico aprueba, Claude Haiku (rápido y barato) hace un chequeo de última instancia
- Solo puede vetar por razones objetivas: evento macro inminente, divergencia RSI obvia, BTC en caída libre, sobreextensión extrema
- Si veta → registra evento `CLAUDE_VETO`
- Si aprueba → la señal pasa a ejecución con convicción fija de 9/10

#### Paso 6b — Análisis Grupo B (Claude Sonnet)
- Para movers del Grupo B sin posición abierta y con análisis pendiente
- Máximo de posiciones Grupo B: 1 (configurable)
- Claude Sonnet analiza con prompt especializado en momentum:
  - Criterios más estrictos: stop 3%, target mínimo 15% (ratio 5:1)
  - RSI >72 o <28 → NEUTRAL obligatorio
- Las señales se suman al pipeline de ejecución

#### Paso 7 — Ejecución de señales
Para cada señal accionable:

1. **Filtro Fear & Greed**:
   - LONG + F&G > 80 (Extreme Greed) → bloqueado
   - SHORT + F&G < 20 (Extreme Fear) → bloqueado
2. **Cálculo de SL/TP** (`executor._calc_sl_tp()`):
   - Descarga velas 1h y calcula ATR(14)
   - SL = entrada ± ATR × 1.5 (multiplicador configurable)
   - TP = entrada ± ATR × 1.5 × 2 (ratio R:R 1:2) o el sugerido por Claude si es válido
   - Fallback: SL 4% fijo si ATR no disponible
3. **Orden de mercado** via `ccxt`:
   - Calcula quantity = MAX_TRADE_USD / precio
   - `exchange.create_order(type='market', side='buy'/'sell')`
4. **Persistencia**: guarda trade en SQLite con entry, SL, TP, cantidad, order_id
5. **Notificaciones**: envía confirmación por Telegram
6. **Evento**: registra `TRADE_OPEN`

#### Paso 8 — Actualizar estadísticas por par
- Contadores: consultas, accionables, descartadas
- Último señal, convicción, régimen, precio, RSI, tendencia

#### Paso 9 — Dashboard state
- Escribe `dashboard_state.json` con todo el estado actual
- Si hay GitHub Gist configurado, sube el JSON al gist (para acceso remoto)

#### Paso 10 — Resumen Telegram
- Cada 3 ciclos de análisis, si hubo señales o trades cerrados:
  - Fear & Greed, régimen de cada par, señales accionables/neutrales, balance USDT
  - Se envía como notificación silenciosa

#### Dormir y repetir
- El agente espera MONITOR_INTERVAL_MINUTES (15 min) y repite el ciclo

---

## 4. Los 4 Regímenes de Mercado (HMM)

El modelo GaussianHMM con 4 estados identifica automáticamente patrones estadísticos en las velas de 4h. Los estados se etiquetan post-hoc analizando las características estadísticas de cada cluster:

### BULL_TREND (📈)
- **Descripción**: Tendencia alcista sostenida con baja volatilidad (~0.70%/4h)
- **Sesgo del agente**: Favorable para LONG
- **Persistencia típica**: ~95% (se mantiene muchas barras)
- **El agente**: busca entradas LONG si los indicadores confirman

### BEAR_TREND (📉)
- **Descripción**: Caída pronunciada con alta volatilidad (~1.50%/4h, 2× normal)
- **Sesgo del agente**: Muy volátil, stops fijos se triggean por ruido
- **Regla especial**: convicción mínima 8 para operar (vs 7 normal)
- **El agente**: puede buscar SHORTs pero con mayor cautela; cierra LONGs abiertos automáticamente

### SIDEWAYS (➡️)
- **Descripción**: Declive gradual con la menor volatilidad de todos los estados (~0.66%/4h)
- **Sesgo del agente**: Sin tendencia clara, deriva bajista suave
- **El agente**: NO entra nuevas posiciones (filtro mecánico bloquea)

### REVERSAL (🔄)
- **Descripción**: Recuperación post-bear con volatilidad media (~1.12%/4h)
- **Sesgo del agente**: Posibles LONGs tempranos, retorno medio positivo
- **El agente**: NO entra nuevas posiciones (filtro mecánico bloquea) — es un estado de transición

### Entrenamiento del HMM
- `backtest/regime_trainer.py` entrena sobre 2 años de datos históricos de velas 4h
- 5 features: log_return, volatilidad rolling 20, ratio de volumen, RSI centrado, pendiente EMA50
- 10 reinicios aleatorios, selecciona el modelo con mejor log-likelihood
- Etiquetado de estados: examina media de log_return y volatilidad de cada cluster
- Modelo serializado con `joblib` en `models/hmm_{SYMBOL}.pkl`

---

## 5. Motor Async Paralelo (TrailingStop en Tiempo Real)

### Arquitectura de dos hilos
`main.py` ejecuta dos componentes en paralelo:
1. **Hilo principal**: ciclo de monitoreo cada 15 min (análisis, ejecución)
2. **Hilo daemon** (TrailingEngine): WebSocket + trailing stop en tiempo real

### Flujo del TrailingEngine (`main_async.py`)
1. Migra esquema DB (agrega columnas `trailing_stop_price`, `atr_value`)
2. Carga posiciones abiertas y restaura stops desde DB
3. Suscribe WebSocket a todos los símbolos monitoreados + símbolos con posición abierta
4. Por cada tick de precio recibido:
   - Si el trade no tiene stop inicializado → calcula ATR(14) de velas 1h → stop = entry ± ATR × 1.5
   - Si ya tiene stop → actualiza trailing:
     - **LONG**: si precio > peak → nuevo peak, nuevo stop = peak - ATR × 1.5 (sube con el precio, nunca baja)
     - **SHORT**: si precio < peak → nuevo peak, nuevo stop = peak + ATR × 1.5 (baja con el precio, nunca sube)
   - Si precio toca el stop → cierra la posición al mercado

### WebSocket (`core/binance_ws.py`)
- Conexión a `wss://stream.binance.com:9443/stream`
- Suscripción a `{symbol}@miniTicker` para cada par (precio, high, low 24h)
- Reconexión automática con backoff exponencial (2s → 4s → 8s → ... → máx 60s)

---

## 6. Reglas de Riesgo (hardcodeadas en config.py)

| Parámetro | Valor | Descripción |
|---|---|---|
| MAX_TRADE_USD | $50 | Monto máximo por operación |
| STOP_LOSS_PCT | 4% | Fallback si ATR no disponible |
| MAX_DAILY_LOSS_USD | $30 | Límite de pérdida diaria (~3 stops) |
| MAX_OPEN_POSITIONS | 2 | Máximo de posiciones simultáneas |
| MIN_SIGNAL_CONVICTION | 8 | Convicción mínima Claude para ejecutar |
| STOP_LOSS_PCT_B | 3% | Stop loss para Grupo B |
| TAKE_PROFIT_PCT_B | 15% | Take profit para Grupo B |

### Protecciones adicionales
- **Daily halt**: si la pérdida del día supera $30, el agente se detiene completamente hasta el día siguiente
- **Filtro Fear & Greed**: no abre LONG en Extreme Greed (>80), no abre SHORT en Extreme Fear (<20)
- **Un par, una posición**: no abre una segunda posición en el mismo par
- **Régimen incompatible**: cierra posiciones automáticamente si el régimen cambia contra la dirección

---

## 7. Árbol de Decisiones Completo

### ¿Debo analizar este par?
```
Par disponible
├── ¿Tiene posición abierta? → SÍ → SKIP (no analizar)
├── ¿Pasaron 4h desde último análisis? → NO → SKIP
└── SÍ → Continuar al filtro mecánico
```

### Filtro mecánico (sin IA)
```
Datos de mercado OK
├── Régimen BULL_TREND → dirección LONG
├── Régimen BEAR_TREND → dirección SHORT
├── Régimen SIDEWAYS/REVERSAL/UNKNOWN → BLOQUEADO
│
├── ¿Señal fuerte? (EMA_CROSS / RSI_RECOVERY / RSI_REJECTION)
│   ├── SÍ → rangos RSI más amplios, volumen mín 1.2x
│   └── NO → tipo ALIGNMENT, rangos estrictos, volumen mín 1.3x
│
├── ¿EMA alineada con dirección?
│   ├── SÍ → continuar
│   ├── NO + tipo EMA_CROSS → OK (el cruce ES la alineación)
│   └── NO + otro tipo → BLOQUEADO
│
├── ¿RSI en zona válida?
│   ├── SÍ → continuar
│   └── NO → BLOQUEADO (sobrecomprado/sobrevendido/débil)
│
└── ¿Volumen suficiente?
    ├── SÍ → CALIFICADO → pasa al veto Claude
    └── NO → BLOQUEADO
```

### Veto Claude (Haiku)
```
Señal mecánicamente calificada
├── ¿Evento macro inminente? → VETO
├── ¿Divergencia RSI obvia? → VETO
├── ¿BTC en caída libre (altcoin)? → VETO
├── ¿Sobreextensión extrema? → VETO
└── Todo OK → APROBADO (convicción 9/10)
```

### Pre-ejecución
```
Señal aprobada
├── ¿Fear & Greed > 80 + LONG? → BLOQUEADO
├── ¿Fear & Greed < 20 + SHORT? → BLOQUEADO
├── ¿Agente halted (pérdida diaria)? → BLOQUEADO
├── ¿Ya hay MAX_OPEN_POSITIONS? → BLOQUEADO
└── Todo OK → EJECUTAR
```

### Ejecución
```
Ejecutar señal
├── Calcular ATR(14) de velas 1h
│   ├── ATR disponible → SL = entry ± ATR×1.5, TP = entry ± ATR×3
│   └── ATR no disponible → SL = entry ± 4%, TP = entry ± 8%
├── quantity = $50 / precio
├── Orden de mercado en Binance (buy para LONG, sell para SHORT)
├── Guardar en SQLite
├── Inicializar trailing stop en motor async
└── Notificar por Telegram
```

### Cierre de posiciones
```
Posición abierta
├── Monitor cada 15 min:
│   ├── Precio ≤ SL → LOSS (cierre)
│   └── Precio ≥ TP → WIN (cierre)
├── Trailing Stop en tiempo real (WebSocket):
│   ├── Precio sube → stop sube con él (ratchet)
│   └── Precio toca trailing stop → cierre
├── Cambio de régimen:
│   ├── LONG + régimen → BEAR → cierre inmediato
│   └── SHORT + régimen → BULL → cierre inmediato
├── Cierre manual (dashboard):
│   └── POST /api/close con trade_id + token
└── Registrar resultado + PnL + notificar Telegram
```

---

## 8. API y Dashboard

### Endpoints

| Método | Path | Descripción |
|---|---|---|
| GET | `/` | Redirige a dashboard.html |
| GET | `/dashboard_state.json` | Estado completo del agente (JSON) |
| GET | `/api/events` | Eventos con paginación y filtros |
| POST | `/api/close` | Cierre manual de posición (requiere token) |
| GET | `/timeline.html` | Vista cronológica de eventos |
| GET | `/system_explainer.html` | Documentación visual |

### Parámetros de /api/events
- `limit` (default 100, max 500)
- `offset` (paginación)
- `type` (filtro: TRADE_OPEN, TRADE_CLOSE, REGIME_CHANGE, etc.)
- `symbol` (filtro por par)

### Tipos de eventos registrados
| Tipo | Descripción |
|---|---|
| STARTUP | Agente iniciado |
| TRADE_OPEN | Posición abierta |
| TRADE_CLOSE | Posición cerrada (SL/TP/trailing/manual/régimen) |
| REGIME_CHANGE | Cambio de régimen HMM |
| REGIME_EXIT | Posición cerrada por régimen incompatible |
| CLAUDE_SIGNAL | Señal aprobada por Claude |
| CLAUDE_VETO | Señal vetada por Claude Haiku |
| ENTRY_CHECK | Par evaluado pero no calificado |
| MOVER_DETECTED | Top mover del Grupo B detectado |
| GROUP_B_SCAN | Scan diario sin movers calificados |
| DAILY_HALT | Límite de pérdida diaria alcanzado |
| DAILY_RESET | Reset de pérdida diaria (nuevo día) |
| MANUAL_CLOSE | Posición cerrada manualmente desde dashboard |

---

## 9. Notificaciones Telegram

El agente envía estos tipos de mensajes:

1. **Startup** — al arrancar: pares monitoreados, intervalos
2. **Señal accionable** — dirección, convicción, entrada, SL, TP, tesis
3. **Confirmación de ejecución** — order ID, cantidad, precio real de entrada
4. **Trade cerrado** — resultado (WIN/LOSS), entrada, salida, PnL en USD
5. **Resumen de ciclo** — cada 3 ciclos de análisis: F&G, régimenes, balance
6. **Error** — cualquier excepción no capturada
7. **Daily halt** — cuando se alcanza el límite de pérdida diaria

---

## 10. Sistema de Backtest

### Componentes offline (directorio `backtest/`)

1. **downloader.py** — Descarga 2 años de velas 1h y 4h de Binance para los 4 pares base (730 días × 4 pares). Guarda en CSV.

2. **regime_trainer.py** — Entrena el modelo GaussianHMM:
   - 5 features sobre velas 4h
   - n_components=4 (4 estados)
   - 10 reinicios aleatorios, selecciona mejor log-likelihood
   - Etiqueta estados post-hoc: analiza media de retorno y volatilidad
   - Serializa con joblib: modelo, scaler, labels

3. **simulator.py** — Simula la estrategia EMA/RSI sobre datos históricos:
   - Reporta: Sharpe ratio, win rate, drawdown máximo, PnL acumulado
   - Usa las mismas reglas de entrada que el agente en producción

4. **optimizer.py** — Grid search sobre parámetros:
   - Validación walk-forward 80/20 (entrena en 80%, valida en 20%)
   - Optimiza: períodos EMA, rangos RSI, multiplicador ATR, umbrales de volumen

5. **report.py** — Genera reportes HTML con gráficos y métricas visuales

---

## 11. Flujo Temporal de un Día Típico

```
00:00 — Reset diario (daily_loss = 0, halted = false)
00:00 — Ciclo #1: monitoreo de posiciones, check SL/TP
00:05 — Scan Grupo B: busca top movers en Binance
00:05 — Si hay movers calificados: registra MOVER_DETECTED
00:15 — Ciclo #2: monitoreo
00:30 — Ciclo #3: monitoreo
...
04:00 — Ciclo con análisis (4h desde arranque):
         - Régimen HMM actualizado
         - Filtro mecánico evalúa cada par libre
         - Si califica → veto Claude Haiku
         - Si aprobado → ejecución en Binance
         - Notificaciones Telegram
04:15 — Ciclo monitoreo: revisa posiciones abiertas
...
08:00 — Segundo análisis del día
...
12:00 — Tercer análisis
...
(mientras tanto, WebSocket + TrailingStop corren en paralelo 24/7)
(cada 15 min se verifican SL/TP estáticos)
(en tiempo real, el trailing stop ajusta el stop dinámicamente)
```

---

## 12. Modelo de Datos (SQLite)

### Tabla `trades`
| Columna | Tipo | Descripción |
|---|---|---|
| id | INTEGER PK | ID autoincremental |
| symbol | TEXT | Par (ej: BTC/USDT) |
| direction | TEXT | LONG o SHORT |
| conviction | INTEGER | Convicción 1-10 |
| entry_price | REAL | Precio de entrada |
| stop_loss | REAL | Stop loss estático |
| take_profit | REAL | Take profit |
| quantity | REAL | Cantidad del activo |
| usd_value | REAL | Valor en USD al abrir |
| order_id | TEXT | ID de la orden en Binance |
| status | TEXT | OPEN, WIN, LOSS |
| exit_price | REAL | Precio de salida |
| pnl_usd | REAL | Ganancia/pérdida en USD |
| opened_at | TEXT | ISO timestamp apertura |
| closed_at | TEXT | ISO timestamp cierre |
| group_name | TEXT | A o B |
| trailing_stop_price | REAL | Stop dinámico actual |
| atr_value | REAL | ATR(14) calculado al abrir |

### Tabla `events`
| Columna | Tipo | Descripción |
|---|---|---|
| id | INTEGER PK | ID autoincremental |
| timestamp | TEXT | ISO timestamp UTC |
| type | TEXT | Tipo de evento |
| symbol | TEXT | Par (puede ser NULL) |
| group_name | TEXT | A, B, o NULL |
| level | TEXT | INFO, WARNING, SUCCESS, ERROR |
| title | TEXT | Título corto del evento |
| details | TEXT | JSON con datos adicionales |

---

## 13. Prompts de Claude

### System Prompt — Análisis principal (claude-sonnet-4)
Claude actúa como analista cuantitativo senior. Proceso obligatorio:
1. Leer régimen HMM (capa más importante)
2. Evaluar Fear & Greed como termómetro de sentimiento
3. Para cada activo: tendencia EMA 4h, momentum RSI 4h, volumen relativo
4. Cruzar activos para detectar correlaciones

Reglas absolutas: convicción ≥ 7 para operar, RSI > 75 no entrar long, RSI < 25 no entrar short, en BEAR_TREND convicción mínima 8, ratio R:R mínimo 2:1.

### System Prompt — Grupo B (claude-sonnet-4)
Especializado en momentum de alta volatilidad. Evalúa si el movimiento tiene continuación o es agotamiento. Criterios más estrictos: stop 3%, target 15%, ratio 4:1 mínimo.

### System Prompt — Veto (claude-haiku-4)
Risk manager. Solo veta por: evento macro inminente, divergencia RSI, BTC en caída libre, sobreextensión extrema. En caso de duda, NO vetea.

---

## 14. Configuración de Variables de Entorno

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
