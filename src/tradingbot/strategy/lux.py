# Signal-Logik nach "SuperTrend AI (Clustering)" (c) LuxAlgo, https://www.luxalgo.com, Lizenz CC BY-NC-SA 4.0
# (https://creativecommons.org/licenses/by-nc-sa/4.0/), und den "Reversal Zones" (Mean Reversion Channel mit
# Ehlers-SuperSmoother) aus einem vom Nutzer gelieferten Pine-Script "Lux Algo Signals & Overlays". Die X-Ausstiege
# stammen aus "WaveTrend 3D" (c) jdehorty (MPL 2.0) samt Gauss-Kernel seiner Bibliothek "KernelFunctions".
# Diese Umsetzung nach Python ist eine Bearbeitung: Namensnennung, nur nicht-kommerziell, Weitergabe unter gleichen
# Bedingungen. Die MIT-Lizenz des uebrigen Projekts gilt fuer diese Datei nicht.
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..indicators import adx, choppiness, ema
from .base import WARMUP_FACTOR, Strategy, fmt_price


def _rma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    acc = np.nan
    for i, v in enumerate(x):
        if v != v:
            continue
        acc = v if acc != acc else acc + (v - acc) / n
        out[i] = acc
    return out


def _true_range(h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev = np.concatenate([[np.nan], c[:-1]])
    tr = np.maximum(h - lo, np.maximum(np.abs(h - prev), np.abs(lo - prev)))
    tr[0] = h[0] - lo[0]
    return tr


def supersmoother(src: np.ndarray, length: int) -> np.ndarray:
    """Ehlers SuperSmoother wie im Pine-Script (erste Werte mit dem Eingang aufgefuellt)."""
    a1 = math.exp(-math.sqrt(2) * math.pi / length)
    b1 = 2 * a1 * math.cos(math.sqrt(2) * math.pi / length)
    c3 = -a1 * a1
    c2 = b1
    c1 = 1 - c2 - c3
    out = np.zeros(len(src))
    for i in range(len(src)):
        s1 = out[i - 1] if i >= 1 else src[max(i - 1, 0)]
        s2 = out[i - 2] if i >= 2 else src[max(i - 2, 0)]
        out[i] = c1 * src[i] + c2 * s1 + c3 * s2
    return out


def _kmeans3(values: np.ndarray, max_iter: int = 250) -> list[np.ndarray]:
    """k-Means mit 3 Gruppen auf einer Zahlenreihe, Start an den Quartilen (wie im Pine-Script). Gibt die
    Indizes je Gruppe zurueck: [schwach, mittel, stark]."""
    cents = list(np.percentile(values, [25, 50, 75]))
    groups = [np.array([], int)] * 3
    for _ in range(max_iter + 1):
        dist = np.abs(values[:, None] - np.array(cents)[None, :])
        idx = dist.argmin(axis=1)
        groups = [np.flatnonzero(idx == g) for g in range(3)]
        new = [values[g].mean() if len(g) else float("nan") for g in groups]
        if new == cents:
            break
        cents = new
    return groups


def _pct(sorted_vals: list, q: float) -> float:
    pos = (len(sorted_vals) - 1) * q
    lo_i = int(pos)
    hi_i = min(lo_i + 1, len(sorted_vals) - 1)
    return sorted_vals[lo_i] + (sorted_vals[hi_i] - sorted_vals[lo_i]) * (pos - lo_i)


def _kmeans3_list(values: list, max_iter: int = 250) -> list[int]:
    """Wie _kmeans3, aber fuer kurze Listen in reinem Python. Gibt nur die Indizes der besten Gruppe zurueck."""
    sv = sorted(values)
    cents = [_pct(sv, 0.25), _pct(sv, 0.5), _pct(sv, 0.75)]
    best: list[int] = []
    last = None
    for _ in range(max_iter + 1):
        groups: list[list[int]] = [[], [], []]
        for j, v in enumerate(values):
            d0, d1, d2 = abs(v - cents[0]), abs(v - cents[1]), abs(v - cents[2])
            g = 0 if d0 <= d1 and d0 <= d2 else 1 if d1 <= d2 else 2
            groups[g].append(j)
        best = groups[2]
        # Gleiche Zuordnung wie zuvor -> gleiche Mittelpunkte, fertig. (Ein Vergleich der Mittelpunkte haengt bei
        # leeren Gruppen fest, weil NaN nie gleich NaN ist; das Ergebnis ist dasselbe.)
        if groups == last:
            break
        last = groups
        cents = [sum(values[j] for j in g) / len(g) if g else float("nan") for g in groups]
    return best


def gaussian_kernel(src: np.ndarray, lookback: int, start_at_bar: int) -> np.ndarray:
    """Gauss-Kernel wie in jdehortys KernelFunctions: gewichteter Mittelwert der Werte i = 0 .. start_at_bar + 1
    Kerzen zurueck, Gewicht exp(-i^2 / (2 * lookback^2)). Nur Vergangenheit, kein Neuzeichnen."""
    n = len(src)
    out = np.full(n, np.nan)
    m = start_at_bar + 2
    w = np.exp(-np.arange(m) ** 2 / (2.0 * lookback ** 2))
    for t in range(m - 1, n):
        win = src[t - m + 1:t + 1][::-1]
        if np.isnan(win).any():
            continue
        out[t] = float((win * w).sum() / w.sum())
    return out


def _dual_pole(src: np.ndarray, lookback: float) -> np.ndarray:
    omega = -99 * math.pi / (70 * lookback)
    alpha = math.exp(omega)
    beta = -alpha * alpha
    gamma = math.cos(omega) * 2 * alpha
    delta = 1 - gamma - beta
    out = np.zeros(len(src))
    for i in range(len(src)):
        x = src[i]
        x1 = src[i - 1] if i >= 1 and src[i - 1] == src[i - 1] else x
        avg = 0.5 * ((x if x == x else 0.0) + (x1 if x1 == x1 else 0.0))
        f1 = out[i - 1] if i >= 1 else 0.0
        f2 = out[i - 2] if i >= 2 else 0.0
        out[i] = delta * avg + gamma * f1 + beta * f2
    return out


def wavetrend3d(close: np.ndarray, lookback: int = 20, qm_len: int = 50) -> dict:
    """Die drei Oszillatoren (schnell, normal, langsam) aus WaveTrend 3D mit den Standardwerten des Scripts."""
    deriv = close - np.concatenate([[np.nan, np.nan], close[:-2]])
    sq = pd.Series(deriv ** 2).rolling(qm_len).sum().to_numpy() / qm_len
    qm = np.sqrt(np.nan_to_num(sq, nan=0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        nd = np.where(qm > 0, deriv / qm, np.nan)
    th = -1 + 2 / (1 + np.exp(-2 * nd))
    th = np.where(np.isnan(th), 0.0, th)
    fast = 0.75 * _dual_pole(th, 0.45 * lookback)
    normal = 1.0 * _dual_pole(th, 1.0 * lookback)
    slow = 1.75 * _dual_pole(th, 2.5 * lookback)
    return {"fast": fast, "normal": normal, "slow": slow}


def trigger_waves(close: np.ndarray, div_threshold: int = 30, size_pct: float = 40.0) -> tuple[np.ndarray, np.ndarray]:
    """X-Marken des Scripts: kleinere Folgewelle (Divergenz) kurz nach einer Welle, bestaetigt vom Gauss-Kernel.
    Gibt (baerisch = Long schliessen, bullisch = Short schliessen) zurueck."""
    w = wavetrend3d(close)
    fast, normal, slow = w["fast"], w["normal"], w["slow"]
    pf, pn = np.concatenate([[np.nan], fast[:-1]]), np.concatenate([[np.nan], normal[:-1]])
    bear_cross = (pf >= pn) & (fast < normal) & (normal > 0)
    bull_cross = (pf <= pn) & (fast > normal) & (normal < 0)
    yhat0 = gaussian_kernel(normal, 6, 6)
    factor = size_pct / 100.0
    n = len(close)
    bear = np.zeros(n, bool)
    bull = np.zeros(n, bool)
    last_bear = last_bull = None          # Wert bei der letzten Kreuzung (fuer valuewhen(..., 1))
    last_bear_i = last_bull_i = None      # Kerze der letzten Kreuzung (fuer barssince(cross[1]))
    for i in range(n):
        if bear_cross[i]:
            in_zone = last_bear_i is not None and i - last_bear_i <= div_threshold
            smaller = last_bear is not None and normal[i] < last_bear * factor
            if in_zone and smaller and yhat0[i] == yhat0[i] and yhat0[i] < slow[i]:
                bear[i] = True
            last_bear, last_bear_i = normal[i], i
        if bull_cross[i]:
            in_zone = last_bull_i is not None and i - last_bull_i <= div_threshold
            smaller = last_bull is not None and normal[i] > last_bull * factor
            if in_zone and smaller and yhat0[i] == yhat0[i] and yhat0[i] > slow[i]:
                bull[i] = True
            last_bull, last_bull_i = normal[i], i
    return bear, bull


class SuperTrendAIStrategy(Strategy):
    """SuperTrend AI: viele Supertrends (Faktor `st_min` bis `st_max`, Schritt 0,5) laufen mit. Fortlaufend werden
    sie per k-Means nach ihrer juengsten Leistung in drei Gruppen geteilt; der mittlere Faktor der besten Gruppe
    bestimmt den eigentlichen Supertrend. Long beim Wechsel nach oben, Short beim Wechsel nach unten.

    `st_sensitivity` wie im Indikator: Faktoren von max(Empfindlichkeit-4, 1) bis Empfindlichkeit. `st_min_strength`
    (1-4) filtert schwache Signale ueber den Leistungsindex. Stop an der Supertrend-Linie beim Einstieg."""

    name = "supertrend_ai"
    buy_reason = "SuperTrend AI dreht nach oben"
    sell_reason = "SuperTrend AI dreht nach unten"
    short_reason = "SuperTrend AI dreht nach unten"
    cover_reason = "SuperTrend AI dreht nach oben"
    stop_columns = ("st_stop_long", "st_stop_short")
    trail_line = "st_line"  # wie im Indikator: Trailing-Stop an der SuperTrend-Linie
    exit_columns = ("st_x_exit_long", "st_x_exit_short")
    default_target_rr = 0.0
    own_exits = True  # Stop an der Linie, Ausstieg ueber Linie und X-Marken, kein festes Ziel
    param_grid = {"st_sensitivity": [5, 10, 18], "st_atr_len": [10], "st_min_strength": [1, 3], "target_rr": [0.0, 2.0]}
    chart_lines = [
        ("st_line", "SuperTrend AI (Trailing Stop)", "main"),
        ("st_ama", "AI Moving Average", "trend"),
        ("rz_up_outer", "Reversal Zone oben (aussen)", "short"),
        ("rz_up_band", "Reversal Zone oben", "short"),
        ("rz_up_inner", "Reversal Zone oben (innen)", "short"),
        ("rz_lo_inner", "Reversal Zone unten (innen)", "long"),
        ("rz_lo_band", "Reversal Zone unten", "long"),
        ("rz_lo_outer", "Reversal Zone unten (aussen)", "long"),
    ]
    # wie im TradingView-Bild: Trailing Stoploss und Reversal Zones an, AI Moving Average aus
    chart_visible = ("st_line", "rz_up_outer", "rz_up_band", "rz_up_inner", "rz_lo_inner", "rz_lo_band", "rz_lo_outer")
    line_direction = {"st_line": "st_dir"}
    chart_markers = [("st_flip_up", "long", "st_strength"), ("st_flip_dn", "short", "st_strength"),
                     ("st_x_exit_long", "exit_long", None), ("st_x_exit_short", "exit_short", None)]

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return params.get("stop_atr_mult", 2.0) == 2.0 and params.get("take_profit_atr_mult", 0.0) == 0.0

    @property
    def warmup_bars(self) -> int:
        # 600 Indikator-Kerzen: Clustering eingeschwungen, Reversal Zones (SuperSmoother 100) im Chart voll sichtbar.
        # Auf groesseren Kerzen (st_timeframe) entsprechend mehr Bot-Kerzen.
        return max(WARMUP_FACTOR * int(self.p("st_atr_len", 10)), 600) * self.htf_factor

    def _supertrend(self, h: np.ndarray, lo: np.ndarray, c: np.ndarray):
        """SuperTrend AI auf diesen Kerzen: (Linie, AMA, Staerke, Richtung 1/0)."""
        n = len(c)
        hl2 = (h + lo) / 2
        atr = _rma(_true_range(h, lo, c), int(self.p("st_atr_len", 10)))
        sens = float(self.p("st_sensitivity", 5))
        lo_f, hi_f = max(sens - 4, 1.0), min(sens, 26.0)
        # Reine Python-Schleife: bei nur ~9 Faktoren ist das deutlich schneller als numpy je Kerze
        factors = [float(f) for f in np.arange(lo_f, hi_f + 1e-9, 0.5)]
        k = len(factors)
        alpha = 2 / (10 + 1)
        cl, hl, at = c.tolist(), hl2.tolist(), atr.tolist()
        up_ = [hl[0]] * k
        dn_ = [hl[0]] * k
        trend = [0] * k
        output = [None] * k
        perf = [0.0] * k

        target = float("nan")
        upper = lower = hl[0]
        os = 0
        ama = float("nan")
        den = float("nan")
        st_line = [float("nan")] * n
        ama_line = [float("nan")] * n
        strength = [0] * n
        osv = [0] * n
        for i in range(n):
            ci = cl[i]
            cp = cl[i - 1] if i else ci
            dabs = abs(ci - cp)
            den = dabs if den != den else den + alpha * (dabs - den)  # EMA(|Aenderung|, 10)
            a = at[i]
            if a != a:
                continue
            h2 = hl[i]
            chg = ci - cp
            for f in range(k):
                fa = a * factors[f]
                u, d = h2 + fa, h2 - fa
                tr_ = 1 if ci > up_[f] else 0 if ci < dn_[f] else trend[f]
                up_[f] = min(u, up_[f]) if cp < up_[f] else u
                dn_[f] = max(d, dn_[f]) if cp > dn_[f] else d
                o = output[f]
                diff = 0.0 if o is None or cp == o else (1.0 if cp > o else -1.0)
                perf[f] += alpha * (chg * diff - perf[f])
                trend[f] = tr_
                output[f] = dn_[f] if tr_ == 1 else up_[f]
            best = _kmeans3_list(perf)
            if best:
                target = sum(factors[j] for j in best) / len(best)
                pb = sum(perf[j] for j in best) / len(best)
                perf_idx = max(pb, 0.0) / den if den else 0.0
            else:
                perf_idx = 0.0
            if target != target:
                continue
            u = h2 + a * target
            d = h2 - a * target
            upper = min(u, upper) if cp < upper else u
            lower = max(d, lower) if cp > lower else d
            os = 1 if ci > upper else 0 if ci < lower else os
            ts = lower if os else upper
            ama = ts if ama != ama else ama + perf_idx * (ts - ama)
            st_line[i], ama_line[i], osv[i] = ts, ama, os
            p10 = int(perf_idx * 10)
            strength[i] = 1 if p10 < 2 else 2 if p10 < 4 else 3 if p10 < 5 else 4
        st_line, ama_line = np.array(st_line), np.array(ama_line)
        strength, osv = np.array(strength), np.array(osv)
        return st_line, ama_line, strength, osv

    @property
    def htf_factor(self) -> int:
        """Wie viele Bot-Kerzen eine Indikator-Kerze umfasst (`st_timeframe`, z. B. 5m auf dem 1m-Bot = 5)."""
        tf = self.p("st_timeframe", None)
        if not tf or not self.timeframe_ms:
            return 1
        from ..data import timeframe_seconds

        return max(1, timeframe_seconds(str(tf)) * 1000 // self.timeframe_ms)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        n = len(df)
        mult = self.htf_factor
        if mult > 1:
            # Indikator auf groesseren Kerzen (z. B. 5m), dann auf die Bot-Kerzen uebertragen. Jede Bot-Kerze sieht nur
            # die letzte ABGESCHLOSSENE grosse Kerze: die 5m-Kerze ab 10:00 gilt ab der 1m-Kerze 10:04 (Schluss 10:05).
            base = self.timeframe_ms
            big = base * mult
            ts = df["timestamp"].to_numpy(dtype="int64")
            bucket = ts // big * big
            g = pd.DataFrame({"b": bucket, "h": df["high"].to_numpy(), "l": df["low"].to_numpy(), "c": df["close"].to_numpy()})
            agg = g.groupby("b", sort=True).agg(h=("h", "max"), l=("l", "min"), c=("c", "last"))
            hb, lb, cb = (agg[k].to_numpy(dtype=float) for k in ("h", "l", "c"))
            line_b, ama_b, str_b, os_b = self._supertrend(hb, lb, cb)
            zones_b = self._zones(hb, lb, cb)
            xb_bear, xb_bull = trigger_waves(cb)
            pos = {int(b): k for k, b in enumerate(agg.index.to_numpy())}
            if self.cfg.realtime_signals:
                avail = ts // big * big  # Echtzeit: die laufende grosse Kerze zaehlt sofort
            else:
                avail = (ts + base) // big * big - big  # Beginn der letzten abgeschlossenen grossen Kerze
            idx = np.array([pos.get(int(b), -1) for b in avail])
            ok = idx >= 0

            def take(arr, fill=np.nan):
                out = np.full(n, fill, dtype=float)
                out[ok] = np.asarray(arr, dtype=float)[idx[ok]]
                return out

            st_line, ama_line = take(line_b), take(ama_b)
            strength, osv = take(str_b, 0).astype(int), take(os_b, 0).astype(int)
            zones = {k: take(v) for k, v in zones_b.items()}
            # X nur an der Bot-Kerze, an der die grosse Kerze schliesst (dort wechselt der Index)
            prev_idx = np.concatenate([[-1], idx[:-1]])
            fresh = ok & (idx != prev_idx) if not self.cfg.realtime_signals else ok
            x_bear, x_bull = fresh & take(xb_bear, 0).astype(bool), fresh & take(xb_bull, 0).astype(bool)
        else:
            h, lo, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
            st_line, ama_line, strength, osv = self._supertrend(h, lo, c)
            zones = self._zones(h, lo, c)
            x_bear, x_bull = trigger_waves(c)

        prev = np.concatenate([[0], osv[:-1]])
        valid = ~np.isnan(st_line) & ~np.isnan(np.concatenate([[np.nan], st_line[:-1]]))
        min_s = int(self.p("st_min_strength", 1))
        valid &= np.arange(n) >= self.warmup_bars  # erst nach dem Einrechnen handeln (wie Bot und Backtest)
        flip_up = valid & (osv > prev)
        flip_dn = valid & (osv < prev)
        df["st_line"], df["st_ama"], df["st_strength"] = st_line, ama_line, strength
        df["st_dir"] = np.where(np.isnan(st_line), np.nan, osv)
        # Marken fuer den Chart: jeder Richtungswechsel, auch im Vorlauf (zum Handeln zaehlt nur flip_up/flip_dn)
        seen = ~np.isnan(st_line) & ~np.isnan(np.concatenate([[np.nan], st_line[:-1]]))
        df["st_flip_up"], df["st_flip_dn"] = seen & (osv > prev), seen & (osv < prev)
        for col, val in zones.items():
            df[col] = val
        trend_long, trend_short = self._trend_filter(df)
        df["st_buy"] = flip_up & (strength >= min_s) & trend_long
        df["st_short"] = flip_dn & (strength >= min_s) & trend_short
        use_x = bool(self.p("st_x_exit", True))
        warm_ok = np.arange(n) >= self.warmup_bars
        df["st_x_exit_long"] = x_bear & warm_ok if use_x else np.zeros(n, bool)
        df["st_x_exit_short"] = x_bull & warm_ok if use_x else np.zeros(n, bool)
        df["st_sell"] = flip_dn | df["st_x_exit_long"].to_numpy()
        df["st_cover"] = flip_up | df["st_x_exit_short"].to_numpy()
        df["st_stop_long"] = np.where(df["st_buy"], st_line, np.nan)
        df["st_stop_short"] = np.where(df["st_short"], st_line, np.nan)

    def _trend_filter(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Einstieg nur in klaren Trends (Ausstiege bleiben immer erlaubt). Alles 0 = aus:
        `st_adx_min` ADX(14) mindestens so hoch, `st_chop_max` Choppiness(14) hoechstens so hoch,
        `st_trend_ema` Long nur ueber, Short nur unter dieser EMA (in Bot-Kerzen),
        `st_min_stop_pct` Linie (= Stop) mindestens so viele Prozent vom Kurs entfernt."""
        n = len(df)
        ok = np.ones(n, dtype=bool)
        adx_min = float(self.p("st_adx_min", 0) or 0)
        if adx_min > 0:
            a = adx(df, 14).to_numpy()
            df["st_adx"] = a
            ok &= np.nan_to_num(a) >= adx_min
        chop_max = float(self.p("st_chop_max", 0) or 0)
        if chop_max > 0:
            ch = choppiness(df, 14).to_numpy()
            df["st_chop"] = ch
            ok &= np.nan_to_num(ch, nan=100.0) <= chop_max
        min_dist = float(self.p("st_min_stop_pct", 0) or 0)
        if min_dist > 0 and "st_line" in df.columns:
            # Stop (Linie) zu nah am Kurs: die Gebuehren waeren im Verhaeltnis zum Risiko zu hoch
            c, line = df["close"].to_numpy(), df["st_line"].to_numpy()
            ok &= np.nan_to_num(np.abs(c - line) / c * 100.0) >= min_dist
        long_ok, short_ok = ok.copy(), ok.copy()
        span = int(self.p("st_trend_ema", 0) or 0)
        if span > 0:
            e = ema(df["close"], span).to_numpy()
            df["st_trend"] = e
            c = df["close"].to_numpy()
            long_ok &= c > e
            short_ok &= c < e
        return long_ok, short_ok

    def _zones(self, h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> dict:
        """Reversal Zones wie im Indikator (SuperSmoother 100 auf hlc3, Breite aus geglaetteter True Range)."""
        rz_len = int(self.p("rz_length", 100))
        mean = supersmoother((h + lo + c) / 3, rz_len)
        rng = supersmoother(_true_range(h, lo, c), rz_len)
        up2, lo2 = mean + rng * math.pi * 2.415, mean - rng * math.pi * 2.415
        warm = np.arange(len(c)) < 2 * rz_len
        return {col: np.where(warm, np.nan, val) for col, val in (
            ("rz_up_outer", up2 + 2 * rng), ("rz_up_band", up2), ("rz_up_inner", up2 - 2 * rng),
            ("rz_lo_inner", lo2 + 2 * rng), ("rz_lo_band", lo2), ("rz_lo_outer", lo2 - 2 * rng))}

    def signals(self, df):
        return df["st_buy"].to_numpy(), df["st_sell"].to_numpy()

    def short_signals(self, df):
        return df["st_short"].to_numpy(), df["st_cover"].to_numpy()

    def decide(self, df, i=-1, context=None):
        from ..models import Signal

        d = super().decide(df, i, context)
        idx = i if i >= 0 else len(df) + i
        if d.signal == Signal.SELL and bool(df["st_x_exit_long"].iloc[idx]) and not bool(df["st_flip_dn"].iloc[idx]):
            d.reason = "X-Marke: Umkehrwarnung (WaveTrend), Long geschlossen"
        if d.signal == Signal.COVER and bool(df["st_x_exit_short"].iloc[idx]) and not bool(df["st_flip_up"].iloc[idx]):
            d.reason = "X-Marke: Umkehrwarnung (WaveTrend), Short geschlossen"
        return d

    def explain(self, df, i=-1):
        r = df.iloc[i]
        if r["st_line"] != r["st_line"]:
            return ["SuperTrend AI rechnet sich noch ein"]
        side = "Long" if r["close"] > r["st_line"] else "Short"
        return [f"SuperTrend AI: Richtung {side}, Linie {fmt_price(float(r['st_line']))}, Signalstaerke {int(r['st_strength'])} von 4"]


class ReversalZonesStrategy(Strategy):
    """Reversal Zones: Mittelwert-Kanal mit Ehlers-SuperSmoother (Mitte aus hlc3, Breite aus geglaetteter True Range).
    Die aeusseren Baender (Mitte +- Breite x pi x 2,415) samt +-2 Breiten Rand sind die Umkehrzonen.

    `rz_entry`: `touch` = Einstieg, sobald eine Kerze in die Zone sticht und gegen die Zone schliesst (Long: Docht
    in die untere Zone, Schluss ueber der Eroeffnung); `reclaim` = erst wenn der Schluss wieder innerhalb des
    aeusseren Bands liegt. Stop am aeusseren Rand der Zone, Ziel an der Mittellinie oder `target_rr` x Risiko."""

    name = "reversal_zones"
    buy_reason = "Umkehr aus der unteren Zone"
    sell_reason = "Mittellinie erreicht"
    short_reason = "Umkehr aus der oberen Zone"
    cover_reason = "Mittellinie erreicht"
    stop_columns = ("rz_stop_long", "rz_stop_short")
    param_grid = {"rz_length": [50, 100, 200], "rz_entry": ["touch", "reclaim"], "target_rr": [0.0, 1.5]}
    chart_lines = [("rz_mean", "Mittellinie", "trend"), ("rz_upper", "Obere Umkehrzone", "short"), ("rz_lower", "Untere Umkehrzone", "long")]

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return params.get("stop_atr_mult", 2.0) == 2.0 and params.get("take_profit_atr_mult", 0.0) == 0.0

    @property
    def warmup_bars(self) -> int:
        return max(3 * int(self.p("rz_length", 100)), WARMUP_FACTOR * self.cfg.atr_period)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        h, lo, c, o = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close", "open"))
        n_len = int(self.p("rz_length", 100))
        src = (h + lo + c) / 3
        mean = supersmoother(src, n_len)
        rng = supersmoother(_true_range(h, lo, c), n_len)
        mult2 = math.pi * 2.415
        up2, lo2 = mean + rng * mult2, mean - rng * mult2
        inner_up, outer_up = up2 - 2 * rng, up2 + 2 * rng   # Zone oben: upband2_9 .. upband2_1
        inner_lo, outer_lo = lo2 + 2 * rng, lo2 - 2 * rng
        reclaim = self.p("rz_entry", "touch") == "reclaim"
        in_low = lo <= inner_lo
        in_high = h >= inner_up
        if reclaim:
            buy = in_low & (c > lo2) & (c > o)
            short = in_high & (c < up2) & (c < o)
        else:
            buy = in_low & (c > o)
            short = in_high & (c < o)
        warm = np.arange(len(df)) >= self.warmup_bars
        df["rz_mean"], df["rz_upper"], df["rz_lower"] = mean, inner_up, inner_lo
        df["rz_buy"], df["rz_short"] = buy & warm, short & warm
        df["rz_sell"], df["rz_cover"] = (c >= mean) & warm, (c <= mean) & warm
        df["rz_stop_long"] = np.where(df["rz_buy"], np.minimum(outer_lo, lo), np.nan)
        df["rz_stop_short"] = np.where(df["rz_short"], np.maximum(outer_up, h), np.nan)

    def signals(self, df):
        ok = (df["atr"] > 0).to_numpy()
        return df["rz_buy"].to_numpy() & ok, df["rz_sell"].to_numpy()

    def short_signals(self, df):
        ok = (df["atr"] > 0).to_numpy()
        return df["rz_short"].to_numpy() & ok, df["rz_cover"].to_numpy()

    def explain(self, df, i=-1):
        r = df.iloc[i]
        return [f"Reversal Zones: Mitte {fmt_price(float(r['rz_mean']))}, Zone unten ab {fmt_price(float(r['rz_lower']))}, oben ab {fmt_price(float(r['rz_upper']))}"]
