/* perfmon dashboard: live (polling the perfmon server) or static (embedded report). */
(function () {
  'use strict';

  const EMBED = window.PERFMON_EMBED || null;
  const FILES = ['host', 'containers', 'processes', 'jvm', 'disks', 'markers'];
  const KEY = { containers: 'container', processes: 'series', jvm: 'series', disks: 'path' };
  const TEXT = new Set(['time', 'epoch', 'container', 'image', 'series', 'target', 'name', 'kind', 'text', 'path', 'mount']);
  const REG = { processes: 'proc', jvm: 'proc', containers: 'ctr', host: 'host', disks: 'disk' };
  const WINDOWS = [['5m', 300], ['15m', 900], ['1h', 3600], ['6h', 21600], ['All', 0]];

  const fmt = window.perfFmt;
  const pct = (v) => (v == null || v !== v ? '–' : fmt(v, Math.abs(v) >= 100 ? 0 : 1) + '%');
  const mb = (v) => (v == null || v !== v ? '–' : v >= 10240 ? fmt(v / 1024, 1) + ' GB' : fmt(v, v >= 100 ? 0 : 1) + ' MB');
  const mbit = (v) => (v == null || v !== v ? '–' : fmt(v, v >= 100 ? 0 : 2) + ' Mb/s');
  const mbs = (v) => (v == null || v !== v ? '–' : fmt(v, v >= 100 ? 0 : 2) + ' MB/s');
  const num = (v) => (v == null || v !== v ? '–' : fmt(v, 0));
  const ms = (v) => (v == null || v !== v ? '–' : v >= 10000 ? fmt(v / 1000, 1) + ' s' : fmt(v, 0) + ' ms');

  // Chart catalogue. `cols` charts plot fixed columns of one table (host);
  // the rest plot one column for every entity (process / JVM / container).
  const SECTIONS = [
    { title: 'Processes', charts: [
      { id: 'p-cpu', file: 'processes', col: 'cpu_pct', title: 'CPU', unit: '% of one core (100% = 1 core)', fmt: pct },
      { id: 'p-rss', file: 'processes', col: 'rss_mb', title: 'Memory (RSS)', unit: 'resident set size, MB', fmt: mb },
      { id: 'p-thr', file: 'processes', col: 'threads', title: 'OS threads', unit: 'count', fmt: num },
      { id: 'p-fds', file: 'processes', col: 'fds', title: 'Open file descriptors', unit: 'count (sockets, files)', fmt: num },
    ] },
    { title: 'JVM', charts: [
      { id: 'j-heap', file: 'jvm', col: 'heap_used_mb', title: 'Heap used', unit: 'young + old generation, MB', fmt: mb },
      { id: 'j-old', file: 'jvm', col: 'old_used_mb', title: 'Old generation used', unit: 'MB; a rising floor after GCs suggests a leak', fmt: mb },
      { id: 'j-gc', file: 'jvm', col: 'gc_pause_pct', title: 'GC pause time', unit: '% of wall-clock time spent in GC pauses', fmt: pct },
      { id: 'j-gcc', file: 'jvm', col: 'gc_young_count', title: 'Young GCs per sample', unit: 'collections per interval', fmt: num },
    ] },
    { title: 'Containers', charts: [
      { id: 'c-cpu', file: 'containers', col: 'cpu_pct', title: 'CPU', unit: '% of one core (100% = 1 core)', fmt: pct },
      { id: 'c-mem', file: 'containers', col: 'mem_used_mb', title: 'Memory', unit: 'MB, excluding reclaimable page cache (same as docker stats)', fmt: mb },
      { id: 'c-thr', file: 'containers', col: 'throttled_pct', title: 'CPU throttling', unit: '% of CFS periods throttled by the --cpus limit', fmt: pct, onlyIf: 'cpu_limit_cores', hideIfZero: true },
      { id: 'c-rx', file: 'containers', col: 'net_rx_mbps', title: 'Network in', unit: 'Mbit/s (host-network containers not shown)', fmt: mbit, hideIfZero: true },
      { id: 'c-tx', file: 'containers', col: 'net_tx_mbps', title: 'Network out', unit: 'Mbit/s', fmt: mbit, hideIfZero: true },
      { id: 'c-dw', file: 'containers', col: 'disk_write_mbs', title: 'Disk write', unit: 'MB/s (block I/O)', fmt: mbs, hideIfZero: true },
    ] },
    { title: 'Host', charts: [
      { id: 'h-cpu', file: 'host', title: 'CPU', unit: '% of all cores', fmt: pct,
        cols: [['cpu_pct', 'CPU busy'], ['iowait_pct', 'I/O wait'], ['steal_pct', 'Steal (VM)']] },
      { id: 'h-mem', file: 'host', title: 'Memory', unit: 'MB; used = total − available', fmt: mb,
        cols: [['mem_used_mb', 'Used'], ['swap_used_mb', 'Swap used']] },
      { id: 'h-net', file: 'host', title: 'Network', unit: 'Mbit/s on physical interfaces', fmt: mbit,
        cols: [['net_rx_mbps', 'In'], ['net_tx_mbps', 'Out']] },
      { id: 'h-load', file: 'host', title: 'Load average (1 min)', unit: 'runnable tasks', fmt: (v) => fmt(v, 2),
        cols: [['load1', 'Load 1m']] },
    ] },
    { title: 'Disk space', charts: [
      { id: 'd-pct', file: 'disks', col: 'fs_used_pct', title: 'Filesystem used', unit: '% of the filesystem each folder is on (same as df Use%)', fmt: pct },
      { id: 'd-free', file: 'disks', col: 'fs_avail_mb', title: 'Free space', unit: 'MB available on that filesystem', fmt: mb },
      { id: 'd-dir', file: 'disks', col: 'dir_size_mb', title: 'Folder size', unit: 'MB used by the folder itself (like du -sx)', fmt: mb },
      { id: 'd-files', file: 'disks', col: 'dir_files', title: 'Files in folder', unit: 'count, including subfolders', fmt: num },
    ] },
  ];

  const $ = (id) => document.getElementById(id);
  const state = {
    runs: [], runId: null, follow: true, meta: null, live: false,
    offsets: {}, headers: {}, data: null, slots: null,
    win: 0, zoom: null, view: null, hidden: {}, filter: null, paused: false,
    timer: null, fetching: false, gen: 0, lastSummary: 0, sort: {},
  };
  const charts = {};

  // ------------------------------------------------------------------ data
  function freshData() {
    return { host: { t: [], cols: {} }, containers: {}, processes: {}, jvm: {}, disks: {}, markers: [], tmin: Infinity, tmax: -Infinity };
  }

  function parseLine(line) {
    if (line.indexOf('"') < 0) return line.split(',');
    const out = [];
    let cur = '', q = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (q) {
        if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += ch;
      } else if (ch === '"') q = true;
      else if (ch === ',') { out.push(cur); cur = ''; } else cur += ch;
    }
    out.push(cur);
    return out;
  }

  function ingest(file, text) {
    const d = state.data;
    const lines = text.split('\n');
    let h = state.headers[file];
    for (let line of lines) {
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line) continue;
      const v = parseLine(line);
      if (!h) {
        const idx = {};
        v.forEach((n, i) => { idx[n] = i; });
        h = state.headers[file] = { idx, numeric: v.map((n, i) => [n, i]).filter(([n]) => !TEXT.has(n)) };
        continue;
      }
      const t = +v[h.idx.epoch];
      if (!(t > 0)) continue;
      if (file === 'markers') {
        d.markers.push({ t, kind: v[h.idx.kind] || 'user', text: v[h.idx.text] || '' });
        continue;
      }
      let tab;
      if (file === 'host') tab = d.host;
      else {
        const k = v[h.idx[KEY[file]]];
        tab = d[file][k] || (d[file][k] = { t: [], cols: {}, info: {} });
        for (const f of ['container', 'image', 'target', 'name', 'mount']) if (f in h.idx) tab.info[f] = v[h.idx[f]];
      }
      tab.t.push(t);
      for (const [n, i] of h.numeric) {
        const s = v[i];
        (tab.cols[n] || (tab.cols[n] = [])).push(s === '' || s === undefined ? NaN : +s);
      }
      if (t < d.tmin) d.tmin = t;
      if (t > d.tmax) d.tmax = t;
    }
  }

  function slot(reg, key) {
    const r = state.slots[reg];
    if (!(key in r)) r[key] = Object.keys(r).length;
    return r[key];
  }

  // ------------------------------------------------------------ networking
  const api = (p) => 'api/' + p;

  async function fetchJSON(p) {
    const r = await fetch(api(p), { cache: 'no-store' });
    if (!r.ok) throw new Error(p + ': HTTP ' + r.status);
    return r.json();
  }

  async function fetchFile(file, gen) {
    const off = state.offsets[file] || 0;
    const r = await fetch(api('runs/' + encodeURIComponent(state.runId) + '/' + file + '.csv?offset=' + off), { cache: 'no-store' });
    if (r.status === 404) return false;
    if (!r.ok) throw new Error(file + '.csv: HTTP ' + r.status);
    const text = await r.text();
    if (gen !== state.gen) return false;
    const next = +r.headers.get('X-Next-Offset'), size = +r.headers.get('X-File-Size');
    ingest(file, text);
    state.offsets[file] = next;
    return next < size;
  }

  async function poll() {
    if (EMBED || state.fetching || !state.runId) return;
    state.fetching = true;
    const gen = state.gen;
    try {
      let more = true, rounds = 0;
      while (more && rounds++ < 50) {
        const res = await Promise.all(FILES.map((f) => fetchFile(f, gen)));
        if (gen !== state.gen) return;
        more = res.some(Boolean);
      }
      if (state.live) {
        const m = await fetchJSON('runs/' + encodeURIComponent(state.runId) + '/meta.json');
        if (gen !== state.gen) return;
        state.meta = m;
        state.live = m.live;
      }
      setStatus('');
      render();
    } catch (e) {
      setStatus('Connection problem: ' + e.message + ' — retrying');
    } finally {
      state.fetching = false;
      schedule();
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    if (EMBED || state.paused || !state.live) return;
    state.timer = setTimeout(poll, Math.max(1000, (state.meta.interval || 1) * 1000));
  }

  async function loadRuns() {
    try {
      state.runs = await fetchJSON('runs');
    } catch (e) {
      setStatus('Cannot reach perfmon server: ' + e.message);
      return;
    }
    const sel = $('run');
    const key = state.runs.map((r) => r.id + (r.live ? '*' : '')).join('|');
    if (key !== state.runsKey) {   // rebuilding closes an open drop-down, so only on change
      state.runsKey = key;
      const keep = state.follow ? '__follow' : state.runId;
      sel.textContent = '';
      const o = document.createElement('option');
      o.value = '__follow';
      o.textContent = 'Latest run (auto-follow)';
      sel.appendChild(o);
      for (const r of state.runs) {
        const opt = document.createElement('option');
        opt.value = r.id;
        opt.textContent = (r.live ? '● ' : '') + r.name + ' — ' + window.PerfTime.full(r.started).slice(0, 16);
        sel.appendChild(opt);
      }
      sel.value = keep && [...sel.options].some((x) => x.value === keep) ? keep : '__follow';
    }
    const want = sel.value === '__follow' ? (state.runs[0] && state.runs[0].id) : sel.value;
    if (want && (want !== state.runId || !state.data)) await openRun(want);
    if (!state.runs.length) setStatus('No recordings yet. Start one on the server:  ./perfmon.sh start my-test');
  }

  async function openRun(id) {
    state.gen++;
    clearTimeout(state.timer);
    state.runId = id;
    state.offsets = {};
    state.headers = {};
    state.data = freshData();
    state.slots = { proc: {}, ctr: {}, host: {}, disk: {} };
    state.zoom = null;
    state.meta = await fetchJSON('runs/' + encodeURIComponent(id) + '/meta.json');
    state.live = state.meta.live;
    applyMeta();
    updateUrl();
    state.fetching = false;
    await poll();
  }

  function applyMeta() {
    const m = state.meta;
    if (m.utc_offset != null) window.PerfTime.offset = m.utc_offset;
    $('host').textContent = m.host + ' · ' + m.cpus + ' CPUs · ' + mb(m.mem_total_mb) + ' RAM';
    $('tz').textContent = 'Times in server time (' + window.PerfTime.zone() + ')';
    document.title = 'perfmon · ' + m.name;
    if (!EMBED) $('dl-report').href = api('runs/' + encodeURIComponent(m.id) + '/report.html');
    for (const s of SECTIONS) for (const c of s.charts) if (charts[c.id]) charts[c.id].chart.gap = Math.max(5, (m.interval || 1) * 2.5);
    if (charts['d-dir']) {
      charts['d-dir'].unitEl.textContent = charts['d-dir'].def.unit +
        (m.disk_scan_interval ? '; re-measured every ' + dur(m.disk_scan_interval) : '');
    }
  }

  // -------------------------------------------------------------- rendering
  function computeView() {
    const d = state.data;
    if (!(d.tmax >= d.tmin)) return null;
    if (state.zoom) return state.zoom;
    const t1 = d.tmax;
    const t0 = state.win ? Math.max(d.tmin, t1 - state.win) : d.tmin;
    return [t0, t1 > t0 ? t1 : t0 + (state.meta.interval || 1)];
  }

  function matches(key) {
    return !state.filter || state.filter.test(key);
  }

  function seriesFor(def) {
    const d = state.data;
    const out = [];
    if (def.cols) {
      for (const [col, label] of def.cols) {
        const y = d.host.cols[col];
        if (!y || !y.some((v) => v > 0)) continue;
        const key = 'host:' + col;
        out.push({ key, label, slot: slot('host', key), t: d.host.t, y, hidden: !!state.hidden[key] });
      }
      return out;
    }
    const tabs = d[def.file];
    for (const key of Object.keys(tabs)) {
      if (!matches(key)) continue;
      const tab = tabs[key], y = tab.cols[def.col];
      if (!y || !y.some((v) => v === v)) continue;
      if (def.onlyIf && !(tab.cols[def.onlyIf] || []).some((v) => v > 0)) continue;
      const hk = REG[def.file] + ':' + key;
      out.push({ key: hk, label: key, slot: slot(REG[def.file], key), t: tab.t, y, hidden: !!state.hidden[hk] });
    }
    return out;
  }

  function buildCharts() {
    const root = $('charts');
    for (const sec of SECTIONS) {
      const section = document.createElement('section');
      section.className = 'chart-section';
      const h = document.createElement('h2');
      h.textContent = sec.title;
      section.appendChild(h);
      const grid = document.createElement('div');
      grid.className = 'chart-grid';
      section.appendChild(grid);
      root.appendChild(section);
      sec.el = section;
      for (const def of sec.charts) {
        const card = document.createElement('article');
        card.className = 'card chart-card';
        const head = document.createElement('header');
        const t = document.createElement('h3');
        t.textContent = def.title;
        const u = document.createElement('p');
        u.className = 'unit';
        u.textContent = def.unit;
        head.appendChild(t);
        head.appendChild(u);
        card.appendChild(head);
        grid.appendChild(card);
        const chart = new window.TimeChart(card, {
          fmt: def.fmt,
          height: 230,
          onHover: (t, src) => { for (const c of Object.values(charts)) c.chart.setHover(t, c.chart === src); },
          onZoom: (a, b) => { state.zoom = a == null ? null : [a, b]; render(true); },
          onToggle: (k) => { state.hidden[k] = !state.hidden[k]; render(true); },
        });
        charts[def.id] = { def, card, chart, unitEl: u };
      }
    }
  }

  function render(force) {
    if (!state.data) return;
    const view = state.view = computeView();
    const markers = state.data.markers;
    for (const sec of SECTIONS) {
      let any = false;
      for (const def of sec.charts) {
        const c = charts[def.id];
        let series = view ? seriesFor(def) : [];
        if (def.hideIfZero && !series.some((s) => s.y.some((v) => v > 0))) series = [];
        c.card.hidden = !series.length;
        if (!series.length) continue;
        any = true;
        c.chart.setData(series, view, markers);
        c.chart.draw();
      }
      sec.el.hidden = !any;
    }
    $('empty').hidden = !!view;
    $('reset-zoom').hidden = !state.zoom;
    renderTiles(view);
    renderRange(view);
    const now = Date.now();
    if (force || !state.live || now - state.lastSummary > 4000) {
      state.lastSummary = now;
      renderSummary(view);
      renderMarkers();
    }
  }

  function renderRange(view) {
    const el = $('range');
    if (!view) { el.textContent = ''; return; }
    el.textContent = window.PerfTime.full(view[0]) + ' → ' + window.PerfTime.full(view[1]).slice(11) +
      ' (' + dur(view[1] - view[0]) + ')' + (state.zoom ? ' · zoomed' : '');
  }

  function dur(s) {
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
    return h ? h + 'h ' + m + 'm' : m ? m + 'm ' + x + 's' : x + 's';
  }

  function stats(t, y, view) {
    let i = lowerBound(t, view[0]);
    const vals = [];
    let first = NaN, last = NaN, sum = 0;
    for (; i < t.length && t[i] <= view[1]; i++) {
      const v = y[i];
      if (v !== v) continue;
      if (first !== first) first = v;
      last = v;
      sum += v;
      vals.push(v);
    }
    if (!vals.length) return { n: 0, avg: NaN, p95: NaN, max: NaN, first, last, sum: 0 };
    vals.sort((a, b) => a - b);
    return { n: vals.length, avg: sum / vals.length, p95: vals[Math.ceil(0.95 * vals.length) - 1],
      max: vals[vals.length - 1], first, last, sum };
  }

  function lowerBound(a, v) {
    let lo = 0, hi = a.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < v) lo = m + 1; else hi = m; }
    return lo;
  }

  function tile(label, value, sub, status) {
    const t = document.createElement('div');
    t.className = 'card tile';
    const l = document.createElement('div');
    l.className = 'tile-label';
    l.textContent = label;
    const v = document.createElement('div');
    v.className = 'tile-value';
    if (status) {
      const dot = document.createElement('span');
      dot.className = 'status ' + status;
      dot.setAttribute('aria-hidden', 'true');
      v.appendChild(dot);
    }
    v.appendChild(document.createTextNode(value));
    const s = document.createElement('div');
    s.className = 'tile-sub';
    s.textContent = sub;
    t.appendChild(l);
    t.appendChild(v);
    t.appendChild(s);
    return t;
  }

  function renderTiles(view) {
    const root = $('tiles');
    root.textContent = '';
    if (!view) return;
    const d = state.data, m = state.meta;
    if (state.live) {
      const age = Date.now() / 1000 - d.tmax;
      const ok = age < Math.max(5, m.interval * 3);
      root.appendChild(tile('Recording', ok ? 'Live' : 'Stalled', 'last sample ' + dur(Math.max(0, age)) + ' ago', ok ? 'good' : 'warning'));
    } else {
      root.appendChild(tile('Recording', dur((m.ended || d.tmax) - m.started), 'every ' + m.interval + ' s · ' +
        (m.ended ? 'finished' : 'stopped'), null));
    }
    const hc = d.host.cols;
    if (hc.cpu_pct) {
      const s = stats(d.host.t, hc.cpu_pct, view);
      root.appendChild(tile('Host CPU', pct(s.last), 'avg ' + pct(s.avg) + ' · peak ' + pct(s.max) + ' in view'));
    }
    if (hc.mem_used_mb) {
      const s = stats(d.host.t, hc.mem_used_mb, view);
      root.appendChild(tile('Host memory', mb(s.last), 'of ' + mb(m.mem_total_mb) + ' · peak ' + mb(s.max)));
    }
    let top = null;
    for (const [k, tab] of Object.entries(d.processes)) {
      if (!matches(k) || !tab.cols.cpu_pct) continue;
      const s = stats(tab.t, tab.cols.cpu_pct, view);
      if (s.n && (!top || s.avg > top.avg)) top = { k, avg: s.avg, max: s.max };
    }
    if (top) root.appendChild(tile('Busiest process', pct(top.avg), top.k + ' · peak ' + pct(top.max)));
    const nP = Object.keys(d.processes).length, nJ = Object.keys(d.jvm).length, nC = Object.keys(d.containers).length;
    root.appendChild(tile('Processes tracked', String(nP), nJ + ' JVMs · ' + nC + ' containers'));
    let full = null;
    for (const [k, tab] of Object.entries(d.disks)) {
      const s = stats(tab.t, tab.cols.fs_used_pct || [], view);
      if (s.n && (!full || s.last > full.pct)) full = { k, pct: s.last, free: stats(tab.t, tab.cols.fs_avail_mb || [], view).last };
    }
    if (full) {
      root.appendChild(tile('Fullest disk', pct(full.pct), full.k + ' · ' + mb(full.free) + ' free',
        full.pct >= 95 ? 'critical' : full.pct >= 85 ? 'warning' : null));
    }
  }

  // --------------------------------------------------------------- tables
  function table(id, cols, rows, title) {
    const wrap = document.createElement('div');
    wrap.className = 'card table-card';
    const head = document.createElement('div');
    head.className = 'table-head';
    const h = document.createElement('h3');
    h.textContent = title;
    head.appendChild(h);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ghost';
    btn.textContent = 'Download CSV';
    btn.addEventListener('click', () => downloadCsv(id, cols, rows));
    head.appendChild(btn);
    wrap.appendChild(head);
    const scroller = document.createElement('div');
    scroller.className = 'table-scroll';
    const tbl = document.createElement('table');
    const sort = state.sort[id] || { col: 1, desc: true };
    const sorted = rows.slice().sort((a, b) => {
      const x = a[sort.col], y = b[sort.col];
      const c = typeof x === 'string' ? x.localeCompare(y) : (x !== x ? -Infinity : x) - (y !== y ? -Infinity : y);
      return sort.desc ? -c : c;
    });
    const thead = tbl.createTHead().insertRow();
    cols.forEach(([label], i) => {
      const th = document.createElement('th');
      th.textContent = label + (sort.col === i ? (sort.desc ? ' ▾' : ' ▴') : '');
      th.className = i ? 'num' : '';
      th.tabIndex = 0;
      th.setAttribute('aria-sort', sort.col === i ? (sort.desc ? 'descending' : 'ascending') : 'none');
      const go = () => { state.sort[id] = { col: i, desc: sort.col === i ? !sort.desc : i > 0 }; renderSummary(state.view); };
      th.addEventListener('click', go);
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
      thead.appendChild(th);
    });
    const body = tbl.createTBody();
    for (const r of sorted) {
      const tr = body.insertRow();
      cols.forEach(([, f], i) => {
        const td = tr.insertCell();
        td.textContent = i ? f(r[i]) : r[i];
        if (i) td.className = 'num';
      });
    }
    scroller.appendChild(tbl);
    wrap.appendChild(scroller);
    return wrap;
  }

  function downloadCsv(id, cols, rows) {
    const esc = (v) => (/[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
    const lines = [cols.map((c) => esc(c[0])).join(',')];
    for (const r of rows) lines.push(r.map((v) => esc(typeof v === 'number' ? (v === v ? +v.toFixed(3) : '') : v)).join(','));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([lines.join('\n') + '\n'], { type: 'text/csv' }));
    a.download = (state.meta.id || 'perfmon') + '_' + id + '.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  function renderSummary(view) {
    const root = $('summary');
    root.textContent = '';
    if (!view) return;
    $('summary-range').textContent = 'for the time range in view — zoom a chart to summarize a single test phase';
    const d = state.data;
    const pRows = [];
    for (const [k, tab] of Object.entries(d.processes)) {
      if (!matches(k)) continue;
      const c = stats(tab.t, tab.cols.cpu_pct || [], view);
      const r = stats(tab.t, tab.cols.rss_mb || [], view);
      const th = stats(tab.t, tab.cols.threads || [], view);
      if (!r.n) continue;
      const row = [k, c.avg, c.p95, c.max, r.first, r.last, r.last - r.first, r.max, th.max];
      const j = d.jvm[k];
      if (j) {
        const hu = stats(j.t, j.cols.heap_used_mb, view), hm = stats(j.t, j.cols.heap_max_mb, view);
        const gm = ['gc_young_ms', 'gc_full_ms', 'gc_other_ms'].reduce((a, n) => a + stats(j.t, j.cols[n], view).sum, 0);
        const gp = stats(j.t, j.cols.gc_pause_pct, view);
        row.push(hu.max, hm.max, gm, gp.max, stats(j.t, j.cols.gc_young_count, view).sum, stats(j.t, j.cols.gc_full_count, view).sum);
      } else row.push(NaN, NaN, NaN, NaN, NaN, NaN);
      pRows.push(row);
    }
    if (pRows.length) {
      root.appendChild(table('processes', [
        ['Process', null], ['CPU avg', pct], ['CPU p95', pct], ['CPU max', pct],
        ['RSS start', mb], ['RSS end', mb], ['RSS Δ', (v) => (v > 0 ? '+' : '') + mb(v)], ['RSS max', mb], ['Threads max', num],
        ['Heap max used', mb], ['Heap limit', mb], ['GC pause total', ms], ['GC pause max', pct], ['Young GCs', num], ['Full GCs', num],
      ], pRows, 'Processes'));
    }
    const cRows = [];
    for (const [k, tab] of Object.entries(d.containers)) {
      if (!matches(k)) continue;
      const cc = tab.cols;
      const c = stats(tab.t, cc.cpu_pct || [], view), m = stats(tab.t, cc.mem_used_mb || [], view);
      if (!m.n) continue;
      cRows.push([k, c.avg, c.p95, c.max, stats(tab.t, cc.cpu_limit_cores || [], view).max,
        stats(tab.t, cc.throttled_pct || [], view).max, m.avg, m.max, stats(tab.t, cc.mem_limit_mb || [], view).max,
        stats(tab.t, cc.net_rx_mbps || [], view).avg, stats(tab.t, cc.net_tx_mbps || [], view).avg,
        stats(tab.t, cc.disk_write_mbs || [], view).avg]);
    }
    if (cRows.length) {
      root.appendChild(table('containers', [
        ['Container', null], ['CPU avg', pct], ['CPU p95', pct], ['CPU max', pct], ['CPU limit', (v) => (v === v ? fmt(v, 2) + ' cores' : 'none')],
        ['Throttled max', pct], ['Mem avg', mb], ['Mem max', mb], ['Mem limit', (v) => (v === v ? mb(v) : 'none')],
        ['Net in avg', mbit], ['Net out avg', mbit], ['Disk write avg', mbs],
      ], cRows, 'Containers'));
    }
    const dRows = [];
    for (const [k, tab] of Object.entries(d.disks)) {
      if (!matches(k)) continue;
      const dc = tab.cols;
      const used = endpoints(tab.t, dc.fs_used_mb || [], view);
      if (!used) continue;
      const free = stats(tab.t, dc.fs_avail_mb || [], view);
      const dir = stats(tab.t, dc.dir_size_mb || [], view);
      const span = used.t1 - used.t0;
      const growth = span >= 60 ? (used.v1 - used.v0) / span * 60 : NaN;   // MB per minute
      const fullIn = growth > 0 ? free.last / growth * 60 : NaN;          // seconds
      dRows.push([k, stats(tab.t, dc.fs_used_pct || [], view).max, free.last, growth, fullIn,
        stats(tab.t, dc.fs_size_mb || [], view).last, tab.info.mount || '', dir.first, dir.last, dir.last - dir.first,
        stats(tab.t, dc.dir_files || [], view).last, stats(tab.t, dc.inodes_used_pct || [], view).max]);
    }
    if (dRows.length) {
      root.appendChild(table('disks', [
        ['Folder', null], ['Disk used max', pct], ['Disk free at end', mb],
        ['Disk growth', (v) => (v === v ? (v >= 0 ? '+' : '') + fmt(v, Math.abs(v) >= 10 ? 0 : 1) + ' MB/min' : '–')],
        ['Disk full in (at that rate)', (v) => (v === v ? dur(v) : '–')],
        ['Disk size', mb], ['Filesystem', (v) => v], ['Folder start', mb], ['Folder end', mb],
        ['Folder Δ', (v) => (v === v ? (v > 0 ? '+' : '') + mb(v) : '–')], ['Files', num], ['Inodes max', pct],
      ], dRows, 'Disk space'));
    }
  }

  // First and last sample inside the view (for growth rates).
  function endpoints(t, y, view) {
    let a = -1, b = -1;
    for (let i = lowerBound(t, view[0]); i < t.length && t[i] <= view[1]; i++) {
      if (y[i] !== y[i]) continue;
      if (a < 0) a = i;
      b = i;
    }
    return a < 0 ? null : { t0: t[a], v0: y[a], t1: t[b], v1: y[b] };
  }

  function renderMarkers() {
    const root = $('markers');
    root.textContent = '';
    const list = state.data.markers.filter((m) => m.kind === 'user' || $('show-events').checked);
    $('markers-section').hidden = !state.data.markers.length;
    if (!list.length) {
      const p = document.createElement('p');
      p.className = 'muted';
      p.textContent = 'No markers. Add one during a test:  ./perfmon.sh mark "ramp to 500 users"';
      root.appendChild(p);
      return;
    }
    const ul = document.createElement('ul');
    ul.className = 'marker-list';
    for (const m of list) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'link';
      b.textContent = window.PerfTime.full(m.t);
      b.title = 'Zoom to ±2 minutes around this marker';
      b.addEventListener('click', () => { state.zoom = [m.t - 120, m.t + 120]; render(true); });
      const s = document.createElement('span');
      s.className = m.kind === 'user' ? '' : 'muted';
      s.textContent = m.text;
      li.appendChild(b);
      li.appendChild(s);
      ul.appendChild(li);
    }
    root.appendChild(ul);
  }

  // ---------------------------------------------------------------- chrome
  function setStatus(msg) {
    const s = $('status');
    s.textContent = msg;
    s.hidden = !msg;
  }

  function updateUrl() {
    if (EMBED) return;
    const p = new URLSearchParams();
    if (!state.follow && state.runId) p.set('run', state.runId);
    const w = WINDOWS.find((x) => x[1] === state.win);
    if (w && w[1]) p.set('w', w[0]);
    const q = p.toString();
    history.replaceState(null, '', q ? '?' + q : location.pathname);
  }

  function setupControls() {
    const seg = $('window');
    for (const [label, secs] of WINDOWS) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.dataset.secs = secs;
      b.addEventListener('click', () => {
        state.win = secs;
        state.zoom = null;
        for (const x of seg.children) x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
        updateUrl();
        render(true);
      });
      seg.appendChild(b);
    }
    const q = new URLSearchParams(location.search);
    const w = WINDOWS.find((x) => x[0] === q.get('w'));
    state.win = w ? w[1] : 0;
    for (const x of seg.children) x.setAttribute('aria-pressed', +x.dataset.secs === state.win ? 'true' : 'false');

    $('reset-zoom').addEventListener('click', () => { state.zoom = null; render(true); });
    let ft = null;
    $('filter').addEventListener('input', (e) => {
      clearTimeout(ft);
      ft = setTimeout(() => {
        const v = e.target.value.trim();
        try { state.filter = v ? new RegExp(v, 'i') : null; } catch (err) {
          state.filter = new RegExp(v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
        }
        render(true);
      }, 150);
    });
    $('show-events').addEventListener('change', () => { renderMarkers(); });
    $('theme').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme');
      const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
      const next = dark ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('perfmon-theme', next); } catch (e) { /* storage unavailable */ }
      render(true);
    });
    try {
      const t = localStorage.getItem('perfmon-theme');
      if (t) document.documentElement.setAttribute('data-theme', t);
    } catch (e) { /* storage unavailable */ }
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => render(true));

    if (EMBED) {
      for (const id of ['run', 'pause', 'dl-report']) $(id).hidden = true;
      return;
    }
    const sel = $('run');
    const want = q.get('run');
    if (want) { state.follow = false; state.runId = want; }
    sel.addEventListener('change', async () => {
      state.follow = sel.value === '__follow';
      const id = state.follow ? (state.runs[0] && state.runs[0].id) : sel.value;
      if (id) await openRun(id);
    });
    $('pause').addEventListener('click', () => {
      state.paused = !state.paused;
      $('pause').textContent = state.paused ? 'Resume' : 'Pause';
      $('pause').setAttribute('aria-pressed', state.paused ? 'true' : 'false');
      if (!state.paused) poll(); else clearTimeout(state.timer);
    });
  }

  async function start() {
    buildCharts();
    setupControls();
    if (EMBED) {
      state.meta = EMBED.meta;
      state.runId = EMBED.meta.id;
      state.data = freshData();
      state.slots = { proc: {}, ctr: {}, host: {}, disk: {} };
      applyMeta();
      $('run-name').textContent = EMBED.meta.name;
      for (const f of FILES) if (EMBED.files[f]) ingest(f, EMBED.files[f]);
      render(true);
      return;
    }
    await loadRuns();
    // Pick up new recordings (auto-follow) and live/finished changes.
    setInterval(async () => {
      if (state.paused) return;
      const before = state.runs.length && state.runs[0].id;
      await loadRuns();
      const cur = state.runs.find((r) => r.id === state.runId);
      if (cur && cur.live && !state.live) { state.live = true; poll(); }
      if (state.follow && state.runs.length && state.runs[0].id !== before) setStatus('');
    }, 15000);
  }

  start();
})();
