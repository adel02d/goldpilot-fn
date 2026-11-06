#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — GoldPilot FN: copiloto de señales 24/7 para la FundedNext Monthly Competition.

IMPORTANTE (reglas del concurso): este agente NO ejecuta operaciones. Analiza el mercado
y envía señales por Telegram con entrada/SL/TP/lote exactos para que TÚ las ejecutes
MANUALMENTE en la plataforma. "Manual trading only" se cumple al 100%.

Modos de ejecución:
  python main.py            → loop 24/7 (VPS, Railway, Render, PC encendida)
  python main.py --once     → un solo ciclo y sale (ideal para GitHub Actions cada 5 min)
  python main.py --dry-run  → imprime las señales en consola sin Telegram (para probar)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

import yaml

from copilot.feed import build_feed
from copilot.risk import PHASES, RiskEngine, State
from copilot.state import StateStore
from copilot.strategy import MomentumStrategy
from copilot.telegram_bot import TelegramChannel

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("goldpilot")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_env(cfg: dict) -> dict:
    """Permite sobreescribir config.yaml con variables de entorno (secrets de GitHub/Railway)."""
    env_map = {
        "initial_equity": float(os.getenv("FN_INITIAL_EQUITY", "0") or 0),
        "start_date": os.getenv("FN_START_DATE", ""),
    }
    if env_map["initial_equity"] > 0:
        cfg.setdefault("account", {})["initial_equity"] = env_map["initial_equity"]
    if env_map["start_date"]:
        cfg.setdefault("account", {})["start_date"] = env_map["start_date"]
    return cfg


