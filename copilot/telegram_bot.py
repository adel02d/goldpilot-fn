# -*- coding: utf-8 -*-
"""
telegram_bot.py — Canal de alertas + control remoto vía Telegram.

Usa la API HTTP de Telegram directamente (requests) para mantener el repo ligero y
compatible con cualquier nube (GitHub Actions, Railway, Render, VPS...).

Comandos soportados:
  /estado  /equidad <valor>  /rank <pos>  /pausar  /reanudar  /reglas  /ayuda
  /ok #n  ·  /skip #n  (journal de señales)
"""
from __future__ import annotations

import logging
import time
from datetime import date

import requests

from .risk import PHASES, RiskEngine, State
from .state import StateStore
from .strategy import Signal

log = logging.getLogger("goldpilot.telegram")

API = "https://api.telegram.org/bot{token}/{method}"

RULES_TEXT = (
    "📜 *La Constitución del concurso*\n"
    "• Pérdida diaria máx: 5% → corte propio *3.5%*\n"
    "• Pérdida total máx: 10% → corte propio *7%*\n"
    "• Máx 5 posiciones (nosotros: 3) · máx 50 trades/día (nosotros: 8 señales)\n"
    "• XAUUSD es *metal*: máx 3 lotes por posición\n"
    "• Mínimo 5 días operados distintos para ser rankeado\n"
    "• ⛔ Prohibido: EAs/bots ejecutores, Trade Manager, multicuenta, IPs compartidas\n"
    "• ✅ Permitido: operar noticias (NFP/FOMC/CPI), mantener overnight y fin de semana"
)

HELP_TEXT = (
    "🤖 *GoldPilot FN — comandos*\n"
    "/estado — panel completo de riesgo y fase\n"
    "/equidad 10250 — reporta tu equidad actual (recalcula fase y lotes)\n"
    "/rank 350 — reporta tu posición en el leaderboard (activa fase de ataque)\n"
    "/pausar · /reanudar — detener/retomar señales\n"
    "/reglas — la constitución del concurso\n"
    "/ok #3 · /skip #3 — registra si ejecutaste o no una señal\n"
    "/ayuda — este mensaje"
)


