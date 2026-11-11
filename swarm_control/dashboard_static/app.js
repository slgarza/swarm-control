/* Local, read-only evidence dashboard. All displayed run data comes from the API. */
'use strict';

(() => {
  const byId = (id) => document.getElementById(id);
  const state = {snapshot: null, autoRefresh: true, fetching: false, selected: null, fetchFailed: false};
  const countFormat = new Intl.NumberFormat('es-MX');
  const moneyFormat = new Intl.NumberFormat('es-MX', {style: 'currency', currency: 'USD', maximumFractionDigits: 2});
  const dateFormat = new Intl.DateTimeFormat('es-MX', {dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC'});
  const timeFormat = new Intl.DateTimeFormat('es-MX', {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC'});
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const number = (value) => finite(value) ? countFormat.format(value) : '—';
  const money = (value) => finite(value) ? moneyFormat.format(value) : 'Sin dato';
  const date = (value) => {const parsed = typeof value === 'string' ? new Date(value) : null; return parsed && Number.isFinite(parsed.getTime()) ? parsed : null;};
  const timeLabel = (value) => {const parsed = date(value); return parsed ? timeFormat.format(parsed) + ' UTC' : 'Sin hora';};
  const dateLabel = (value) => {const parsed = date(value); return parsed ? dateFormat.format(parsed) + ' UTC' : 'Sin dato';};
  const setText = (id, value) => {byId(id).textContent = String(value);};
  const node = (tag, className, text) => {const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = String(text); return result;};
  const clear = (element) => element.replaceChildren();
  const safeURL = (value) => {
    if (typeof value !== 'string') return null;
    try {const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && url.hostname === 'github.com' ? url.href : null;} catch {return null;}
  };
  const link = (label, href, className = 'table-link') => {
    const url = safeURL(href);
    if (!url) return node('span', 'subtle', 'Sin enlace');
    const anchor = node('a', className, label); anchor.href = url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; return anchor;
  };
  const pill = (text, type = 'neutral') => node('span', 'pill ' + type, text);
  const tasks = () => Array.isArray(state.snapshot?.tasks) ? state.snapshot.tasks : [];
  const phaseLabels = {integration: 'Integración en curso', complete: 'Cohorte cerrada', paused: 'Integración en pausa', unavailable: 'Sin snapshot'};
  const originalLabel = (task) => ({needs_repair: 'Necesita reparación', validated: 'Validado', working: 'En ejecución', preparing: 'Preparando', checking: 'Verificando', failed: 'Falló', rejected: 'Rechazado', needs_recovery: 'Necesita recuperación'}[task.original_status] || 'Sin estado');
  const repaired = (task) => task.attribution === 'coordinator_code_or_test_repair';
  const recoveredLabel = (task) => task.provisional ? (repaired(task) ? 'Con reparación local' : 'Bytes originales') : task.empty_patch ? 'Parche vacío' : 'Sin calificar';
  const originalType = (task) => task.original_status === 'validated' ? 'mint-pill' : ['needs_repair', 'failed', 'rejected', 'needs_recovery'].includes(task.original_status) ? 'coral-pill' : 'neutral';
  const secondsLabel = (value) => {if (!finite(value)) return 'Sin dato'; if (value < 60) return value.toFixed(1) + ' s'; const hours = Math.floor(value / 3600); const minutes = Math.floor(value % 3600 / 60); return hours ? hours + ' h ' + minutes + ' min' : minutes + ' min';};

  function announce(message) {setText('screen-reader-status', message);}

  function renderOverview(data) {
    const summary = data.summary || {};
    setText('repo-name', data.repository || 'Repositorio sin identificar');
    setText('run-name', data.run_id || 'Sin identificador de ejecución');
    setText('run-phase', phaseLabels[data.phase] || 'Estado desconocido');
    byId('run-phase').className = 'pill ' + (data.phase === 'integration' ? 'amber-pill' : data.phase === 'complete' ? 'mint-pill' : 'neutral');
    setText('snapshot-time', 'Fuente: ' + dateLabel(data.source_as_of));
    setText('nav-worker-count', number(summary.assigned));
    setText('metric-workers', number(summary.assigned));
    setText('metric-workers-note', number(summary.original_needs_repair) + ' originales necesitan reparación');
    setText('metric-validated', number(summary.original_validated));
    setText('metric-validated-note', 'Validación original · sin reescribir resultados');
    setText('metric-recovered', number(summary.provisional_product));
    setText('metric-recovered-note', number(summary.provisional_support) + ' de soporte · calidad aún provisional');
    setText('metric-active', number(summary.active_workers));
    setText('metric-active-note', summary.active_workers === null ? 'Inventario no disponible' : 'Último inventario · no observación continua');
    setText('workers-title', (finite(summary.assigned) ? number(summary.assigned) + ' workers.' : 'Cohorte de workers.') + ' Una historia completa.');
    setText('truth-note', 'Los resultados originales y la recuperación son etapas distintas. Las VMs y los eventos de comandos no prueban 100 agentes útiles simultáneos; tampoco una victoria frente a AFK.');
    const connection = byId('connection-status'); clear(connection);
    connection.append(node('span', 'status-dot ' + (data.stale ? 'amber' : 'mint')), document.createTextNode(data.stale ? 'Fuente desactualizada' : 'Snapshot disponible'));
    const alert = byId('data-alert');
    alert.hidden = !data.stale;
    if (data.stale) alert.textContent = 'La fuente está marcada como desactualizada. Estos datos describen su hora de captura, no el estado actual del proveedor.';
  }

  function renderGrid(data) {
    const grid = byId('worker-grid'); clear(grid);
    for (const task of tasks()) {
      const tile = node('button', 'worker-tile' + (['needs_repair', 'failed', 'rejected', 'needs_recovery'].includes(task.original_status) ? ' is-failed' : '') + (task.provisional ? ' is-recovered' : '') + (task.feature_candidate === false ? ' is-support' : ''), String(task.id || '').replace(/^B/, ''));
      tile.type = 'button';
      tile.setAttribute('aria-label', String(task.id || 'Tarea') + ': original ' + originalLabel(task) + '; ' + recoveredLabel(task) + (task.feature_candidate === false ? '; soporte' : ''));
      tile.title = String(task.id || '') + (task.title ? ' · ' + task.title : '') + ' · ' + originalLabel(task) + ' · ' + recoveredLabel(task);
      if (task.provisional) {const marker = node('span', 'recovery-marker'); marker.setAttribute('aria-hidden', 'true'); tile.append(marker);}
      tile.addEventListener('click', () => openDetail(task.id)); grid.append(tile);
    }
    const summary = data.summary || {};
    setText('worker-summary', number(summary.original_needs_repair) + ' originales necesitan reparación · ' + number(summary.empty_patches) + ' parches vacíos');
  }

  function renderChart(data) {
    const container = byId('recovery-chart'); clear(container);
    const summary = data.summary || {};
    setText('chart-total', number(summary.provisional_product) + ' provisionales');
    const split = byId('recovery-split'); clear(split);
    for (const [value, label] of [[summary.unchanged_recoveries, 'bytes originales'], [summary.coordinator_repaired, 'con reparación local'], [summary.provisional_support, 'de soporte']]) {
      const item = node('span'); item.append(node('strong', '', number(value)), document.createTextNode(' ' + label)); split.append(item);
    }
    const t0 = date(data.clock?.t0);
    const events = tasks().filter((task) => task.provisional && task.feature_candidate !== false).map((task) => {
      const green = date(task.delivery?.green_at), merged = date(task.delivery?.merged_at);
      return {at: green && merged ? new Date(Math.max(green.getTime(), merged.getTime())) : green || merged, id: task.id};
    }).filter((event) => event.at).sort((a, b) => a.at - b.at);
    if (!t0 || !events.length) {
      container.append(node('div', 'chart-empty', 'Sin hitos de producto verificados para graficar.'));
      container.setAttribute('aria-label', 'Sin hitos de producto verificados para graficar.'); return;
    }
    const last = date(data.clock?.t1) || date(data.source_as_of) || events[events.length - 1].at;
    const start = t0.getTime(), end = Math.max(last.getTime(), events[events.length - 1].at.getTime(), start + 60000);
    const max = Math.max(5, Math.ceil(Math.max(events.length, finite(summary.provisional_product) ? summary.provisional_product : 0) / 5) * 5);
    const ns = 'http://www.w3.org/2000/svg';
    const svgNode = (tag, attributes, text) => {const element = document.createElementNS(ns, tag); for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value)); if (text !== undefined) element.textContent = String(text); return element;};
    const svg = svgNode('svg', {viewBox: '0 0 600 180', preserveAspectRatio: 'none', 'aria-hidden': 'true'});
    const x = (time) => 34 + Math.max(0, Math.min(1, (time - start) / (end - start))) * 547;
    const y = (value) => 146 - Math.min(max, value) / max * 125;
    for (let tick = 0; tick <= 4; tick++) {
      const value = Math.round(max * tick / 4), atY = y(value);
      svg.append(svgNode('line', {x1: 34, x2: 581, y1: atY, y2: atY, stroke: '#303c44', 'stroke-width': '.6', 'stroke-dasharray': '3 4'}));
      svg.append(svgNode('text', {x: 23, y: atY + 3, fill: '#788c9a', 'font-size': 8, 'text-anchor': 'end', 'font-family': 'sans-serif'}, value));
    }
    let line = 'M 34 ' + y(0), value = 0;
    for (const event of events) {line += ' H ' + x(event.at.getTime()) + ' V ' + y(++value);}
    line += ' H ' + x(end);
    const area = line + ' L ' + x(end) + ' ' + y(0) + ' H 34 Z';
    svg.append(svgNode('path', {d: area, fill: '#8be1c10d'}), svgNode('path', {d: line, fill: 'none', stroke: '#8be1c1', 'stroke-width': 1.7, 'stroke-linejoin': 'round'}));
    svg.append(svgNode('circle', {cx: x(end), cy: y(value), r: 3, fill: '#8be1c1'}));
    for (let tick = 0; tick <= 3; tick++) {const at = start + (end - start) * tick / 3; svg.append(svgNode('text', {x: x(at), y: 169, fill: '#788c9a', 'font-size': 8, 'text-anchor': tick === 0 ? 'start' : tick === 3 ? 'end' : 'middle', 'font-family': 'sans-serif'}, timeFormat.format(new Date(at))));}
    container.append(svg);
    container.setAttribute('aria-label', 'Recuperación provisional: ' + events.length + ' módulos con hitos de merge y CI, desde ' + timeLabel(data.clock.t0) + ' hasta ' + timeLabel(last.toISOString()) + '. No es aceptación tras la ventana de calidad.');
  }

  function renderTimeline(data) {
    const list = byId('timeline'); clear(list);
    const source = Array.isArray(data.timeline) ? data.timeline : [];
    const sorted = [...source].filter((item) => date(item.at)).sort((a, b) => date(a.at) - date(b.at));
    const shown = sorted.length > 5 ? [sorted[0], ...sorted.slice(-4)] : sorted;
    if (!shown.length) {const item = node('li'); item.append(node('span', 'timeline-point amber'), node('div', 'timeline-copy', 'Sin hitos disponibles')); list.append(item);}
    for (const event of shown) {
      const item = node('li'), point = node('span', 'timeline-point ' + (event.status === 'failed' ? 'coral' : event.status === 'pending' ? 'amber' : ''));
      point.setAttribute('aria-hidden', 'true');
      const copy = node('div', 'timeline-copy'); copy.append(node('strong', '', event.label || event.kind || 'Hito registrado'));
      if (event.task_id || event.kind) copy.append(node('p', '', [event.task_id, event.kind].filter(Boolean).join(' · ')));
      const timestamp = node('time', '', timeLabel(event.at)); timestamp.dateTime = date(event.at).toISOString();
      item.append(point, copy, timestamp); list.append(item);
    }
    const elapsed = secondsLabel(data.clock?.elapsed_seconds);
    setText('clock-note', 'Desde la liberación: ' + elapsed + '. ' + (data.clock?.t1 ? 'Cierre: ' + timeLabel(data.clock.t1) + '.' : 'El reloj sigue abierto.') + ' Revisión, reparaciones y CI incluidas. ' + (sorted.length > 5 ? sorted.length + ' hitos en el JSON exportable.' : ''));
  }

  function renderTable() {
    const filter = byId('task-filter').value, query = byId('task-search').value.trim().toLocaleLowerCase('es');
    const filtered = tasks().filter((task) => {
      const searchable = [task.id, task.title, task.delivery?.pr_url, task.queue_status, task.review?.reviewer, task.attribution].filter(Boolean).join(' ').toLocaleLowerCase('es');
      return (!query || searchable.includes(query)) && (filter !== 'recovered' || task.provisional) && (filter !== 'pending' || !task.provisional) && (filter !== 'empty' || task.empty_patch) && (filter !== 'support' || task.feature_candidate === false);
    });
    const body = byId('task-rows'); clear(body);
    if (!filtered.length) {const row = node('tr', 'empty-table'), cell = node('td', '', 'No hay tareas que coincidan con este filtro.'); cell.colSpan = 6; row.append(cell); body.append(row);}
    for (const task of filtered) {
      const row = node('tr');
      const taskCell = node('td'), taskButton = node('button', 'task-name-button'); taskButton.type = 'button';
      taskButton.append(node('span', 'mono', task.id || 'Sin ID'));
      const taskCopy = node('span'); taskCopy.append(node('span', 'task-title', task.title || (task.feature_candidate === false ? 'Soporte del piloto' : 'Módulo de producto')), node('span', 'task-kind', task.empty_patch ? 'Sin implementación recuperada' : 'Parche original conservado'));
      taskButton.append(taskCopy); taskButton.addEventListener('click', () => openDetail(task.id)); taskCell.append(taskButton);
      const original = node('td'); original.append(pill(originalLabel(task), originalType(task)));
      const recovery = node('td'); recovery.append(pill(recoveredLabel(task), task.provisional ? 'mint-pill' : task.empty_patch ? 'neutral' : 'amber-pill'));
      const pr = node('td');
      if (safeURL(task.delivery?.pr_url)) {const path = new URL(task.delivery.pr_url).pathname; pr.append(link('#' + path.split('/').pop(), task.delivery.pr_url));} else pr.append(node('span', 'subtle', '—'));
      const ci = node('td');
      if (task.provisional && date(task.delivery?.green_at)) ci.append(node('span', 'ci-good', 'Verificado'));
      else ci.append(node('span', 'subtle', task.empty_patch ? 'Sin candidato' : 'Pendiente'));
      const detail = node('td'), open = node('button', 'detail-open', '↗'); open.type = 'button'; open.setAttribute('aria-label', 'Ver detalle de ' + task.id); open.addEventListener('click', () => openDetail(task.id)); detail.append(open);
      row.append(taskCell, original, recovery, pr, ci, detail); body.append(row);
    }
    setText('table-count', number(filtered.length) + ' / ' + number(tasks().length) + ' tareas');
    setText('table-summary', 'Producto: ' + number(state.snapshot?.summary?.provisional_product) + ' provisional · soporte: ' + number(state.snapshot?.summary?.provisional_support) + '.');
  }

  function renderBudget(data) {
    const budget = data.financials || {};
    setText('budget-reserved', money(budget.admitted_new_usd));
    setText('budget-authorized', money(budget.authorized_new_usd));
    setText('budget-reserved-note', 'Reservas nuevas de setup y cohorte, si están incluidas en la fuente. No son cargos ni crédito disponible.');
    const fill = finite(budget.admitted_new_usd) && finite(budget.authorized_new_usd) && budget.authorized_new_usd > 0 ? Math.min(100, Math.max(0, budget.admitted_new_usd / budget.authorized_new_usd * 100)) : 0;
    byId('budget-fill').value = fill;
    setText('budget-ceiling', money(budget.model_exposure_ceiling_usd));
    setText('budget-ceiling-note', 'Comprometido conservador: ' + money(budget.estimated_model_committed_usd) + ' · incierto retenido: ' + money(budget.model_uncertain_retained_usd) + '. No es factura.');
    setText('budget-ceiling-source', budget.source_observed_at ? 'Captura: ' + timeLabel(budget.source_observed_at) : 'Hora de fuente sin dato');
    setText('budget-invoice', finite(budget.actual_billed_total_usd) ? money(budget.actual_billed_total_usd) : 'Pendiente');
    setText('budget-invoice-note', finite(budget.actual_billed_total_usd) ? 'Costo reportado en la fuente; la elegibilidad y aceptación son controles separados.' : 'Sin costo real atribuible conciliado. Incluye límites pendientes de Codex local, servicios compartidos y CI.');
    setText('metric-per-dollar', 'Sin dato');
  }

  function renderEvidence(data) {
    const target = byId('evidence-links'); clear(target);
    for (const [label, href] of [['Informe público', data.evidence?.report_url], ['Seguimiento en GitHub', data.evidence?.tracking_url]]) {
      if (safeURL(href)) {const anchor = link(label, href, ''); anchor.append(node('span', '', '↗')); target.append(anchor);}
    }
    if (!target.childElementCount) target.append(node('span', 'subtle', 'Enlaces de evidencia no disponibles.'));
  }

  function facts(parent, title, pairs) {
    const section = node('section', 'detail-section'); section.append(node('h3', '', title));
    const list = node('dl', 'detail-facts');
    for (const [name, value, hash] of pairs) {const detail = node('dd', hash ? 'hash' : '', value === null || value === undefined || value === '' ? 'Sin dato' : String(value)); list.append(node('dt', '', name), detail);}
    section.append(list); parent.append(section);
  }

  function renderDetail(task) {
    setText('detail-title', task.id || 'Detalle de tarea');
    const target = byId('detail-content'); clear(target);
    if (task.title) target.append(node('p', '', task.title));
    target.append(node('p', '', task.feature_candidate === false ? 'Tarea de soporte. Se cuenta aparte de las entregas de producto.' : 'Módulo de producto. Esta clasificación describe la tarea, no demuestra que esté entregada.'));
    facts(target, 'Resultado del worker original', [['Estado', originalLabel(task)], ['Salida del agente', task.agent_exit], ['Salida del helper', task.check_exit], ['Parche', task.empty_patch ? 'Vacío · sin implementación' : 'No vacío · requiere aceptación']]);
    facts(target, 'Recuperación independiente', [['Calificación', recoveredLabel(task)], ['Cola', task.queue_status], ['Atribución', task.attribution === 'coordinator_code_or_test_repair' ? 'Coordinador: reparación de código o tests' : task.attribution === 'trusted_local_unchanged_failed_worker_recovery' ? 'Bytes originales recuperados sin cambios' : 'Sin atribución'], ['Revisor', task.review?.reviewer], ['Revisión iniciada', dateLabel(task.review?.started_at)], ['Revisión terminada', dateLabel(task.review?.finished_at)], ['Duración registrada', secondsLabel(task.review?.duration_seconds)]]);
    facts(target, 'Integración y calidad', [['Merge', dateLabel(task.delivery?.merged_at)], ['CI combinado verde', dateLabel(task.delivery?.green_at)], ['Fin previsto de ventana', dateLabel(task.delivery?.quality_until)], ['Commit revisado', task.delivery?.head_sha, true], ['Commit de merge', task.delivery?.merge_sha, true]]);
    const urls = node('section', 'detail-section'); urls.append(node('h3', '', 'Enlaces a la evidencia')); const links = node('div', 'detail-link-list');
    for (const [label, href] of [['Issue original ↗', task.issue_url], ['Pull request ↗', task.delivery?.pr_url], ['Checks del head revisado ↗', task.delivery?.head_checks_url], ['Checks del árbol combinado ↗', task.delivery?.combined_checks_url]]) if (safeURL(href)) links.append(link(label, href, ''));
    if (!links.childElementCount) links.append(node('p', 'subtle', 'Sin enlaces verificables disponibles.'));
    urls.append(links); target.append(urls);
    facts(target, 'SHA-256 de trazabilidad', [['Parche original', task.hashes?.original_patch, true], ['Parche revisado', task.hashes?.reviewed_patch, true], ['Recibo de revisión', task.hashes?.review_receipt, true], ['Delta del coordinador', task.hashes?.coordinator_delta, true]]);
    target.append(node('p', 'detail-warning', 'Una recuperación no cambia el estado fallido del worker original. La aceptación de calidad, el costo real y la comparación AFK siguen siendo mediciones separadas. Una salida −1 o 137 no prueba por sí sola la causa de terminación.'));
  }

  function openDetail(id) {
    const task = tasks().find((item) => item.id === id); if (!task) return;
    state.selected = id; renderDetail(task);
    const dialog = byId('task-dialog'); if (!dialog.open) dialog.showModal(); byId('detail-title').focus();
  }

  function render(data) {
    renderOverview(data); renderGrid(data); renderChart(data); renderTimeline(data); renderTable(); renderBudget(data); renderEvidence(data);
    if (state.selected && byId('task-dialog').open) {const selected = tasks().find((item) => item.id === state.selected); if (selected) renderDetail(selected);}
    byId('export-button').disabled = false;
  }

  function showError(message) {
    state.fetchFailed = true;
    const alert = byId('data-alert'); alert.hidden = false; alert.textContent = message + (state.snapshot ? ' Se conserva el último snapshot recibido; no representa un estado en vivo.' : ' No se muestran datos de ejemplo.');
    const connection = byId('connection-status'); clear(connection); connection.append(node('span', 'status-dot amber'), document.createTextNode('Fuente no disponible'));
    if (!state.snapshot) {setText('run-phase', 'Sin snapshot'); setText('run-name', 'Esperando una fuente válida');}
    announce('No se pudo actualizar la fuente.');
  }

  async function refresh() {
    if (state.fetching) return;
    state.fetching = true; byId('refresh-button').disabled = true;
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/snapshot', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('HTTP');
      const data = await response.json();
      if (data?.schema_version !== 1 || data.available !== true || !Array.isArray(data.tasks) || !data.summary) throw new Error('SNAPSHOT');
      state.snapshot = data; state.fetchFailed = false; render(data);
      setText('last-fetch', 'Lectura: ' + timeLabel(new Date().toISOString()));
      announce('Snapshot actualizado: ' + number(data.summary.provisional_product) + ' módulos provisionales.');
    } catch {showError('No se pudo leer el snapshot saneado del servidor local.');}
    finally {clearTimeout(timeout); state.fetching = false; byId('refresh-button').disabled = false;}
  }

  byId('refresh-button').addEventListener('click', refresh);
  byId('task-filter').addEventListener('change', renderTable);
  byId('task-search').addEventListener('input', renderTable);
  byId('close-detail').addEventListener('click', () => byId('task-dialog').close());
  byId('task-dialog').addEventListener('close', () => {state.selected = null;});
  byId('task-dialog').addEventListener('click', (event) => {const dialog = byId('task-dialog'); if (event.target === dialog && event.offsetX < 0) dialog.close();});
  byId('autorefresh-button').addEventListener('click', () => {
    state.autoRefresh = !state.autoRefresh; const button = byId('autorefresh-button'); button.setAttribute('aria-pressed', String(state.autoRefresh)); clear(button); button.append(node('span', 'status-dot ' + (state.autoRefresh ? 'mint' : 'amber')), document.createTextNode(state.autoRefresh ? 'Autorrefresco · 10 s' : 'Autorrefresco pausado')); announce(state.autoRefresh ? 'Autorrefresco activado.' : 'Autorrefresco pausado.');
  });
  byId('export-button').addEventListener('click', () => {
    if (!state.snapshot) return;
    const blob = new Blob([JSON.stringify(state.snapshot, null, 2) + '\n'], {type: 'application/json'}), url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'swarm-snapshot-' + String(state.snapshot.run_id || 'local').replace(/[^A-Za-z0-9_-]/g, '').slice(0,90) + '.json'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); announce('Snapshot saneado exportado.');
  });
  for (const anchor of document.querySelectorAll('.nav-item')) anchor.addEventListener('click', () => {for (const item of document.querySelectorAll('.nav-item')) {item.classList.toggle('is-active', item === anchor); if (item === anchor) item.setAttribute('aria-current', 'location'); else item.removeAttribute('aria-current');}});
  document.querySelector('.nav-item')?.setAttribute('aria-current', 'location');
  window.setInterval(() => {if (state.autoRefresh && !document.hidden) refresh();}, 10000);
  document.addEventListener('visibilitychange', () => {if (!document.hidden && state.autoRefresh) refresh();});
  refresh();
})();
