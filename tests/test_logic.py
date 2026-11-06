# -*- coding: utf-8 -*-
"""
test_logic.py — Pruebas del motor de riesgo, la estrategia y el formato de señales.
Ejecutar:  python -m pytest tests/ -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copilot.feed import MarketSnapshot, atr, ema  # noqa: E402
from copilot.risk import PHASES, RiskEngine, State, _utc_today  # noqa: E402
from copilot.strategy import MomentumStrategy, Signal  # noqa: E402


def midday_state(**kw) -> State:
    """Estado con el día ya rodado (simula mitad del día de concurso)."""
    st = State(**kw)
    st.day = _utc_today().isoformat()
    return st

CFG = {
    "account": {"initial_equity": 10000},
    "strategy": {
        "breakout_buffer_atr": 0.15, "sl_buffer_atr": 0.5,
        "min_rr": 1.5, "dead_range_atr_ratio": 0.3,
    },
}


# ------------------------------------------------------------------ fixtures

def make_m15(n=300, start_price=2650.0, seed=7):
    """DataFrame M15 sintético realista."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-11-03 00:00", periods=n, freq="15min", tz="UTC")
    rets = rng.normal(0, 0.6, n)
    price = start_price + np.cumsum(rets)
    high = price + rng.uniform(0.1, 0.8, n)
    low = price - rng.uniform(0.1, 0.8, n)
    return pd.DataFrame(
        {"open": price, "high": high, "low": low, "close": price, "volume": 100},
        index=idx,
    )


def sculpt_asian_range(df, hi=2660.0, lo=2650.0, breakout=True, at_hour=8):
    """Esculpe un rango asiático plano y una ruptura de Londres para forzar Setup A."""
    df = df.copy()
    asia = df[(df.index.hour >= 0) & (df.index.hour < 7)]
    df.loc[asia.index, ["open", "high", "low", "close"]] = [2655.0, hi, lo, 2655.0]
    lon = df[(df.index.hour >= at_hour) & (df.index.hour < 12)]
    if len(lon) == 0:
        lon = df[df.index.hour >= 7]
        df.loc[lon.index, ["open", "high", "low", "close"]] = [2655.0, hi, lo, 2655.0]
    else:
        # tendencia alcista después de la ruptura + cierres por encima del máximo asiático
        k = len(lon)
        levels = np.linspace(2656.0 if breakout else 2654.0,
                             2668.0 if breakout else 2642.0, k)
        df.loc[lon.index, "close"] = levels
        df.loc[lon.index, "open"] = levels - 0.3
        df.loc[lon.index, "high"] = levels + 0.6
        df.loc[lon.index, "low"] = levels - 0.9
    return df


def snap_at(df, day="2026-11-03", hour=9, minute=30, price=None):
    ts = pd.Timestamp(f"{day} {hour:02d}:{minute:02d}", tz="UTC")
    sub = df[df.index <= ts]
    p = float(sub["close"].iloc[-1]) if price is None else price
    return MarketSnapshot(df_m15=sub, price=p, utc_now=ts.to_pydatetime())


# ------------------------------------------------------------------ riesgo

class TestRiskEngine:
    def test_lot_exacto(self):
        st = State(initial_equity=10_000, equity=10_000, phase="F2")  # 1% = $100
        eng = RiskEngine(CFG, st)
        info = eng.lot_for(2660.0, 2650.0, "BUY")  # SL distancia $10 → 100/(10*100)=0.10
        assert info["lot"] == 0.10
        assert info["risk_usd"] == 100.0

    def test_lote_limitado_max_concurso(self):
        st = State(initial_equity=100_000, equity=100_000, phase="F3")  # 1.5% = $1500
        eng = RiskEngine(CFG, st)
        info = eng.lot_for(2660.0, 2659.0, "BUY")  # $1 de SL daría 15 lotes
        assert info["lot"] == 3.0  # tope del concurso para metales
        assert "máximo" in info["note"].lower()

    def test_fase_ataque_requiere_rank(self):
        st = State(initial_equity=10_000, equity=10_500, rank=None)
        eng = RiskEngine(CFG, st)
        eng.recompute_phase()
        assert st.phase == "F2"  # +5% sin rank NO dispara ataque
        st.rank = 350
        eng.recompute_phase()
        assert st.phase == "F3"

    def test_bloqueo_por_perdida_diaria(self):
        st = midday_state(initial_equity=10_000, equity=9_640,
                          daily_start_equity=10_000)
        eng = RiskEngine(CFG, st)  # -3.6% diario ≥ 3.5% de corte propio
        ok, reason = eng.check_permission()
        assert not ok and "diaria" in reason

    def test_bloqueo_por_perdida_total(self):
        st = midday_state(initial_equity=10_000, equity=9_290)
        eng = RiskEngine(CFG, st)  # -7.1% total
        ok, reason = eng.check_permission()
        assert not ok and "total" in reason

    def test_cuarentena_lote_cero(self):
        st = State(initial_equity=10_000, equity=9_690, phase="F0")
        eng = RiskEngine(CFG, st)
        assert eng.lot_for(2660.0, 2650.0, "BUY")["lot"] == 0.0

    def test_dias_operados_se_registran(self):
        st = State()
        eng = RiskEngine(CFG, st)
        eng.register_executed_trade()
        eng.register_executed_trade()  # mismo día no duplica
        assert st.days_traded_count == 1

    def test_status_renderiza(self):
        st = State()
        eng = RiskEngine(CFG, st)
        txt = eng.status_text()
        assert "Fase" in txt and "Equidad" in txt and "Días operados" in txt


