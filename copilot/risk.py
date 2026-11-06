# -*- coding: utf-8 -*-
"""
risk.py — Motor de riesgo "escalera adaptativa" con la Constitución del concurso.

Límites duros de FundedNext Competition (nov. 2026):
  · pérdida diaria máx. 5%  → corte propio 3.5%
  · pérdida total máx. 10%  → corte propio 7%
  · máx. 5 posiciones (metales) por posición máx. 3 lotes → propio: 3 posiciones
  · máx. 50 trades/día      → propio: 8 señales/día
  · mín. 5 días operados distintos para ser rankeado
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

log = logging.getLogger("goldpilot.risk")


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()

XAU_CONTRACT_SIZE = 100  # 1 lote = 100 onzas → $1 de precio = $100/lote
COMMISSION_PER_LOT = 3.0  # $/lote (forex y materias primas, según reglas del concurso)

PHASES = {
    "F0": {"name": "CUARENTENA",  "risk_pct": 0.0,  "max_pos": 0, "max_signals": 0},
    "F1": {"name": "PROTECCIÓN",  "risk_pct": 0.5,  "max_pos": 2, "max_signals": 4},
    "F2": {"name": "CONSTRUCCIÓN", "risk_pct": 1.0, "max_pos": 3, "max_signals": 6},
    "F3": {"name": "ATAQUE",      "risk_pct": 1.5,  "max_pos": 3, "max_signals": 8},
}


@dataclass
class State:
    """Estado persistente de la participación (JSON en disco)."""
    initial_equity: float = 10_000.0
    equity: float = 10_000.0
    phase: str = "F1"
    paused: bool = False
    rank: int | None = None
    signals_today: int = 0
    open_positions: int = 0
    day: str = ""                 # fecha UTC "YYYY-MM-DD" de los contadores
    days_traded: list[str] = field(default_factory=list)
    daily_start_equity: float = 10_000.0
    total_days: int = 14
    start_date: str = ""          # "YYYY-MM-DD" del día 1 del concurso

    # ------------------------------------------------------------ helpers
    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def total_pnl_pct(self) -> float:
        return (self.equity - self.initial_equity) / self.initial_equity * 100.0

    @property
    def daily_pnl_pct(self) -> float:
        if self.daily_start_equity <= 0:
            return 0.0
        return (self.equity - self.daily_start_equity) / self.daily_start_equity * 100.0

    @property
    def day_number(self) -> int:
        if not self.start_date:
            return 1
        start = date.fromisoformat(self.start_date)
        return min((_utc_today() - start).days + 1, self.total_days)

    @property
    def days_traded_count(self) -> int:
        return len(set(self.days_traded))


class RiskEngine:
    """Decide si hay permiso para señalizar y calcula el lote exacto."""

    DAILY_SOFT_LIMIT = 3.5   # % corte propio (regla real: 5%)
    TOTAL_SOFT_LIMIT = 7.0   # % corte propio (regla real: 10%)
    MAX_LOT = 3.0            # límite del concurso para metales
    MAX_CONSECUTIVE_LOSSES = 3

    def __init__(self, cfg: dict, state: "State") -> None:
        self.cfg = cfg
        self.state = state

    # ------------------------------------------------------------ fases
    def recompute_phase(self) -> tuple[str, str]:
        """Escalera adaptativa. Devuelve (fase_nueva, motivo)."""
        s = self.state
        old = s.phase
        if s.total_pnl_pct <= -3.0 or s.daily_pnl_pct <= -3.5:
            new, why = "F0", "cuarentena: pérdida ≥ 3% (total) o corte diario tocado"
        elif s.total_pnl_pct >= 5.0 and (s.rank is not None and s.rank <= 600):
            new, why = "F3", "equidad ≥ +5% y top 600 reportado → ataque controlado"
        elif s.total_pnl_pct >= 2.0:
            new, why = "F2", "equidad ≥ +2% → construcción"
        else:
            new, why = "F1", "fase base de protección"
        s.phase = new
        if new != old:
            log.info("Fase %s → %s (%s)", old, new, why)
        return new, why

    def check_permission(self) -> tuple[bool, str]:
        """Gate maestro antes de emitir CUALQUIER señal."""
        s = self.state
        self._roll_day_if_needed()
        if s.paused:
            return False, "bot en pausa (/reanudar para volver)"
        if s.total_pnl_pct <= -self.TOTAL_SOFT_LIMIT:
            return False, (f"pérdida total {s.total_pnl_pct:.2f}% ≥ corte propio "
                           f"{self.TOTAL_SOFT_LIMIT}% — NO se opera más esta ventana")
        if s.daily_pnl_pct <= -self.DAILY_SOFT_LIMIT:
            return False, (f"pérdida diaria {s.daily_pnl_pct:.2f}% ≥ corte propio "
                           f"{self.DAILY_SOFT_LIMIT}% — día terminado")
        phase = PHASES[s.phase]
        if s.signals_today >= phase["max_signals"]:
            return False, (f"límite de {phase['max_signals']} señales del día alcanzado "
                           f"(fase {s.phase})")
        if s.open_positions >= phase["max_pos"]:
            return False, (f"máximo {phase['max_pos']} posiciones abiertas en fase {s.phase}")
        return True, f"OK · fase {s.phase} ({phase['name']})"

    # ------------------------------------------------------------ lote
    def lot_for(self, entry: float, sl: float, signal_kind: str) -> dict:
        """
        Lote = Riesgo($) / (distancia_SL_en_$ × 100).  Con comisión $3/lote.
        En cuarentena (F0) devuelve lote 0 (solo observación).
        """
        s = self.state
        risk_pct = PHASES[s.phase]["risk_pct"]
        risk_usd = s.equity * risk_pct / 100.0
        dist = abs(entry - sl)
        if dist <= 0:
            return {"lot": 0.0, "risk_usd": 0.0, "risk_pct": 0.0, "commission": 0.0,
                    "note": "SL inválido"}
        lot = risk_usd / (dist * XAU_CONTRACT_SIZE)
        lot = max(0.0, lot)
        # redondeo SIEMPRE hacia abajo a 0.01 (nunca arriesgar más de lo planeado)
        lot = math.floor(lot * 100) / 100.0
        capped = False
        if lot > self.MAX_LOT:
            lot, capped = self.MAX_LOT, True
        commission = round(lot * COMMISSION_PER_LOT, 2)
        note = ""
        if capped:
            note = (f"lote limitado al máximo del concurso ({self.MAX_LOT}); "
                    f"riesgo efectivo menor al planificado")
        return {
            "lot": lot,
            "risk_usd": round(risk_usd, 2),
            "risk_pct": risk_pct,
            "commission": commission,
            "note": note,
        }

    # ------------------------------------------------------------ registro
    def register_signal(self) -> None:
        self._roll_day_if_needed()
        self.state.signals_today += 1

    def register_executed_trade(self) -> None:
        """Llamado cuando el humano confirma /ok — cuenta como día operado."""
        self._roll_day_if_needed()
        today = date.today().isoformat()
        if today not in self.state.days_traded:
            self.state.days_traded.append(today)

    def update_equity(self, value: float) -> None:
        s = self.state
        if s.daily_start_equity == s.initial_equity and s.equity == s.initial_equity:
            s.daily_start_equity = value
        s.equity = float(value)
        self.recompute_phase()

    def _roll_day_if_needed(self) -> None:
        s = self.state
        today = _utc_today().isoformat()
        if s.day != today:
            s.day = today
            s.signals_today = 0
            s.daily_start_equity = s.equity

    # ------------------------------------------------------------ panel
    def status_text(self) -> str:
        s = self.state
        ph = PHASES[s.phase]
        used_daily = abs(min(0.0, s.daily_pnl_pct))
        return (
            f"📊 ESTADO GOLDPILOT FN\n"
            f"• Fase: {s.phase} — {ph['name']} (riesgo {ph['risk_pct']}%/trade, "
            f"máx {ph['max_pos']} pos, {ph['max_signals']} señales/día)\n"
            f"• Equidad: ${s.equity:,.2f}  ({s.total_pnl_pct:+.2f}% total)\n"
            f"• Hoy: {s.daily_pnl_pct:+.2f}%  (corte propio {self.DAILY_SOFT_LIMIT}%)\n"
            f"• Señales hoy: {s.signals_today}/{ph['max_signals']}\n"
            f"• Días operados: {s.days_traded_count}/5 mínimos · día {s.day_number}/{s.total_days}\n"
            f"• Rank reportado: {s.rank if s.rank else '—'}\n"
            f"• {'⏸ EN PAUSA' if s.paused else '✅ ACTIVO'}"
        )
