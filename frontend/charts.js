/* Hand-rolled SVG charts.
   No chart library: full control over the mark specs, and nothing to fetch at runtime.

   Mark specs follow the project's visualisation rules — 2px lines, 4px rounded
   data-ends anchored to the baseline, a 2px surface gap between adjacent fills,
   >=8px hover targets, recessive grid, legend whenever there are 2+ series, and
   selective direct labels rather than a number on every mark. */
(function (global) {
  const NS = 'http://www.w3.org/2000/svg';
  const SERIES = ['--s1','--s2','--s3','--s4','--s5','--s6','--s7'];
  const MAX_SERIES = 7;         // an 8th folds into "Other", never a generated hue

  const el = (n, a = {}) => {
    const e = document.createElementNS(NS, n);
    for (const k in a) if (a[k] != null) e.setAttribute(k, a[k]);
    return e;
  };
  const cssVar = (v) => getComputedStyle(document.documentElement)
    .getPropertyValue(v).trim() || '#888';
  const seriesColor = (i) => cssVar(SERIES[i % SERIES.length]);

  const fmt = (v, kind) => {
    if (v == null || Number.isNaN(v)) return '—';
    if (kind === 'percent') return (v * 100).toFixed(2) + '%';
    if (kind === 'pp') return (v >= 0 ? '+' : '') + v.toFixed(2) + 'pp';
    if (kind === 'currency') return '₹' + Math.round(v).toLocaleString('en-IN');
    if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString('en-IN');
    return (Math.round(v * 100) / 100).toLocaleString('en-IN');
  };
  const niceMax = (v) => {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    return Math.ceil(v / p * 2) / 2 * p;
  };

  // ── tooltip ──────────────────────────────────────────────────────────────
  const tip = () => document.getElementById('tip');
  function bindTip(node, html) {
    node.addEventListener('pointerenter', (e) => {
      const t = tip(); t.innerHTML = html; t.classList.add('on');
      t.style.left = Math.min(e.clientX + 14, innerWidth - 300) + 'px';
      t.style.top = (e.clientY - 12) + 'px';
    });
    node.addEventListener('pointermove', (e) => {
      const t = tip();
      t.style.left = Math.min(e.clientX + 14, innerWidth - 300) + 'px';
      t.style.top = (e.clientY - 12) + 'px';
    });
    node.addEventListener('pointerleave', () => tip().classList.remove('on'));
  }

  function legend(items) {
    const d = document.createElement('div');
    d.className = 'legend';
    items.forEach(([label, color]) => {
      const s = document.createElement('span');
      s.innerHTML = `<i style="background:${color}"></i>${label}`;
      d.appendChild(s);
    });
    return d;
  }

  /* Fold beyond MAX_SERIES into a single "Other" rather than inventing hues. */
  function foldSeries(rows, keyCol, valCol) {
    if (rows.length <= MAX_SERIES) return rows;
    const sorted = [...rows].sort((a, b) => Math.abs(b[valCol]) - Math.abs(a[valCol]));
    const keep = sorted.slice(0, MAX_SERIES - 1);
    const rest = sorted.slice(MAX_SERIES - 1);
    const other = { [keyCol]: `Other (${rest.length})`, [valCol]:
      rest.reduce((s, r) => s + (r[valCol] || 0), 0) / rest.length, __other: true };
    return [...keep, other];
  }

  // ── bar / horizontal bar ─────────────────────────────────────────────────
  function bar(rows, { x, y, kind, horizontal = false }) {
    const data = foldSeries(rows.filter(r => r[y] != null), x, y);
    if (!data.length) return null;
    const wrap = document.createDocumentFragment();
    const W = 760, barH = 30, gap = 10;

    if (horizontal) {
      const padL = Math.min(210, Math.max(...data.map(d => String(d[x]).length)) * 7.6 + 16);
      const H = data.length * (barH + gap) + 26;
      const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
      const max = niceMax(Math.max(...data.map(d => d[y])));
      const plotW = W - padL - 86;
      for (let i = 0; i <= 4; i++) {
        const gx = padL + plotW * i / 4;
        svg.appendChild(el('line', { x1: gx, y1: 0, x2: gx, y2: H - 26, class: 'gridline' }));
        const t = el('text', { x: gx, y: H - 10, class: 'tick', 'text-anchor': 'middle' });
        t.textContent = fmt(max * i / 4, kind); svg.appendChild(t);
      }
      data.forEach((d, i) => {
        const yy = i * (barH + gap);
        const w = Math.max(2, plotW * (d[y] / max));
        const lab = el('text', { x: padL - 10, y: yy + barH / 2 + 4, class: 'tick',
                                 'text-anchor': 'end' });
        lab.textContent = String(d[x]).slice(0, 26); svg.appendChild(lab);
        // 4px rounded data-end, square against the baseline
        const r = el('path', {
          d: `M${padL},${yy} H${padL + w - 4} a4,4 0 0 1 4,4 V${yy + barH - 4}
              a4,4 0 0 1 -4,4 H${padL} Z`.replace(/\s+/g, ' '),
          fill: d.__other ? cssVar('--other') : seriesColor(i),
          stroke: cssVar('--card'), 'stroke-width': 2 });
        bindTip(r, `<b>${d[x]}</b><br>${y.replace(/_/g,' ')}: ${fmt(d[y], kind)}` +
          (d.attempts != null ? `<br>attempts: ${fmt(d.attempts)}` : ''));
        svg.appendChild(r);
        const v = el('text', { x: padL + w + 9, y: yy + barH / 2 + 4, class: 'dlabel' });
        v.textContent = fmt(d[y], kind); svg.appendChild(v);
      });
      wrap.appendChild(svg);
      return wrap;
    }

    const H = 300, padB = 46, padL = 62;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    const max = niceMax(Math.max(...data.map(d => d[y])));
    const plotH = H - padB - 14, plotW = W - padL - 18;
    const bw = Math.min(64, plotW / data.length - 12);
    for (let i = 0; i <= 4; i++) {
      const gy = 14 + plotH * (1 - i / 4);
      svg.appendChild(el('line', { x1: padL, y1: gy, x2: W - 18, y2: gy, class: 'gridline' }));
      const t = el('text', { x: padL - 9, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = fmt(max * i / 4, kind); svg.appendChild(t);
    }
    data.forEach((d, i) => {
      const cx = padL + (plotW / data.length) * (i + 0.5);
      const h = Math.max(2, plotH * (d[y] / max));
      const yy = 14 + plotH - h;
      const r = el('path', {
        d: `M${cx - bw / 2},${14 + plotH} V${yy + 4} a4,4 0 0 1 4,-4 H${cx + bw / 2 - 4}
            a4,4 0 0 1 4,4 V${14 + plotH} Z`.replace(/\s+/g, ' '),
        fill: d.__other ? cssVar('--other') : seriesColor(i),
        stroke: cssVar('--card'), 'stroke-width': 2 });
      bindTip(r, `<b>${d[x]}</b><br>${y.replace(/_/g,' ')}: ${fmt(d[y], kind)}` +
        (d.attempts != null ? `<br>attempts: ${fmt(d.attempts)}` : ''));
      svg.appendChild(r);
      const v = el('text', { x: cx, y: yy - 7, class: 'dlabel', 'text-anchor': 'middle' });
      v.textContent = fmt(d[y], kind); svg.appendChild(v);
      const lb = el('text', { x: cx, y: H - 24, class: 'tick', 'text-anchor': 'middle' });
      lb.textContent = String(d[x]).slice(0, 14); svg.appendChild(lb);
    });
    wrap.appendChild(svg);
    return wrap;
  }

  // ── line (multi-series) ──────────────────────────────────────────────────
  function line(rows, { x, y, series, kind }) {
    const wrap = document.createDocumentFragment();
    const groups = new Map();
    rows.forEach(r => {
      const k = series ? String(r[series] ?? '—') : (y || 'value');
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    });
    let keys = [...groups.keys()];
    if (keys.length > MAX_SERIES) {
      keys = keys.slice(0, MAX_SERIES);                 // fold, never cycle hues
    }
    const xs = [...new Set(rows.map(r => String(r[x])))].sort();
    const vals = rows.map(r => r[y]).filter(v => v != null);
    if (!vals.length || xs.length < 2) return null;
    const min = Math.min(...vals), max = Math.max(...vals);
    const lo = min - (max - min) * 0.12, hi = max + (max - min) * 0.12;

    const W = 760, H = 310, padL = 62, padB = 42;
    const plotW = W - padL - 18, plotH = H - padB - 14;
    const sx = (v) => padL + plotW * (xs.indexOf(String(v)) / (xs.length - 1));
    const sy = (v) => 14 + plotH * (1 - (v - lo) / (hi - lo || 1));

    if (keys.length >= 2) {
      wrap.appendChild(legend(keys.map((k, i) => [k, seriesColor(i)])));
    }
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    for (let i = 0; i <= 4; i++) {
      const gy = 14 + plotH * (1 - i / 4);
      svg.appendChild(el('line', { x1: padL, y1: gy, x2: W - 18, y2: gy, class: 'gridline' }));
      const t = el('text', { x: padL - 9, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = fmt(lo + (hi - lo) * i / 4, kind); svg.appendChild(t);
    }
    const step = Math.max(1, Math.ceil(xs.length / 7));
    xs.forEach((v, i) => {
      if (i % step && i !== xs.length - 1) return;
      const t = el('text', { x: sx(v), y: H - 20, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = String(v).slice(0, 10); svg.appendChild(t);
    });
    keys.forEach((k, i) => {
      const pts = groups.get(k).filter(r => r[y] != null)
        .sort((a, b) => String(a[x]).localeCompare(String(b[x])));
      if (pts.length < 2) return;
      const col = seriesColor(i);
      svg.appendChild(el('path', {
        d: pts.map((p, j) => `${j ? 'L' : 'M'}${sx(p[x])},${sy(p[y])}`).join(' '),
        fill: 'none', stroke: col, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
      pts.forEach(p => {
        const c = el('circle', { cx: sx(p[x]), cy: sy(p[y]), r: 4.5, fill: col,
                                 stroke: cssVar('--card'), 'stroke-width': 2 });
        // hit target larger than the mark
        const hit = el('circle', { cx: sx(p[x]), cy: sy(p[y]), r: 11, fill: 'transparent' });
        bindTip(hit, `<b>${k}</b><br>${p[x]}<br>${fmt(p[y], kind)}`);
        svg.appendChild(c); svg.appendChild(hit);
      });
      if (keys.length <= 4) {                       // direct-label up to 4 series
        const last = pts[pts.length - 1];
        const t = el('text', { x: sx(last[x]) + 8, y: sy(last[y]) + 4, class: 'dlabel',
                               fill: col });
        t.textContent = k.slice(0, 12); svg.appendChild(t);
      }
    });
    wrap.appendChild(svg);
    return wrap;
  }

  // ── waterfall: baseline -> mix -> rate -> target ──────────────────────────
  function waterfall(dec, kind = 'percent') {
    const wrap = document.createDocumentFragment();
    const steps = [
      { label: 'Baseline', value: dec.baseline_value, kind: 'abs' },
      { label: 'Mix effect', value: dec.mix_pp, kind: 'delta' },
      { label: 'Rate effect', value: dec.rate_pp, kind: 'delta' },
      { label: 'Current', value: dec.target_value, kind: 'abs' },
    ];
    const W = 760, H = 320, padL = 62, padB = 54;
    const plotH = H - padB - 26, plotW = W - padL - 24;
    const all = [dec.baseline_value, dec.target_value];
    const lo = Math.min(...all) - 0.9, hi = Math.max(...all) + 0.5;
    const sy = (v) => 26 + plotH * (1 - (v - lo) / (hi - lo || 1));
    const bw = Math.min(96, plotW / steps.length - 26);

    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    for (let i = 0; i <= 4; i++) {
      const gy = 26 + plotH * (1 - i / 4);
      svg.appendChild(el('line', { x1: padL, y1: gy, x2: W - 24, y2: gy, class: 'gridline' }));
      const t = el('text', { x: padL - 9, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = (lo + (hi - lo) * i / 4).toFixed(1) + '%'; svg.appendChild(t);
    }
    let running = dec.baseline_value;
    steps.forEach((s, i) => {
      const cx = padL + (plotW / steps.length) * (i + 0.5);
      let top, bot, col, label;
      if (s.kind === 'abs') {
        top = sy(s.value); bot = sy(lo); col = cssVar('--s4');
        label = s.value.toFixed(2) + '%';
        running = s.value;
      } else {
        const from = running, to = running + s.value;
        top = sy(Math.max(from, to)); bot = sy(Math.min(from, to));
        col = s.value < 0 ? cssVar('--s5') : cssVar('--s1');
        label = (s.value >= 0 ? '+' : '') + s.value.toFixed(2) + 'pp';
        running = to;
        svg.appendChild(el('line', { x1: cx - bw / 2 - 14, y1: sy(from),
          x2: cx - bw / 2, y2: sy(from), stroke: cssVar('--border'),
          'stroke-width': 1, 'stroke-dasharray': '3 3' }));
      }
      const h = Math.max(3, bot - top);
      const r = el('rect', { x: cx - bw / 2, y: top, width: bw, height: h, rx: 4,
        fill: col, stroke: cssVar('--card'), 'stroke-width': 2 });
      bindTip(r, `<b>${s.label}</b><br>${label}` + (s.kind === 'delta'
        ? `<br>${s.label === 'Mix effect'
            ? 'traffic moved between segments' : 'segments changed their own rate'}` : ''));
      svg.appendChild(r);
      const v = el('text', { x: cx, y: top - 9, class: 'dlabel', 'text-anchor': 'middle' });
      v.textContent = label; svg.appendChild(v);
      const lb = el('text', { x: cx, y: H - 30, class: 'tick', 'text-anchor': 'middle' });
      lb.textContent = s.label; svg.appendChild(lb);
    });
    const note = el('text', { x: padL, y: H - 8, class: 'tick' });
    note.textContent = `${Math.abs(dec.mix_share_pct).toFixed(0)}% of the `
      + `${dec.delta_pp.toFixed(2)}pp move is traffic mix, not changed success rates`;
    svg.appendChild(note);
    wrap.appendChild(legend([['Level', cssVar('--s4')],
                             ['Negative effect', cssVar('--s5')],
                             ['Positive effect', cssVar('--s1')]]));
    wrap.appendChild(svg);
    return wrap;
  }

  // ── 100% stacked area: share over time (mix shift made visible) ──────────
  function shareArea(rows, { x, series, weightCol = 'attempts' }) {
    const xs = [...new Set(rows.map(r => String(r[x])))].sort();
    let keys = [...new Set(rows.map(r => String(r[series])))];
    if (keys.length > MAX_SERIES) keys = keys.slice(0, MAX_SERIES);
    if (xs.length < 2 || keys.length < 2) return null;
    const idx = new Map();
    rows.forEach(r => idx.set(`${r[x]}|${r[series]}`, r[weightCol] || 0));

    const W = 760, H = 300, padL = 48, padB = 40;
    const plotW = W - padL - 18, plotH = H - padB - 14;
    const sx = (i) => padL + plotW * (i / (xs.length - 1));
    const wrap = document.createDocumentFragment();
    wrap.appendChild(legend(keys.map((k, i) => [k, seriesColor(i)])));
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
    const cum = xs.map(() => 0);
    keys.forEach((k, ki) => {
      const top = [], bot = [];
      xs.forEach((xv, i) => {
        const total = keys.reduce((s, kk) => s + (idx.get(`${xv}|${kk}`) || 0), 0) || 1;
        const share = (idx.get(`${xv}|${k}`) || 0) / total;
        bot.push(cum[i]); cum[i] += share; top.push(cum[i]);
      });
      const path = top.map((v, i) => `${i ? 'L' : 'M'}${sx(i)},${14 + plotH * (1 - v)}`)
        .concat(bot.map((v, i) => `L${sx(bot.length - 1 - i)},${14 + plotH * (1 - bot[bot.length - 1 - i])}`))
        .join(' ') + ' Z';
      const p = el('path', { d: path, fill: seriesColor(ki), 'fill-opacity': .92,
                             stroke: cssVar('--card'), 'stroke-width': 2 });
      bindTip(p, `<b>${k}</b><br>share of attempts over time`);
      svg.appendChild(p);
    });
    for (let i = 0; i <= 4; i++) {
      const gy = 14 + plotH * (1 - i / 4);
      const t = el('text', { x: padL - 8, y: gy + 4, class: 'tick', 'text-anchor': 'end' });
      t.textContent = (i * 25) + '%'; svg.appendChild(t);
    }
    const step = Math.max(1, Math.ceil(xs.length / 7));
    xs.forEach((v, i) => {
      if (i % step && i !== xs.length - 1) return;
      const t = el('text', { x: sx(i), y: H - 18, class: 'tick', 'text-anchor': 'middle' });
      t.textContent = String(v).slice(0, 10); svg.appendChild(t);
    });
    wrap.appendChild(svg);
    return wrap;
  }

  function kpi(value, kind, sub) {
    const d = document.createElement('div');
    d.innerHTML = `<div class="kpi">${fmt(value, kind)}</div>
                   <div class="kpi-sub">${sub || ''}</div>`;
    return d;
  }

  global.PLCharts = { bar, line, waterfall, shareArea, kpi, fmt, legend, seriesColor };
})(window);
