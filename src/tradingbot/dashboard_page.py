INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Bot</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header class="top">
  <div class="brand">
    <h1>Trading Bot</h1>
    <span id="mode" class="badge">…</span>
    <span id="status" class="pill">…</span>
  </div>
  <div class="meta">
    <span id="meta"></span>
    <span id="updated" class="muted"></span>
  </div>
</header>

<main>
  <div id="banner" class="banner" hidden></div>

  <section class="kpis">
    <div class="kpi"><div class="label">Kapital</div><div id="k-equity" class="value">–</div><div id="k-equity-sub" class="sub"></div></div>
    <div class="kpi"><div class="label">Rendite gesamt</div><div id="k-return" class="value">–</div><div id="k-return-sub" class="sub"></div></div>
    <div class="kpi"><div class="label">Heute</div><div id="k-day" class="value">–</div><div id="k-day-sub" class="sub"></div></div>
    <div class="kpi"><div class="label">Realisierter Gewinn</div><div id="k-pnl" class="value">–</div><div id="k-pnl-sub" class="sub"></div></div>
    <div class="kpi"><div class="label">Trefferquote</div><div id="k-win" class="value">–</div><div id="k-win-sub" class="sub"></div></div>
  </section>

  <section class="card">
    <div class="card-head"><h2>Kapitalverlauf</h2><span id="chart-range" class="muted"></span></div>
    <div id="chart" class="chart" role="img" aria-label="Kapitalverlauf"></div>
  </section>

  <section class="card">
    <div class="card-head"><h2>Offene Positionen</h2></div>
    <div class="table-wrap"><table id="positions">
      <thead><tr><th>Paar</th><th class="num">Menge</th><th class="num">Einstieg</th><th class="num">Aktuell</th><th class="num">Stop-Loss</th><th class="num">Take-Profit</th><th class="num">G/V</th><th>Seit</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <p id="positions-empty" class="empty" hidden>Keine offenen Positionen.</p>
  </section>

  <section class="card" id="ai-card" hidden>
    <div class="card-head"><h2>KI-Entscheidungen</h2><span id="ai-meta" class="muted"></span></div>
    <div class="table-wrap"><table id="ai-table">
      <thead><tr><th>Zeit</th><th>Paar</th><th>Entscheidung</th><th class="num">Konfidenz</th><th>Umgesetzt</th><th>Begründung</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <p id="ai-empty" class="empty" hidden>Noch keine Entscheidungen. Die KI wird bei jeder neuen Kerze gefragt.</p>
  </section>

  <section class="card">
    <div class="card-head"><h2>Letzte Trades</h2></div>
    <div class="table-wrap"><table id="trades">
      <thead><tr><th>Geschlossen</th><th>Paar</th><th class="num">Einstieg</th><th class="num">Ausstieg</th><th class="num">G/V</th><th class="num">%</th><th>Grund</th></tr></thead>
      <tbody></tbody>
    </table></div>
    <p id="trades-empty" class="empty" hidden>Noch keine abgeschlossenen Trades.</p>
  </section>

  <section class="card controls" id="controls" hidden>
    <div>
      <h2>Steuerung</h2>
      <p class="muted">Pausieren stoppt nur neue Einstiege. Offene Positionen werden weiter mit Stop-Loss und Take-Profit verwaltet.</p>
    </div>
    <button id="toggle" type="button">…</button>
  </section>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

