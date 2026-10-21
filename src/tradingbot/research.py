from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from .backtest import Backtester, BacktestResult
from .config import DaytradingConfig, PaperConfig, ProtectionConfig, RiskConfig, StrategyConfig
from .risk import RiskManager
from .strategy import EXIT_GRID, REGISTRY

DD_FLOOR = 5.0
METRICS = ("calmar", "sharpe", "sortino", "profit")
METRIC_NAMES = {
    "calmar": "Rendite im Verhaeltnis zum Drawdown (Calmar)",
    "sharpe": "Sharpe-Ratio auf Tagesbasis",
    "sortino": "Sortino-Ratio auf Tagesbasis",
    "profit": "reine Rendite",
}
MIN_TRADES_RELIABLE = 30
MIN_PROFIT_FACTOR = 1.2
MIN_SHARE_POSITIVE_COINS = 0.6


def is_robust(o: "StrategyOutcome") -> bool:
    """Eine Strategie gilt nur als robust, wenn sie im Pruefzeitraum UND im Auswahlzeitraum Gewinn macht,
    genug Trades hat, einen Profitfaktor ueber der Kostenschwelle zeigt und bei den meisten Coins im Plus liegt."""
    a, i = o.best.oos_agg, o.best.is_agg
    positive = sum(1 for r in o.best.oos_results if r.total_return_pct > 0) / len(o.best.oos_results)
    return (
        a.ret > 0
        and i.ret > 0
        and a.trades >= MIN_TRADES_RELIABLE
        and a.profit_factor is not None
        and a.profit_factor >= MIN_PROFIT_FACTOR
        and positive >= MIN_SHARE_POSITIVE_COINS
    )


def expand_grid(strategy_name: str) -> list[dict]:
    cls = REGISTRY[strategy_name]
    grid = {**cls.param_grid, **EXIT_GRID}
    keys = list(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]
    return [c for c in combos if cls.valid_params(c)]


def _indicator_key(strategy_name: str, params: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in params.items() if k not in EXIT_GRID))


@dataclass
class Aggregate:
    ret: float
    drawdown: float
    trades: int
    win_rate: float | None
    profit_factor: float | None
    buy_hold: float

    @property
    def calmar(self) -> float | None:
        return self.ret / self.drawdown if self.drawdown > 0 else None


def aggregate(results: list[BacktestResult]) -> Aggregate:
    all_trades = [t for r in results for t in r.trades]
    gross_profit = sum(t.pnl_quote for t in all_trades if t.pnl_quote > 0)
    gross_loss = -sum(t.pnl_quote for t in all_trades if t.pnl_quote <= 0)
    n = len(results)
    return Aggregate(
        ret=sum(r.total_return_pct for r in results) / n,
        drawdown=sum(r.max_drawdown_pct for r in results) / n,
        trades=len(all_trades),
        win_rate=(sum(1 for t in all_trades if t.pnl_quote > 0) / len(all_trades) * 100.0) if all_trades else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        buy_hold=sum(r.buy_hold_return_pct for r in results) / n,
    )


def _daily_returns(r: BacktestResult) -> pd.Series:
    daily = r.equity_curve.resample("1D").last().dropna()
    return daily.pct_change().dropna()


def metric_value(r: BacktestResult, metric: str) -> float:
    if metric == "profit":
        return r.total_return_pct
    if metric == "calmar":
        return r.total_return_pct / max(r.max_drawdown_pct, DD_FLOOR)
    rets = _daily_returns(r)
    if len(rets) < 5:
        return 0.0
    mean = float(rets.mean())
    if metric == "sharpe":
        sd = float(rets.std())
        return mean / sd * math.sqrt(365) if sd > 0 else 0.0
    if metric == "sortino":
        downside = rets[rets < 0]
        dd = math.sqrt(float((downside**2).sum()) / len(rets))
        return mean / dd * math.sqrt(365) if dd > 0 else 0.0
    raise ValueError(f"Unbekanntes Bewertungsmass '{metric}'. Verfuegbar: {METRICS}")


