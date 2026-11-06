# 🟡 GoldPilot FN — Copiloto IA para la FundedNext Monthly Competition

Agente de inteligencia artificial que **vigila el oro (XAUUSD) 24/7** durante la
[competencia mensual de FundedNext](https://fundednext.com/es/competition) y te envía por
**Telegram señales listas para ejecutar** (entrada, Stop Loss, Take Profits y lote exacto),
calculadas con un motor de riesgo que **nunca supera los límites del concurso**.

```
┌─────────────┐   velas 15m    ┌──────────────┐   señal válida   ┌──────────────┐
│ Feed de datos│ ─────────────▶ │  Estrategia   │ ───────────────▶ │    Telegram   │
│ MT5/yfinance │                │  momentum     │   + motor riesgo │  📲 tu móvil  │
└─────────────┘                └──────────────┘   (escalera      └──────┬───────┘
                                            ▲           adaptativa)      │ /equidad /rank
                                     ┌──────────────┐                    ▼
                                     │ Estado JSON  │◀──────────── TÚ ejecutas
                                     │ (persistente)│                MANUALMENTE
                                     └──────────────┘              en FundedNext
```

## ⚖️ Por qué este diseño es 100% reglamentario

El reglamento del concurso dice: *"Run EAs, automation or Trade Manager — **Manual trading
only**"*. Por eso GoldPilot FN está construido como **copiloto de señales**:

| Rol | Quién lo hace |
|---|---|
| Vigilar el mercado día y noche | 🤖 El agente (GitHub Actions cada 5 min) |
| Analizar y calcular la señal exacta | 🤖 El agente |
| Controlar riesgo, fases y límites | 🤖 El agente |
| **Hacer clic en COMPRAR/VENDER en la plataforma** | 👤 **Tú (manual)** |

Tú decides si ejecutar o no cada señal. El bot nunca toca tu cuenta.

## 🧠 Los dos cerebros del sistema

1. **`SUPER_PROMPT.md`** — el súper prompt maestro. Pégalo en tu IA favorita (GLM, GPT,
   Claude...) y se convierte en *GoldPilot FN*: analiza, explica, calcula lotes, gestiona
   fases de riesgo y te disciplina. Úsalo como "sesión de escritorio".
2. **Este repositorio** — la vigilancia autónoma 24/7 con código Python + Telegram, que
   funciona aunque no tengas un chat abierto con la IA.

## 📦 Estructura del repositorio

```
fundednext-ai-copilot/
├── SUPER_PROMPT.md              ← ⭐ el súper prompt maestro del agente
├── main.py                      ← punto de entrada (loop 24/7 o --once)
├── config.yaml                  ← equidad, feed, parámetros de estrategia
├── copilot/
│   ├── feed.py                  ← datos: MetaTrader5 (real) o yfinance (nube)
│   ├── strategy.py              ← Setups A (ruptura asiática) y B (pullback NY)
│   ├── risk.py                  ← Constitución + escalera adaptativa de fases
│   ├── state.py                 ← persistencia JSON + journal CSV
│   └── telegram_bot.py          ← fichas de señal, comandos, control remoto
├── .github/workflows/
│   └── goldpilot.yml            ← despliegue gratis: cada 5 min, 24/7
├── tests/test_logic.py          ← pruebas del motor de riesgo y la estrategia
├── requirements.txt  .env.example  .gitignore  LICENSE
```

## 🚀 Despliegue en GitHub (paso a paso, 15 minutos)

### Rutas de despliegue — elige la tuya

| Ruta | Quién lo hace | Cuándo usarla |
|---|---|---|
| **Web (recomendada 1ª vez)** | Tú: 5 clics siguiendo los pasos de abajo | Ahora — es la más simple |
| **`tools/deploy_github.py`** | Tú desde tu PC: crea repo + push + secrets + primer ciclo, todo automático | Para actualizaciones futuras o si prefieres terminal |
| **Vigilancia sin GitHub** | Tu asistente IA (tareas programadas) — necesitas darle solo el token de Telegram | Para recibir señales HOY mientras configuras GitHub |

> 💡 GitHub está disponible para desarrolladores en Cuba (desde 2021) y Actions es
> **gratis e ilimitado en repos públicos**. Por eso el despliegue crea un repo público:
> los secretos viven cifrados en los Actions Secrets, no en el código.

### Paso 1 — Crea tu bot de Telegram
1. Busca **@BotFather** en Telegram → `/newbot` → nombre y username.
2. Copia el **token** que te da (formato `123456:ABC...`).
3. Escríbele un "hola" a tu bot nuevo y abre:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` → copia tu `chat_id` numérico.

### Paso 2 — Sube el repositorio a tu GitHub
**Opción web (sin terminal):**
1. Entra a github.com → botón **New repository** → nombre: `goldpilot-fn` → Create.
2. **Add file → Upload files** → arrastra TODO el contenido de esta carpeta
   (incluidas las carpetas ocultas `.github`) → **Commit changes**.

**Opción con terminal:**
```bash
cd fundednext-ai-copilot
git init && git add . && git commit -m "GoldPilot FN v1"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/goldpilot-fn.git
git push -u origin main
```

### Paso 3 — Configura los secretos
En tu repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | el token de @BotFather |
| `TELEGRAM_CHAT_ID` | tu chat_id numérico |
| `FN_INITIAL_EQUITY` | ej. `10000` (equidad de tu cuenta del concurso) |
| `FN_START_DATE` | ej. `2026-11-03` (día 1 de la ventana) |

### Paso 4 — Activa el vigilante 24/7
1. Pestaña **Actions** del repo → si pregunta, pulsa **I understand, enable**.
2. El workflow `GoldPilot FN — vigilancia 24/7` corre **solo cada 5 minutos**.
3. Prueba manual: **Run workflow** → deberías recibir en Telegram el mensaje de
   pre-apertura o un ciclo silencioso (fuera de horario Londres/NY no hay señales, es normal).

> 💡 **Alternativa más robusta:** una VPS de 4–5 $/mes (o Railway/Render) ejecutando
> `python main.py` en loop continuo. El modo `--once` de GitHub Actions es la vía 0 $.

### Paso 5 — Úsalo durante el concurso
- En Telegram: `/estado` (panel), `/equidad 10250` (tras cerrar trades),
  `/rank 350` (posición en el leaderboard), `/ok #3` (marcaste ejecutada).
- Cada señal llega con **lote exacto según tu fase** — copia los 5 números a la app
  de FundedNext y ejecuta. Eso es todo tu trabajo: 30 segundos por trade.

## 🎚️ La escalera adaptativa (motor de riesgo)

| Fase | Cuándo | Riesgo/trade | Posiciones | Señales/día |
|---|---|---|---|---|
| **F1 Protección** | default / días 1–3 | 0.5% | 2 | 4 |
| **F2 Construcción** | equidad ≥ +2% | 1.0% | 3 | 6 |
| **F3 Ataque** | ≥ +5% y top 600 | 1.5% | 3 | 8 |
| **F0 Cuarentena** | ≤ −3% o 3 pérdidas seguidas | 0 (solo observa) | 0 | 0 |

Cortes propios (más estrictos que el concurso): **−3.5% diario** y **−7% total** → el bot
deja de emitir señales automáticamente.

## 🧪 Pruebas locales

```bash
pip install -r requirements.txt
python -m pytest tests/ -v          # motor de riesgo + estrategia
python main.py --dry-run --once     # un ciclo en consola, sin Telegram
python main.py                      # loop 24/7 local
```

## ⚠️ Descargo de responsabilidad (léelo de verdad)

- Este proyecto es **educativo y de apoyo al proceso**. **Ninguna IA ni bot puede
  garantizar posiciones de premio**: el leaderboard depende del mercado y de miles de
  participantes. Lo que sí controla este sistema es la disciplina, la velocidad de
  análisis y el cumplimiento estricto de las reglas.
- Operar derivados implica riesgo elevado de pérdida. Las señales no son asesoramiento
  financiero; tú asumes cada decisión de ejecución.
- No uses VPN ni multicuenta: es descalificación permanente. Cuba no está en la lista de
  restricciones de FundedNext CFDs, pero **confírmalo con su soporte antes de competir**,
  y verifica que puedas recibir premios (vía cripto) desde tu país.
- Las reglas del concurso pueden cambiar: revisa la página oficial cada mes.
