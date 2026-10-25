from __future__ import annotations

import numpy as np
import pandas as pd

from .base import WARMUP_FACTOR, Strategy, fmt_price


class SmartMoneyStrategy(Strategy):
    """Smart-Money-Konzepte als feste Regeln: Marktstruktur, Order Blocks, Fair Value Gaps, Liquidity Sweeps.

    - Swings: Hoch bzw. Tief, das `smc_swing` Kerzen links und rechts uebertrifft. Es steht erst `smc_swing` Kerzen
      spaeter fest und gilt erst ab dann (kein Blick in die Zukunft).
    - Struktur: Schlusskurs ueber dem letzten Swing-Hoch = Bruch nach oben (BOS), Richtung Long. Unten gespiegelt.
    - Order Block: beim Bruch nach oben die letzte rote Kerze davor (Hoch bis Tief), nach unten die letzte gruene.
    - Fair Value Gap: Tief der Kerze ueber dem Hoch von vor zwei Kerzen (mind. `smc_fvg_atr` x ATR), unten gespiegelt.
    - Einstieg `zone`: In Richtung der Struktur zurueck in eine unbenutzte Zone, die Kerze schliesst wieder darueber
      (Long) bzw. darunter (Short). Stop knapp hinter der Zone.
    - Einstieg `sweep` (frueh gegen den Trend): Docht ueber das letzte Swing-Hoch, Schluss wieder darunter -> Short.
      Gespiegelt Long. Stop knapp hinter dem Docht. Das ist die frueheste greifbare Umkehr, vor jedem Indikator.
    - Ziel: `target_rr` x Risiko. Ausstieg ausserdem beim Strukturbruch gegen die Position.
    """

    name = "smc"
    buy_reason = "Smart Money: Long"
    sell_reason = "Strukturbruch nach unten"
    short_reason = "Smart Money: Short"
    cover_reason = "Strukturbruch nach oben"
    stop_columns = ("smc_stop_long", "smc_stop_short")
    default_target_rr = 2.0
    param_grid = {
        "smc_entry": ["zone", "sweep", "both"],
        "smc_swing": [3, 5],
        "target_rr": [1.5, 2.5],
        "smc_trend_ema": [0, 200],
    }
    chart_lines = [
        ("smc_sh", "Letztes Swing-Hoch", "short"),
        ("smc_sl", "Letztes Swing-Tief", "long"),
        ("smc_zone_top", "Zone oben", "trend"),
        ("smc_zone_bot", "Zone unten", "trend"),
    ]

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        # Stop und Ziel kommen aus der Struktur: die ATR-Ausstiegsvarianten des Rasters waeren nur Doppelungen
        return params.get("stop_atr_mult", 2.0) == 2.0 and params.get("take_profit_atr_mult", 0.0) == 0.0

    @property
    def swing(self) -> int:
        return int(self.p("smc_swing", 3))

    @property
    def mode(self) -> str:
        return str(self.p("smc_entry", "both"))

    @property
    def warmup_bars(self) -> int:
        trend = int(self.p("smc_trend_ema", 0) or 0)
        return max(WARMUP_FACTOR * self.cfg.atr_period, 3 * trend, 100)

    def _add_indicators(self, df: pd.DataFrame) -> None:
        n = len(df)
        o, h, lo, c = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
        atr = df["atr"].to_numpy(dtype=float)
        L = self.swing
        fvg_min = float(self.p("smc_fvg_atr", 0.3))
        max_age = int(self.p("smc_zone_age", 100))
        buf = float(self.p("smc_stop_buffer_atr", 0.1))
        trend_n = int(self.p("smc_trend_ema", 0) or 0)
        ema = df["close"].ewm(span=trend_n, adjust=False).mean().to_numpy() if trend_n else None
        mode = self.mode

        buy = np.zeros(n, bool)
        short = np.zeros(n, bool)
        sell = np.zeros(n, bool)
        cover = np.zeros(n, bool)
        stop_l = np.full(n, np.nan)
        stop_s = np.full(n, np.nan)
        sh_line = np.full(n, np.nan)
        sl_line = np.full(n, np.nan)
        z_top = np.full(n, np.nan)
        z_bot = np.full(n, np.nan)

        sh = sl = None          # letztes bestaetigtes Swing-Hoch/-Tief (Kurs)
        sh_broken = sl_broken = True
        trend = 0               # +1 nach Bruch nach oben, -1 nach unten
        bull: list[list] = []   # Zonen: [oben, unten, Entstehungs-Kerze]
        bear: list[list] = []

        for i in range(n):
            a = atr[i]
            # 1) Swing an Kerze j = i - L steht jetzt fest
            j = i - L
            if j >= L:
                win_h = h[j - L:i + 1]
                win_l = lo[j - L:i + 1]
                if h[j] == win_h.max() and (win_h == h[j]).sum() == 1:
                    sh, sh_broken = h[j], False
                if lo[j] == win_l.min() and (win_l == lo[j]).sum() == 1:
                    sl, sl_broken = lo[j], False
            if a != a or a <= 0:
                continue
            trend_ok_long = ema is None or c[i] > ema[i]
            trend_ok_short = ema is None or c[i] < ema[i]

            # 2) Zonen pflegen: alt oder durchbrochen -> weg
            bull = [z for z in bull if i - z[2] <= max_age and c[i] >= z[1]]
            bear = [z for z in bear if i - z[2] <= max_age and c[i] <= z[0]]

            # 3) Einstiege (nur mit Zonen, die vor dieser Kerze entstanden sind)
            if mode in ("zone", "both"):
                if trend == 1 and trend_ok_long:
                    for z in reversed(bull):
                        if z[2] < i and lo[i] <= z[0] and c[i] > z[0] and c[i] > z[1]:
                            buy[i] = True
                            stop_l[i] = z[1] - buf * a
                            bull.remove(z)
                            break
                if trend == -1 and trend_ok_short:
                    for z in reversed(bear):
                        if z[2] < i and h[i] >= z[1] and c[i] < z[1] and c[i] < z[0]:
                            short[i] = True
                            stop_s[i] = z[0] + buf * a
                            bear.remove(z)
                            break
            if mode in ("sweep", "both"):
                if sl is not None and not sl_broken and lo[i] < sl and c[i] > sl and trend_ok_long and not short[i]:
                    buy[i] = True
                    stop_l[i] = min(stop_l[i], lo[i] - buf * a) if stop_l[i] == stop_l[i] else lo[i] - buf * a
                    sl_broken = True  # Liquiditaet unter diesem Tief ist abgeholt
                if sh is not None and not sh_broken and h[i] > sh and c[i] < sh and trend_ok_short and not buy[i]:
                    short[i] = True
                    stop_s[i] = max(stop_s[i], h[i] + buf * a) if stop_s[i] == stop_s[i] else h[i] + buf * a
                    sh_broken = True

            # 4) Struktur: Bruch per Schlusskurs -> Richtung, Order Block, Ausstieg der Gegenrichtung
            if sh is not None and not sh_broken and c[i] > sh:
                sh_broken = True
                trend = 1
                cover[i] = True
                for k in range(i - 1, max(i - 20, 0) - 1, -1):
                    if c[k] < o[k]:
                        bull.append([h[k], lo[k], i])
                        break
            if sl is not None and not sl_broken and c[i] < sl:
                sl_broken = True
                trend = -1
                sell[i] = True
                for k in range(i - 1, max(i - 20, 0) - 1, -1):
                    if c[k] > o[k]:
                        bear.append([h[k], lo[k], i])
                        break

            # 5) Fair Value Gaps dieser Kerze
            if i >= 2:
                if lo[i] > h[i - 2] and lo[i] - h[i - 2] >= fvg_min * a:
                    bull.append([lo[i], h[i - 2], i])
                if h[i] < lo[i - 2] and lo[i - 2] - h[i] >= fvg_min * a:
                    bear.append([lo[i - 2], h[i], i])

            sh_line[i] = sh if sh is not None else np.nan
            sl_line[i] = sl if sl is not None else np.nan
            zones = bull if trend >= 0 else bear
            if zones:
                z_top[i], z_bot[i] = zones[-1][0], zones[-1][1]

        df["smc_buy"], df["smc_short"], df["smc_sell"], df["smc_cover"] = buy, short, sell, cover
        df["smc_stop_long"], df["smc_stop_short"] = stop_l, stop_s
        df["smc_sh"], df["smc_sl"] = sh_line, sl_line
        df["smc_zone_top"], df["smc_zone_bot"] = z_top, z_bot

    def signals(self, df):
        return df["smc_buy"].to_numpy(), df["smc_sell"].to_numpy()

    def short_signals(self, df):
        return df["smc_short"].to_numpy(), df["smc_cover"].to_numpy()

    def explain(self, df, i=-1):
        r = df.iloc[i]

        def px(v):
            return fmt_price(float(v)) if v == v else "noch keins"

        out = [f"Smart Money ({self.mode}): Swing-Hoch {px(r['smc_sh'])}, Swing-Tief {px(r['smc_sl'])}"]
        if r["smc_zone_top"] == r["smc_zone_top"]:
            out.append(f"Naechste Zone {fmt_price(r['smc_zone_bot'])} bis {fmt_price(r['smc_zone_top'])}")
        out.append(f"Ziel {self.p('target_rr', self.default_target_rr)} x Risiko, Stop knapp hinter Zone bzw. Docht")
        return out