def selection_score(results: list[BacktestResult], min_trades: int, metric: str = "calmar") -> float:
    """Auswahlkriterium, ausschliesslich auf dem Auswahlzeitraum, gemittelt ueber alle Coins.
    Zu wenige Trades gelten als nicht aussagekraeftig."""
    if metric not in METRICS:
        raise ValueError(f"Unbekanntes Bewertungsmass '{metric}'. Verfuegbar: {METRICS}")
    if sum(r.n_trades for r in results) < min_trades:
        return -1e9
    return sum(metric_value(r, metric) for r in results) / len(results)


@dataclass
class ComboResult:
    strategy: str
    params: dict
    is_agg: Aggregate
    oos_agg: Aggregate
    score: float
    is_results: list[BacktestResult] = field(repr=False, default_factory=list)
    oos_results: list[BacktestResult] = field(repr=False, default_factory=list)


@dataclass
class StrategyOutcome:
    strategy: str
    combos: list[ComboResult]
    best: ComboResult

    @property
    def tried(self) -> int:
        return len(self.combos)


@dataclass
class Comparison:
    symbols: list[str]
    timeframe: str
    bars: dict[str, int]
    split: float
    split_dates: dict[str, str]
    outcomes: list[StrategyOutcome]
    buy_hold_is: float
    buy_hold_oos: float
    min_trades: int
    fee_pct: float
    slippage_pct: float
    start_date: str
    end_date: str
    metric: str = "calmar"
    daytrading: DaytradingConfig | None = None

    @property
    def ranking(self) -> list[StrategyOutcome]:
        return sorted(self.outcomes, key=lambda o: o.best.oos_agg.ret, reverse=True)

    @property
    def winner(self) -> StrategyOutcome | None:
        for o in self.ranking:
            if is_robust(o):
                return o
        return None


