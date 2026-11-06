# -*- coding: utf-8 -*-
"""
strategy.py — Playbook intradía momentum para XAUUSD (Setups A y B del súper prompt).

Setup A: Ruptura del rango asiático en la apertura de Londres.
Setup B: Momentum de continuación (retroceso 38–61%) en Nueva York.

Cada señal sale completa (entrada, SL, TP1, TP2, lote) o NO sale.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .feed import MarketSnapshot, atr, ema

log = logging.getLogger("goldpilot.strategy")

# Ventanas operativas (UTC) — ajustables en config.yaml
SESSION_LONDON = (7, 11.5)   # 07:00–11:30
SESSION_NY = (13, 16.5)      # 13:00–16:30
ASIA = (0, 7)                # rango de referencia


@dataclass
class Signal:
    kind: str            # "BUY" | "SELL"
    setup: str           # "A" | "B"
    entry: float
    sl: float
    tp1: float
    tp2: float
    reason: str
    invalidation: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.sl)
        reward = abs(self.tp2 - self.entry)
        return round(reward / risk, 2) if risk > 0 else 0.0

    @property
    def sl_distance(self) -> float:
        return abs(self.entry - self.sl)


# ------------------------------------------------------------------ helpers

def _session_utc(dt: datetime) -> str:
    h = dt.hour + dt.minute / 60
    if ASIA[0] <= h < ASIA[1]:
        return "ASIA"
    if SESSION_LONDON[0] <= h < SESSION_LONDON[1]:
        return "LONDON"
    if SESSION_NY[0] <= h < SESSION_NY[1]:
        return "NY"
    if SESSION_NY[1] <= h < 24:
        return "PM"
    return "EARLY"


def _asian_range(df: pd.DataFrame, now: datetime) -> tuple[float, float] | None:
    """Máximo y mínimo de la sesión asiática del día actual (00:00–07:00 UTC)."""
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    asia = df[(df.index >= day0) & (df.index < day0 + pd.Timedelta(hours=ASIA[1] - ASIA[0]))]
    if len(asia) < 8:  # menos de 2 h de velas: rango poco fiable
        return None
    return float(asia["high"].max()), float(asia["low"].min())


def _london_range(df: pd.DataFrame, now: datetime) -> tuple[float, float] | None:
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lon_start = day0 + pd.Timedelta(hours=SESSION_LONDON[0])
    lon = df[(df.index >= lon_start) & (df.index <= now)]
    if len(lon) < 6:
        return None
    return float(lon["high"].max()), float(lon["low"].min())


def _last_swing(df: pd.DataFrame, lookback: int = 40) -> tuple[float, float]:
    """Último impulso: (máximo, mínimo) de las últimas N velas M15."""
    win = df.iloc[-lookback:]
    return float(win["high"].max()), float(win["low"].min())


# ------------------------------------------------------------------ generador

class MomentumStrategy:
    def __init__(self, cfg: dict) -> None:
        s = cfg.get("strategy", {})
        self.buffer_atr_mult = float(s.get("breakout_buffer_atr", 0.15))
        self.sl_atr_mult = float(s.get("sl_buffer_atr", 0.5))
        self.rr_min = float(s.get("min_rr", 1.5))
        self.dead_range_ratio = float(s.get("dead_range_atr_ratio", 0.3))
        self.daily_atr_period = 14

    # ---------------------------------------------------------------- setup A
    def setup_asian_breakout(self, snap: MarketSnapshot) -> Signal | None:
        df, price, now = snap.df_m15, snap.price, snap.utc_now
        if _session_utc(now) != "LONDON":
            return None
        rng = _asian_range(df, now)
        if rng is None:
            return None
        hi, lo = rng
        a = snap.atr_m15
        if a <= 0:
            return None

        day = df[df.index >= now.replace(hour=0, minute=0, second=0, microsecond=0)]
        if day.empty:
            return None
        daily_atr = float(atr(day.resample("1D").agg(
            {"high": "max", "low": "min", "close": "last"}).dropna(), self.daily_atr_period
        ) or 0)
        # mercado muerto: rango asiático demasiado estrecho respecto al ATR diario
        if daily_atr > 0 and (hi - lo) < self.dead_range_ratio * daily_atr:
            return None

        close = df["close"]
        ema20, ema50 = ema(close, 20), ema(close, 50)
        buf = self.buffer_atr_mult * a

        # ruptura alcista confirmada por cierre
        if price > hi + buf and ema20.iloc[-1] > ema50.iloc[-1]:
            entry = round(price, 2)
            sl = round(lo - self.sl_atr_mult * a, 2)
            risk = entry - sl
            if risk <= 0:
                return None
            tp1, tp2 = round(entry + 1.5 * risk, 2), round(entry + 2.5 * risk, 2)
            s = Signal(
                kind="BUY", setup="A", entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                reason=(f"Ruptura del rango asiático ({lo:.2f}–{hi:.2f}) con EMA20>EMA50 "
                        f"en M15; ATR M15 = {a:.2f}"),
                invalidation="Vela M15 que cierre por debajo del máximo asiático antes de TP1",
            )
            return s if s.rr >= self.rr_min else None

        # ruptura bajista confirmada por cierre
        if price < lo - buf and ema20.iloc[-1] < ema50.iloc[-1]:
            entry = round(price, 2)
            sl = round(hi + self.sl_atr_mult * a, 2)
            risk = sl - entry
            if risk <= 0:
                return None
            tp1, tp2 = round(entry - 1.5 * risk, 2), round(entry - 2.5 * risk, 2)
            s = Signal(
                kind="SELL", setup="A", entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                reason=(f"Ruptura bajista del rango asiático ({lo:.2f}–{hi:.2f}) con EMA20<EMA50 "
                        f"en M15; ATR M15 = {a:.2f}"),
                invalidation="Vela M15 que cierre por encima del mínimo asiático antes de TP1",
            )
            return s if s.rr >= self.rr_min else None
        return None

    # ---------------------------------------------------------------- setup B
    def setup_ny_pullback(self, snap: MarketSnapshot) -> Signal | None:
        df, price, now = snap.df_m15, snap.price, snap.utc_now
        if _session_utc(now) != "NY":
            return None
        win = df.iloc[-40:]
        if len(win) < 40:
            return None
        hi, lo = _last_swing(df, 40)
        a = snap.atr_m15
        if a <= 0:
            return None

        close = df["close"]
        ema20, ema50 = ema(close, 20), ema(close, 50)

        # tendencia alcista + retroceso a la zona 38–61% del impulso
        if ema20.iloc[-1] > ema50.iloc[-1] and hi > lo:
            zone_top = hi - 0.38 * (hi - lo)
            zone_bot = hi - 0.61 * (hi - lo)
            last = win.iloc[-1]
            rejection = (last["low"] - max(last["open"], last["close"])) > 0.4 * (
                last["high"] - last["low"]
            )
            if zone_bot <= price <= zone_top and rejection:
                entry = round(price, 2)
                sl = round(min(last["low"], lo) - 0.3 * a, 2)
                risk = entry - sl
                if risk <= 0:
                    return None
                tp1 = round(hi, 2)
                tp2 = round(entry + max(1.5 * risk, hi - entry + 0.5 * a), 2)
                s = Signal(
                    kind="BUY", setup="B", entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                    reason=(f"Tendencia alcista (EMA20>EMA50) y retroceso a la zona "
                            f"38–61% ({zone_bot:.2f}–{zone_top:.2f}) con vela de rechazo"),
                    invalidation="Cierre M15 por debajo del mínimo de la vela de rechazo",
                )
                return s if s.rr >= self.rr_min else None

        # tendencia bajista espejo
        if ema20.iloc[-1] < ema50.iloc[-1] and hi > lo:
            zone_top = lo + 0.61 * (hi - lo)
            zone_bot = lo + 0.38 * (hi - lo)
            last = win.iloc[-1]
            rejection = (min(last["open"], last["close"]) - last["high"]) > 0.4 * (
                last["high"] - last["low"]
            )
            if zone_bot <= price <= zone_top and rejection:
                entry = round(price, 2)
                sl = round(max(last["high"], hi) + 0.3 * a, 2)
                risk = sl - entry
                if risk <= 0:
                    return None
                tp1 = round(lo, 2)
                tp2 = round(entry - max(1.5 * risk, entry - lo + 0.5 * a), 2)
                s = Signal(
                    kind="SELL", setup="B", entry=entry, sl=sl, tp1=tp1, tp2=tp2,
                    reason=(f"Tendencia bajista (EMA20<EMA50) y rebote a la zona "
                            f"38–61% ({zone_bot:.2f}–{zone_top:.2f}) con vela de rechazo"),
                    invalidation="Cierre M15 por encima del máximo de la vela de rechazo",
                )
                return s if s.rr >= self.rr_min else None
        return None

    # ---------------------------------------------------------------- API
    def evaluate(self, snap: MarketSnapshot) -> Signal | None:
        """Devuelve la primera señal válida del momento o None. Sin señal = no forzar."""
        try:
            sig = self.setup_asian_breakout(snap)
            if sig:
                return sig
            return self.setup_ny_pullback(snap)
        except Exception as exc:  # nunca tumbar el loop 24/7
            log.exception("Error en evaluación: %s", exc)
            return None
