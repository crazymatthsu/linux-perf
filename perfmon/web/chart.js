/* perfmon chart.js - a small, dependency-free canvas time-series chart.
 *
 * Features: HiDPI canvas, min/max decimation (peaks survive any zoom level),
 * gap breaks, synced crosshair + tooltip, drag-to-zoom, markers, clickable
 * legend with latest values. Colors come from CSS custom properties so the
 * page theme (light/dark) drives everything.
 */
(function (global) {
  'use strict';

  const DASHES = [[], [7, 4], [2, 3], [10, 3, 2, 3]];
  const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
    7200, 10800, 21600, 43200, 86400, 172800, 604800];
  const FONT = '11px system-ui, -apple-system, "Segoe UI", sans-serif';

  function el(tag, cls, parent) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  // Seconds east of UTC used for every displayed time (server time by default).
  const Time = {
    offset: -new Date().getTimezoneOffset() * 60,
    parts(epoch) {
      const d = new Date((epoch + Time.offset) * 1000);
      return { Y: d.getUTCFullYear(), M: d.getUTCMonth() + 1, D: d.getUTCDate(),
        h: d.getUTCHours(), m: d.getUTCMinutes(), s: d.getUTCSeconds() };
    },
    fmt(epoch, step) {
      const p = Time.parts(epoch);
      if (step >= 86400) return p.Y + '-' + pad(p.M) + '-' + pad(p.D);
      const hm = pad(p.h) + ':' + pad(p.m);
      return step < 60 ? hm + ':' + pad(p.s) : hm;
    },
    full(epoch) {
      const p = Time.parts(epoch);
      return p.Y + '-' + pad(p.M) + '-' + pad(p.D) + ' ' + pad(p.h) + ':' + pad(p.m) + ':' + pad(p.s);
    },
    zone() {
      const o = Time.offset, s = o < 0 ? '-' : '+', a = Math.abs(o);
      return 'UTC' + s + pad(Math.floor(a / 3600)) + ':' + pad(Math.floor(a / 60) % 60);
    },
  };

  function fmtNum(v, digits) {
    if (v == null || v !== v) return '–';
    const a = Math.abs(v);
    if (digits == null) digits = a >= 100 ? 0 : a >= 10 ? 1 : 2;
    return v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function niceStep(range, ticks) {
    const rough = range / Math.max(1, ticks);
    const p = Math.pow(10, Math.floor(Math.log10(rough)));
    const m = rough / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }

  // First index i with a[i] >= v.
  function lowerBound(a, v) {
    let lo = 0, hi = a.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (a[mid] < v) lo = mid + 1; else hi = mid; }
    return lo;
  }

  function nearest(a, v) {
    const i = lowerBound(a, v);
    if (i <= 0) return 0;
    if (i >= a.length) return a.length - 1;
    return v - a[i - 1] <= a[i] - v ? i - 1 : i;
  }

  function theme() {
    const cs = getComputedStyle(document.documentElement);
    const g = (n) => cs.getPropertyValue(n).trim();
    const series = [];
    for (let i = 1; i <= 8; i++) series.push(g('--series-' + i));
    return { surface: g('--surface'), ink: g('--ink'), ink2: g('--ink-2'), muted: g('--muted'),
      grid: g('--grid'), axis: g('--axis'), marker: g('--marker'), event: g('--event'), series };
  }

  function seriesStyle(th, slot) {
    return { color: th.series[slot % 8], dash: DASHES[Math.floor(slot / 8) % DASHES.length] };
  }

  /**
   * new TimeChart(container, {height, fmt, gap, onHover(t), onZoom(t0,t1|null), onToggle(key)})
   * series: [{key, label, slot, t:[epoch], y:[value], hidden}]
   */
  function TimeChart(container, opts) {
    this.opts = opts || {};
    this.height = this.opts.height || 220;
    this.fmt = this.opts.fmt || fmtNum;
    this.gap = this.opts.gap || 5;
    this.series = [];
    this.markers = [];
    this.view = null;
    this.hoverT = null;
    this.geom = null;

    this.wrap = el('div', 'tc-plot', container);
    this.wrap.style.height = this.height + 'px';
    this.base = el('canvas', 'tc-base', this.wrap);
    this.over = el('canvas', 'tc-over', this.wrap);
    this.sel = el('div', 'tc-sel', this.wrap);
    this.tip = el('div', 'tc-tip', this.wrap);
    this.sel.hidden = this.tip.hidden = true;
    this.legend = el('div', 'tc-legend', container);
    this.legendItems = {};

    const ov = this.over;
    ov.addEventListener('pointermove', (e) => this._move(e));
    ov.addEventListener('pointerleave', () => { if (!this.drag) this._hover(null); });
    ov.addEventListener('pointerdown', (e) => this._down(e));
    ov.addEventListener('pointerup', (e) => this._up(e));
    ov.addEventListener('dblclick', () => this.opts.onZoom && this.opts.onZoom(null));
    if (global.ResizeObserver) {
      let w = 0;
      new ResizeObserver(() => { if (this.wrap.clientWidth !== w) { w = this.wrap.clientWidth; this.draw(); } })
        .observe(this.wrap);
    } else {
      global.addEventListener('resize', () => this.draw());
    }
  }

  TimeChart.prototype.setData = function (series, view, markers) {
    this.series = series;
    this.view = view;
    this.markers = markers || [];
  };

  TimeChart.prototype._size = function (c) {
    const dpr = global.devicePixelRatio || 1;
    const W = this.wrap.clientWidth, H = this.height;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
      c.width = Math.round(W * dpr);
      c.height = Math.round(H * dpr);
      c.style.width = W + 'px';
      c.style.height = H + 'px';
    }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    return { ctx, W, H };
  };

  TimeChart.prototype.draw = function () {
    const { ctx, W, H } = this._size(this.base);
    if (!this.view || W < 50) { this.geom = null; return; }
    const th = theme();
    let t0 = this.view[0], t1 = this.view[1];
    if (!(t1 > t0)) t1 = t0 + 1;

    // Y scale from the visible data (all metrics are >= 0).
    let ymax = 0;
    for (const s of this.series) {
      if (s.hidden || !s.t.length) continue;
      const i0 = Math.max(0, lowerBound(s.t, t0) - 1), i1 = Math.min(s.t.length, lowerBound(s.t, t1) + 1);
      for (let i = i0; i < i1; i++) { const v = s.y[i]; if (v > ymax) ymax = v; }
    }
    if (!(ymax > 0)) ymax = 1;
    const step = niceStep(ymax, 4);
    const top = Math.ceil((ymax * 1.04) / step) * step;
    const digits = step < 1 ? Math.min(3, Math.ceil(-Math.log10(step) - 1e-9)) : 0;

    ctx.font = FONT;
    const ticks = [];
    let lw = 0;
    for (let v = 0; v <= top + step / 2; v += step) {
      const label = fmtNum(v, digits);
      ticks.push([v, label]);
      lw = Math.max(lw, ctx.measureText(label).width);
    }
    const L = Math.ceil(lw) + 12, R = 12, T = 10, B = 22;
    const pw = Math.max(10, W - L - R), ph = H - T - B;
    const g = this.geom = { L, T, pw, ph, t0, t1, top, W, H };
    const X = (t) => L + ((t - t0) / (t1 - t0)) * pw;
    const Y = (v) => T + ph - (v / top) * ph;

    // Grid + y labels
    ctx.lineWidth = 1;
    ctx.fillStyle = th.muted;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const [v, label] of ticks) {
      const y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = v === 0 ? th.axis : th.grid;
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
      ctx.fillText(label, L - 6, y);
    }

    // X labels on round times in the display time zone
    const span = t1 - t0;
    const maxTicks = Math.max(2, Math.floor(pw / 86));
    let xs = TIME_STEPS[TIME_STEPS.length - 1];
    for (const s of TIME_STEPS) { if (span / s <= maxTicks) { xs = s; break; } }
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const first = Math.ceil((t0 + Time.offset) / xs) * xs - Time.offset;
    for (let t = first; t <= t1; t += xs) {
      const x = Math.round(X(t)) + 0.5;
      ctx.strokeStyle = th.axis;
      ctx.beginPath(); ctx.moveTo(x, T + ph); ctx.lineTo(x, T + ph + 4); ctx.stroke();
      ctx.fillText(Time.fmt(t, xs), x, T + ph + 6);
    }

    // Markers: user annotations labelled, automatic events faint
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    const rowEnd = [-Infinity, -Infinity, -Infinity];  // label rows, so close markers stagger
    for (const m of this.markers) {
      if (m.t < t0 || m.t > t1) continue;
      const x = Math.round(X(m.t)) + 0.5;
      ctx.strokeStyle = m.kind === 'user' ? th.marker : th.event;
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + ph); ctx.stroke();
    }
    for (const m of this.markers) {
      if (m.kind !== 'user' || m.t < t0 || m.t > t1) continue;
      const x = Math.round(X(m.t)) + 0.5;
      const text = m.text.length > 28 ? m.text.slice(0, 27) + '…' : m.text;
      const w = ctx.measureText(text).width;
      const tx = Math.max(L, Math.min(x + 4, L + pw - w - 2));
      let row = rowEnd.findIndex((end) => tx - 2 > end);
      if (row < 0) row = rowEnd.indexOf(Math.min(...rowEnd));
      rowEnd[row] = tx + w + 2;
      const ty = T + row * 15;
      ctx.fillStyle = th.surface;
      ctx.fillRect(tx - 2, ty, w + 4, 14);
      ctx.fillStyle = th.ink2;
      ctx.fillText(text, tx, ty + 1);
    }

    // Series
    ctx.save();
    ctx.beginPath(); ctx.rect(L, T - 2, pw, ph + 4); ctx.clip();
    for (const s of this.series) {
      if (s.hidden || !s.t.length) continue;
      const st = seriesStyle(th, s.slot);
      ctx.strokeStyle = st.color;
      ctx.setLineDash(st.dash);
      ctx.lineWidth = 2;
      ctx.lineJoin = ctx.lineCap = 'round';
      this._path(ctx, s, X, Y);
    }
    ctx.restore();
    this._legend(th);
    this._overlay();
  };

  // Line path with gap breaks; above ~2 points per pixel it switches to
  // per-pixel min/max columns so spikes are never averaged away.
  TimeChart.prototype._path = function (ctx, s, X, Y) {
    const g = this.geom, t = s.t, y = s.y, gap = this.gap;
    const i0 = Math.max(0, lowerBound(t, g.t0) - 1), i1 = Math.min(t.length, lowerBound(t, g.t1) + 1);
    ctx.beginPath();
    let pen = false, prevT = -Infinity, lone = null;
    if (i1 - i0 <= g.pw * 2) {
      for (let i = i0; i < i1; i++) {
        const v = y[i];
        if (v !== v) { pen = false; continue; }
        const x = X(t[i]), yy = Y(v);
        if (!pen || t[i] - prevT > gap) { ctx.moveTo(x, yy); lone = [x, yy]; } else { ctx.lineTo(x, yy); lone = null; }
        pen = true; prevT = t[i];
      }
      ctx.stroke();
      if (lone) { ctx.beginPath(); ctx.arc(lone[0], lone[1], 1.5, 0, 6.3); ctx.stroke(); }
      return;
    }
    let col = null, mn = 0, mx = 0, f = 0, l = 0, brk = true;
    const emit = () => {
      if (col === null) return;
      const x = g.L + col + 0.5;
      if (!pen || brk) ctx.moveTo(x, Y(f)); else ctx.lineTo(x, Y(f));
      ctx.lineTo(x, Y(mn)); ctx.lineTo(x, Y(mx)); ctx.lineTo(x, Y(l));
      pen = true;
    };
    for (let i = i0; i < i1; i++) {
      const v = y[i];
      if (v !== v) { emit(); col = null; pen = false; continue; }
      const c = Math.floor(X(t[i]) - g.L);
      if (c !== col) {
        emit();
        brk = t[i] - prevT > gap;
        col = c; mn = mx = f = l = v;
      } else {
        if (v < mn) mn = v;
        if (v > mx) mx = v;
        l = v;
      }
      prevT = t[i];
    }
    emit();
    ctx.stroke();
  };

  TimeChart.prototype._legend = function (th) {
    const seen = {};
    const sorted = this.series.slice().sort((a, b) => a.label.localeCompare(b.label));
    let i = 0;
    for (const s of sorted) {
      seen[s.key] = true;
      let item = this.legendItems[s.key];
      if (!item) {
        item = this.legendItems[s.key] = {
          root: el('button', 'tc-li'),
        };
        item.root.type = 'button';
        item.key = el('span', 'tc-key', item.root);
        item.label = el('span', 'tc-label', item.root);
        item.val = el('span', 'tc-val', item.root);
        item.label.textContent = s.label;
        item.root.title = 'Click to hide / show';
        item.root.addEventListener('click', () => this.opts.onToggle && this.opts.onToggle(s.key));
      }
      if (this.legend.children[i] !== item.root) this.legend.insertBefore(item.root, this.legend.children[i] || null);
      i++;
      const st = seriesStyle(th, s.slot);
      item.key.style.borderTopColor = st.color;
      item.key.style.borderTopStyle = st.dash.length ? 'dashed' : 'solid';
      item.root.classList.toggle('off', !!s.hidden);
      item.root.setAttribute('aria-pressed', s.hidden ? 'false' : 'true');
      // latest value inside the view
      const j = Math.min(s.t.length, lowerBound(s.t, this.view[1] + 1e-6)) - 1;
      const txt = j >= 0 && s.t[j] >= this.view[0] ? this.fmt(s.y[j]) : '';
      if (item.val.textContent !== txt) item.val.textContent = txt;
    }
    for (const k of Object.keys(this.legendItems)) {
      if (!seen[k]) { this.legendItems[k].root.remove(); delete this.legendItems[k]; }
    }
  };

  // ---------------------------------------------------------------- hover
  TimeChart.prototype.setHover = function (t, local) {
    this.hoverT = t;
    this.hoverLocal = local;
    this._overlay();
  };

  TimeChart.prototype._overlay = function () {
    const { ctx } = this._size(this.over);
    const g = this.geom;
    const t = this.hoverT;
    if (!g || t == null || t < g.t0 || t > g.t1) { this.tip.hidden = true; return; }
    const th = theme();
    const x = Math.round(g.L + ((t - g.t0) / (g.t1 - g.t0)) * g.pw) + 0.5;
    ctx.strokeStyle = th.ink2;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, g.T); ctx.lineTo(x, g.T + g.ph); ctx.stroke();
    if (!this.hoverLocal) { this.tip.hidden = true; return; }

    const rows = [];
    for (const s of this.series) {
      if (s.hidden || !s.t.length) continue;
      const i = nearest(s.t, t);
      if (Math.abs(s.t[i] - t) > this.gap / 2 || s.y[i] !== s.y[i]) continue;
      rows.push({ s, v: s.y[i] });
    }
    rows.sort((a, b) => b.v - a.v);
    for (const r of rows) {
      const st = seriesStyle(th, r.s.slot);
      const y = g.T + g.ph - (r.v / g.top) * g.ph;
      ctx.beginPath(); ctx.arc(x, y, 4, 0, 6.3);
      ctx.fillStyle = st.color; ctx.fill();
      ctx.lineWidth = 2; ctx.strokeStyle = th.surface; ctx.stroke();
    }
    this._tip(t, x, rows, th);
  };

  TimeChart.prototype._tip = function (t, x, rows, th) {
    const tip = this.tip;
    tip.textContent = '';
    el('div', 'tc-tip-time', tip).textContent = Time.full(t);
    for (const m of this.markers) {
      if (Math.abs(m.t - t) <= this.gap) el('div', 'tc-tip-mark', tip).textContent = '▍' + m.text;
    }
    const MAX = 14;
    for (const r of rows.slice(0, MAX)) {
      const row = el('div', 'tc-tip-row', tip);
      const key = el('span', 'tc-key', row);
      const st = seriesStyle(th, r.s.slot);
      key.style.borderTopColor = st.color;
      key.style.borderTopStyle = st.dash.length ? 'dashed' : 'solid';
      el('strong', null, row).textContent = this.fmt(r.v);
      el('span', 'tc-tip-label', row).textContent = r.s.label;
    }
    if (rows.length > MAX) el('div', 'tc-tip-more', tip).textContent = '+' + (rows.length - MAX) + ' more';
    if (!rows.length) el('div', 'tc-tip-more', tip).textContent = 'no samples here';
    tip.hidden = false;
    const W = this.wrap.clientWidth, tw = tip.offsetWidth;
    tip.style.left = (x + 14 + tw > W ? Math.max(0, x - 14 - tw) : x + 14) + 'px';
    tip.style.top = (this.geom.T + 4) + 'px';
  };

  TimeChart.prototype._timeAt = function (e) {
    const g = this.geom;
    if (!g) return null;
    const r = this.over.getBoundingClientRect();
    const px = Math.min(Math.max(e.clientX - r.left, g.L), g.L + g.pw);
    return { px, t: g.t0 + ((px - g.L) / g.pw) * (g.t1 - g.t0), inside: e.clientX - r.left >= g.L };
  };

  TimeChart.prototype._snap = function (t) {
    let best = null;
    for (const s of this.series) {
      if (s.hidden || !s.t.length) continue;
      const c = s.t[nearest(s.t, t)];
      if (best === null || Math.abs(c - t) < Math.abs(best - t)) best = c;
    }
    return best === null ? t : best;
  };

  TimeChart.prototype._hover = function (t) {
    if (this.opts.onHover) this.opts.onHover(t, this); else this.setHover(t, true);
  };

  TimeChart.prototype._move = function (e) {
    const p = this._timeAt(e);
    if (!p) return;
    if (this.drag) {
      const a = Math.min(this.drag.px, p.px), b = Math.max(this.drag.px, p.px);
      this.sel.hidden = false;
      this.sel.style.left = a + 'px';
      this.sel.style.width = (b - a) + 'px';
      this.sel.style.top = this.geom.T + 'px';
      this.sel.style.height = this.geom.ph + 'px';
      return;
    }
    this._hover(p.inside ? this._snap(p.t) : null);
  };

  TimeChart.prototype._down = function (e) {
    if (e.button !== 0) return;
    const p = this._timeAt(e);
    if (!p || !p.inside) return;
    this.drag = p;
    this.over.setPointerCapture(e.pointerId);
  };

  TimeChart.prototype._up = function (e) {
    const d = this.drag;
    this.drag = null;
    this.sel.hidden = true;
    if (!d) return;
    const p = this._timeAt(e);
    if (p && Math.abs(p.px - d.px) > 6 && this.opts.onZoom) {
      this.opts.onZoom(Math.min(d.t, p.t), Math.max(d.t, p.t));
    }
  };

  global.TimeChart = TimeChart;
  global.PerfTime = Time;
  global.perfFmt = fmtNum;
  global.perfSeriesStyle = (slot) => seriesStyle(theme(), slot);
})(window);