class GoldPilot:
    def __init__(self, cfg: dict, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.poll_interval = int(cfg.get("general", {}).get("poll_seconds", 60))

        self.store = StateStore(cfg.get("general", {}).get("state_file", "state.json"))
        self.state = self.store.load()

        acc = cfg.get("account", {})
        if acc.get("initial_equity") and self.state.equity == 10_000.0:
            self.state.initial_equity = float(acc["initial_equity"])
            self.state.equity = float(acc["initial_equity"])
            self.state.daily_start_equity = float(acc["initial_equity"])
        if acc.get("start_date"):
            self.state.start_date = str(acc["start_date"])

        self.engine = RiskEngine(cfg, self.state)
        self.strategy = MomentumStrategy(cfg)
        self.feed = build_feed(cfg)

        tg = cfg.get("telegram", {})
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))
        self.tg = None
        if self.token and self.chat_id and not dry_run:
            self.tg = TelegramChannel(self.token, self.chat_id, self.store,
                                      self.engine, self.state)
        self._preopen_sent_day = ""
        self._close_sent_day = ""
        self._last_signal_sig = None  # anti-duplicados

    # ---------------------------------------------------------------- ciclo
    def cycle(self) -> None:
        try:
            self._cycle_body()
        finally:
            # Guarda el estado en TODOS los ciclos (fases, comandos, conteos).
            # Si el contenido no cambia, el workflow no crea commits innecesarios.
            self.store.save(self.state)

    def _cycle_body(self) -> None:
        now = datetime.now(timezone.utc)
        h = now.hour + now.minute / 60.0

        # 0) fin de semana: mercado de oro cerrado (sábado=5, domingo=6)
        if now.weekday() >= 5:
            return

        # 1) leer comandos de Telegram (si está activo)
        if self.tg:
            self.tg.poll_commands(seconds=0)

        # 2) rutinas diarias
        today = now.date().isoformat()
        if h >= 6.92 and self._preopen_sent_day != today:
            self._preopen(today)
        if h >= 21.0 and self._close_sent_day != today:
            if self.tg:
                self.tg.send_daily_close()
            self._close_sent_day = today

        # 3) ¿estamos en ventana de señales? (07:00–11:30 y 13:00–16:30 UTC)
        in_london = 7.0 <= h < 11.5
        in_ny = 13.0 <= h < 16.5
        if not (in_london or in_ny):
            return

        # 4) gate de riesgo ANTES de mirar el mercado
        allowed, reason = self.engine.check_permission()
        if not allowed:
            if self.tg and "pérdida" in reason:  # avisos de bloqueo graves, una vez por estado
                self.tg.send_blocked(reason)
            return

        # 5) análisis del mercado
        try:
            snap = self.feed.snapshot()
        except Exception as exc:
            log.warning("Feed caído: %s", exc)
            time.sleep(30)
            return

        sig = self.strategy.evaluate(snap)
        if sig is None:
            return

        # anti-duplicados: misma señal en la misma dirección/niveles → ignorar
        if self._last_signal_sig == (sig.kind, sig.entry, sig.sl):
            return
        self._last_signal_sig = (sig.kind, sig.entry, sig.sl)

        ph = PHASES[self.state.phase]
        if self.state.phase == "F0":
            log.info("Fase cuarentena: señal omitida (%s @ %.2f)", sig.kind, sig.entry)
            return

        if self.tg:
            self.tg.send_signal(sig, self.state.signals_today + 1, ph["max_signals"])
        else:
            lot = self.engine.lot_for(sig.entry, sig.sl, sig.kind)
            print(f"[SEÑAL] {sig.kind} XAUUSD entrada={sig.entry} SL={sig.sl} "
                  f"TP1={sig.tp1} TP2={sig.tp2} lote={lot['lot']} RR=1:{sig.rr} "
                  f"| {sig.reason}")

    # ---------------------------------------------------------------- rutinas
    def _preopen(self, today: str) -> None:
        self._preopen_sent_day = today
        try:
            snap = self.feed.snapshot()
            df, now = snap.df_m15, snap.utc_now
            day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            asia = df[(df.index >= day0) & (df.index < day0 + pd_hours(7))]
            if len(asia) >= 4:
                hi, lo = float(asia["high"].max()), float(asia["low"].min())
                ctx = (f"Rango asiático: {lo:.2f} — {hi:.2f} "
                       f"(amplitud {hi - lo:.2f} $) · ATR M15: {snap.atr_m15:.2f} · "
                       f"precio actual {snap.price:.2f}\n"
                       f"Plan: esperar ruptura confirmada del rango en Londres "
                       f"(buffer {self.cfg['strategy']['breakout_buffer_atr']}×ATR).")
            else:
                ctx = "Aún no hay rango asiático completo; se re-evaluará."
        except Exception as exc:
            ctx = f"(feed no disponible: {exc})"
        if self.tg:
            self.tg.send_preopen(ctx)
        else:
            print(f"[PRE-APERTURA] {ctx}")


def pd_hours(n: float):
    import pandas as pd
    return pd.Timedelta(hours=n)


def main() -> None:
    ap = argparse.ArgumentParser(description="GoldPilot FN — copiloto FundedNext")
    ap.add_argument("--once", action="store_true", help="un solo ciclo (GitHub Actions)")
    ap.add_argument("--dry-run", action="store_true", help="sin Telegram, solo consola")
    args = ap.parse_args()

    cfg = apply_env(load_config())
    bot = GoldPilot(cfg, dry_run=args.dry_run)
    log.info("GoldPilot FN iniciado · fase %s · equidad %.2f",
             bot.state.phase, bot.state.equity)

    if args.once:
        bot.cycle()
        return

    fail_streak = 0
    while True:
        try:
            bot.cycle()
            fail_streak = 0
        except KeyboardInterrupt:
            log.info("Apagado manual")
            sys.exit(0)
        except Exception:
            fail_streak += 1
            log.exception("Error en ciclo (%d consecutivos)", fail_streak)
            if fail_streak >= 10:  # curar y seguir: 24/7 no se rinde
                fail_streak = 0
        time.sleep(bot.poll_interval)


if __name__ == "__main__":
    main()