def run_comparison(
    data: dict[str, pd.DataFrame],
    strategy_names: list[str],
    base_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    paper_cfg: PaperConfig,
    timeframe: str,
    split: float = 0.6,
    min_trades: int = 10,
    progress: Callable[[str], None] | None = None,
    protections: ProtectionConfig | None = None,
    metric: str = "calmar",
    daytrading: DaytradingConfig | None = None,
) -> Comparison:
    if daytrading is not None and not daytrading.enabled:
        daytrading = None
    if metric not in METRICS:
        raise ValueError(f"Unbekanntes Bewertungsmass '{metric}'. Verfuegbar: {METRICS}")
    if not 0.2 <= split <= 0.9:
        raise ValueError("split muss zwischen 0.2 und 0.9 liegen")
    risk = RiskManager(risk_cfg)
    symbols = list(data)
    split_idx = {s: int(len(df) * split) for s, df in data.items()}
    outcomes: list[StrategyOutcome] = []
    bh_is: list[float] = []
    bh_oos: list[float] = []

    for name in strategy_names:
        combos = expand_grid(name)
        groups: dict[tuple, list[dict]] = {}
        for params in combos:
            groups.setdefault(_indicator_key(name, params), []).append(params)

        results: list[ComboResult] = []
        started = time.monotonic()
        for ind_key, variants in groups.items():
            ind_params = dict(ind_key)
            first_cfg = base_cfg.model_copy(update={"name": name, **ind_params})
            first_strategy = REGISTRY[name](first_cfg, daytrading)
            prepared = {}
            for s, df in data.items():
                if len(df) < first_strategy.warmup_bars + 50:
                    raise ValueError(f"{s}: zu wenig Kerzen ({len(df)}) fuer {name}")
                p = first_strategy.prepare(df)
                prepared[s] = (p, first_strategy.signals(p))

            for params in variants:
                cfg = base_cfg.model_copy(update={"name": name, **params})
                strategy = REGISTRY[name](cfg, daytrading)
                bt = Backtester(strategy, risk, cfg, paper_cfg, protections_cfg=protections, daytrading=daytrading)
                is_res, oos_res = [], []
                for s in symbols:
                    p, sig = prepared[s]
                    cut = split_idx[s]
                    is_res.append(bt.run(p, s, start=1, end=cut, prepared=True, signals=sig))
                    oos_res.append(bt.run(p, s, start=cut, end=len(p), prepared=True, signals=sig))
                results.append(
                    ComboResult(name, params, aggregate(is_res), aggregate(oos_res), selection_score(is_res, min_trades, metric), is_res, oos_res)
                )
        best = max(results, key=lambda c: c.score)
        outcomes.append(StrategyOutcome(name, results, best))
        if progress:
            progress(f"{name}: {len(results)} Parametersaetze in {time.monotonic() - started:.1f}s getestet")
        if not bh_is:
            bh_is = [r.buy_hold_return_pct for r in best.is_results]
            bh_oos = [r.buy_hold_return_pct for r in best.oos_results]

    def date(df: pd.DataFrame, idx: int) -> str:
        return datetime.fromtimestamp(int(df["timestamp"].iloc[min(idx, len(df) - 1)]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    first = data[symbols[0]]
    return Comparison(
        symbols=symbols,
        timeframe=timeframe,
        bars={s: len(df) for s, df in data.items()},
        split=split,
        split_dates={s: date(df, split_idx[s]) for s, df in data.items()},
        outcomes=outcomes,
        buy_hold_is=sum(bh_is) / len(bh_is),
        buy_hold_oos=sum(bh_oos) / len(bh_oos),
        min_trades=min_trades,
        fee_pct=paper_cfg.fee_pct,
        slippage_pct=paper_cfg.slippage_pct,
        start_date=date(first, 0),
        end_date=date(first, len(first) - 1),
        metric=metric,
        daytrading=daytrading,
    )


def _daytrading_line(dt: DaytradingConfig | None) -> str:
    if dt is None:
        return "- **Tageshandel:** aus, Positionen duerfen ueber Nacht laufen"
    limit = f"max. {dt.max_trades_per_day} Einstiege/Tag" if dt.max_trades_per_day else "Einstiege/Tag unbegrenzt"
    hold = f", max. Haltedauer {dt.max_hold_minutes} min" if dt.max_hold_minutes else ""
    return (
        f"- **Tageshandel:** Einstiege {dt.entry_start}-{dt.entry_end}, alle Positionen zu ab {dt.flat_time} "
        f"({dt.timezone}), {limit}{hold}, Tagesverlustlimit aktiv. Keine Position ueber Nacht."
    )


def _f(v: float | None, spec: str = "+.1f", suffix: str = "") -> str:
    return "–" if v is None else f"{v:{spec}}{suffix}"


def _params_text(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def format_report(cmp: Comparison, synthetic: bool = False, notes: list[str] | None = None) -> str:
    lines: list[str] = []
    if synthetic:
        lines += ["> **ACHTUNG: SYNTHETISCHE DATEN.** Diese Zahlen stammen aus kuenstlich erzeugten Kursreihen und sagen nichts ueber echte Maerkte.", ""]
    lines += [
        "# Strategievergleich",
        "",
        f"- **Daten:** {', '.join(cmp.symbols)}, Zeitrahmen {cmp.timeframe}, {cmp.start_date} bis {cmp.end_date}",
        f"- **Auswahlzeitraum:** bis {next(iter(cmp.split_dates.values()))}. Hier werden die Parameter gewaehlt, nach: {METRIC_NAMES[cmp.metric]}.",
        f"- **Pruefzeitraum:** danach, nie zur Auswahl benutzt. Nur dieser Zeitraum zaehlt fuer die Bewertung.",
        f"- **Kosten:** Gebuehr {cmp.fee_pct}% pro Order, Kursabweichung {cmp.slippage_pct}%",
        _daytrading_line(cmp.daytrading),
        f"- **Buy & Hold im Pruefzeitraum:** {cmp.buy_hold_oos:+.1f}% (Vergleichswert, ohne Handel)",
        "",
        "## Rangliste nach Rendite im Pruefzeitraum",
        "",
        "| # | Strategie | Rendite Pruefzeitraum | Max. Drawdown | Trades | Trefferquote | Profitfaktor | Rendite Auswahlzeitraum | Getestete Varianten |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for rank, o in enumerate(cmp.ranking, 1):
        a, i = o.best.oos_agg, o.best.is_agg
        flag = "" if a.trades >= MIN_TRADES_RELIABLE else " (wenige Trades)"
        lines.append(
            f"| {rank} | {o.strategy} | {a.ret:+.1f}%{flag} | {a.drawdown:.1f}% | {a.trades} | {_f(a.win_rate, '.0f', '%')} | {_f(a.profit_factor, '.2f')} | {i.ret:+.1f}% | {o.tried} |"
        )
    lines += ["", "## Gewaehlte Parameter je Strategie", ""]
    for o in cmp.ranking:
        lines.append(f"- **{o.strategy}:** {_params_text(o.best.params)}")

    lines += ["", "## Ergebnis je Coin im Pruefzeitraum", "", "| Strategie | " + " | ".join(cmp.symbols) + " |", "|---|" + "---|" * len(cmp.symbols)]
    for o in cmp.ranking:
        cells = [f"{r.total_return_pct:+.1f}% ({r.n_trades} Trades)" for r in o.best.oos_results]
        lines.append(f"| {o.strategy} | " + " | ".join(cells) + " |")
    bh = [f"{r.buy_hold_return_pct:+.1f}%" for r in ranking_first(cmp).best.oos_results]
    lines.append("| Buy & Hold | " + " | ".join(bh) + " |")

    if notes:
        lines += ["", "## Pruefung der Strategien", ""] + [f"- {n}" for n in notes]
    lines += ["", "## Bewertung", ""]
    winner = cmp.winner
    if winner is None:
        lines += [
            "**Keine Strategie hat sich als robust profitabel erwiesen.** Eine robuste Strategie muss im Auswahl- "
            "und im Pruefzeitraum Gewinn machen, mindestens "
            f"{MIN_TRADES_RELIABLE} Trades haben, einen Profitfaktor ab {MIN_PROFIT_FACTOR} erreichen und bei mindestens "
            f"{int(MIN_SHARE_POSITIVE_COINS * 100)}% der Coins im Plus liegen. Keine erfuellt das. Handle damit kein echtes Geld.",
        ]
    else:
        a = winner.best.oos_agg
        lines += [
            f"**Bester Kandidat: {winner.strategy}** mit {a.ret:+.1f}% im Pruefzeitraum, Drawdown {a.drawdown:.1f}%, {a.trades} Trades.",
            "",
            "Eintrag fuer die `config.yaml`:",
            "",
            "```yaml",
            "strategy:",
            f"  name: {winner.strategy}",
        ]
        for k, v in winner.best.params.items():
            lines.append(f"  {k}: {v}")
        lines += ["  trailing_stop: true", "```"]
        if a.ret < cmp.buy_hold_oos:
            lines += ["", f"Hinweis: Buy & Hold hat im selben Zeitraum mehr gebracht ({cmp.buy_hold_oos:+.1f}%). Die Strategie schuetzt eher vor Verlusten, als dass sie mehr verdient."]
    lines += [
        "",
        "Wichtig fuer die Einordnung:",
        "",
        f"- Es wurden {sum(o.tried for o in cmp.outcomes)} Varianten getestet. Je mehr man testet, desto eher gewinnt eine nur durch Zufall. Deshalb zaehlt nur der Pruefzeitraum.",
        "- Ein einziger Pruefzeitraum ist keine Garantie. Prüfe zusaetzlich andere Zeitraeume, andere Coins und danach mehrere Wochen Papierhandel.",
        "- Weniger als 30 Trades im Pruefzeitraum sind statistisch kaum aussagekraeftig.",
        "- Der Backtest kennt weder Orderbuch noch Latenz noch Luecken ueber Nacht. Echte Ergebnisse fallen meist schlechter aus.",
    ]
    return "\n".join(lines) + "\n"


def ranking_first(cmp: Comparison) -> StrategyOutcome:
    return cmp.ranking[0]


def grid_dataframe(cmp: Comparison) -> pd.DataFrame:
    rows = []
    for o in cmp.outcomes:
        for c in o.combos:
            rows.append(
                {
                    "strategy": o.strategy,
                    "params": _params_text(c.params),
                    "selected": c is o.best,
                    "is_return_pct": c.is_agg.ret,
                    "is_drawdown_pct": c.is_agg.drawdown,
                    "is_trades": c.is_agg.trades,
                    "score": c.score,
                    "oos_return_pct": c.oos_agg.ret,
                    "oos_drawdown_pct": c.oos_agg.drawdown,
                    "oos_trades": c.oos_agg.trades,
                    "oos_win_rate_pct": c.oos_agg.win_rate,
                    "oos_profit_factor": c.oos_agg.profit_factor,
                }
            )
    return pd.DataFrame(rows)
