# -*- coding: utf-8 -*-
"""
state.py — Persistencia simple del estado en JSON (sobrevive a reinicios del despliegue).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .risk import State

log = logging.getLogger("goldpilot.state")


class StateStore:
    def __init__(self, path: str = "state.json") -> None:
        self.path = Path(path)

    def load(self) -> State:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                state = State(**data)
                log.info("Estado cargado de %s", self.path)
                return state
            except Exception as exc:
                log.warning("Estado corrupto (%s); se crea uno nuevo", exc)
        state = State()
        self.save(state)
        return state

    def save(self, state: State) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)  # escritura atómica

    # ---------------- journal de señales (CSV apéndice)
    def log_signal(self, row: dict) -> None:
        import csv

        new_file = not Path("signals_log.csv").exists()
        with open("signals_log.csv", "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new_file:
                w.writeheader()
            w.writerow(row)
