# Logik nach "Opening Range with Breakouts & Targets" (c) LuxAlgo, https://www.luxalgo.com
# Original lizenziert unter CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/).
# Diese Umsetzung nach Python ist eine Bearbeitung und steht deshalb ebenfalls unter CC BY-NC-SA 4.0:
# Namensnennung, nur nicht-kommerziell, Weitergabe unter gleichen Bedingungen. Die MIT-Lizenz des uebrigen
# Projekts gilt fuer diese Datei nicht.
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import session_vwap
from ..session import session_day_key
from .base import WARMUP_FACTOR, Strategy, fmt_price


def _minutes(hhmm: str) -> int:
    hh, mm = str(hhmm).split(":")
    return int(hh) * 60 + int(mm)


# Kurznamen fuer die Erklaerung in der App
MARKET_NAMES = {"Europe/Berlin": "Europa", "Europe/London": "London", "America/New_York": "USA", "Asia/Tokyo": "Tokio", "UTC": "UTC"}


class OpeningRangeBreakoutStrategy(Strategy):
    """Ausbruch aus der Eroeffnungsspanne wie im LuxAlgo-Indikator "Opening Range with Breakouts & Targets".

    - Spanne: Hoch (ORH) und Tief (ORL) der ersten `orb_minutes` ab `orb_start` in `orb_timezone`
      (TradingView-Standard fuer Krypto: 30 Minuten ab 00:00 UTC). Mitte ORM, Breite ORW.
    - Long, wenn der Schlusskurs ORH nach oben kreuzt. Danach erst wieder, wenn ein Schlusskurs unter ORM lag.
      Short gespiegelt an ORL. So sind mehrere Ausbrueche pro Tag moeglich, aber keine Serie im selben Schub.
    - Ziele im Abstand von `orb_target_pct` % der Breite (Ziel 1 = ORH + 50 % ORW usw.), im Chart sichtbar.
    - `orb_bias`: Liegt die heutige ORM ueber der vorigen, gelten Short-Ausbrueche erst ab Ziel 1 unten
      (und umgekehrt). Ohne Bias zaehlt jeder Ausbruch.
    - `orb_sessions` (ergaenzt): mehrere Eroeffnungen, z. B. ["09:00 Europe/Berlin", "09:30 America/New_York"].
      Jede neue Spanne loest die vorige ab: Europa gilt bis zur US-Eroeffnung, die US-Spanne bis zum naechsten
      Morgen. `orb_weekdays`: Spannen nur Montag bis Freitag (Boersentage), am Wochenende gilt die letzte weiter.
    - Ausstieg (ergaenzt, der Indikator hat keinen): Schlusskurs zurueck hinter ORM, also dort, wo der Indikator
      das naechste Signal wieder scharf macht. Dazu Stop-Loss, Ziel und Trailing-Stop wie bei jeder Strategie.
    """

    name = "opening_range"
    buy_reason = "Ausbruch ueber die Eroeffnungsspanne (ORH)"
    sell_reason = "Schlusskurs unter der Mitte der Spanne (ORM)"
    short_reason = "Ausbruch unter die Eroeffnungsspanne (ORL)"
    cover_reason = "Schlusskurs ueber der Mitte der Spanne (ORM)"
    param_grid = {"orb_minutes": [15, 30, 60], "orb_bias": [False, True]}
    chart_lines = [
        ("orh", "ORH (Long-Ausbruch)", "long"),
        ("orl", "ORL (Short-Ausbruch)", "short"),
        ("orm", "Mitte der Spanne", "trend"),
        ("or_t1_up", "Ziel 1 oben", "long"),
        ("or_t1_down", "Ziel 1 unten", "short"),
        ("vwap", "Tages-VWAP", "main"),
    ]

    # ------------------------------------------------------------ Einstellungen
    @property
    def orb_minutes(self) -> int:
        return int(self.p("orb_minutes", 30))

    @property
    def orb_start(self) -> int:
        return _minutes(self.p("orb_start", "00:00"))

    @property
    def orb_timezone(self) -> str:
        return str(self.p("orb_timezone", "UTC"))

    @property
    def sessions(self) -> list[tuple[int, str]]:
        """(Startminute, Zeitzone) je Eroeffnung. Ohne orb_sessions: die eine aus orb_start/orb_timezone."""
        raw = self.p("orb_sessions", None)
        if not raw:
            return [(self.orb_start, self.orb_timezone)]
        out = []
        for item in raw:
            start, _, tz = str(item).strip().partition(" ")
            out.append((_minutes(start), tz.strip() or "UTC"))
        return out

    @property
    def weekdays_only(self) -> bool:
        return bool(self.p("orb_weekdays", False))

    @property
    def target_frac(self) -> float:
        return float(self.p("orb_target_pct", 50.0)) / 100.0

    @property
    def use_bias(self) -> bool:
        return bool(self.p("orb_bias", False))

    @property
    def warmup_bars(self) -> int:
        # mindestens ein ganzer Tag, damit die letzte Spanne und ihr Signalzustand im Live-Fenster liegen.
        # Nur Boersentage: am Montagmorgen liegt die letzte Spanne (Freitag USA) knapp drei Tage zurueck.
        days = 3 if self.weekdays_only else 1
        day = days * (self.bars_per_day or 0) + 20
        return max(300, day, WARMUP_FACTOR * self.cfg.atr_period)

    # ------------------------------------------------------------ Berechnung
    def _add_indicators(self, df: pd.DataFrame) -> None:
        ts = df["timestamp"].astype("int64")
        utc = pd.to_datetime(ts, unit="ms", utc=True)
        in_range = np.zeros(len(df), dtype=bool)
        or_start = np.zeros(len(df), dtype=bool)
        which = np.full(len(df), -1)  # welche Eroeffnung startete die laufende Spanne
        for k, (start_min, tz) in enumerate(self.sessions):
            local = utc.dt.tz_convert(tz)
            open_min = (local.dt.hour * 60 + local.dt.minute).to_numpy()
            # Kerze gehoert zur Spanne, wenn sie in [Start, Start + Dauer) beginnt (auch ueber Mitternacht)
            since = (open_min - start_min) % (24 * 60)
            mine = since < self.orb_minutes
            if self.weekdays_only:
                # Wochentag des Spannen-Beginns (bei Spannen ueber Mitternacht der Vortag)
                begin_day = (local - pd.to_timedelta(since, unit="m")).dt.dayofweek.to_numpy()
                mine &= begin_day < 5
            prev = np.concatenate([[False], mine[:-1]])
            starts = mine & ~prev
            or_start |= starts
            which[starts] = k
            in_range |= mine
        which = pd.Series(np.where(which >= 0, which, np.nan)).ffill().fillna(-1).astype(int).to_numpy()
        session_id = np.cumsum(or_start)  # jede Spanne beginnt eine Session, die bis zur naechsten Spanne reicht

        run_h = df["high"].where(in_range).groupby(session_id).cummax().groupby(session_id).ffill()
        run_l = df["low"].where(in_range).groupby(session_id).cummin().groupby(session_id).ffill()
        valid = (session_id > 0) & (run_h > run_l).to_numpy()
        ready = valid & ~in_range  # Spanne ist fertig
        orm = (run_h + run_l) / 2.0
        orw = run_h - run_l

        # Richtung: Mitte der aktuellen Spanne gegen die der vorigen
        final_orm = orm.groupby(session_id).last()
        prev_orm = pd.Series(session_id).map(final_orm.shift(1)).to_numpy()
        day_dir = np.sign(np.nan_to_num(orm.to_numpy() - prev_orm, nan=0.0))

        close = df["close"].to_numpy()
        rh, rl, rm, rw = run_h.to_numpy(), run_l.to_numpy(), orm.to_numpy(), orw.to_numpy()
        t = self.target_frac
        bias = self.use_bias
        buy = np.zeros(len(df), dtype=bool)
        short = np.zeros(len(df), dtype=bool)
        up_check = down_check = False
        for i in range(len(df)):
            if or_start[i]:
                up_check = down_check = True
            if i == 0 or not ready[i]:
                continue
            c, cp = close[i], close[i - 1]
            # Reihenfolge wie im Indikator: erst wieder scharf machen, dann Ausbruch pruefen
            if c > rm[i] and not down_check:
                down_check = True
            x_down = c < rl[i] and cp >= rl[i - 1]
            x_down2 = c < rl[i] - rw[i] * t and cp >= rl[i - 1] - rw[i - 1] * t
            down = ((day_dir[i] != 1 and x_down) or (day_dir[i] == 1 and x_down2)) if bias else x_down
            if down and down_check:
                short[i] = True
                down_check = False
            if c < rm[i] and not up_check:
                up_check = True
            x_up = c > rh[i] and cp <= rh[i - 1]
            x_up2 = c > rh[i] + rw[i] * t and cp <= rh[i - 1] + rw[i - 1] * t
            up = ((day_dir[i] != -1 and x_up) or (day_dir[i] == -1 and x_up2)) if bias else x_up
            if up and up_check:
                buy[i] = True
                up_check = False

        df["orh"] = run_h.where(ready)
        df["orl"] = run_l.where(ready)
        df["orm"] = orm.where(ready)
        df["or_t1_up"] = (run_h + orw * t).where(ready)
        df["or_t1_down"] = (run_l - orw * t).where(ready)
        df["or_day_dir"] = day_dir
        df["or_in_range"] = in_range
        df["or_session"] = which
        df["orb_buy"] = buy
        df["orb_short"] = short
        df["vwap"] = session_vwap(df, session_day_key(ts, self.session.timezone))

    def signals(self, df):
        ok = (df["atr"] > 0).to_numpy()
        buy = df["orb_buy"].to_numpy() & ok
        sell = (df["close"] < df["orm"]).to_numpy() & df["atr"].notna().to_numpy()
        return buy, sell

    def short_signals(self, df):
        ok = (df["atr"] > 0).to_numpy()
        short = df["orb_short"].to_numpy() & ok
        cover = (df["close"] > df["orm"]).to_numpy() & df["atr"].notna().to_numpy()
        return short, cover

    def explain(self, df, i=-1):
        r = df.iloc[i]
        c = float(r["close"])
        sessions = self.sessions
        k = int(r["or_session"]) if "or_session" in r and r["or_session"] >= 0 else 0
        start_min, tz = sessions[min(k, len(sessions) - 1)]
        start = f"{start_min // 60:02d}:{start_min % 60:02d}"
        market = MARKET_NAMES.get(tz, tz)
        head = f"Eroeffnungsspanne {market}: {self.orb_minutes} Minuten ab {start} ({tz})"
        if len(sessions) > 1:
            head = "Aktiv: " + head
        if r["orh"] != r["orh"]:
            state = " laeuft gerade" if bool(r["or_in_range"]) else ", noch keine fertige Spanne"
            return [head + state + ". Bis sie fertig ist, kein Einstieg."]
        h, low, m = float(r["orh"]), float(r["orl"]), float(r["orm"])
        out = [
            f"{head}: ORH {fmt_price(h)}, ORL {fmt_price(low)}, Mitte {fmt_price(m)}, Kurs {fmt_price(c)}",
            f"Ziel 1: oben {fmt_price(float(r['or_t1_up']))}, unten {fmt_price(float(r['or_t1_down']))} ({self.target_frac * 100:g} % der Breite)",
        ]
        if self.use_bias:
            d = int(r["or_day_dir"])
            out.append("Tagesrichtung: " + ("aufwaerts, Short erst ab Ziel 1 unten" if d == 1 else "abwaerts, Long erst ab Ziel 1 oben" if d == -1 else "neutral"))
        out.append("Long beim Schluss ueber ORH: " + ("Kurs ist darueber" if c > h else f"noch {fmt_price(h - c)} entfernt"))
        if self.cfg.allow_short:
            out.append("Short beim Schluss unter ORL: " + ("Kurs ist darunter" if c < low else f"noch {fmt_price(c - low)} entfernt"))
        out.append("Nach einem Ausbruch erst wieder scharf, wenn der Kurs zurueck hinter die Mitte geht")
        if len(self.sessions) > 1:
            names = ", ".join(f"{MARKET_NAMES.get(tz, tz)} {m // 60:02d}:{m % 60:02d}" for m, tz in self.sessions)
            out.append(f"Eroeffnungen: {names}. Jede neue Spanne loest die vorige ab" + (", nur Montag bis Freitag" if self.weekdays_only else ""))
        return out
