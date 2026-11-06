# -*- coding: utf-8 -*-
"""
feed.py — Conector de datos de mercado para XAUUSD.

Dos fuentes (se eligen automáticamente):
  1. MetaTrader5 (si el bot corre en un Windows/VPS con MT5 instalado) → precios reales del broker.
  2. yfinance (cualquier nube: GitHub Actions, Railway, Render...) → oro spot/futuros como proxy.

Estrategia intradía + señales manuales NO requieren precios tick a tick: velas de 5/15 min son suficientes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("goldpilot.feed")

# Intentar importar MetaTrader5 (solo existe en Windows)
try:  # pragma: no cover
    import MetaTrader5 as mt5  # type: ignore
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False


@dataclass
class MarketSnapshot:
    """Última foto del mercado lista para la estrategia."""
    df_m15: pd.DataFrame        # velas 15 min
    price: float                # último precio
    utc_now: datetime

    @property
    def atr_m15(self) -> float:
        return atr(self.df_m15, 14)


# ---------------------------------------------------------------- utilidades

def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range (Wilder)."""
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat(
        [(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    return float(tr.ewm(alpha=1 / period, min_periods=period).mean().iloc[-1])


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


# ---------------------------------------------------------------- feed yfinance

class YFinanceFeed:
    """Feed universal (nube). Usa 'GC=F' (oro COMEX) como proxy de XAUUSD."""

    SYMBOL = "GC=F"

    def __init__(self) -> None:
        import yfinance as yf  # importación diferida

        self._yf = yf

    def snapshot(self) -> MarketSnapshot:
        df = self._yf.download(
            self.SYMBOL, interval="15m", period="5d",
            progress=False, auto_adjust=True,
        )
        if df is None or df.empty:
            raise RuntimeError("yfinance no devolvió datos (¿red caída?)")

        if isinstance(df.columns, pd.MultiIndex):  # yfinance>=0.2.x multi-ticker
            df.columns = df.columns.get_level_values(0)

        df = df.rename(
            columns={"Open": "open", "High": "high", "Low": "low",
                     "Close": "close", "Volume": "volume"}
        ).dropna(subset=["open", "high", "low", "close"])

        # yfinance da índices naive en UTC para intradía → normalizar a tz-aware
        idx = df.index
        if idx.tz is None:
            df.index = idx.tz_localize("UTC")
        else:
            df.index = idx.tz_convert("UTC")

        price = float(df["close"].iloc[-1])
        return MarketSnapshot(df_m15=df, price=price, utc_now=datetime.now(timezone.utc))


# ---------------------------------------------------------------- feed MT5 (opcional)

class MT5Feed:
    """Feed real desde un terminal MT5 (Windows/VPS). Requiere MT5 instalado y logueado."""

    SYMBOL = "XAUUSD"

    def __init__(self, login: int | None = None, password: str | None = None,
                 server: str | None = None) -> None:
        if not MT5_AVAILABLE:  # pragma: no cover
            raise RuntimeError("MetaTrader5 no está instalado en este entorno")
        ok = mt5.initialize(
            login=login, password=password, server=server
        ) if login else mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize falló: {mt5.last_error()}")
        # algunos brokers usan GOLD / XAUUSD.a / XAUUSDm
        if mt5.symbol_select(self.SYMBOL, True) is False:
            for alt in ("GOLD", "XAUUSD.a", "XAUUSDm", "XAUUSD.m"):
                if mt5.symbol_select(alt, True):
                    self.SYMBOL = alt
                    break

    def snapshot(self) -> MarketSnapshot:
        rates = mt5.copy_rates_from_pos(self.SYMBOL, mt5.TIMEFRAME_M15, 0, 400)
        if rates is None or len(rates) < 60:
            raise RuntimeError("MT5 no devolvió velas suficientes")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").rename(
            columns={"open": "open", "high": "high", "low": "low", "close": "close"}
        )
        tick = mt5.symbol_info_tick(self.SYMBOL)
        price = float((tick.bid + tick.ask) / 2) if tick else float(df["close"].iloc[-1])
        return MarketSnapshot(df_m15=df, price=price, utc_now=datetime.now(timezone.utc))


# ---------------------------------------------------------------- fábrica

def build_feed(cfg: dict):
    """Devuelve MT5Feed si el config lo pide y es posible; si no, YFinanceFeed."""
    mode = str(cfg.get("feed", {}).get("mode", "auto")).lower()
    if mode in ("mt5", "auto") and MT5_AVAILABLE:
        log.info("Usando feed MetaTrader5 (precios reales del broker)")
        return MT5Feed(
            login=cfg.get("feed", {}).get("mt5_login"),
            password=cfg.get("feed", {}).get("mt5_password"),
            server=cfg.get("feed", {}).get("mt5_server"),
        )
    log.info("Usando feed yfinance (proxy GC=F del oro)")
    return YFinanceFeed()