STYLE_CSS = """:root {
  --bg: #f6f7f9; --surface: #ffffff; --text: #14181f; --muted: #667085; --border: #e3e6ec;
  --accent: #2563eb; --pos: #15803d; --neg: #b91c1c; --warn-bg: #fff4e0; --warn-text: #8a4b00;
  --live-bg: #fde8e8; --live-text: #b42318; --paper-bg: #e8f0fe; --paper-text: #1d4ed8;
  --grid: #e8ebf0; --line: #2563eb; --area: rgba(37, 99, 235, .10);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0e1116; --surface: #161b22; --text: #e6e9ef; --muted: #8b95a5; --border: #262d38;
    --accent: #6ea0ff; --pos: #4ade80; --neg: #f87171; --warn-bg: #3a2a0c; --warn-text: #f5c26b;
    --live-bg: #3b1414; --live-text: #ff9b92; --paper-bg: #14243f; --paper-text: #9bbcff;
    --grid: #222a35; --line: #6ea0ff; --area: rgba(110, 160, 255, .12);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--text); font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; -webkit-font-smoothing: antialiased; }
.top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 24px; background: var(--surface); border-bottom: 1px solid var(--border); }
.brand { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 18px; margin: 0; font-weight: 650; }
h2 { font-size: 15px; margin: 0; font-weight: 650; }
.meta { display: flex; gap: 14px; align-items: center; color: var(--muted); font-size: 13px; flex-wrap: wrap; }
.muted { color: var(--muted); }
.badge, .pill { font-size: 12px; font-weight: 650; padding: 3px 10px; border-radius: 999px; letter-spacing: .02em; }
.badge.paper { background: var(--paper-bg); color: var(--paper-text); }
.badge.live { background: var(--live-bg); color: var(--live-text); }
.pill { background: var(--border); color: var(--muted); }
.pill.run { background: rgba(21, 128, 61, .14); color: var(--pos); }
.pill.warn { background: var(--warn-bg); color: var(--warn-text); }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; display: grid; gap: 16px; }
.banner { background: var(--warn-bg); color: var(--warn-text); border-radius: 10px; padding: 12px 16px; font-weight: 550; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.kpi, .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
.kpi { padding: 14px 16px; }
.kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.kpi .value { font-size: 22px; font-weight: 650; margin-top: 4px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.kpi .sub { color: var(--muted); font-size: 12px; min-height: 17px; font-variant-numeric: tabular-nums; }
.card { padding: 16px; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
.pos { color: var(--pos); } .neg { color: var(--neg); }
.chart { width: 100%; height: 260px; position: relative; }
.chart svg { width: 100%; height: 100%; display: block; }
.chart .empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--muted); font-weight: 550; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
th.num, td.num { text-align: right; }
td.wrap { white-space: normal; min-width: 280px; max-width: 520px; }
tbody tr:last-child td { border-bottom: 0; }
.empty { color: var(--muted); margin: 8px 0 0; }
.controls { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.controls p { margin: 4px 0 0; max-width: 560px; font-size: 13px; }
button { font: inherit; font-weight: 600; padding: 9px 18px; border-radius: 9px; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; }
button:hover { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:disabled { opacity: .5; cursor: wait; }
@media (max-width: 600px) { .top { padding: 12px 16px; } .kpi .value { font-size: 20px; } }
"""

