# -*- coding: utf-8 -*-
"""
executor_main.py — BRAZO EJECUTOR AUTÓNOMO de GoldPilot (sin Telegram).

Dónde corre: un PC con Windows o un VPS con el terminal MetaTrader 5
abierto y con sesión iniciada. En Linux solo el modo simulación.

    # Simulación gratis (hoy mismo, sin cuenta):
    python executor_main.py --sim

    # Órdenes reales (demo o challenge MT5 < $50K con add-on EA):
    python executor_main.py --mt5

╔══════════════════════════════════════════════════════════════════╗
║  LA CUENTA DEL CONCURSO NUNCA PASA POR AQUÍ.                     ║
║  Automatizar el concurso = descalificación inmediata.            ║
║  En el concurso opera el copiloto (main.py) con señales Telegram ║
║  y ejecución MANUAL tuya. Este ejecutor es para demo/challenge.  ║
╚══════════════════════════════════════════════════════════════════╝

Cada ciclo:
  1. marca posiciones a mercado (TP1→breakeven, SL/TP2→cierre)
  2. actualiza equidad y fase de la escalera
  3. gate de riesgo (cortes propios 3.5%/7%, máx posiciones, señales/día)
  4. evalúa setups A/B y ejecuta con el lote exacto de la fase
  5. persiste estado (executor_state.json) para sobrevivir reinicios
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone

import yaml

from copilot.executor import build_executor
from copilot.feed import build_feed
from copilot.risk import PHASES, RiskEngine, State
from copilot.state import StateStore
from copilot.strategy import MomentumStrategy

log = logging.getLogger("goldpilot.executor_main")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def cycle(cfg: dict, feed, strategy, engine, executor, store, dedupe: dict) -> None:
    now = datetime.now(timezone.utc)
    h = now.hour + now.minute / 60.0

    if now.weekday() >= 5:                     # fin de semana: solo gestionar
        try:
            snap = feed.snapshot()
            for ev in executor.manage_positions(snap.price):
                log.info("evento: %s", ev)
        except Exception as exc:
            log.warning("fin de semana sin datos: %s", exc)
        return

    # 1) foto del mercado
    try:
        snap = feed.snapshot()
    except Exception as exc:
        log.warning("Feed caído: %s", exc)
        time.sleep(30)
        return

    # 2) equidad real (cuenta) o simulada → fase de la escalera
    equity = executor.current_equity(snap.price)
    if equity > 0:
        engine.update_equity(equity)

    # 3) gestión de posiciones abiertas (TP1/breakeven/cierres)
    for ev in executor.manage_positions(snap.price):
        log.info("evento: %s", ev)

    # 4) gate de riesgo
    allowed, reason = engine.check_permission()
    if not allowed:
        log.info("Bloqueado: %s", reason)
        return

    # 5) señal
    sig = strategy.evaluate(snap)
    if sig is None:
        return
    key = (sig.kind, round(sig.entry, 2), round(sig.sl, 2))
    if dedupe.get("last") == key:
        return

    ph = PHASES[engine.state.phase]
    lot = engine.lot_for(sig.entry, sig.sl, sig.kind)
    if lot["lot"] <= 0:
        log.info("Lote 0 (%s) — sin ejecución", lot.get("note", "fase"))
        return

    # 6) ¡ejecución!
    res = executor.execute_signal(sig, lot)
    if not res.get("ok"):
        log.warning("Ejecución rechazada: %s", res.get("reason"))
        return
    dedupe["last"] = key
    engine.register_signal()
    engine.register_executed_trade()
    log.info("EJECUTADA %s %s XAUUSD lote=%.2f @ %.2f SL=%.2f TP2=%.2f "
             "(%d/%d señales hoy, %d/%d posiciones)",
             sig.kind, f"Setup{sig.setup}", res["lot"], res["price"],
             sig.sl, sig.tp2, engine.state.signals_today, ph["max_signals"],
             executor.open_positions(), ph["max_pos"])


def main() -> None:
    ap = argparse.ArgumentParser(description="GoldPilot FN — brazo ejecutor")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sim", action="store_true",
                      help="simulación con dinero virtual (corre en cualquier SO)")
    mode.add_argument("--mt5", action="store_true",
                      help="órdenes reales vía terminal MT5 (Windows/VPS)")
    ap.add_argument("--once", action="store_true", help="un solo ciclo y salir")
    args = ap.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    ex_cfg = cfg.get("execution", {})
    if ex_cfg.get("mode") == "disabled" and args.mt5:
        ap.error("execution.mode está 'disabled' en config.yaml — "
                 "cámbialo a 'mt5' cuando tengas la cuenta demo/challenge lista.")

    feed = build_feed(cfg)
    strategy = MomentumStrategy(cfg.get("strategy", {}))

    store = StateStore(ex_cfg.get("state_file", "executor_state.json"))
    state = store.load()
    state.initial_equity = float(ex_cfg.get("sim_balance",
                                            state.initial_equity))
    engine = RiskEngine(cfg, state)

    executor = build_executor(cfg, "sim" if args.sim else "mt5")
    if args.mt5:
        executor.connect()
        log.warning("EJECUTOR MT5 REAL — garantiza que esta cuenta NO sea la "
                    "del concurso (automatizar el concurso = descalificación).")

    dedupe: dict = {"last": None}
    poll = int(cfg.get("general", {}).get("poll_seconds", 60))
    log.info("GoldPilot EXECUTOR en marcha · modo=%s · equidad=%.2f · fase=%s",
             "SIM" if args.sim else "MT5", state.equity, state.phase)

    while True:
        try:
            cycle(cfg, feed, strategy, engine, executor, store, dedupe)
        except Exception as exc:                       # nunca morir en silencio
            log.exception("ciclo falló: %s", exc)
        store.save(engine.state)
        if args.once:
            break
        time.sleep(poll)

    if args.mt5:
        executor.shutdown()


if __name__ == "__main__":
    main()