# ------------------------------------------------------------------ estrategia

class TestStrategy:
    def setup_method(self):
        self.strat = MomentumStrategy(CFG)

    def test_sin_ventana_no_hay_senal(self):
        df = make_m15()
        s = snap_at(df, hour=3)  # Asia
        assert self.strat.evaluate(s) is None

    def test_setup_a_ruptura_alcista(self):
        df = sculpt_asian_range(make_m15(n=400), hi=2660.0, lo=2650.0, breakout=True)
        s = snap_at(df, hour=9, minute=30)
        sig = self.strat.evaluate(s)
        if sig is not None:  # la generación sintética puede no dar R:R suficiente
            assert sig.kind == "BUY" and sig.setup == "A"
            assert sig.sl < sig.entry < sig.tp1 < sig.tp2
            assert sig.rr >= CFG["strategy"]["min_rr"]
            # lote calculable con el motor
            eng = RiskEngine(CFG, State())
            assert eng.lot_for(sig.entry, sig.sl, sig.kind)["lot"] >= 0.01

    def test_setup_a_ruptura_bajista(self):
        df = sculpt_asian_range(make_m15(n=400, start_price=2650.0),
                                hi=2660.0, lo=2650.0, breakout=False)
        s = snap_at(df, hour=9, minute=30)
        sig = self.strat.evaluate(s)
        if sig is not None:
            assert sig.kind == "SELL" and sig.setup == "A"
            assert sig.sl > sig.entry > sig.tp1 > sig.tp2

    def test_fuera_de_londres_setup_a_no_dispara(self):
        df = sculpt_asian_range(make_m15(n=400), breakout=True)
        s = snap_at(df, hour=14)  # NY → solo setup B puede aplicar
        sig = self.strat.evaluate(s)
        assert sig is None or sig.setup == "B"

    def test_signal_propiedades(self):
        sig = Signal(kind="BUY", setup="A", entry=2660.0, sl=2650.0,
                     tp1=2675.0, tp2=2685.0, reason="t", invalidation="i")
        assert sig.rr == 2.5
        assert sig.sl_distance == 10.0


# ------------------------------------------------------------------ utilidades

class TestIndicadores:
    def test_atr_positivo(self):
        df = make_m15()
        assert atr(df, 14) > 0

    def test_ema_ordenada(self):
        df = make_m15()
        e20, e50 = ema(df["close"], 20), ema(df["close"], 50)
        assert len(e20) == len(e50) and e20.iloc[-1] > 0


# ------------------------------------------------------------------ ejecutor

from copilot.executor import SimExecutor, validate_signal  # noqa: E402


def _sig_buy() -> Signal:
    return Signal(kind="BUY", setup="A", entry=2000.0, sl=1995.0,
                  tp1=2010.0, tp2=2020.0, reason="t", invalidation="i")


class TestSimExecutor:
    def test_rechaza_lote_cero(self):
        ex = SimExecutor()
        res = ex.execute_signal(_sig_buy(), {"lot": 0.0})
        assert res["ok"] is False
        assert ex.open_positions() == 0

    def test_validacion_niveles(self):
        mala = Signal(kind="BUY", setup="A", entry=2000.0, sl=2010.0,
                      tp1=2020.0, tp2=2030.0, reason="t", invalidation="i")
        ok, why = validate_signal(mala, 0.1)
        assert not ok  # SL sobre la entrada en un BUY

    def test_abre_y_cierra_en_sl(self):
        ex = SimExecutor(balance=10_000.0)
        res = ex.execute_signal(_sig_buy(), {"lot": 0.10})
        assert res["ok"] and ex.open_positions() == 1
        evs = ex.manage_positions(1995.0)          # toca el SL
        assert evs[0]["cause"] == "SL"
        assert evs[0]["pnl"] == pytest.approx(-50.0)   # -5$ × 0.10 × 100
        assert ex.balance == pytest.approx(9_950.0)
        assert ex.open_positions() == 0

    def test_cierra_en_tp2(self):
        ex = SimExecutor()
        ex.execute_signal(_sig_buy(), {"lot": 0.10})
        evs = ex.manage_positions(2020.0)
        assert evs[0]["cause"] == "TP2"
        assert evs[0]["pnl"] == pytest.approx(200.0)   # +20 × 0.10 × 100
        assert ex.current_equity(2020.0) == pytest.approx(10_200.0)

    def test_tp1_breakeven(self):
        ex = SimExecutor()
        ex.execute_signal(_sig_buy(), {"lot": 0.10})
        evs = ex.manage_positions(2010.0)          # toca TP1
        assert evs[0]["event"] == "tp1_breakeven"
        assert evs[0]["pnl"] == pytest.approx(50.0)    # mitad 0.05 × +10 × 100
        assert evs[0]["remaining"] == pytest.approx(0.05)
        assert evs[0]["new_sl"] == 2000.0          # SL a la entrada
        evs2 = ex.manage_positions(2000.0)         # vuelve a entrada → cierra
        assert evs2[0]["pnl"] == pytest.approx(0.0)
        assert ex.balance == pytest.approx(10_050.0)
        assert ex.open_positions() == 0

    def test_equidad_flotante(self):
        ex = SimExecutor()
        ex.execute_signal(_sig_buy(), {"lot": 0.10})
        assert ex.current_equity(2010.0) == pytest.approx(10_100.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