APP_JS = """(function () {
  'use strict';
  var REFRESH_MS = 5000;
  var $ = function (id) { return document.getElementById(id); };
  var snap = null;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function nf(n, d) {
    if (n === null || n === undefined || isNaN(n)) return '–';
    return new Intl.NumberFormat('de-DE', { minimumFractionDigits: d, maximumFractionDigits: d }).format(n);
  }
  function price(n) {
    if (n === null || n === undefined || isNaN(n)) return '–';
    var a = Math.abs(n);
    return nf(n, a >= 1000 ? 2 : a >= 1 ? 4 : 6);
  }
  function signed(n, d, suffix) {
    if (n === null || n === undefined || isNaN(n)) return '–';
    return (n > 0 ? '+' : '') + nf(n, d) + (suffix || '');
  }
  function cls(n) { return n === null || n === undefined || n === 0 ? '' : (n > 0 ? 'pos' : 'neg'); }
  function when(iso) {
    if (!iso) return '–';
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function setKpi(id, value, sub, c) {
    var v = $('k-' + id);
    v.textContent = value;
    v.className = 'value' + (c ? ' ' + c : '');
    $('k-' + id + '-sub').textContent = sub || '';
  }
  function cell(tr, text, cssClass) { tr.appendChild(el('td', cssClass || '', text)); }

  function reasonText(r) {
    var map = { stop_loss: 'Stop-Loss', take_profit: 'Take-Profit', end_of_data: 'Ende der Daten' };
    if (map[r]) return map[r];
    return r.indexOf('signal:') === 0 ? 'Signal:' + r.slice(7) : r;
  }

  function renderHeader(s) {
    var mode = $('mode');
    mode.textContent = s.mode === 'live' ? 'LIVE – echtes Geld' : 'Papierhandel';
    mode.className = 'badge ' + (s.mode === 'live' ? 'live' : 'paper');
    var st = $('status');
    var banner = $('banner');
    var msg = null;
    if (!s.running) { st.textContent = 'Bot nicht aktiv'; st.className = 'pill'; }
    else if (s.halted) { st.textContent = 'Tagesverlustlimit'; st.className = 'pill warn'; msg = 'Tagesverlustlimit erreicht: Es werden bis zum nächsten Tag keine neuen Positionen eröffnet.'; }
    else if (s.paused) { st.textContent = 'Einstiege pausiert'; st.className = 'pill warn'; msg = 'Neue Einstiege sind pausiert. Offene Positionen werden weiter verwaltet.'; }
    else { st.textContent = 'Läuft'; st.className = 'pill run'; }
    if (!s.running && !s.controls) msg = 'Nur Ansicht: Der Bot läuft in einem anderen Fenster. Angezeigt wird der zuletzt gespeicherte Stand.';
    banner.hidden = !msg;
    banner.textContent = msg || '';
    $('meta').textContent = s.exchange + ' · ' + s.symbols.join(', ') + ' · ' + s.timeframe;
    $('updated').textContent = s.last_cycle ? 'Letzter Zyklus ' + when(s.last_cycle) : 'Noch kein Zyklus';
    document.title = (s.mode === 'live' ? '[LIVE] ' : '') + 'Trading Bot';
  }

  function renderKpis(s) {
    var q = s.quote;
    setKpi('equity', s.equity === null ? '–' : nf(s.equity, 2) + ' ' + q, s.free_quote === null || s.free_quote === undefined ? '' : 'frei: ' + nf(s.free_quote, 2) + ' ' + q);
    setKpi('return', signed(s.return_pct, 2, ' %'), s.start_equity ? 'seit ' + nf(s.start_equity, 2) + ' ' + q : '', cls(s.return_pct));
    setKpi('day', signed(s.day_pnl_pct, 2, ' %'), s.day_start_equity ? 'Start ' + nf(s.day_start_equity, 2) + ' ' + q : '', cls(s.day_pnl_pct));
    var st = s.stats;
    setKpi('pnl', st.n_trades ? signed(st.total_pnl, 2, ' ' + q) : '–', st.n_trades ? 'Gebühren ' + nf(st.fees, 2) + ' ' + q : '', cls(st.total_pnl));
    setKpi('win', st.win_rate_pct === null ? '–' : nf(st.win_rate_pct, 1) + ' %', st.n_trades ? st.wins + ' von ' + st.n_trades + (st.profit_factor !== null ? ' · PF ' + nf(st.profit_factor, 2) : '') : '');
  }

  function renderChart(s) {
    var box = $('chart');
    while (box.firstChild) box.removeChild(box.firstChild);
    var h = s.equity_history || [];
    if (h.length < 2) {
      box.appendChild(el('div', 'empty', 'Der Verlauf erscheint nach den ersten Zyklen.'));
      $('chart-range').textContent = '';
      return;
    }
    var W = 1000, H = 260, pl = 64, pr = 12, pt = 12, pb = 26;
    var xs = h.map(function (p) { return new Date(p[0]).getTime(); });
    var ys = h.map(function (p) { return p[1]; });
    var x0 = xs[0], x1 = xs[xs.length - 1] || x0 + 1;
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    var sx = function (x) { return pl + (x - x0) / (x1 - x0 || 1) * (W - pl - pr); };
    var sy = function (y) { return pt + (hi - y) / (hi - lo) * (H - pt - pb); };
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'none');
    function node(name, attrs, text) {
      var n = document.createElementNS(ns, name);
      for (var k in attrs) {
        var val = String(attrs[k]);
        if (val.indexOf('var(') === 0) n.style.setProperty(k, val); else n.setAttribute(k, val);
      }
      if (text) n.textContent = text;
      return n;
    }
    for (var i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4, y = sy(v);
      svg.appendChild(node('line', { x1: pl, x2: W - pr, y1: y, y2: y, stroke: 'var(--grid)', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke' }));
      svg.appendChild(node('text', { x: pl - 8, y: y + 4, 'text-anchor': 'end', fill: 'var(--muted)', 'font-size': 12 }, nf(v, hi - lo >= 20 ? 0 : 2)));
    }
    var pts = h.map(function (p, i) { return sx(xs[i]).toFixed(1) + ',' + sy(ys[i]).toFixed(1); });
    svg.appendChild(node('polygon', { points: pts.join(' ') + ' ' + sx(x1).toFixed(1) + ',' + (H - pb) + ' ' + sx(x0).toFixed(1) + ',' + (H - pb), fill: 'var(--area)' }));
    svg.appendChild(node('polyline', { points: pts.join(' '), fill: 'none', stroke: 'var(--line)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke' }));
    var fmt = function (t) { return new Date(t).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }); };
    svg.appendChild(node('text', { x: pl, y: H - 6, fill: 'var(--muted)', 'font-size': 12 }, fmt(x0)));
    svg.appendChild(node('text', { x: W - pr, y: H - 6, 'text-anchor': 'end', fill: 'var(--muted)', 'font-size': 12 }, fmt(x1)));
    box.appendChild(svg);
    $('chart-range').textContent = fmt(x0) + ' – ' + fmt(x1);
  }

  function renderPositions(s) {
    var body = document.querySelector('#positions tbody');
    body.textContent = '';
    s.positions.forEach(function (p) {
      var tr = document.createElement('tr');
      cell(tr, p.symbol);
      cell(tr, nf(p.amount, 6), 'num');
      cell(tr, price(p.entry_price), 'num');
      cell(tr, price(p.price), 'num');
      cell(tr, price(p.stop_loss) + (p.stop_on_exchange ? ' (Börse)' : ''), 'num');
      cell(tr, p.take_profit === null ? '–' : price(p.take_profit), 'num');
      cell(tr, p.pnl_quote === null ? '–' : signed(p.pnl_quote, 2) + ' (' + signed(p.pnl_pct, 2, ' %') + ')', 'num ' + cls(p.pnl_quote));
      cell(tr, when(p.opened_at));
      body.appendChild(tr);
    });
    $('positions-empty').hidden = s.positions.length > 0;
    $('positions').hidden = s.positions.length === 0;
  }

  function renderTrades(s) {
    var body = document.querySelector('#trades tbody');
    body.textContent = '';
    s.trades.forEach(function (t) {
      var tr = document.createElement('tr');
      cell(tr, when(t.closed_at));
      cell(tr, t.symbol);
      cell(tr, price(t.entry_price), 'num');
      cell(tr, price(t.exit_price), 'num');
      cell(tr, signed(t.pnl_quote, 2), 'num ' + cls(t.pnl_quote));
      cell(tr, signed(t.pnl_pct, 2, ' %'), 'num ' + cls(t.pnl_pct));
      cell(tr, reasonText(t.reason));
      body.appendChild(tr);
    });
    $('trades-empty').hidden = s.trades.length > 0;
    $('trades').hidden = s.trades.length === 0;
  }

  function renderAi(s) {
    var card = $('ai-card');
    card.hidden = !s.ai;
    if (!s.ai) return;
    $('ai-meta').textContent = s.ai.model + ' · Aufrufe heute ' + s.ai.calls_today + ' von ' + s.ai.max_calls;
    var body = document.querySelector('#ai-table tbody');
    body.textContent = '';
    var names = { buy: 'Kaufen', sell: 'Verkaufen', hold: 'Halten' };
    s.ai.decisions.forEach(function (d) {
      var tr = document.createElement('tr');
      cell(tr, when(d.ts));
      cell(tr, d.symbol);
      cell(tr, names[d.action] || d.action);
      cell(tr, nf(d.confidence * 100, 0) + ' %', 'num');
      cell(tr, d.applied ? 'ja' : 'nein');
      cell(tr, d.reason, 'wrap');
      body.appendChild(tr);
    });
    $('ai-empty').hidden = s.ai.decisions.length > 0;
    $('ai-table').hidden = s.ai.decisions.length === 0;
  }

  function renderControls(s) {
    var box = $('controls');
    box.hidden = !s.controls || !s.running;
    var b = $('toggle');
    b.textContent = s.paused ? 'Einstiege fortsetzen' : 'Einstiege pausieren';
    b.className = s.paused ? 'primary' : '';
  }

  function render(s) {
    snap = s;
    renderHeader(s); renderKpis(s); renderChart(s); renderPositions(s); renderAi(s); renderTrades(s); renderControls(s);
  }

  function load() {
    fetch('/api/snapshot', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(render)
      .catch(function () {
        var st = $('status');
        st.textContent = 'Keine Verbindung';
        st.className = 'pill warn';
      });
  }

  $('toggle').addEventListener('click', function () {
    if (!snap) return;
    var b = this;
    b.disabled = true;
    fetch('/api/' + (snap.paused ? 'resume' : 'pause'), { method: 'POST', headers: { 'X-Dashboard': '1' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(render)
      .catch(function () { alert('Aktion fehlgeschlagen. Details stehen im Bot-Log.'); })
      .then(function () { b.disabled = false; });
  });

  load();
  setInterval(load, REFRESH_MS);
})();
"""