class TelegramChannel:
    def __init__(self, token: str, chat_id: str, store: StateStore,
                 engine: RiskEngine, state: State) -> None:
        self.token = token
        self.chat_id = chat_id
        self.store = store
        self.engine = engine
        self.state = state
        self._offset = 0

    # ---------------------------------------------------------------- envío
    def send(self, text: str, silent: bool = False) -> None:
        url = API.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_notification": silent,
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 429:  # rate limit
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)))
                requests.post(url, json=payload, timeout=15)
            elif not r.ok:
                log.warning("Telegram sendMessage %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as exc:
            log.warning("Telegram no disponible: %s", exc)

    # ---------------------------------------------------------------- tarjetas
    def send_signal(self, sig: Signal, idx: int, max_signals: int) -> None:
        lot_info = self.engine.lot_for(sig.entry, sig.sl, sig.kind)
        arrow = "🔼 COMPRA" if sig.kind == "BUY" else "🔽 VENTA"
        sl_pips = sig.sl_distance  # en $ del precio (XAUUSD)
        phase = self.state.phase
        msg = (
            f"🟡 *GOLDPILOT FN — SEÑAL #{idx}/{max_signals}* · {phase}\n"
            f"⏰ {sig.created_at:%H:%M} UTC · Setup {sig.setup}\n\n"
            f"📊 XAUUSD — {arrow}\n"
            f"📍 Entrada: `{sig.entry}` (mercado)\n"
            f"🛑 Stop Loss: `{sig.sl}`  ({sl_pips:.2f} $)\n"
            f"🎯 TP1 (50%): `{sig.tp1}`  ·  TP2: `{sig.tp2}`\n"
            f"⚖️ Lote: *{lot_info['lot']}*  "
            f"(riesgo {lot_info['risk_pct']}% = ${lot_info['risk_usd']} "
            f"sobre ${self.state.equity:,.0f})\n"
            f"💰 R:R = 1:{sig.rr}  ·  Comisión est.: ${lot_info['commission']}\n\n"
            f"🧠 {sig.reason}\n"
            f"✋ Invalidación: {sig.invalidation}\n"
        )
        if lot_info.get("note"):
            msg += f"⚠️ {lot_info['note']}\n"
        msg += f"\n👤 /ok #{idx} si la ejecutas · /skip #{idx} si no"
        self.engine.register_signal()
        self.store.log_signal({
            "ts": sig.created_at.isoformat(),
            "n": idx, "setup": sig.setup, "kind": sig.kind,
            "entry": sig.entry, "sl": sig.sl, "tp1": sig.tp1, "tp2": sig.tp2,
            "lot": lot_info["lot"], "rr": sig.rr, "phase": phase,
        })
        self.send(msg)

    def send_blocked(self, reason: str) -> None:
        self.send(f"🚫 *BLOQUEO DE RIESGO:* {reason}\nProhibido operar ahora.", silent=True)

    def send_daily_close(self) -> None:
        s = self.state
        ph = PHASES[s.phase]
        self.send(
            f"📅 *CIERRE DEL DÍA {date.today().isoformat()}* · Día {s.day_number}/{s.total_days}\n"
            f"Equidad: ${s.equity:,.2f} ({s.total_pnl_pct:+.2f}% total) · "
            f"Hoy: {s.daily_pnl_pct:+.2f}%\n"
            f"Señales hoy: {s.signals_today}/{ph['max_signals']} · "
            f"Fase: {s.phase} · Días operados: {s.days_traded_count}/5\n"
            f"{'🧘 Mañana: mantener fase y disciplina.' if s.total_pnl_pct >= 0 else '🧯 Mañana: FASE PROTECCIÓN. No forzar.'}"
        )

    def send_preopen(self, context: str) -> None:
        self.send(f"🌅 *PRE-APERTURA LONDRES*\n{context}", silent=True)

    # ---------------------------------------------------------------- comandos
    def handle_command(self, text: str) -> None:
        parts = text.strip().split()
        cmd = parts[0].lower().split("@")[0]
        s = self.state

        if cmd == "/start" or cmd == "/ayuda":
            self.send(HELP_TEXT)
        elif cmd == "/reglas":
            self.send(RULES_TEXT)
        elif cmd == "/estado":
            self.send(self.engine.status_text())
        elif cmd == "/equidad" and len(parts) > 1:
            try:
                val = float(parts[1].replace(",", "."))
                if s.equity == s.initial_equity and s.daily_start_equity == s.initial_equity:
                    s.initial_equity = val
                self.engine.update_equity(val)
                self.send(
                    f"✅ Equidad actualizada: ${val:,.2f}\n{self.engine.status_text()}"
                )
            except ValueError:
                self.send("❌ Formato: /equidad 10250")
        elif cmd == "/rank" and len(parts) > 1:
            try:
                s.rank = int(parts[1])
                phase, why = self.engine.recompute_phase()
                self.send(f"✅ Rank {s.rank} registrado. Fase → *{phase}*\nMotivo: {why}")
            except ValueError:
                self.send("❌ Formato: /rank 350")
        elif cmd == "/pausar":
            s.paused = True
            self.send("⏸ Bot en pausa. No se emitirán señales.")
        elif cmd == "/reanudar":
            s.paused = False
            self.send("▶️ Bot reactivado.")
        elif cmd.startswith("/ok"):
            self.engine.register_executed_trade()
            self.send(f"📝 Señal {parts[1] if len(parts) > 1 else ''} marcada como "
                      f"EJECUTADA. Día operado registrado "
                      f"({s.days_traded_count}/5).")
        elif cmd.startswith("/skip"):
            self.send("📝 Anotado: señal descartada. Sin señal también es una posición.")
        else:
            self.send("Comando no reconocido. /ayuda para ver la lista.")

    # ---------------------------------------------------------------- polling
    def poll_commands(self, seconds: float = 1.0) -> None:
        """Lectura no bloqueante de comandos pendientes (long polling corto)."""
        url = API.format(token=self.token, method="getUpdates")
        try:
            r = requests.post(
                url,
                json={"offset": self._offset, "timeout": int(seconds),
                      "allowed_updates": ["message"]},
                timeout=seconds + 10,
            )
            data = r.json()
        except requests.RequestException as exc:
            log.debug("poll error: %s", exc)
            return
        if not data.get("ok"):
            return
        for upd in data.get("result", []):
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                continue  # solo el dueño
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                try:
                    self.handle_command(text)
                except Exception:
                    log.exception("error procesando comando %s", text)
