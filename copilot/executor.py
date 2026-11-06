# -*- coding: utf-8 -*-
"""
executor.py — BRAZO EJECUTOR de GoldPilot: convierte señales en posiciones.

╔══════════════════════════════════════════════════════════════════════╗
║  REGLA DE USO — verificado en fundednext.com (nov. 2026):            ║
║                                                                      ║
║  · El CONCURSO mensual prohíbe EAs/automatización ("Manual trading   ║
║    only — will disqualify the entry"). NUNCA apuntes este ejecutor   ║
║    a la cuenta del concurso: ahí solo opera el copiloto de señales.  ║
║                                                                      ║
║  · Este ejecutor SÍ es legal en: cuenta DEMO (gratis) y challenges   ║
║    FundedNext MT5 de MENOS de $50,000 con el add-on de EA pagado.    ║
║    Prohibido en cTrader y Match-Trader (no permiten automatización)  ║
║    y en cuentas ≥ $50,000.                                           ║
║                                                                      ║
║  · Sin Telegram en la ruta de ejecución: las reglas de EA de         ║
║    FundedNext prohíben EAs integrados con apps de terceros.          ║
╚══════════════════════════════════════════════════════════════════════╝

Dos implementaciones con la MISMA interfaz:
  · SimExecutor — simulación sin dinero (corre en cualquier sistema).
  · MT5Executor — órdenes reales vía terminal MetaTrader 5
    (requiere Windows/VPS con MT5 abierto y sesión iniciada).

Gestión de posiciones (playbook del súper prompt):
  · SL y TP2 se colocan en la orden desde el primer segundo.
  · Al tocar TP1: cierra el 50% y sube el SL de lo restante a entrada
    (breakeven) → la operación queda "sin riesgo".
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .strategy import Signal

log = logging.getLogger("goldpilot.executor")

XAU_CONTRACT = 100.0   # 1 lote XAUUSD = 100 oz → $1 de precio = $100 por lote


@dataclass
class Position:
    ticket: int
    kind: str                # "BUY" | "SELL"
    lot: float
    entry: float
    sl: float
    tp1: float
    tp2: float
    opened_at: datetime
    tp1_done: bool = False


def validate_signal(sig: Signal, lot: float) -> tuple[bool, str]:
    """Comprobaciones de cordura ANTES de enviar cualquier orden."""
    if lot <= 0:
        return False, "lote 0 (fase F0 o equidad insuficiente) — no se ejecuta"
    if sig.sl_distance <= 0:
        return False, "SL inválido"
    if sig.kind == "BUY" and not (sig.sl < sig.entry < sig.tp2):
        return False, "niveles BUY inconsistentes"
    if sig.kind == "SELL" and not (sig.tp2 < sig.entry < sig.sl):
        return False, "niveles SELL inconsistentes"
    if sig.rr < 1.0:
        return False, f"R:R {sig.rr} < 1.0 — señal defectuosa"
    return True, "OK"


# ══════════════════════════════════════════════════════════════════════
#  SIMULADOR — sin dinero, sin MT5, corre en cualquier máquina
# ══════════════════════════════════════════════════════════════════════
class SimExecutor:
    """Ejecución simulada: llena al precio de mercado, sin slippage ni spread.
    Sirve para validar la estrategia y la gestión de riesgo con datos reales
    pero dinero virtual."""

    def __init__(self, balance: float = 10_000.0) -> None:
        self.balance = float(balance)
        self.positions: list[Position] = []
        self._next_ticket = 1

    # ------------------------------------------------------------ orden
    def execute_signal(self, sig: Signal, lot_info: dict) -> dict:
        lot = float(lot_info.get("lot", 0.0))
        ok, why = validate_signal(sig, lot)
        if not ok:
            log.info("Señal rechazada por el ejecutor: %s", why)
            return {"ok": False, "reason": why}
        pos = Position(
            ticket=self._next_ticket, kind=sig.kind, lot=lot,
            entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2,
            opened_at=datetime.now(timezone.utc),
        )
        self._next_ticket += 1
        self.positions.append(pos)
        log.info("[SIM] %s %.2f lotes @ %.2f · SL %.2f · TP2 %.2f (ticket %d)",
                 sig.kind, lot, sig.entry, sig.sl, sig.tp2, pos.ticket)
        return {"ok": True, "ticket": pos.ticket, "price": sig.entry, "lot": lot}

    # ------------------------------------------------------------ gestión
    def manage_positions(self, price: float) -> list[dict]:
        """Marca a mercado: TP1→breakeven, SL/TP2→cierre. Devuelve eventos."""
        events: list[dict] = []
        for pos in list(self.positions):
            long = pos.kind == "BUY"
            hit_sl = price <= pos.sl if long else price >= pos.sl
            hit_tp1 = price >= pos.tp1 if long else price <= pos.tp1
            hit_tp2 = price >= pos.tp2 if long else price <= pos.tp2

            if hit_sl:
                pnl = self._close(pos, pos.sl)
                events.append({"event": "close", "cause": "SL", "ticket": pos.ticket,
                               "pnl": pnl})
            elif hit_tp2:
                pnl = self._close(pos, pos.tp2)
                events.append({"event": "close", "cause": "TP2", "ticket": pos.ticket,
                               "pnl": pnl})
            elif hit_tp1 and not pos.tp1_done:
                half = math.floor(pos.lot / 2 * 100) / 100.0
                if half >= 0.01:
                    pnl = self._close(pos, pos.tp1, volume=half)
                    pos.tp1_done = True
                    pos.sl = pos.entry          # breakeven para lo restante
                    # pos.lot ya fue reducido por _close() → es lo restante
                    events.append({"event": "tp1_breakeven", "ticket": pos.ticket,
                                   "pnl": pnl, "remaining": round(pos.lot, 2),
                                   "new_sl": pos.entry})
                else:
                    pos.tp1_done = True
                    pos.sl = pos.entry
                    events.append({"event": "breakeven", "ticket": pos.ticket,
                                   "new_sl": pos.entry})
        return events

    def _close(self, pos: Position, price: float, volume: float | None = None) -> float:
        vol = pos.lot if volume is None else volume
        sign = 1.0 if pos.kind == "BUY" else -1.0
        pnl = round((price - pos.entry) * sign * vol * XAU_CONTRACT, 2)
        self.balance = round(self.balance + pnl, 2)
        if volume is None:
            self.positions.remove(pos)
        else:
            pos.lot = round(pos.lot - vol, 2)
        return pnl

    # ------------------------------------------------------------ estado
    def open_positions(self) -> int:
        return len(self.positions)

    def floating_pnl(self, price: float) -> float:
        total = 0.0
        for p in self.positions:
            sign = 1.0 if p.kind == "BUY" else -1.0
            total += (price - p.entry) * sign * p.lot * XAU_CONTRACT
        return round(total, 2)

    def current_equity(self, price: float) -> float:
        return round(self.balance + self.floating_pnl(price), 2)


# ══════════════════════════════════════════════════════════════════════
#  MT5 REAL — órdenes reales (Windows/VPS con terminal MT5 abierto)
# ══════════════════════════════════════════════════════════════════════
class MT5Executor:
    """Envía órdenes reales al terminal MetaTrader 5 (paquete oficial de
    MetaQuotes). Solo para demo o challenge MT5 < $50K con add-on EA."""

    def __init__(self, symbol: str = "XAUUSD", magic: int = 20261106,
                 deviation: int = 20, comment: str = "GoldPilot",
                 login: int | None = None, password: str = "",
                 server: str = "") -> None:
        self.symbol = symbol
        self.magic = int(magic)
        self.deviation = int(deviation)
        self.comment = comment
        self.login = login
        self.password = password
        self.server = server
        self.mt5 = None                       # módulo cargado en connect()
        self._known: dict[int, Position] = {}  # ticket → registro interno

    # ------------------------------------------------------------ conexión
    def connect(self) -> None:
        try:
            import MetaTrader5 as mt5          # solo existe en Windows
        except ImportError as exc:
            raise RuntimeError(
                "El paquete MetaTrader5 solo funciona en Windows (PC o VPS) "
                "con el terminal MT5 instalado. En Linux usa --sim.") from exc
        self.mt5 = mt5
        kwargs = {}
        if self.login:
            kwargs = {"login": int(self.login), "password": self.password,
                      "server": self.server}
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize falló: {mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"Símbolo {self.symbol} no visible en el broker")
        acc = mt5.account_info()
        log.info("MT5 conectado: cuenta %s · servidor %s · balance %.2f %s",
                 acc.login, acc.server, acc.balance, acc.currency)

    def shutdown(self) -> None:
        if self.mt5:
            self.mt5.shutdown()

    # ------------------------------------------------------------ orden
    def execute_signal(self, sig: Signal, lot_info: dict) -> dict:
        lot = float(lot_info.get("lot", 0.0))
        ok, why = validate_signal(sig, lot)
        if not ok:
            return {"ok": False, "reason": why}
        mt5 = self.mt5
        tick = mt5.symbol_info_tick(self.symbol)
        price = tick.ask if sig.kind == "BUY" else tick.bid
        otype = mt5.ORDER_TYPE_BUY if sig.kind == "BUY" else mt5.ORDER_TYPE_SELL
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK,
                        mt5.ORDER_FILLING_RETURN):
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": lot,
                "type": otype,
                "price": price,
                "sl": float(sig.sl),
                "tp": float(sig.tp2),
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": self.comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            res = mt5.order_send(req)
            if res is None:
                continue
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                self._known[res.order] = Position(
                    ticket=res.order, kind=sig.kind, lot=lot,
                    entry=res.price or price, sl=sig.sl, tp1=sig.tp1,
                    tp2=sig.tp2, opened_at=datetime.now(timezone.utc))
                log.info("[MT5] %s %.2f @ %.2f (ticket %d, filling %d)",
                         sig.kind, lot, price, res.order, filling)
                return {"ok": True, "ticket": res.order, "price": price, "lot": lot}
            if res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:   # 10030
                return {"ok": False, "reason": f"retcode {res.retcode}: {res.comment}"}
        return {"ok": False, "reason": "sin filling mode compatible"}

    # ------------------------------------------------------------ gestión
    def manage_positions(self, price: float) -> list[dict]:
        """TP1 → cierre parcial 50% + SL a breakeven. Detecta cierres del SL/TP
        del servidor y devuelve sus P&L (del historial de deals)."""
        mt5 = self.mt5
        events: list[dict] = []
        live = mt5.positions_get(symbol=self.symbol) or ()
        live_tickets = {p.ticket for p in live if p.magic == self.magic}

        # 1) posiciones cerradas desde la última pasada → pnl realizado
        for ticket in list(self._known):
            if ticket not in live_tickets:
                pos = self._known.pop(ticket)
                deals = mt5.history_deals_get(position=ticket)
                pnl = round(sum(d.profit for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), 2) \
                    if deals else 0.0
                events.append({"event": "close", "cause": "SL/TP servidor",
                               "ticket": ticket, "pnl": pnl})
                log.info("[MT5] posición %d cerrada · P&L %.2f", ticket, pnl)

        # 2) TP1 → parcial + breakeven
        for p in live:
            if p.magic != self.magic or p.ticket not in self._known:
                continue
            reg = self._known[p.ticket]
            if reg.tp1_done:
                continue
            hit_tp1 = (p.price_current >= reg.tp1) if reg.kind == "BUY" \
                else (p.price_current <= reg.tp1)
            if not hit_tp1:
                continue
            reg.tp1_done = True
            half = math.floor(p.volume / 2 * 100) / 100.0
            if half >= 0.01:
                tick = mt5.symbol_info_tick(self.symbol)
                close_type = mt5.ORDER_TYPE_SELL if reg.kind == "BUY" \
                    else mt5.ORDER_TYPE_BUY
                price_c = tick.bid if reg.kind == "BUY" else tick.ask
                req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
                       "volume": half, "type": close_type, "position": p.ticket,
                       "price": price_c, "deviation": self.deviation,
                       "magic": self.magic, "comment": self.comment + "-TP1",
                       "type_time": mt5.ORDER_TIME_GTC,
                       "type_filling": mt5.ORDER_FILLING_IOC}
                res = mt5.order_send(req)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    events.append({"event": "tp1_breakeven", "ticket": p.ticket,
                                   "pnl": round(half * (reg.tp1 - reg.entry) *
                                                (1 if reg.kind == "BUY" else -1) *
                                                XAU_CONTRACT, 2),
                                   "remaining": round(p.volume - half, 2),
                                   "new_sl": reg.entry})
            # SL restante a entrada (con o sin parcial)
            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP,
                            "symbol": self.symbol, "position": p.ticket,
                            "sl": float(reg.entry), "tp": float(reg.tp2)})
            reg.sl = reg.entry
        return events

    # ------------------------------------------------------------ estado
    def open_positions(self) -> int:
        live = self.mt5.positions_get(symbol=self.symbol) or ()
        return sum(1 for p in live if p.magic == self.magic)

    def current_equity(self, price: float | None = None) -> float:
        acc = self.mt5.account_info()
        return float(acc.equity) if acc else 0.0


# ------------------------------------------------------------------ fábrica
def build_executor(cfg: dict, mode: str) -> SimExecutor | MT5Executor:
    """mode: 'sim' | 'mt5' (la cuenta del concurso jamás pasa por aquí)."""
    ex = cfg.get("execution", {})
    if mode == "sim":
        return SimExecutor(balance=float(ex.get("sim_balance", 10_000.0)))
    if mode == "mt5":
        return MT5Executor(
            symbol=ex.get("symbol", "XAUUSD"), magic=ex.get("magic", 20261106),
            deviation=ex.get("deviation", 20), comment=ex.get("comment", "GoldPilot"),
            login=ex.get("mt5_login"), password=ex.get("mt5_password", ""),
            server=ex.get("mt5_server", ""))
    raise ValueError(f"modo de ejecución desconocido: {mode}")
