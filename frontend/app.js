/* PayLens front end. Vanilla JS against the FastAPI backend.
   No build step: this is served as static files by the same app that answers /ask. */
(function () {
  const API = location.origin;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  let ROLES = [], CURRENT = null;

  // ── theme ────────────────────────────────────────────────────────────────
  const savedTheme = (() => { try { return localStorage.getItem('pl-theme'); } catch { return null; } })();
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  else document.documentElement.removeAttribute('data-theme');
  $('theme').onclick = () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('pl-theme', next); } catch {}
    if (CURRENT) render(CURRENT);             // re-draw: SVG colours are CSS vars
  };

  // ── boot ─────────────────────────────────────────────────────────────────
  async function boot() {
    try {
      const [roles, meta] = await Promise.all([
        fetch(`${API}/roles`).then(r => r.json()),
        fetch(`${API}/datasets`).then(r => r.json()),
      ]);
      ROLES = roles.roles;
      const sel = $('role');
      sel.innerHTML = ROLES.map(r => `<option value="${r.key}">${esc(r.label)}</option>`).join('');
      sel.onchange = onRole;
      renderMeta(meta);
      await onRole();
    } catch (e) {
      $('meta').innerHTML = `<div class="empty">Backend unreachable.<br>
        Start it with <code>uvicorn backend.api.main:app</code></div>`;
    }
  }

  async function onRole() {
    const role = $('role').value;
    const def = ROLES.find(r => r.key === role);
    const needs = def && def.required_claim;
    const wrap = $('claimwrap'), cs = $('claim');
    if (needs === 'merchant_id') {
      wrap.hidden = false;
      cs.innerHTML = ['M-1042', 'M-1108', 'M-1187', 'M-1001']
        .map(m => `<option value="${m}">${m}</option>`).join('');
    } else if (needs === 'region') {
      wrap.hidden = false;
      cs.innerHTML = ['South', 'North', 'West', 'East']
        .map(m => `<option value="${m}">${m}</option>`).join('');
    } else {
      wrap.hidden = true; cs.innerHTML = '';
    }
    const s = await fetch(`${API}/suggestions?role=${role}`).then(r => r.json());
    $('sugg').innerHTML = s.suggestions.map(x =>
      `<button data-q="${esc(x.q)}">${esc(x.q)}<small>${esc(x.why)}</small></button>`).join('');
    $('sugg').querySelectorAll('button').forEach(b => b.onclick = () => {
      $('q').value = b.dataset.q; ask();
    });
  }

  function renderMeta(meta) {
    const m = meta.metrics.filter(x => x.decomposable).length;
    $('meta').innerHTML = `
      <div class="kv">
        <dt>Semantic model</dt><dd>${esc(meta.version)}</dd>
        <dt>Datasets</dt><dd>${meta.datasets.length}</dd>
        <dt>Metrics</dt><dd>${meta.metrics.length} (${m} decomposable)</dd>
        <dt>Dimensions</dt><dd>${meta.dimensions.length}</dd>
      </div>
      <p style="color:var(--ink-3);font-size:12.5px;margin:13px 0 0">
        Questions are resolved against this model only. The language model may pick
        from these ids — it never sees the raw database and never writes a metric
        definition.
      </p>`;
  }

  // ── ask ──────────────────────────────────────────────────────────────────
  async function ask() {
    const question = $('q').value.trim();
    if (!question) return;
    const role = $('role').value;
    const def = ROLES.find(r => r.key === role);
    const claims = {};
    if (def && def.required_claim && !$('claimwrap').hidden) {
      claims[def.required_claim] = $('claim').value;
    }
    $('go').disabled = true;
    $('go').innerHTML = '<span class="spin"></span> Analysing';
    $('answer').innerHTML = `<div class="panel-b"><div class="empty">
      resolving → planning → validating → executing → decomposing…</div></div>`;
    try {
      const res = await fetch(`${API}/ask`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question, role, claims }),
      });
      if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
      CURRENT = await res.json();
      render(CURRENT);
    } catch (e) {
      $('answer').innerHTML = `<div class="panel-b"><div class="refusal">
        <h4>Request failed</h4><p>${esc(e.message)}</p></div></div>`;
    } finally {
      $('go').disabled = false; $('go').textContent = 'Analyse';
    }
  }
  $('go').onclick = ask;
  $('clear').onclick = () => { $('q').value = ''; $('q').focus(); };
  $('q').addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') ask();
  });

  // ── render ───────────────────────────────────────────────────────────────
  function render(d) {
    if (d.refused) return renderRefusal(d);

    const conf = d.confidence || { score: 0, bucket: 'low', deductions: [] };
    const cls = { high: 'hi', medium: 'md', low: 'lo' }[conf.bucket] || 'lo';
    const t = d.trace || {};
    const ev = d.evidence || {};

    const chips = [
      `<span class="chip ${cls}" title="${esc((conf.deductions || [])
        .map(x => `${x.amount > 0 ? '+' : ''}${x.amount}  ${x.explanation}`).join('\n'))}">
        confidence ${conf.bucket} · ${conf.score}</span>`,
      `<span class="chip mono">${esc(ev.window || '')}</span>`,
      ev.baseline_window ? `<span class="chip mono">vs ${esc(ev.baseline_window)}</span>` : '',
      `<span class="chip mono">${t.cache_tier || 'cold'}${
        t.answer_cache_hit ? ' · cached' : ''}</span>`,
      `<span class="chip mono">${t.llm_calls ? t.llm_calls.length : 0} LLM call${
        (t.llm_calls || []).length === 1 ? '' : 's'}</span>`,
      `<span class="chip mono">${t.total_ms || 0} ms</span>`,
      t.healed ? `<span class="chip md">self-healed ×${t.heal_attempts}</span>` : '',
    ].filter(Boolean).join('');

    const n = d.narration || {};
    const observed = (n.observed || []).map(f => `<li>${esc(f.text)}</li>`).join('');
    const hypo = (n.hypotheses || []).map(h =>
      `<li><span class="tag ${h.confidence}">${h.confidence}</span>${esc(h.text)}
       ${h.testable_with ? `<em><br>test with: ${esc(h.testable_with)}</em>` : ''}</li>`).join('');
    const recs = (n.recommendations || []).map(r =>
      `<li><span class="owner">${esc(r.owner)}</span> — ${esc(r.action)}
       <em><br>${esc(r.rationale)}</em></li>`).join('');

    const assumptions = (d.assumptions || []).map(a =>
      `<div class="caveat info"><div class="sev">assumed</div><p>${esc(a.statement)}
        ${a.alternative ? `<br><span style="color:var(--ink-3)">alternative: ${esc(a.alternative)}</span>` : ''}
      </p></div>`).join('');
    const caveats = (d.caveats || []).map(c =>
      `<div class="caveat ${c.severity}"><div class="sev">${c.severity}</div>
        <p>${esc(c.message)}</p></div>`).join('');

    $('answer').innerHTML = `
      <div class="panel-b">
        <div class="headline">${esc(n.headline || '')}</div>
        <div class="chips">${chips}</div>
        ${observed ? `<div class="block observed"><h4>Observed — what the data shows</h4>
          <ul>${observed}</ul></div>` : ''}
        ${hypo ? `<div class="block hypo"><h4>Hypotheses — not proven by this query</h4>
          <ul>${hypo}</ul></div>` : ''}
        ${recs ? `<div class="block rec"><h4>Recommended next steps</h4>
          <ul>${recs}</ul></div>` : ''}
        ${assumptions || caveats ? `<div style="margin-top:20px">${assumptions}${caveats}</div>` : ''}
      </div>
      <div class="tabs" role="tablist">
        <button role="tab" aria-selected="true"  data-t="chart">Chart</button>
        <button role="tab" aria-selected="false" data-t="table">Table</button>
        <button role="tab" aria-selected="false" data-t="sql">Query</button>
        <button role="tab" aria-selected="false" data-t="evidence">Evidence</button>
        <button role="tab" aria-selected="false" data-t="trace">Trace</button>
      </div>
      <div class="panel-b" id="tabbody"></div>`;

    const tabs = $('answer').querySelectorAll('.tabs button');
    tabs.forEach(b => b.onclick = () => {
      tabs.forEach(x => x.setAttribute('aria-selected', String(x === b)));
      showTab(b.dataset.t, d);
    });
    showTab('chart', d);
  }

  function renderRefusal(d) {
    const v = (d.scope_violations || [])[0];
    $('answer').innerHTML = `<div class="panel-b">
      <div class="refusal">
        <h4>Request refused</h4>
        <p>${esc(d.refusal_reason)}</p>
        ${v ? `<p style="margin-top:9px;color:var(--ink-3);font-size:13px">
          out-of-scope entity: <code>${esc(v.entity)}</code> (${esc(v.entity_type)})</p>` : ''}
      </div>
      <p style="color:var(--ink-3);font-size:13px;margin-top:14px">
        A filtered answer would have returned a number that looks right and is wrong,
        so the request is refused rather than silently narrowed.
      </p></div>`;
  }

  // ── tabs ─────────────────────────────────────────────────────────────────
  function showTab(which, d) {
    const body = $('tabbody');
    if (which === 'chart') return drawChart(body, d);
    if (which === 'table') return drawTable(body, d);
    if (which === 'sql') {
      body.innerHTML = `
        <p style="color:var(--ink-3);font-size:12.5px;margin:0 0 9px">
          Executed SQL — after validation, row cap and any access-scope predicate.</p>
        <pre>${esc((d.evidence || {}).sql || '')}</pre>
        <p style="color:var(--ink-3);font-size:12.5px;margin:15px 0 9px">
          Stored template — pre-scope, pre-limit, parameterised. This is what the
          cache keeps, so it carries no tenant filter and no absolute dates.</p>
        <pre>${esc((d.evidence || {}).template_sql || '')}</pre>`;
      return;
    }
    if (which === 'evidence') return drawEvidence(body, d);
    return drawTrace(body, d);
  }

  function drawChart(body, d) {
    body.innerHTML = '';
    const spec = d.chart || { chart_type: 'table' };
    const rows = d.rows || [];
    const metric = (d.plan || {}).metric;
    const kindOf = (col) => (col === metric && /rate|share/.test(col || '')) ? 'percent'
      : col === 'gmv' || col === 'settled_amount' || col === 'avg_ticket' ? 'currency'
      : /rate/.test(col || '') ? 'percent' : 'number';

    let node = null;
    const dec = (d.decompositions || [])[0];
    if (spec.chart_type === 'waterfall' && dec) {
      node = PLCharts.waterfall(dec);
    } else if (spec.chart_type === 'kpi') {
      const v = rows.length ? rows[0][spec.y || metric] : null;
      node = PLCharts.kpi(v, kindOf(spec.y || metric),
        `${(d.plan || {}).metric || ''} · ${(d.evidence || {}).window || ''}`);
    } else if (spec.chart_type === 'line' && spec.x) {
      node = PLCharts.line(rows, { x: spec.x, y: spec.y, series: spec.series,
                                   kind: kindOf(spec.y) });
    } else if (spec.chart_type === 'stacked_area_100' && spec.x && spec.series) {
      node = PLCharts.shareArea(rows, { x: spec.x, series: spec.series });
    } else if ((spec.chart_type === 'bar' || spec.chart_type === 'horizontal_bar')
               && spec.x && spec.y) {
      node = PLCharts.bar(rows, { x: spec.x, y: spec.y, kind: kindOf(spec.y),
                                  horizontal: spec.chart_type === 'horizontal_bar'
                                              || rows.length > 8 });
    }
    if (!node) { drawTable(body, d); return; }

    if (spec.title) {
      const h = document.createElement('h4');
      h.textContent = spec.title;
      h.style.cssText = 'margin-bottom:12px;font-size:15px';
      body.appendChild(h);
    }
    body.appendChild(node);

    // Per-segment effect table under the waterfall — the decomposition in full.
    if (dec && dec.segments && dec.segments.length) {
      const t = document.createElement('div');
      t.className = 'scroll';
      t.style.marginTop = '18px';
      t.innerHTML = `<table><thead><tr>
          <th>${esc(dec.dimension.replace(/_/g, ' '))}</th>
          <th class="num">share before</th><th class="num">share after</th>
          <th class="num">own rate before</th><th class="num">own rate after</th>
          <th class="num">mix</th><th class="num">rate</th></tr></thead><tbody>
        ${dec.segments.map(s => `<tr>
          <td>${esc(s.key)}</td>
          <td class="num">${(s.weight_base * 100).toFixed(2)}%</td>
          <td class="num">${(s.weight_target * 100).toFixed(2)}%</td>
          <td class="num">${(s.value_base * 100).toFixed(2)}%</td>
          <td class="num">${(s.value_target * 100).toFixed(2)}%</td>
          <td class="num">${s.mix_pp >= 0 ? '+' : ''}${s.mix_pp.toFixed(3)}pp</td>
          <td class="num">${s.rate_pp >= 0 ? '+' : ''}${s.rate_pp.toFixed(3)}pp</td>
        </tr>`).join('')}</tbody></table>`;
      body.appendChild(t);
      const foot = document.createElement('p');
      foot.style.cssText = 'color:var(--ink-3);font-size:12.5px;margin-top:10px';
      foot.textContent = `mix ${dec.mix_pp.toFixed(3)}pp + rate ${dec.rate_pp.toFixed(3)}pp`
        + ` = ${dec.delta_pp.toFixed(3)}pp observed (residual `
        + `${dec.residual_pp.toExponential(1)} — the identity is exact).`;
      body.appendChild(foot);
    }
    if (spec.overrides && spec.overrides.length) {
      const o = document.createElement('p');
      o.style.cssText = 'color:var(--ink-3);font-size:12px;margin-top:12px';
      o.innerHTML = '<b>chart corrections:</b> ' + spec.overrides.map(esc).join(' · ');
      body.appendChild(o);
    }
  }

  function drawTable(body, d) {
    const rows = d.rows || [];
    if (!rows.length) { body.innerHTML = '<div class="empty">No rows returned.</div>'; return; }
    const cols = Object.keys(rows[0]);
    const isNum = (c) => rows.some(r => typeof r[c] === 'number');
    body.innerHTML = `
      ${(d.evidence || {}).truncated ? `<p style="color:var(--warn);font-size:12.5px;margin:0 0 9px">
        Showing ${rows.length.toLocaleString()} of
        ${((d.evidence || {}).total_rows || 0).toLocaleString()} rows — detail listing,
        representative but incomplete.</p>` : ''}
      <div class="scroll"><table><thead><tr>${cols.map(c =>
        `<th class="${isNum(c) ? 'num' : ''}">${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(0, 300).map(r => `<tr>${cols.map(c => {
        const v = r[c];
        const txt = typeof v === 'number'
          ? (Math.abs(v) < 1 && v !== 0 ? v.toFixed(4) : PLCharts.fmt(v))
          : esc(v);
        return `<td class="${isNum(c) ? 'num' : ''}">${txt}</td>`;
      }).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  function drawEvidence(body, d) {
    const ev = d.evidence || {}, md = ev.metric_definition || {}, p = d.plan || {};
    body.innerHTML = `
      <dl class="kv">
        <dt>Datasets</dt><dd>${(ev.datasets || []).map(esc).join(', ')}</dd>
        <dt>Time window</dt><dd>${esc(ev.window || '')}</dd>
        ${ev.baseline_window ? `<dt>Baseline window</dt><dd>${esc(ev.baseline_window)}</dd>` : ''}
        <dt>Filters</dt><dd>${(ev.filters || []).length ? (ev.filters || []).map(esc).join('; ') : 'none'}</dd>
        <dt>Rows returned</dt><dd>${ev.row_count ?? 0}${ev.truncated ? ' (truncated)' : ''}</dd>
        <dt>Execution time</dt><dd>${ev.exec_ms ?? 0} ms</dd>
        <dt>Intent</dt><dd>${esc(p.intent || '')}</dd>
        <dt>Segmented by</dt><dd>${(p.segment_by || []).map(esc).join(', ') || 'none'}</dd>
      </dl>
      <h4 style="margin:20px 0 9px;font-size:13px">Metric definition</h4>
      <dl class="kv">
        <dt>Metric</dt><dd>${esc(md.label || '')} (<code>${esc(md.metric || '')}</code>)</dd>
        <dt>Numerator</dt><dd>${esc(md.numerator || '')}</dd>
        ${md.denominator ? `<dt>Denominator</dt><dd>${esc(md.denominator)}</dd>` : ''}
      </dl>
      ${md.note ? `<p style="color:var(--ink-2);font-size:13px;margin-top:10px">${esc(md.note)}</p>` : ''}
      <h4 style="margin:20px 0 9px;font-size:13px">Validation gate</h4>
      <div class="checks">${(ev.guard_checks || []).map(c => `<span>${esc(c)}</span>`).join('')}</div>
      ${(ev.injected_predicates || []).length ? `
        <p style="color:var(--ink-2);font-size:13px;margin-top:11px">
          Access-scope predicates injected into the query AST:
          ${(ev.injected_predicates || []).map(x => `<code>${esc(x)}</code>`).join(', ')}
        </p>` : `<p style="color:var(--ink-3);font-size:12.5px;margin-top:11px">
          No row-scope predicate for this role.</p>`}`;
  }

  function drawTrace(body, d) {
    const t = d.trace || {};
    const steps = Object.entries(t.step_ms || {});
    body.innerHTML = `
      <dl class="kv">
        <dt>Cache tier</dt><dd>${esc(t.cache_tier || '')}${
          t.cache_similarity != null ? ` (similarity ${t.cache_similarity})` : ''}</dd>
        ${t.cache_demotion_reason ? `<dt>Why not reused</dt><dd>${esc(t.cache_demotion_reason)}</dd>` : ''}
        <dt>Answer cache</dt><dd>${t.answer_cache_hit ? 'hit' : 'miss'}</dd>
        <dt>Self-healed</dt><dd>${t.healed ? `yes (${t.heal_attempts} attempt(s))` : 'no'}</dd>
        <dt>Guard passed first try</dt><dd>${t.guard_first_try ? 'yes' : 'no'}</dd>
        <dt>LLM calls</dt><dd>${(t.llm_calls || []).length === 0 ? '0 — deterministic path'
          : (t.llm_calls || []).map(c => `${c.purpose}:${c.model} ${c.ms}ms`).join(', ')}</dd>
        <dt>Total</dt><dd>${t.total_ms || 0} ms</dd>
      </dl>
      <h4 style="margin:20px 0 9px;font-size:13px">Per-step timing</h4>
      <div class="trace">${steps.map(([k, v]) => `<span>${esc(k)} ${v}ms</span>`).join('')}</div>
      <p style="color:var(--ink-3);font-size:12.5px;margin-top:14px">
        Every answer records what it cost. "0 LLM calls" means the deterministic
        resolver and compiler handled the question end to end.
      </p>`;
  }

  /* Deep links: /?q=...&role=...&claim=... asks on load.
     Makes an answer shareable — paste the link and the reader gets the same
     evidence card, not a screenshot of it. */
  async function bootAndMaybeAsk() {
    await boot();
    const p = new URLSearchParams(location.search);
    const q = p.get('q');
    if (!q) return;
    if (p.get('role')) { $('role').value = p.get('role'); await onRole(); }
    if (p.get('claim') && !$('claimwrap').hidden) $('claim').value = p.get('claim');
    if (p.get('theme')) document.documentElement.dataset.theme = p.get('theme');
    $('q').value = q;
    ask();
  }

  bootAndMaybeAsk();
})();
