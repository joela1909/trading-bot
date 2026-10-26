(function () {
  'use strict';

  var LC = window.LightweightCharts;
  var REFRESH_MS = 3000;
  var CHART_REFRESH_MS = 2000; // laufende Kerze im Chart
  var TIMEFRAMES = [['1m', '1m'], ['5m', '5m'], ['15m', '15m'], ['1h', '1h'], ['4h', '4h'], ['1d', '1T']];
  var TF_SECONDS = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400 };
  var CHART_BARS = 160; // so viele Kerzen zeigt der Chart beim Oeffnen
  var NAMES = {
    ensemble: 'Ensemble (alle)', ema_cross: 'EMA-Kreuz', donchian: 'Donchian-Ausbruch',
    rsi_reversion: 'RSI-Umkehr', bollinger_reversion: 'Bollinger-Umkehr', macd_trend: 'MACD-Trend',
    vwap_reversion: 'VWAP-Umkehr', opening_range: 'Eröffnungsspanne', claude_advisor: 'KI (Claude)',
    supertrend_ai: 'SuperTrend AI (LuxAlgo)', reversal_zones: 'Reversal Zones (LuxAlgo)', smc: 'Smart Money', tsmom: 'Momentum'
  };
  var REASONS = { reset: 'Konto zurückgesetzt', stop_loss: 'Stop-Loss', take_profit: 'Take-Profit', end_of_data: 'Ende der Daten', session_end: 'Handelsschluss', max_hold: 'Max. Haltedauer' };

  var $ = function (id) { return document.getElementById(id); };
  var snap = null;
  var page = 'uebersicht';
  var chartSymbol = null;
  var chartTf = null; // null = Zeitrahmen des Bots
  var tradeView = 'list';
  // Kategorie: crypto | stocks. Jede Seite zeigt nur sie, gemerkt im Browser.
  var market = 'crypto';
  try { if (localStorage.getItem('tb-market') === 'stocks') market = 'stocks'; } catch (e) { /* ohne Speicher */ }
  var snaps = { crypto: null, stocks: null };
  var tradeFilter = null;
  var settingsLoaded = false;
  var restarting = false;
  // req: Nummer der letzten Chart-Anfrage. Antworten auf aeltere Anfragen (z. B. nach schnellem Paarwechsel) zaehlen nicht.
  var C = { chart: null, candles: null, lines: {}, markers: null, priceLines: [], data: null, hidden: {}, firstFit: true, req: 0, symbol: null };

  // ------------------------------------------------------------ Hilfen
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
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
  function tone(n) { return n === null || n === undefined || n === 0 ? '' : (n > 0 ? 'pos' : 'neg'); }
  function when(iso) {
    if (!iso) return '–';
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function clock(iso) { var d = new Date(iso); return isNaN(d) ? '–' : d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }); }
  function hm(ms) { return new Date(ms).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' }); }
  function pair(symbol) { return String(symbol || '').split(':')[0]; }
  function coin(symbol) { return pair(symbol).split('/')[0]; }
  function sideText(side) { return side === 'short' ? 'Short' : 'Long'; }
  function sideTag(side) { return el('span', 'side ' + (side === 'short' ? 'short' : 'long'), sideText(side)); }
  function reasonText(r) {
    if (REASONS[r]) return REASONS[r];
    return String(r || '').indexOf('signal:') === 0 ? 'Signal:' + r.slice(7) : (r || '–');
  }
  function stratName(n) { return NAMES[n] || n; }
  // Ausloeser eines Trades: kurzer Name, bei mehreren Strategien die Anzahl der Stimmen
  var SHORT = { supertrend_ai: 'SuperTrend', opening_range: 'Opening Range', reversal_zones: 'Reversal Zones', smc: 'Smart Money',
    vwap_reversion: 'VWAP', ema_cross: 'EMA', donchian: 'Donchian', rsi_reversion: 'RSI', bollinger_reversion: 'Bollinger',
    macd_trend: 'MACD', tsmom: 'Momentum', ensemble: 'Ensemble' };
  function triggerText(s) {
    if (!s) return '';
    var parts = s.split('+');
    return parts.length > 1 ? parts.length + ' Stimmen' : (SHORT[s] || stratName(s));
  }
  var STOP_KINDS = { initial: 'Stop beim Einstieg', line: 'SuperTrend-Linie', trailing: 'Trailing-Stop' };
  function stopKindText(k) { return STOP_KINDS[k] || 'unbekannt'; }
  // Zusatzzeile eines Trades: Hebel, Stop beim Einstieg (Abstand in %), welcher Stop gegriffen hat
  function stopInfo(t) {
    var parts = [];
    if (t.leverage) parts.push('Hebel ' + nf(t.leverage, 0) + 'x');
    if (t.initial_stop) parts.push('Stop beim Einstieg ' + price(t.initial_stop) + ' (' + nf(Math.abs(t.initial_stop / t.entry_price - 1) * 100, 2) + ' %)');
    if (t.stop_kind) parts.push('gegriffen: ' + stopKindText(t.stop_kind));
    return parts.join(' · ');
  }
  function triggerTag(s) {
    if (!s) return null;
    var t = el('span', 'strat' + (s === 'supertrend_ai' ? ' st' : s === 'opening_range' ? ' orb' : ''), triggerText(s));
    t.title = s.split('+').map(stratName).join(', ');
    return t;
  }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function chevron() {
    var ns = 'http://www.w3.org/2000/svg';
    var s = document.createElementNS(ns, 'svg'); s.setAttribute('class', 'chev'); s.setAttribute('viewBox', '0 0 8 13'); s.setAttribute('aria-hidden', 'true');
    var p = document.createElementNS(ns, 'path'); p.setAttribute('d', 'M1.5 1.5 6.5 6.5 1.5 11.5'); s.appendChild(p);
    return s;
  }

  // Abgelaufene Anmeldung: zur Anmeldeseite statt eines Browser-Dialogs
  function checkAuth(r) { if (r.status === 401) { location.href = '/login'; throw new Error('Anmeldung abgelaufen'); } return r; }
  // Abbruch nach 15 s, damit eine haengende Anfrage nicht alle weiteren blockiert
  function getJSON(url) {
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, 15000) : null;
    return fetch(url, { cache: 'no-store', credentials: 'same-origin', signal: ctl ? ctl.signal : undefined }).then(checkAuth).then(function (r) {
      return r.json().then(function (body) { if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status)); return body; });
    }).then(function (b) { clearTimeout(timer); return b; }, function (e) {
      clearTimeout(timer);
      throw e && e.name === 'AbortError' ? new Error('Zeitüberschreitung') : e;
    });
  }
  function postJSON(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'X-Dashboard': '1', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
      .then(checkAuth)
      .then(function (r) { return r.json().then(function (b) { if (!r.ok) throw new Error(b.error || ('HTTP ' + r.status)); return b; }); });
  }

  // ------------------------------------------------------------ Seiten
  function route() {
    var hash = location.hash.replace(/^#\/?/, '');
    var parts = hash.split('?');
    var name = parts[0] || 'uebersicht';
    if (!document.querySelector('[data-page="' + name + '"]')) name = 'uebersicht';
    var params = new URLSearchParams(parts[1] || '');
    if (params.get('m') && params.get('m') !== market) setMarket(params.get('m') === 'stocks' ? 'stocks' : 'crypto', true);
    if (params.get('s')) chartSymbol = params.get('s');
    var wasChart = page === 'chart';
    page = name;
    showPages();
    document.querySelectorAll('[data-nav]').forEach(function (a) {
      if (a.getAttribute('data-nav') === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    // Chart bei jedem Oeffnen frisch aufbauen: ein Chart, der im versteckten Zustand lebte, hat oft falsche Masse
    if (name === 'chart' && !wasChart) { resetChart(); renderChartToolbar(); loadChart(); }
    if (name === 'einstellungen' && !settingsLoaded) loadSettings();
    if (snap) renderPage();
    window.scrollTo(0, 0);
  }

  // Aktien noch nicht bereit: statt der Seiten die Einrichtung zeigen (Einstellungen gehen trotzdem)
  function stocksPending() { return market === 'stocks' && !(snap && snap.ready); }

  function showPages() {
    var pending = stocksPending() && page !== 'einstellungen';
    document.querySelectorAll('[data-page]').forEach(function (s) { s.hidden = pending || s.getAttribute('data-page') !== page; });
    $('stocks-setup').hidden = !pending;
    document.querySelectorAll('#market-switch button').forEach(function (b) { b.setAttribute('aria-selected', b.getAttribute('data-market') === market ? 'true' : 'false'); });
  }

  // Pfade der API je Kategorie
  var API = {
    crypto: { snapshot: '/api/snapshot', chart: '/api/chart', settings: '/api/settings', post: '/api/' },
    stocks: { snapshot: '/api/stocks', chart: '/api/stocks-chart', settings: '/api/stocks-settings', post: '/api/stocks-' }
  };

  function setMarket(m, quiet) {
    if (m === market) return;
    market = m;
    try { localStorage.setItem('tb-market', m); } catch (e) { /* ohne Speicher */ }
    snap = snaps[m];
    chartSymbol = null; chartTf = null; tradeFilter = null; settingsLoaded = false; C.toolbarKey = null;
    resetChart();
    if (quiet) return;
    showPages();
    if (snap) renderPage(); else renderHeader();
    if (page === 'chart') { renderChartToolbar(); loadChart(); }
    if (page === 'einstellungen') loadSettings();
    load();
  }

  function renderPage() {
    renderHeader();
    showPages();
    if (stocksPending()) { renderSetup(); if (page !== 'einstellungen') return; }
    if (page === 'uebersicht') renderOverview();
    if (page === 'chart') renderChartToolbar();
    if (page === 'trades') renderTrades();
    if (page === 'strategien') renderStrategies();
  }

  function renderSetup() {
    var s = snap;
    $('stocks-problem').textContent = !s ? 'Lade …' : !s.enabled ? 'Aktienhandel ist in config.yaml ausgeschaltet (stocks.enabled).'
      : s.problem === 'Alpaca-Schluessel fehlen' ? 'Noch nicht eingerichtet: Es fehlen die Alpaca-Schlüssel.' : (s.problem || 'Der Aktien-Bot startet gerade …');
  }

  // ------------------------------------------------------------ Kopf
  function renderHeader() {
    var s = snap;
    if (!s || (market === 'stocks' && !s.ready)) {
      $('mode').textContent = 'Papier'; $('mode').className = 'badge paper';
      $('run').className = 'dot'; $('run-text').textContent = s ? 'nicht aktiv' : 'lade …';
      $('banner').hidden = !restarting;
      if (restarting) $('banner').textContent = 'Der Bot startet mit den neuen Einstellungen neu …';
      return;
    }
    var mode = $('mode');
    mode.textContent = s.mode === 'live' ? 'Live' : 'Papier';
    mode.className = 'badge ' + (s.mode === 'live' ? 'live' : 'paper');
    $('run').className = 'dot' + (s.running ? (s.paused || s.halted ? ' warn' : ' on') : '');
    $('run-text').textContent = !s.running ? 'angehalten' : (s.paused ? 'pausiert' : s.halted ? 'Tageslimit' : clock(s.last_cycle));
    document.title = (s.mode === 'live' ? 'LIVE · ' : '') + 'Trading Bot';
    var msg = null;
    if (restarting) msg = 'Der Bot startet mit den neuen Einstellungen neu …';
    else if (s.mode === 'live') msg = 'Live-Modus: Der Bot handelt mit echtem Geld.';
    else if (s.halted) msg = 'Tagesverlustlimit erreicht. Bis morgen keine neuen Einstiege.';
    else if (s.paused) msg = 'Neue Einstiege sind pausiert.';
    else if (market === 'stocks' && !s.market_open) msg = 'Die US-Börse ist zu. Sie öffnet Mo–Fr um 15:30 Uhr deutscher Zeit.';
    var b = $('banner');
    b.hidden = !msg;
    if (msg) b.textContent = msg;
  }

  // ------------------------------------------------------------ Übersicht
  function renderOverview() {
    var s = snap, q = s.quote, st = s.stats;
    $('hero-label').textContent = market === 'stocks' ? 'Aktienkonto' : 'Kapital';
    var hero = clear($('hero-equity'));
    hero.appendChild(document.createTextNode(nf(s.equity, 2)));
    hero.appendChild(el('small', '', q));
    var hs = clear($('hero-stats'));
    var stat = function (label, value, cls) { var sp = el('span', '', label + ' '); sp.appendChild(el('b', cls || '', value)); hs.appendChild(sp); };
    if (market === 'stocks') stat('Börse', s.market_open ? 'offen' : 'zu', s.market_open ? 'pos' : '');
    stat('Heute', signed(s.day_pnl_pct, 2, ' %'), tone(s.day_pnl_pct));
    stat('Gesamt', signed(s.return_pct, 2, ' %'), tone(s.return_pct));
    stat('Frei', nf(s.free_quote, 2));
    if (st.n_trades) stat('Treffer', nf(st.win_rate_pct, 0) + ' %');

    var pos = clear($('positions'));
    if (!s.positions.length) pos.appendChild(el('p', 'empty', 'Keine offene Position.'));
    s.positions.forEach(function (p) { pos.appendChild(positionRow(p, q)); });

    var coins = clear($('coins'));
    if (!s.market.length) coins.appendChild(el('p', 'empty', 'Der Bot sammelt gerade die ersten Kerzen.'));
    s.market.forEach(function (v) { coins.appendChild(coinRow(v)); });

    renderEquity(s);
    var btn = $('toggle');
    btn.textContent = s.paused ? 'Fortsetzen' : 'Pausieren';
    btn.className = 'btn small' + (s.paused ? ' primary' : '');
    btn.hidden = !s.controls;
  }

  function dots(v) {
    var votes = v.votes || [];
    var longs = votes.filter(function (x) { return x.vote === 'long'; }).length;
    var shorts = votes.filter(function (x) { return x.vote === 'short'; }).length;
    var d = el('span', 'dots');
    d.setAttribute('role', 'img');
    d.setAttribute('aria-label', longs + ' von ' + votes.length + ' für Long, ' + shorts + ' für Short, Einstieg ab ' + (v.min_votes || 1));
    for (var i = 0; i < votes.length; i++) d.appendChild(el('i', i < longs ? 'l' : (i >= votes.length - shorts ? 's' : '')));
    return d;
  }

  function coinRow(v) {
    var a = el('a', 'row');
    a.href = '#/chart?s=' + encodeURIComponent(v.symbol);
    var main = el('span', 'row-main');
    var t = el('span', 'row-title', coin(v.symbol));
    if (v.position) t.appendChild(sideTag(v.position.side));
    main.appendChild(t);
    main.appendChild(el('span', 'row-sub', v.status || ''));
    a.appendChild(main);
    var end = el('span', 'row-end');
    end.appendChild(el('span', 'row-value', price(v.price)));
    if (v.votes) end.appendChild(dots(v));
    a.appendChild(end);
    a.appendChild(chevron());
    return a;
  }

  function positionRow(p, q) {
    var a = el('a', 'row');
    a.href = '#/chart?s=' + encodeURIComponent(p.symbol);
    var main = el('span', 'row-main');
    var t = el('span', 'row-title', coin(p.symbol));
    t.appendChild(sideTag(p.side));
    if (triggerTag(p.strategy)) t.appendChild(triggerTag(p.strategy));
    if (p.leverage > 1) t.appendChild(el('span', 'row-note', nf(p.leverage, 0) + 'x'));
    main.appendChild(t);
    main.appendChild(el('span', 'row-sub', 'Einstieg ' + price(p.entry_price) + ' · Stop ' + price(p.stop_loss) + (p.take_profit === null ? '' : ' · Ziel ' + price(p.take_profit))));
    main.appendChild(el('span', 'row-sub', nf(p.notional, 0) + ' ' + q + ' · Risiko ' + nf(p.risk_quote, 2) + (p.liquidation ? ' · Liq. ' + price(p.liquidation) : '') + ' · seit ' + clock(p.opened_at)));
    if (p.stop_kind) main.appendChild(el('span', 'row-sub', 'Stop gerade: ' + stopKindText(p.stop_kind) + (p.initial_stop ? ' · beim Einstieg ' + price(p.initial_stop) : '')));
    a.appendChild(main);
    var end = el('span', 'row-end');
    end.appendChild(el('span', 'row-value big ' + tone(p.pnl_quote), p.pnl_quote === null ? '–' : signed(p.pnl_quote, 2)));
    end.appendChild(el('span', 'row-note ' + tone(p.pnl_pct), p.pnl_pct === null ? '' : signed(p.pnl_pct, 2, ' %')));
    a.appendChild(end);
    return a;
  }

  function renderEquity(s) { curve($('equity'), s.equity_history || []); }

  function curve(target, h) {
    var box = clear(target);
    if (h.length < 2) { box.appendChild(el('p', 'empty', 'Der Verlauf erscheint nach den ersten Minuten.')); return; }
    var W = Math.max(300, box.clientWidth || 600), H = 110, pt = 6, pb = 6;
    var xs = h.map(function (p) { return new Date(p[0]).getTime(); }), ys = h.map(function (p) { return p[1]; });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (hi === lo) { hi += 1; lo -= 1; }
    var x0 = xs[0], x1 = xs[xs.length - 1];
    var sx = function (x) { return (x - x0) / (x1 - x0 || 1) * W; };
    var sy = function (y) { return pt + (hi - y) / (hi - lo) * (H - pt - pb); };
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'none');
    var start = ys[0], last = ys[ys.length - 1];
    var color = css(last >= start ? '--gain' : '--loss');
    var pts = h.map(function (p, i) { return sx(xs[i]).toFixed(1) + ',' + sy(ys[i]).toFixed(1); });
    var area = document.createElementNS(ns, 'polygon');
    area.setAttribute('points', '0,' + H + ' ' + pts.join(' ') + ' ' + W + ',' + H);
    area.style.fill = color; area.style.opacity = '0.08';
    svg.appendChild(area);
    var base = document.createElementNS(ns, 'line');
    [['x1', 0], ['x2', W], ['y1', sy(start)], ['y2', sy(start)], ['stroke-dasharray', '2 4'], ['vector-effect', 'non-scaling-stroke']].forEach(function (a) { base.setAttribute(a[0], a[1]); });
    base.style.stroke = css('--line-strong');
    svg.appendChild(base);
    var line = document.createElementNS(ns, 'polyline');
    line.setAttribute('points', pts.join(' '));
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-linejoin', 'round');
    line.setAttribute('vector-effect', 'non-scaling-stroke');
    line.style.stroke = color;
    svg.appendChild(line);
    box.appendChild(svg);
  }

  // ------------------------------------------------------------ Chart
  function chartSrc() { return snap; }

  function renderChartToolbar() {
    if (!snap || stocksPending()) return;
    var src = snap;
    var symbols = src.symbols;
    if (!chartSymbol || symbols.indexOf(chartSymbol) < 0) chartSymbol = symbols[0];
    // Knoepfe nur neu bauen, wenn sich etwas geaendert hat. Sonst gehen Tipps verloren, die genau in eine
    // Aktualisierung fallen (Finger runter auf dem alten, hoch auf dem neuen Knopf).
    var key = JSON.stringify([market, symbols, chartSymbol, chartTf, (src.market || []).map(function (m) { return m.position ? m.position.side : ''; })]);
    if (key === C.toolbarKey && page === 'chart' && C.symbol === chartSymbol) return;
    C.toolbarKey = key;
    var seg = clear($('chart-coins'));
    symbols.forEach(function (sym) {
      var v = (src.market || []).filter(function (m) { return m.symbol === sym; })[0];
      var b = el('button', '', coin(sym) + (v && v.position ? ' · ' + sideText(v.position.side) : ''));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', sym === chartSymbol ? 'true' : 'false');
      b.addEventListener('click', function () {
        if (sym === chartSymbol) return;
        chartSymbol = sym;
        history.replaceState(null, '', '#/chart?s=' + encodeURIComponent(sym));
        resetChart(); renderChartToolbar(); loadChart();
      });
      seg.appendChild(b);
    });
    var rg = clear($('chart-tf'));
    var current = chartTf || src.timeframe;
    TIMEFRAMES.forEach(function (r) {
      var b = el('button', '', r[1]);
      b.type = 'button';
      b.setAttribute('aria-pressed', r[0] === current ? 'true' : 'false');
      b.setAttribute('aria-label', 'Kerzen ' + r[1]);
      b.addEventListener('click', function () {
        if (r[0] === current) return;
        chartTf = r[0] === src.timeframe ? null : r[0];
        resetChart(); renderChartToolbar(); loadChart();
      });
      rg.appendChild(b);
    });
    // direkt geoeffnet: das Paar steht erst nach den ersten Daten fest, dann sofort laden
    if (page === 'chart' && C.symbol !== chartSymbol) { resetChart(); loadChart(); }
  }

  function chartColors() {
    return {
      text: css('--muted'), grid: css('--line'), border: css('--line'), up: css('--gain'), down: css('--loss'),
      main: css('--accent'), long: css('--long'), short: css('--short'), trend: css('--muted'), ink: css('--text')
    };
  }

  // Chart komplett verwerfen: neues Paar, neue Preisskala, keine Reste vom vorigen
  function resetChart() {
    if (C.chart) { try { C.chart.remove(); } catch (e) { /* schon weg */ } }
    C.chart = null; C.candles = null; C.lines = {}; C.markers = null; C.priceLines = []; C.data = null; C.firstFit = true; C.symbol = chartSymbol;
    C.req++; C.busy = false; C.fullAt = 0;
    clear($('chart'));
    clear($('chart-lines'));
    clear($('readout'));
  }

  function ensureChart() {
    if (C.chart || !LC) return !!C.chart;
    var col = chartColors();
    C.chart = LC.createChart($('chart'), {
      autoSize: true,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: col.text, fontFamily: css('--font'), fontSize: 11, attributionLogo: true },
      grid: { vertLines: { visible: false }, horzLines: { color: col.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: {
        borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 4,
        tickMarkFormatter: function (t, type) {
          var d = new Date(t * 1000);
          return type < 3 ? d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) : d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
        }
      },
      localization: {
        locale: 'de-DE',
        priceFormatter: price,
        timeFormatter: function (t) { return new Date(t * 1000).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }); }
      },
      crosshair: { mode: LC.CrosshairMode.Normal }
    });
    C.candles = C.chart.addSeries(LC.CandlestickSeries, {
      upColor: col.up, downColor: col.down, wickUpColor: col.up, wickDownColor: col.down, borderVisible: false, priceLineColor: col.main
    });
    C.markers = LC.createSeriesMarkers(C.candles, []);
    C.chart.subscribeCrosshairMove(showReadout);
    return true;
  }

  function lineStyle(style) {
    if (style === 'trend') return LC.LineStyle.Dotted;
    if (style === 'long' || style === 'short') return LC.LineStyle.Dashed;
    return LC.LineStyle.Solid;
  }

  var CHART_FULL_MS = 20000; // so oft kommt der Chart komplett (Trades, Signal-Marken), dazwischen nur das Neue
  function loadChart(force) {
    if (page !== 'chart' || !chartSymbol) return;
    if (!LC) { clear($('chart')).appendChild(el('p', 'empty', 'Die Chart-Bibliothek konnte nicht geladen werden.')); return; }
    if (C.busy && !force) return; // vorige Anfrage laeuft noch: nicht stauen
    var req = ++C.req, sym = chartSymbol, tf = chartTf || '';
    var api = API[market].chart;
    var have = C.data && C.data.candles && C.data.candles.length && C.tf === tf && C.data.symbol === sym;
    var delta = have && !force && Date.now() - (C.fullAt || 0) < CHART_FULL_MS;
    var url = api + '?symbol=' + encodeURIComponent(sym) + (tf ? '&tf=' + tf : '')
      + (delta ? '&since=' + C.data.candles[C.data.candles.length - 1][0] : '');
    C.busy = true;
    getJSON(url).then(function (d) {
      if (req !== C.req || sym !== chartSymbol || page !== 'chart') return; // veraltet
      if (d.delta) {
        var merged = mergeChart(C.data, d);
        if (!merged) { C.fullAt = 0; return; } // passt nicht zusammen: naechstes Mal komplett
        drawChart(merged);
      } else {
        C.tf = tf; C.fullAt = Date.now();
        drawChart(d);
      }
    }).catch(function (e) {
      if (req === C.req) $('chart-status').textContent = 'Chart nicht verfügbar: ' + e.message;
    }).then(function () { if (req === C.req) C.busy = false; });
  }

  // Neue Kerzen und Linienwerte an den bisherigen Chart haengen (ab der ersten Kerze der Antwort ersetzen)
  function mergeChart(old, d) {
    if (!d.candles.length) return null;
    var oc = old.candles, t = d.candles[0][0], k = oc.length;
    while (k > 0 && oc[k - 1][0] >= t) k--;
    if (k === oc.length && oc.length && oc[k - 1][0] < t - 10 * 86400000) return null;
    var lines = [];
    var byKey = {};
    (old.lines || []).forEach(function (l) { byKey[l.key] = l; });
    for (var i = 0; i < (d.lines || []).length; i++) {
      var nl = d.lines[i], ol = byKey[nl.key];
      if (!ol) return null; // neue Linie: lieber komplett laden
      var m = Object.assign({}, nl, { values: ol.values.slice(0, k).concat(nl.values) });
      if (nl.directions) m.directions = (ol.directions || []).slice(0, k).concat(nl.directions);
      lines.push(m);
    }
    var candles = oc.slice(0, k).concat(d.candles);
    var cut = Math.max(0, candles.length - 1000);
    if (cut) {
      candles = candles.slice(cut);
      lines.forEach(function (l) { l.values = l.values.slice(cut); if (l.directions) l.directions = l.directions.slice(cut); });
    }
    var out = Object.assign({}, d, { candles: candles, lines: lines, trades: old.trades, markers: old.markers });
    delete out.delta;
    return out;
  }

  function drawChart(d) {
    ensureChart();
    var col = chartColors();
    var candles = d.candles || [];
    var tf = candles.length > 1 ? candles[1][0] - candles[0][0] : 60000;
    var range = C.firstFit ? null : C.chart.timeScale().getVisibleLogicalRange();
    C.data = d;
    C.candles.setData(candles.map(function (k) { return { time: Math.floor(k[0] / 1000), open: k[1], high: k[2], low: k[3], close: k[4] }; }));

    // Linien der Strategie: vorhandene behalten, neue anlegen, verschwundene entfernen
    var seen = {};
    (d.lines || []).forEach(function (l) {
      seen[l.key] = true;
      if (!(l.key in C.hidden)) C.hidden[l.key] = !l.visible;
      var s = C.lines[l.key];
      if (!s) {
        s = C.chart.addSeries(LC.LineSeries, {
          color: col[l.style] || col.main, lineWidth: l.style === 'main' ? 2 : 1.5, lineStyle: lineStyle(l.style),
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title: ''
        });
        C.lines[l.key] = s;
      }
      s.applyOptions({ visible: !C.hidden[l.key] });
      s.setData(l.values.map(function (val, i) {
        var t = Math.floor(candles[i][0] / 1000);
        if (val === null) return { time: t };
        var pt = { time: t, value: val };
        // Linie mit Richtung (SuperTrend): Farbe je Kerze, beim Richtungswechsel eine Luecke wie im Indikator
        if (l.directions) {
          var dir = l.directions[i];
          if (i > 0 && l.directions[i - 1] && dir && l.directions[i - 1] !== dir) return { time: t };
          pt.color = dir === 'short' ? col.short : col.long;
        }
        return pt;
      }));
      s._label = l.label; s._style = l.style;
    });
    Object.keys(C.lines).forEach(function (k) { if (!seen[k]) { C.chart.removeSeries(C.lines[k]); delete C.lines[k]; } });

    // Trades als Markierungen genau auf Ein- und Ausstiegskurs
    var t0 = candles.length ? candles[0][0] : 0, tEnd = candles.length ? candles[candles.length - 1][0] + tf : 0;
    var snapT = function (iso) { var ms = Date.parse(iso); return Math.floor(Math.floor(ms / tf) * tf / 1000); };
    var marks = [];
    (d.trades || []).forEach(function (t) {
      var o = Date.parse(t.opened_at), c = Date.parse(t.closed_at);
      if (c < t0 || o > tEnd) return;
      var sc = t.side === 'short' ? col.short : col.long;
      if (o >= t0) marks.push({ time: snapT(t.opened_at), position: 'atPriceMiddle', price: t.entry_price, shape: t.side === 'short' ? 'arrowDown' : 'arrowUp', color: sc, text: sideText(t.side) + (t.strategy ? ' · ' + triggerText(t.strategy) : '') });
      if (c <= tEnd) marks.push({ time: snapT(t.closed_at), position: 'atPriceMiddle', price: t.exit_price, shape: 'circle', color: t.pnl_quote > 0 ? col.up : col.down, text: signed(t.pnl_quote, 2) });
    });
    if (d.position && d.position.opened_at && Date.parse(d.position.opened_at) >= t0) {
      marks.push({ time: snapT(d.position.opened_at), position: 'atPriceMiddle', price: d.position.entry, shape: d.position.side === 'short' ? 'arrowDown' : 'arrowUp', color: d.position.side === 'short' ? col.short : col.long, text: sideText(d.position.side) + ' offen' + (d.position.strategy ? ' · ' + triggerText(d.position.strategy) : '') });
    }
    // Signale der Strategie (z. B. LuxAlgo: Dreieck mit Staerke 1-4) unter bzw. ueber der Kerze
    (d.markers || []).forEach(function (m) {
      if (m.time < t0) return;
      var time = Math.floor(m.time / 1000);
      // X-Marke (Umkehrwarnung): ueber der Kerze schliesst Long, unter der Kerze schliesst Short
      if (m.side === 'exit_long' || m.side === 'exit_short') {
        marks.push({ time: time, position: m.side === 'exit_long' ? 'aboveBar' : 'belowBar', shape: 'circle', size: 0.5,
          color: m.side === 'exit_long' ? col.long : col.short, text: '✖' });
        return;
      }
      marks.push({ time: time, position: m.side === 'short' ? 'aboveBar' : 'belowBar', shape: m.side === 'short' ? 'arrowDown' : 'arrowUp',
        color: m.side === 'short' ? col.down : col.up, text: m.text });
    });
    marks.sort(function (a, b) { return a.time - b.time; });
    C.markers.setMarkers(marks);

    // offene Position: Einstieg, Stop und Ziel als Linien mit Preis an der Achse
    C.priceLines.forEach(function (pl) { C.candles.removePriceLine(pl); });
    C.priceLines = [];
    if (d.position) {
      var p = d.position;
      [[p.entry, col.ink, sideText(p.side)], [p.stop, col.down, 'Stop'], [p.take_profit, col.up, 'Ziel']].forEach(function (x) {
        if (x[0] === null || x[0] === undefined) return;
        C.priceLines.push(C.candles.createPriceLine({ price: x[0], color: x[1], lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: x[2] }));
      });
    }

    if (range) C.chart.timeScale().setVisibleLogicalRange(range);
    else {
      // Beim ersten Zeichnen kennt der Chart seine Breite oft noch nicht (autoSize misst erst danach).
      // Sonst bleibt er beim Standard-Kerzenabstand haengen und zeigt nur einen Ausschnitt.
      fitRange();
      requestAnimationFrame(fitRange);
      setTimeout(fitRange, 250);
    }
    C.firstFit = false;
    renderChips();
    showReadout(null);
    $('chart-status').textContent = d.status || '';
    var se = d.session;
    $('chart-session').textContent = se ? 'Einstiege ' + se.window + ' (' + (se.entry_open ? 'jetzt möglich' : 'jetzt nicht') + ') · heute ' + se.entries_today + (se.max_entries ? ' von ' + se.max_entries : '') + ' · alles zu um ' + se.flat : '';
    var ul = clear($('chart-explain'));
    (d.explain || []).forEach(function (t) { ul.appendChild(el('li', '', t)); });
    var note = $('chart-note');
    note.hidden = !d.lines_note;
    note.textContent = d.lines_note || '';
    renderChartTrades(d);
    if (C.focus) { var f = C.focus; C.focus = null; focusTrade(f); }
  }

  // Trades dieses Paars unter dem Chart, neueste zuerst
  function renderChartTrades(d) {
    var trades = (d.trades || []).slice().reverse();
    $('chart-trades-title').textContent = 'Trades mit ' + coin(d.symbol) + (trades.length ? ' · ' + trades.length : '');
    var list = clear($('chart-trades'));
    if (!trades.length) { list.appendChild(el('p', 'empty', 'Mit ' + coin(d.symbol) + ' gab es noch keinen Trade.')); return; }
    var q = chartSrc() ? chartSrc().quote : '';
    trades.forEach(function (t) {
      var r = tradeRow(t, q, false);
      r.classList.add('tap');
      r.tabIndex = 0;
      r.setAttribute('role', 'button');
      r.setAttribute('aria-label', 'Trade ' + sideText(t.side) + ' vom ' + when(t.opened_at) + ' im Chart zeigen');
      var go = function () { focusTrade(t); window.scrollTo({ top: 0, behavior: 'smooth' }); };
      r.addEventListener('click', go);
      r.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      list.appendChild(r);
    });
  }

  // Trade im Chart zeigen. Liegt er ausserhalb der geladenen Kerzen, in einen groesseren Zeitrahmen wechseln.
  function focusTrade(t) {
    if (!C.chart || !C.data) return;
    var c = C.data.candles || [];
    var o = Date.parse(t.opened_at), x = Date.parse(t.closed_at);
    if (c.length && o >= c[0][0]) {
      var i0 = 0, i1 = c.length - 1;
      for (var i = 0; i < c.length; i++) { if (c[i][0] <= o) i0 = i; if (c[i][0] <= x) i1 = i; }
      var pad = Math.max(20, Math.round((i1 - i0) * 0.8));
      C.chart.timeScale().setVisibleLogicalRange({ from: i0 - pad, to: i1 + pad });
      return;
    }
    var age = (Date.now() - o) / 1000;
    var tf = TIMEFRAMES.map(function (r) { return r[0]; }).filter(function (k) { return TF_SECONDS[k] * 450 > age; })[0] || '1d';
    var base = chartSrc().timeframe;
    if (tf === (chartTf || base)) return;
    chartTf = tf === base ? null : tf;
    resetChart(); C.focus = t; renderChartToolbar(); loadChart();
  }

  function fitRange() {
    if (!C.chart || !C.data || C.focus) return;
    var n = (C.data.candles || []).length;
    C.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - CHART_BARS), to: n + 3 });
    C.chart.priceScale('right').applyOptions({ autoScale: true });
  }

  function renderChips() {
    var box = clear($('chart-lines'));
    Object.keys(C.lines).forEach(function (k) {
      var s = C.lines[k];
      var b = el('button', 'chip');
      b.type = 'button';
      b.setAttribute('aria-pressed', C.hidden[k] ? 'false' : 'true');
      var mark = el('i', s._style === 'main' ? '' : 'dash');
      mark.style.color = chartColors()[s._style] || chartColors().main;
      b.appendChild(mark);
      b.appendChild(document.createTextNode(s._label));
      b.addEventListener('click', function () {
        C.hidden[k] = !C.hidden[k];
        s.applyOptions({ visible: !C.hidden[k] });
        b.setAttribute('aria-pressed', C.hidden[k] ? 'false' : 'true');
        showReadout(null);
      });
      box.appendChild(b);
    });
  }

  function showReadout(param) {
    var box = clear($('readout'));
    if (!C.data || !C.data.candles || !C.data.candles.length) return;
    var bar = param && param.time !== undefined ? param.seriesData.get(C.candles) : null;
    var idx = C.data.candles.length - 1;
    if (bar) {
      var t = bar.time * 1000;
      for (var i = C.data.candles.length - 1; i >= 0; i--) if (C.data.candles[i][0] <= t) { idx = i; break; }
    }
    var k = C.data.candles[idx];
    var item = function (label, value) { var s = el('span', '', label ? label + ' ' : ''); s.appendChild(el('b', '', value)); box.appendChild(s); };
    var tfLabel = (TIMEFRAMES.filter(function (r) { return r[0] === (C.data.timeframe || ''); })[0] || ['', ''])[1];
    item('', coin(C.data.symbol) + ' · ' + tfLabel + ' · ' + (C.data.timeframe === '1d' ? new Date(k[0]).toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) : when(new Date(k[0]).toISOString())));
    item('E', price(k[1]));
    item('S', price(k[4]));
    item('H', price(k[2])); item('T', price(k[3]));
  }

  // ------------------------------------------------------------ Trades
  function statBox(label, value, sub, cls) {
    var d = el('dl', 'stat');
    d.appendChild(el('dt', '', label));
    var dd = el('dd', cls || '', value);
    d.appendChild(dd);
    if (sub) d.appendChild(el('span', 'sub', sub));
    return d;
  }

  function renderTrades() {
    var s = snap, st = s.stats, q = s.quote;
    var f = clear($('trade-figures'));
    f.appendChild(statBox('Ergebnis', st.n_trades ? signed(st.total_pnl, 2) : '–', q + ' · Gebühren ' + nf(st.fees, 2), tone(st.total_pnl)));
    f.appendChild(statBox('Treffer', st.win_rate_pct === null ? '–' : nf(st.win_rate_pct, 0) + ' %', st.wins + ' von ' + st.n_trades));
    f.appendChild(statBox('Profitfaktor', st.profit_factor === null ? '–' : nf(st.profit_factor, 2), 'Gewinne ÷ Verluste'));

    var seg = clear($('trade-filter'));
    [null].concat(s.symbols).forEach(function (sym) {
      var b = el('button', '', sym ? coin(sym) : 'Alle');
      b.type = 'button';
      b.setAttribute('aria-pressed', tradeFilter === sym ? 'true' : 'false');
      b.addEventListener('click', function () { tradeFilter = sym; renderTrades(); });
      seg.appendChild(b);
    });

    var list = clear($('trade-list'));
    var trades = s.trades.filter(function (t) { return !tradeFilter || t.symbol === tradeFilter; });
    if (!trades.length) list.appendChild(el('p', 'empty', 'Noch keine abgeschlossenen Trades' + (tradeFilter ? ' für ' + coin(tradeFilter) : '') + '.'));
    trades.forEach(function (t) {
      var r = tradeRow(t, q, true);
      list.appendChild(r);
    });
    $('trade-filter').hidden = tradeView !== 'list';
    $('trade-list-view').hidden = tradeView !== 'list';
    $('trade-stats-view').hidden = tradeView !== 'stats';
    document.querySelectorAll('#trade-view button').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-view') === tradeView ? 'true' : 'false'); });
    if (tradeView === 'stats') renderStats(s.analytics, q);
  }

  // Ein Trade als Listenzeile. withChart: Tippen oeffnet den Chart an dieser Stelle.
  function tradeRow(t, q, withChart) {
    var r = el(withChart ? 'a' : 'div', 'row');
    if (withChart) {
      r.href = '#/chart?s=' + encodeURIComponent(t.symbol);
      r.addEventListener('click', function () { C.focus = t; });
    }
    var main = el('span', 'row-main');
    var title = el('span', 'row-title', coin(t.symbol));
    title.appendChild(sideTag(t.side));
    if (triggerTag(t.strategy)) title.appendChild(triggerTag(t.strategy));
    main.appendChild(title);
    main.appendChild(el('span', 'row-sub', when(t.opened_at) + ' – ' + clock(t.closed_at) + ' · ' + duration(t.opened_at, t.closed_at) + ' · ' + reasonText(t.reason)));
    if (t.strategy && t.strategy.indexOf('+') > 0) main.appendChild(el('span', 'row-sub', 'Ausgelöst von ' + t.strategy.split('+').map(function (n) { return SHORT[n] || n; }).join(', ')));
    if (stopInfo(t)) main.appendChild(el('span', 'row-sub wrap', stopInfo(t)));
    main.appendChild(el('span', 'row-sub', price(t.entry_price) + ' → ' + price(t.exit_price) + ' · ' + nf(t.amount * t.entry_price, 0) + ' ' + q));
    r.appendChild(main);
    var end = el('span', 'row-end');
    end.appendChild(el('span', 'row-value big ' + tone(t.pnl_quote), signed(t.pnl_quote, 2)));
    end.appendChild(el('span', 'row-note ' + tone(t.pnl_pct), signed(t.pnl_pct, 2, ' %')));
    r.appendChild(end);
    return r;
  }

  function duration(a, b) { return minutes((Date.parse(b) - Date.parse(a)) / 60000); }
  function minutes(m) {
    if (m === null || m === undefined || isNaN(m)) return '–';
    if (m < 1) return '< 1 min';
    if (m < 60) return nf(m, 0) + ' min';
    if (m < 1440) return nf(m / 60, 1) + ' Std';
    return nf(m / 1440, 1) + ' Tage';
  }

  // ------------------------------------------------------------ Statistik
  function kvList(rows) {
    var list = el('div', 'list kv');
    rows.forEach(function (x) {
      if (!x) return;
      var r = el('div', 'row');
      var label = el('span', 'row-main');
      label.appendChild(el('span', '', x[0]));
      if (x[3]) label.appendChild(el('span', 'row-sub wrap', x[3]));
      r.appendChild(label);
      r.appendChild(el('span', 'row-value ' + (x[2] || ''), x[1]));
      list.appendChild(r);
    });
    return list;
  }

  function groupTitle(box, text) { box.appendChild(el('h2', 'group-title', text)); }

  // Balken fuer Gewinn/Verlust je Gruppe (Stunde, Wochentag, Tag). Nach oben gruen, nach unten rot.
  function pnlBars(items, labelEvery) {
    var wrap = el('div', 'bars');
    var ns = 'http://www.w3.org/2000/svg';
    var W = 300, H = 120, mid = H / 2;
    var max = Math.max.apply(null, items.map(function (x) { return Math.abs(x.pnl); }).concat([1e-9]));
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', items.map(function (x) { return x.label + ': ' + signed(x.pnl, 2) + ' (' + x.n + ' Trades)'; }).join(', '));
    var base = document.createElementNS(ns, 'line');
    [['x1', 0], ['x2', W], ['y1', mid], ['y2', mid], ['vector-effect', 'non-scaling-stroke']].forEach(function (a) { base.setAttribute(a[0], a[1]); });
    base.style.stroke = css('--line-strong');
    svg.appendChild(base);
    var bw = W / items.length;
    items.forEach(function (x, i) {
      if (!x.n) {
        var dot = document.createElementNS(ns, 'rect');
        [['x', i * bw + bw * 0.3], ['y', mid - 1], ['width', bw * 0.4], ['height', 2], ['rx', 1]].forEach(function (a) { dot.setAttribute(a[0], a[1]); });
        dot.style.fill = css('--line');
        svg.appendChild(dot);
        return;
      }
      var h = Math.max(2, Math.abs(x.pnl) / max * (mid - 4));
      var rect = document.createElementNS(ns, 'rect');
      [['x', i * bw + bw * 0.18], ['y', x.pnl >= 0 ? mid - h : mid], ['width', bw * 0.64], ['height', h], ['rx', Math.min(3, bw * 0.2)]].forEach(function (a) { rect.setAttribute(a[0], a[1]); });
      rect.style.fill = css(x.pnl >= 0 ? '--gain' : '--loss');
      var tt = document.createElementNS(ns, 'title');
      tt.textContent = x.label + ': ' + signed(x.pnl, 2) + ' · ' + x.n + ' Trades';
      rect.appendChild(tt);
      svg.appendChild(rect);
    });
    wrap.appendChild(svg);
    var lg = el('div', 'legend');
    items.forEach(function (x, i) { if (i % labelEvery === 0) lg.appendChild(el('span', '', x.label)); });
    wrap.appendChild(lg);
    return wrap;
  }

  function streakText(n) { return n > 0 ? n + ' Gewinne in Folge' : n < 0 ? (-n) + ' Verluste in Folge' : '–'; }

  function renderStats(a, q) {
    var box = clear($('trade-stats-view'));
    if (!a || !a.n) { box.appendChild(el('p', 'empty', 'Die Statistik erscheint nach den ersten abgeschlossenen Trades.')); return; }
    var big = el('div', 'big-stats');
    big.appendChild(statBox('Ergebnis', signed(a.total_pnl, 2), q + ' nach Gebühren', tone(a.total_pnl)));
    big.appendChild(statBox('Je Trade', signed(a.expectancy, 2), signed(a.expectancy_pct, 2, ' %') + ' im Schnitt', tone(a.expectancy)));
    big.appendChild(statBox('Trefferquote', nf(a.win_rate_pct, 0) + ' %', a.wins + ' Gewinner, ' + a.losses + ' Verlierer'));
    big.appendChild(statBox('Profitfaktor', a.profit_factor === null ? '–' : nf(a.profit_factor, 2), 'über 1 heißt Gewinn'));
    box.appendChild(big);

    groupTitle(box, 'Ergebnis');
    box.appendChild(kvList([
      ['Ohne Gebühren', signed(a.pnl_before_fees, 2) + ' ' + q, tone(a.pnl_before_fees)],
      ['Gebühren', '−' + nf(a.fees, 2) + ' ' + q, 'neg', 'Im Schnitt ' + nf(a.fees / a.n, 2) + ' ' + q + ' je Trade'],
      ['Summe Gewinne', signed(a.gross_profit, 2) + ' ' + q, 'pos'],
      ['Summe Verluste', signed(-a.gross_loss, 2) + ' ' + q, 'neg'],
      ['Größter Rückgang', a.max_drawdown_pct === null ? '–' : '−' + nf(a.max_drawdown_pct, 2) + ' %', 'neg', a.max_drawdown_abs ? 'vom Höchststand, ' + nf(a.max_drawdown_abs, 2) + ' ' + q : null]
    ]));

    groupTitle(box, 'Gewinner und Verlierer');
    box.appendChild(kvList([
      ['Ø Gewinn', signed(a.avg_win, 2) + ' ' + q, 'pos'],
      ['Ø Verlust', signed(a.avg_loss, 2) + ' ' + q, 'neg'],
      ['Gewinn ÷ Verlust', a.payoff === null ? '–' : nf(a.payoff, 2), '', a.payoff !== null && a.win_rate_pct !== null ? 'Bei ' + nf(a.win_rate_pct, 0) + ' % Treffern lohnt es sich ab ' + nf((100 - a.win_rate_pct) / Math.max(a.win_rate_pct, 1e-9), 2) + '.' : null],
      ['Bester Trade', signed(a.best, 2) + ' ' + q, 'pos'],
      ['Schlechtester Trade', signed(a.worst, 2) + ' ' + q, 'neg'],
      ['Längste Gewinnserie', String(a.best_streak)],
      ['Längste Verlustserie', String(a.worst_streak)],
      ['Gerade', streakText(a.current_streak), tone(a.current_streak)]
    ]));

    groupTitle(box, 'Haltedauer');
    box.appendChild(kvList([
      ['Im Schnitt', minutes(a.hold_avg_min), '', 'Median ' + minutes(a.hold_median_min)],
      ['Gewinner', minutes(a.hold_win_min)],
      ['Verlierer', minutes(a.hold_loss_min)],
      ['Trades pro Tag', a.trades_per_day === null ? '–' : nf(a.trades_per_day, 1)],
      ['Tage im Plus / Minus', a.days_green + ' / ' + a.days_red]
    ]));

    groupTitle(box, 'Long und Short');
    box.appendChild(kvList(['long', 'short'].map(function (side) {
      var g = a.by_side[side];
      return [sideText(side), g.n ? signed(g.pnl, 2) : '–', tone(g.pnl), g.n ? g.n + ' Trades · ' + nf(g.win_rate_pct, 0) + ' % Treffer' : 'keine Trades'];
    })));

    groupTitle(box, 'Paare');
    box.appendChild(kvList(a.by_symbol.map(function (g) { return [coin(g.symbol), signed(g.pnl, 2), tone(g.pnl), g.n + ' Trades · ' + nf(g.win_rate_pct, 0) + ' % Treffer']; })));

    if (a.by_strategy && a.by_strategy.length) {
      groupTitle(box, 'Nach auslösender Strategie');
      box.appendChild(kvList(a.by_strategy.map(function (g) {
        var label = g.strategy === 'unbekannt' ? 'Ohne Kennzeichen (ältere Trades)' : g.strategy.split('+').map(function (n) { return SHORT[n] || n; }).join(' + ');
        return [label, signed(g.pnl, 2), tone(g.pnl), g.n + ' Trades · ' + nf(g.win_rate_pct, 0) + ' % Treffer'];
      })));
    }

    if (a.by_stop_kind && a.by_stop_kind.length) {
      groupTitle(box, 'Stop-Loss ausgelöst durch');
      box.appendChild(kvList(a.by_stop_kind.map(function (g) {
        return [g.kind === 'unbekannt' ? 'Ohne Angabe (ältere Trades)' : stopKindText(g.kind), signed(g.pnl, 2), tone(g.pnl), g.n + ' Trades'];
      })));
    }
    if (a.by_leverage && a.by_leverage.length > 1) {
      groupTitle(box, 'Nach Hebel');
      box.appendChild(kvList(a.by_leverage.map(function (g) {
        return [g.leverage ? nf(g.leverage, 0) + 'x' : 'Ohne Angabe (ältere Trades)', signed(g.pnl, 2), tone(g.pnl), g.n + ' Trades · ' + nf(g.win_rate_pct, 0) + ' % Treffer'];
      })));
    }

    groupTitle(box, 'Ausstieg durch');
    box.appendChild(kvList(a.by_reason.map(function (g) { return [g.reason === 'signal' ? 'Gegensignal' : reasonText(g.reason), signed(g.pnl, 2), tone(g.pnl), g.n + ' Trades · ' + nf(g.win_rate_pct, 0) + ' % Treffer']; })));

    groupTitle(box, 'Letzte 14 Tage');
    box.appendChild(pnlBars(a.daily.map(function (d) { var dt = new Date(d.date + 'T12:00:00'); return { label: dt.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }), pnl: d.pnl, n: d.n }; }), 3));
    groupTitle(box, 'Nach Uhrzeit des Einstiegs');
    box.appendChild(pnlBars(a.by_hour.map(function (h) { return { label: h.hour + ' Uhr', pnl: h.pnl, n: h.n }; }), 6));
    groupTitle(box, 'Nach Wochentag');
    box.appendChild(pnlBars(a.by_weekday.map(function (d) { return { label: d.day, pnl: d.pnl, n: d.n }; }), 1));
    box.appendChild(el('p', 'footnote', 'Zeiten in ' + a.timezone + '. Gebühren sind in allen Ergebnissen schon abgezogen.'));
  }

  // ------------------------------------------------------------ Strategien
  function renderStrategies() {
    var s = snap;
    var withVotes = (s.market || []).filter(function (v) { return v.votes; });
    var first = (s.market || [])[0];
    var ref = $('referee');
    if (withVotes.length) {
      var n = withVotes[0].votes.length, k = withVotes[0].min_votes;
      ref.textContent = n + ' Strategien stimmen bei jeder Kerze ab. Einstieg ab ' + k + ' Stimmen in eine Richtung, wenn mehr dafür als dagegen sind. '
        + 'Ausstieg bei so einer Mehrheit für die Gegenrichtung, sonst über Stop, Ziel, Trailing oder Handelsschluss.';
      var soloName = (withVotes[0].votes.filter(function (x) { return x.solo; })[0] || {}).strategy;
      if (soloName) ref.textContent += ' ' + stratName(soloName) + ' reicht allein.';
    } else {
      ref.textContent = first ? 'Aktive Strategie: ' + stratName(first.strategy) + '. Für die Stimmen aller Strategien in den Einstellungen das Ensemble wählen.' : 'Noch keine Daten.';
    }
    var box = clear($('votes-tables'));
    (s.market || []).forEach(function (v) {
      var head = el('h2', 'group-title', pair(v.symbol) + ' · Stand ' + clock(v.updated));
      box.appendChild(head);
      var list = el('div', 'list');
      if (v.votes) {
        v.votes.forEach(function (x) {
          var r = el('div', 'row');
          var main = el('span', 'row-main');
          main.appendChild(el('span', '', stratName(x.strategy)));
          if (x.solo) main.appendChild(el('span', 'row-sub', 'Reicht allein'));
          r.appendChild(main);
          r.appendChild(el('span', 'vote ' + (x.vote || 'none'), x.vote ? sideText(x.vote) : 'keine'));
          list.appendChild(r);
        });
      } else {
        list.appendChild(el('p', 'empty', v.status || ''));
      }
      box.appendChild(list);
      if ((v.explain || []).length) {
        var ul = el('ul', 'reasons footnote');
        v.explain.forEach(function (t) { ul.appendChild(el('li', '', t)); });
        box.appendChild(ul);
      }
    });
  }

  // ------------------------------------------------------------ Einstellungen
  var form = {};
  function loadSettings() {
    var forMarket = market;
    getJSON(API[market].settings).then(function (d) { if (forMarket !== market) return; settingsLoaded = true; fillSettings(d); }).catch(function (e) { showFormError('Einstellungen nicht abrufbar: ' + e.message); });
  }

  function check(name, value, checked, label) {
    var l = el('label');
    var i = el('input');
    i.type = 'checkbox'; i.name = name; i.value = value; i.checked = !!checked;
    l.appendChild(i);
    l.appendChild(document.createTextNode(label || value));
    return l;
  }

  function fillSettings(d) {
    form = d;
    var v = d.values, o = d.options;
    var tickers = o.symbol_kind === 'ticker';
    $('s-symbols-title').textContent = tickers ? 'Aktien' : 'Handelspaare';
    $('s-symbol-add').placeholder = tickers ? 'z. B. AMD' : 'AVAX/USDT';
    $('s-times-title').textContent = 'Handelszeiten (' + d.locked.timezone + ')';
    $('s-times-hint').textContent = 'Uhrzeiten in ' + d.locked.timezone + (tickers ? ', also New-Yorker Zeit: 10:00 dort ist 16:00 bei uns.' : '.') + ' Einstiege pro Tag 0 heißt: unbegrenzt.';
    var symBox = clear($('s-symbols'));
    var all = o.symbols.slice();
    v.symbols.forEach(function (s) { if (all.indexOf(s) < 0) all.push(s); });
    all.forEach(function (s) { symBox.appendChild(check('symbol', s, v.symbols.indexOf(s) >= 0, s.split('/')[0])); });

    var tf = clear($('s-timeframe'));
    o.timeframes.forEach(function (t) { var op = el('option', '', t === '1m' ? '1 Minute' : t === '5m' ? '5 Minuten' : t === '15m' ? '15 Minuten' : '1 Stunde'); op.value = t; tf.appendChild(op); });
    tf.value = v.timeframe;
    var st = clear($('s-strategy'));
    o.strategies.forEach(function (n) { var op = el('option', '', stratName(n)); op.value = n; st.appendChild(op); });
    st.value = v.strategy.name;
    $('s-votes').value = v.strategy.min_votes;
    var mem = clear($('s-members'));
    o.members.forEach(function (n) { mem.appendChild(check('member', n, !v.strategy.members || v.strategy.members.indexOf(n) >= 0, stratName(n))); });
    var solo = clear($('s-solo'));
    var none = el('option', '', 'Keine'); none.value = ''; solo.appendChild(none);
    o.members.forEach(function (n) { var op = el('option', '', stratName(n)); op.value = n; solo.appendChild(op); });
    solo.value = v.strategy.solo_member || '';
    $('s-short').checked = v.strategy.allow_short;
    $('s-stopmult').value = v.strategy.stop_atr_mult;
    $('s-minstop').value = v.strategy.min_stop_pct;
    $('s-tpmult').value = v.strategy.take_profit_atr_mult;
    $('s-trailpct').value = v.strategy.trailing_pct;
    $('s-realtime').checked = !!v.strategy.realtime_signals;
    $('s-spotsig').checked = v.strategy.signal_source === 'spot';
    $('s-ststr').value = v.strategy.st_min_strength || 1;
    $('s-sttf').value = v.strategy.st_timeframe || '';
    $('s-stadx').value = v.strategy.st_adx_min || '';
    $('s-stchop').value = v.strategy.st_chop_max || '';
    $('s-stema').value = v.strategy.st_trend_ema || '';
    $('s-spotsig').disabled = d.locked.market !== 'futures';
    $('s-lev').value = v.futures.leverage;
    $('s-lev-range').value = v.futures.leverage;
    $('s-lev').max = $('s-lev-range').max = o.max_leverage;
    $('s-lev').disabled = $('s-lev-range').disabled = o.max_leverage <= 1;
    levHint();
    $('s-short').disabled = !o.short_possible;
    renderExits(v.strategy, o);
    toggleEnsemble();

    $('s-risk').value = v.risk.risk_per_trade_pct;
    $('s-maxpos').value = v.risk.max_position_pct;
    $('s-maxopen').value = v.risk.max_open_positions;
    $('s-dayloss').value = v.risk.max_daily_loss_pct;
    $('s-costratio').value = v.risk.min_target_cost_ratio;
    var pr = v.protections || { stoploss_guard_stops: 3, window_candles: 60, lock_candles: 60 };
    form.slguardStops = pr.stoploss_guard_stops > 0 ? pr.stoploss_guard_stops : 3;
    $('s-slguard').checked = pr.stoploss_guard_stops > 0;
    $('s-slguardhint').textContent = 'Sperre: Nach ' + form.slguardStops + ' Stop-Losses innerhalb von ' + pr.window_candles + ' Kerzen steigt der Bot '
      + pr.lock_candles + ' Kerzen lang nirgends ein. Aus heißt: Er handelt auch nach mehreren Stops sofort weiter.';
    var cd = pr.cooldown_candles === undefined || pr.cooldown_candles === null ? 5 : pr.cooldown_candles;
    form.cooldownCandles = cd > 0 ? cd : 5;
    $('s-cooldown').checked = cd > 0;
    form.trackTrades = pr.track_record_trades > 0 ? pr.track_record_trades : 3;
    $('s-track').checked = pr.track_record_trades > 0;
    $('s-trackhint').textContent = 'Bilanz-Filter: Vor jedem Einstieg spielt der Bot dieselbe Strategie auf den letzten Tagen dieses Paars nach. '
      + 'Haben ihre letzten ' + form.trackTrades + ' Trades im Schnitt verloren, steigt er nicht ein. Im Test über 60 Tage: weniger als halb so viel Verlust, aber noch kein Gewinn.';
    $('s-cooldownhint').textContent = 'Abkühlphase: Nach einem Ausstieg steigt der Bot ' + form.cooldownCandles + ' Kerzen lang nicht wieder in dasselbe Paar ein. Aus heißt: sofortiger Wiedereinstieg möglich.';
    $('s-costhint').textContent = 'Kosten je Trade hin und zurück: ' + nf(v.costs_pct, 2) + ' %. Bei 2 braucht ein Trade also mindestens ' + nf(2 * v.costs_pct, 2) + ' % Ziel.';
    $('s-dt').checked = v.daytrading.enabled;
    $('s-start').value = v.daytrading.entry_start;
    $('s-end').value = v.daytrading.entry_end;
    $('s-flat').value = v.daytrading.flat_time;
    $('s-maxtrades').value = v.daytrading.max_trades_per_day;

    var lk = clear($('s-locked'));
    var L = d.locked;
    lk.appendChild(document.createTextNode('Fest in config.yaml: '));
    [['', L.mode === 'live' ? 'Live' : 'Papierhandel'], ['', L.exchange], ['', L.market === 'futures' ? 'Futures' : L.market === 'stocks' ? 'US-Aktien' : 'Spot'], ['Zeitzone ', L.timezone]]
      .forEach(function (x, i) { if (i) lk.appendChild(document.createTextNode(' · ')); lk.appendChild(document.createTextNode(x[0])); lk.appendChild(el('b', '', x[1])); });
    $('s-reset').hidden = !d.has_override;
    showFormError(null);
  }

  function levHint() {
    var lev = parseFloat($('s-lev').value) || 1;
    var liq = lev > 1 ? Math.max(100 / lev - 0.5, 0) : null;
    $('s-levhint').textContent = lev <= 1 ? 'Ohne Hebel bindet eine Position ihren vollen Wert. „Einsatz je Position“ ist dann ihr Wert.'
      : 'Bei ' + nf(lev, 0) + 'x bindet eine Position 1/' + nf(lev, 0) + ' ihres Werts. Liquidation etwa ' + nf(liq, 2)
        + ' % gegen dich, der Stop liegt deshalb höchstens ' + nf(liq * 0.8, 2) + ' % entfernt. Das Risiko je Trade bleibt gleich, die Position wird größer.';
  }

  var EXIT_FIELDS = [['stop_atr_mult', 'Stop x ATR', '0.5', 's-stopmult'], ['min_stop_pct', 'Mind. %', '0.1', 's-minstop'], ['take_profit_atr_mult', 'Ziel x ATR', '0.5', 's-tpmult']];
  // Je Strategie eine Zeile mit Stop, Mindest-Stop und Ziel; SuperTrend setzt beides selbst
  function renderExits(sv, o) {
    var box = clear($('s-exits'));
    var ex = sv.exits || {};
    o.members.forEach(function (n) {
      var row = el('div', 'row exit-row');
      row.dataset.strategy = n;
      var main = el('div', 'row-main');
      main.appendChild(el('span', 'row-title', stratName(n)));
      row.appendChild(main);
      if ((o.own_exits || []).indexOf(n) >= 0) {
        main.appendChild(el('span', 'row-sub wrap', 'Setzt Stop (Linie) und Ausstieg (Linie, X-Marke) selbst. Kein festes Ziel, kein Kostenfilter.'));
      } else {
        var grid = el('div', 'exit-grid');
        EXIT_FIELDS.forEach(function (f) {
          var lab = el('label', 'exit-field');
          lab.appendChild(el('span', '', f[1]));
          var inp = el('input');
          inp.type = 'number'; inp.inputMode = 'decimal'; inp.step = f[2]; inp.min = '0';
          inp.dataset.field = f[0];
          var cur = ex[n] && ex[n][f[0]];
          inp.value = cur === null || cur === undefined ? '' : cur;
          inp.placeholder = $(f[3]).value;
          lab.appendChild(inp);
          grid.appendChild(lab);
        });
        row.appendChild(grid);
      }
      box.appendChild(row);
    });
    toggleExits();
  }
  // Nur Strategien zeigen, die gerade handeln: die gewaehlte oder die Mitglieder des Ensembles
  function toggleExits() {
    var name = $('s-strategy').value;
    var members = Array.prototype.map.call(document.querySelectorAll('input[name="member"]:checked'), function (i) { return i.value; });
    Array.prototype.forEach.call(document.querySelectorAll('#s-exits .exit-row'), function (r) {
      var n = r.dataset.strategy;
      r.hidden = name === 'ensemble' ? members.indexOf(n) < 0 : n !== name;
    });
  }
  function collectExits() {
    var out = {};
    Array.prototype.forEach.call(document.querySelectorAll('#s-exits .exit-row'), function (r) {
      var inputs = r.querySelectorAll('input[data-field]');
      if (!inputs.length) return;
      var e = {};
      Array.prototype.forEach.call(inputs, function (i) { var x = parseFloat(i.value); e[i.dataset.field] = isNaN(x) ? null : x; });
      out[r.dataset.strategy] = e;
    });
    return out;
  }

  function toggleEnsemble() { $('s-ensemble').hidden = $('s-strategy').value !== 'ensemble'; toggleExits(); }

  function showFormError(msg) { var e = $('s-error'); e.hidden = !msg; e.textContent = msg || ''; }

  function collect() {
    var vals = function (name) { return Array.prototype.map.call(document.querySelectorAll('input[name="' + name + '"]:checked'), function (i) { return i.value; }); };
    var members = vals('member');
    var all = form.options ? form.options.members.length : 0;
    return {
      symbols: vals('symbol'),
      timeframe: $('s-timeframe').value,
      strategy: {
        name: $('s-strategy').value,
        min_votes: parseInt($('s-votes').value, 10),
        members: members.length === all ? null : members,
        allow_short: $('s-short').checked,
        stop_atr_mult: parseFloat($('s-stopmult').value),
        take_profit_atr_mult: parseFloat($('s-tpmult').value) || 0,
        min_stop_pct: parseFloat($('s-minstop').value) || 0,
        trailing_pct: parseFloat($('s-trailpct').value) || 0,
        solo_member: $('s-solo').value || null,
        realtime_signals: $('s-realtime').checked,
        signal_source: $('s-spotsig').checked ? 'spot' : 'market',
        exits: collectExits(),
        st_min_strength: parseInt($('s-ststr').value, 10) || 1,
        st_adx_min: parseFloat($('s-stadx').value) || 0,
        st_chop_max: parseFloat($('s-stchop').value) || 0,
        st_trend_ema: parseInt($('s-stema').value, 10) || 0,
        st_timeframe: $('s-sttf').value || null
      },
      futures: { leverage: parseFloat($('s-lev').value) || 1 },
      protections: {
        stoploss_guard_stops: $('s-slguard').checked ? (form.slguardStops || 3) : 0,
        cooldown_candles: $('s-cooldown').checked ? (form.cooldownCandles || 5) : 0,
        track_record_trades: $('s-track').checked ? (form.trackTrades || 3) : 0
      },
      risk: {
        risk_per_trade_pct: parseFloat($('s-risk').value),
        max_position_pct: parseFloat($('s-maxpos').value),
        max_open_positions: parseInt($('s-maxopen').value, 10),
        max_daily_loss_pct: parseFloat($('s-dayloss').value),
        min_target_cost_ratio: parseFloat($('s-costratio').value) || 0
      },
      daytrading: {
        enabled: $('s-dt').checked,
        entry_start: $('s-start').value,
        entry_end: $('s-end').value,
        flat_time: $('s-flat').value.trim(),
        max_trades_per_day: parseInt($('s-maxtrades').value, 10)
      }
    };
  }

  function addSymbol() {
    var input = $('s-symbol-add');
    var s = input.value.trim().toUpperCase();
    if (!s) return;
    if (form.options && form.options.symbol_kind === 'ticker') {
      if (!/^[A-Z][A-Z0-9.]{0,9}$/.test(s)) { showFormError('„' + input.value + '“ ist kein US-Tickersymbol. Beispiel: AMD'); return; }
    } else {
      if (s.indexOf('/') < 0) s += '/USDT';
      if (!/^[A-Z0-9]{2,12}\/USDT$/.test(s)) { showFormError('„' + input.value + '“ ist kein gültiges Paar. Beispiel: AVAX/USDT'); return; }
    }
    var existing = document.querySelector('input[name="symbol"][value="' + s + '"]');
    if (!existing) $('s-symbols').appendChild(check('symbol', s, true, s.split('/')[0]));
    else existing.checked = true;
    input.value = '';
    showFormError(null);
  }

  function saveSettings(ev) {
    ev.preventDefault();
    var payload = collect();
    if (!payload.symbols.length) { showFormError('Mindestens ein Handelspaar auswählen.'); return; }
    if (payload.strategy.name === 'ensemble' && payload.strategy.members && !payload.strategy.members.length) { showFormError('Mindestens eine Strategie fürs Ensemble auswählen.'); return; }
    if (payload.strategy.solo_member && payload.strategy.members && payload.strategy.members.indexOf(payload.strategy.solo_member) < 0) { showFormError(stratName(payload.strategy.solo_member) + ' soll allein reichen, ist aber nicht im Ensemble ausgewählt.'); return; }
    var btn = $('s-save');
    btn.disabled = true;
    postJSON(API[market].post + 'settings', payload).then(function () { waitForRestart(); })
      .catch(function (e) { showFormError(e.message); })
      .then(function () { btn.disabled = false; });
  }

  function resetSettings() {
    if (!window.confirm('App-Einstellungen für ' + (market === 'stocks' ? 'Aktien' : 'Krypto') + ' löschen und wieder config.yaml verwenden? Der Bot startet dabei neu.')) return;
    postJSON(API[market].post + 'settings-reset').then(waitForRestart).catch(function (e) { showFormError(e.message); });
  }

  // Nach dem Speichern beendet sich der Bot und wird von start-24-7.bat, Docker oder Cloudflare neu gestartet.
  function waitForRestart() {
    restarting = true;
    if (snap) renderHeader();
    var started = Date.now(), wentDown = false;
    (function poll() {
      fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' }).then(function (r) {
        if (r.ok && (wentDown || Date.now() - started > 8000)) {
          restarting = false; settingsLoaded = false;
          load(); if (page === 'einstellungen') loadSettings(); if (page === 'chart') { resetChart(); loadChart(); }
          return;
        }
        if (!r.ok) wentDown = true;
        setTimeout(poll, 2000);
      }).catch(function () {
        wentDown = true;
        if (Date.now() - started > 90000) {
          restarting = false;
          var b = $('banner');
          b.hidden = false;
          b.textContent = 'Der Bot läuft nicht wieder an. Mit start-24-7.bat (oder auf dem Server/Cloudflare) startet er nach dem Speichern von selbst neu.';
          return;
        }
        setTimeout(poll, 2000);
      });
    })();
  }

  // ------------------------------------------------------------ Laden
  // Solange ein Finger oder die Maus gedrueckt ist, nicht neu zeichnen: sonst geht der Tipp ins Leere
  var pressing = false, pendingRender = false;
  document.addEventListener('pointerdown', function () { pressing = true; }, true);
  ['pointerup', 'pointercancel'].forEach(function (t) {
    document.addEventListener(t, function () {
      setTimeout(function () { pressing = false; if (pendingRender && snap) { pendingRender = false; renderPage(); } }, 300);
    }, true);
  });

  var loading = false, loadFails = 0;
  function load() {
    if (document.hidden || loading) return;
    var forMarket = market;
    loading = true;
    getJSON(API[market].snapshot).then(function (s) {
      loading = false; loadFails = 0;
      snaps[forMarket] = s;
      if (forMarket !== market) return; // inzwischen umgeschaltet
      snap = s;
      if (pressing) { pendingRender = true; return; }
      renderPage();
    }).catch(function () {
      loading = false;
      // einzelne Aussetzer im Mobilfunk nicht gleich als Abbruch zeigen
      if (++loadFails < 3 && !restarting) { $('run-text').textContent = 'Verbindung langsam …'; return; }
      $('run').className = 'dot';
      $('run-text').textContent = restarting ? 'startet neu …' : 'keine Verbindung';
    });
  }

  $('toggle').addEventListener('click', function () {
    if (!snap) return;
    var btn = $('toggle');
    btn.disabled = true;
    var forMarket = market;
    postJSON(API[market].post + (snap.paused ? 'resume' : 'pause')).then(function (s) { snaps[forMarket] = s; if (forMarket === market) { snap = s; renderPage(); } })
      .catch(function (e) { window.alert('Das hat nicht geklappt: ' + e.message); })
      .then(function () { btn.disabled = false; });
  });
  document.querySelectorAll('#trade-view button').forEach(function (b) {
    b.addEventListener('click', function () { tradeView = b.getAttribute('data-view'); if (snap) renderTrades(); });
  });
  document.querySelectorAll('#market-switch button').forEach(function (b) {
    b.addEventListener('click', function () { setMarket(b.getAttribute('data-market')); });
  });
  $('s-account-reset').addEventListener('click', function () {
    var clear = $('s-reset-trades').checked;
    var what = market === 'stocks' ? 'Aktienkonto' : 'Krypto-Konto';
    var open = snap && snap.positions ? snap.positions.length : 0;
    if (!window.confirm(what + ' auf Werkszustand zurücksetzen?\n\nKapital zurück aufs Startkapital, Kapitalverlauf, Tageslimit und Sperren zurück'
      + (clear ? ', Trade-Verlauf und Statistik gelöscht' : ', Trade-Verlauf bleibt') + '.'
      + (open ? '\n' + open + ' offene Position(en) werden zum aktuellen Kurs geschlossen.' : ''))) return;
    var btn = $('s-account-reset');
    btn.disabled = true;
    postJSON(API[market].post + 'account-reset', { clear_trades: clear })
      .then(function () { showFormError(null); var b = $('banner'); b.hidden = false; b.textContent = what + ' wird zurückgesetzt …'; setTimeout(load, 7000); })
      .catch(function (e) { showFormError(e.message); })
      .then(function () { btn.disabled = false; });
  });
  $('settings').addEventListener('submit', saveSettings);
  $('s-reset').addEventListener('click', resetSettings);
  $('s-strategy').addEventListener('change', toggleEnsemble);
  $('s-members').addEventListener('change', toggleExits);
  $('s-lev').addEventListener('input', function () { $('s-lev-range').value = $('s-lev').value; levHint(); });
  $('s-lev-range').addEventListener('input', function () { $('s-lev').value = $('s-lev-range').value; levHint(); });
  $('s-symbol-add').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); addSymbol(); } });
  $('s-symbol-add').addEventListener('change', addSymbol);
  window.addEventListener('hashchange', route);
  // zurueck in die App (z. B. vom Homescreen): sofort aktualisieren, Chart frisch aufbauen
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    load();
    if (page === 'chart') { resetChart(); loadChart(); }
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
    if (page === 'chart') { resetChart(); loadChart(); }
    if (snap) renderPage();
  });
  // lokal (ohne Cloudflare-Anmeldung) gibt es kein Abmelden
  if (/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) document.querySelector('a[href="/logout"]').hidden = true;

  route();
  load();
  setInterval(load, REFRESH_MS);
  setInterval(function () { if (page === 'chart' && !document.hidden) loadChart(); }, CHART_REFRESH_MS);
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(function () {});
})();
