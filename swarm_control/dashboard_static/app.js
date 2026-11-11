/* Local, read-only evidence dashboard. All displayed run data comes from the API. */
'use strict';

(() => {
  const byId = (id) => document.getElementById(id);
  const state = {snapshot: null, autoRefresh: true, fetching: false, selected: null, fetchFailed: false, catalog: null, catalogLoading: false, projectId: null, runId: null, requestVersion: 0, controller: null};
  const countFormat = new Intl.NumberFormat("en-US");
  const moneyFormat = new Intl.NumberFormat("en-US", {style: 'currency', currency: 'USD', maximumFractionDigits: 2});
  const dateFormat = new Intl.DateTimeFormat("en-US", {dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC'});
  const timeFormat = new Intl.DateTimeFormat("en-US", {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC'});
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const number = (value) => finite(value) ? countFormat.format(value) : '—';
  const money = (value) => finite(value) ? moneyFormat.format(value) : "No data";
  const date = (value) => {const parsed = typeof value === 'string' ? new Date(value) : null; return parsed && Number.isFinite(parsed.getTime()) ? parsed : null;};
  const timeLabel = (value) => {const parsed = date(value); return parsed ? timeFormat.format(parsed) + ' UTC' : "No time";};
  const dateLabel = (value) => {const parsed = date(value); return parsed ? dateFormat.format(parsed) + ' UTC' : "No data";};
  const setText = (id, value) => {byId(id).textContent = String(value);};
  const node = (tag, className, text) => {const result = document.createElement(tag); if (className) result.className = className; if (text !== undefined) result.textContent = String(text); return result;};
  const clear = (element) => element.replaceChildren();
  const safeURL = (value) => {
    if (typeof value !== 'string') return null;
    try {const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && url.hostname === 'github.com' ? url.href : null;} catch {return null;}
  };
  const link = (label, href, className = 'table-link') => {
    const url = safeURL(href);
    if (!url) return node('span', 'subtle', "No link");
    const anchor = node('a', className, label); anchor.href = url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; return anchor;
  };
  const pill = (text, type = 'neutral') => node('span', 'pill ' + type, text);
  const tasks = () => Array.isArray(state.snapshot?.tasks) ? state.snapshot.tasks : [];
  const phaseLabels = {integration: "Integration in progress", complete: "Cohort closed", window_closed: "Delivery window closed", paused: "Integration paused", unavailable: "No snapshot"};
  const originalLabel = (task) => ({needs_repair: "Needs repair", validated: "Validated", working: "Running", preparing: "Preparing", checking: "Checking", failed: "Failed", rejected: "Rejected", needs_recovery: "Needs recovery"}[task.original_status] || "No status");
  const repaired = (task) => task.attribution === 'coordinator_code_or_test_repair';
  const recoveredLabel = (task) => task.provisional ? (repaired(task) ? "Coordinator repair" : "Unchanged recovery") : task.empty_patch ? "Empty patch" : "Not qualified";
  const originalType = (task) => task.original_status === 'validated' ? 'mint-pill' : ['needs_repair', 'failed', 'rejected', 'needs_recovery'].includes(task.original_status) ? 'coral-pill' : 'neutral';
  const secondsLabel = (value) => {if (!finite(value)) return "No data"; if (value < 60) return value.toFixed(1) + ' s'; const hours = Math.floor(value / 3600); const minutes = Math.floor(value % 3600 / 60); return hours ? hours + ' h ' + minutes + ' min' : minutes + ' min';};

  function announce(message) {setText('screen-reader-status', message);}

  function runContent(visible) {
    for (const element of document.querySelectorAll('[data-run-content]')) element.hidden = !visible;
    byId('workspace-empty').hidden = visible;
  }

  function resetSelection(title, copy) {
    state.controller?.abort(); state.requestVersion += 1; state.fetching = false;
    state.snapshot = null; state.selected = null; state.fetchFailed = false;
    byId('task-dialog').close(); byId('export-button').disabled = true;
    for (const id of ['worker-grid', 'task-rows', 'recovery-chart', 'timeline', 'evidence-links', 'recovery-split', 'detail-content']) clear(byId(id));
    for (const id of ['metric-workers', 'metric-validated', 'metric-recovered', 'metric-active', 'budget-reserved', 'budget-authorized', 'budget-ceiling', 'budget-invoice', 'metric-per-dollar']) setText(id, '—');
    byId('refresh-button').disabled = false; byId('data-alert').hidden = true;
    setText('workspace-empty-title', title); setText('workspace-empty-copy', copy);
    setText('run-name', state.runId || 'No run selected'); setText('run-phase', 'No snapshot');
    byId('run-phase').className = 'pill neutral';
    clear(byId('connection-status')); byId('connection-status').append(node('span', 'status-dot amber'), document.createTextNode(state.runId ? 'Loading source' : 'No run evidence'));
    setText('snapshot-time', 'Source pending'); setText('nav-worker-count', '—');
    setText('last-fetch', 'Not read yet'); runContent(false);
  }

  function persistSelection() {
    const url = new URL(window.location.href);
    for (const [key, value] of [['project', state.projectId], ['run', state.runId]]) {
      if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
    window.history.replaceState(null, '', url);
  }

  function selectOptions(element, rows, placeholder) {
    clear(element);
    if (!rows.length) element.append(node('option', '', placeholder));
    for (const row of rows) {const option = node('option', '', row.label || row.id); option.value = row.id; element.append(option);}
    element.disabled = rows.length === 0;
  }

  function chooseProject(id, preferredRun) {
    const project = state.catalog?.projects.find((item) => item.id === id);
    if (!project) return;
    state.projectId = project.id;
    state.runId = project.runs.some((run) => run.id === preferredRun) ? preferredRun : project.runs[0]?.id || null;
    byId('project-select').value = state.projectId;
    selectOptions(byId('run-select'), project.runs, 'No runs yet');
    if (state.runId) byId('run-select').value = state.runId;
    setText('repo-name', project.repository || project.label || project.id);
    setText('workspace-summary', number(state.catalog.projects.length) + ' configured project(s) · ' + number(project.runs.length) + ' recorded run(s) in this project. This viewer is read only.');
    persistSelection();
    resetSelection(state.runId ? 'Loading selected run…' : 'No run evidence yet', state.runId ? 'Reading the reviewed evidence for this selection.' : 'This project has no recorded runs in the local catalog. Run agents through the controller, then load their reviewed evidence here.');
    if (state.runId) refresh();
  }

  async function loadCatalog() {
    if (state.catalogLoading) return;
    state.catalogLoading = true;
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/catalog', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('CATALOG');
      const data = await response.json(), validId = (id) => typeof id === 'string' && id.length > 0;
      if (!Array.isArray(data?.projects) || data.projects.some((project) => !validId(project.id) || !Array.isArray(project.runs) || project.runs.some((run) => !validId(run.id)) || new Set(project.runs.map((run) => run.id)).size !== project.runs.length) || new Set(data.projects.map((project) => project.id)).size !== data.projects.length) throw new Error('CATALOG');
      state.catalog = data;
      selectOptions(byId('project-select'), data.projects, 'No configured projects');
      const query = new URL(window.location.href).searchParams;
      const wanted = state.projectId || query.get('project') || data.default_project_id;
      const project = data.projects.find((item) => item.id === wanted) || data.projects.find((item) => item.id === data.default_project_id) || data.projects[0];
      if (project) chooseProject(project.id, state.runId || query.get('run') || data.default_run_id);
      else {state.projectId = null; state.runId = null; persistSelection(); selectOptions(byId('run-select'), [], 'No runs yet'); resetSelection('No configured projects', 'Add your projects to the local server catalog to inspect their reviewed run evidence. No example projects or results are displayed.'); setText('workspace-summary', 'No projects are configured on this local server.');}
    } catch {resetSelection('Workspace unavailable', 'The local server did not provide a valid project catalog. Refresh to try reading it again. No example projects or results are displayed.'); showError('Could not read the local project catalog.');}
    finally {clearTimeout(timeout); state.catalogLoading = false;}
  }

  function renderOverview(data) {
    const summary = data.summary || {};
    setText('repo-name', data.repository || "Unidentified repository");
    setText('run-name', data.run_id || "No run identifier");
    setText('run-phase', phaseLabels[data.phase] || "Unknown status");
    byId('run-phase').className = 'pill ' + (data.phase === 'integration' ? 'amber-pill' : data.phase === 'complete' ? 'mint-pill' : 'neutral');
    setText('snapshot-time', "Source: " + dateLabel(data.source_as_of));
    setText('nav-worker-count', number(summary.assigned));
    setText('metric-workers', number(summary.assigned));
    setText('metric-workers-note', number(summary.original_needs_repair) + " originals need repair");
    setText('metric-validated', number(summary.original_validated));
    setText('metric-validated-note', "Original validation · outcomes preserved");
    setText('metric-recovered', number(summary.provisional_product));
    setText('metric-recovered-note', number(summary.provisional_support) + " supporting · quality still provisional");
    setText('metric-active', number(summary.active_workers));
    setText('metric-active-note', summary.active_workers === null ? "Inventory unavailable" : "Latest inventory · not continuous observation");
    setText('workers-title', (finite(summary.assigned) ? number(summary.assigned) + ' workers.' : "A worker cohort.") + " The complete picture.");
    setText('truth-note', "Original outcomes and recovery are separate stages. VMs and command events prove neither 100 simultaneous useful agents nor an AFK efficiency win.");
    const connection = byId('connection-status'); clear(connection);
    connection.append(node('span', 'status-dot ' + (data.stale ? 'amber' : 'mint')), document.createTextNode(data.stale ? "Source is stale" : "Snapshot available"));
    const alert = byId('data-alert');
    alert.hidden = !data.stale;
    if (data.stale) alert.textContent = "The source is marked stale. These data describe its capture time, not the current provider state.";
  }

  function renderGrid(data) {
    const grid = byId('worker-grid'); clear(grid);
    for (const task of tasks()) {
      const tile = node('button', 'worker-tile' + (['needs_repair', 'failed', 'rejected', 'needs_recovery'].includes(task.original_status) ? ' is-failed' : '') + (task.provisional ? ' is-recovered' : '') + (task.feature_candidate === false ? ' is-support' : ''), String(task.id || '').replace(/^B/, ''));
      tile.type = 'button';
      tile.setAttribute('aria-label', String(task.id || "Task") + ': original ' + originalLabel(task) + '; ' + recoveredLabel(task) + (task.feature_candidate === false ? "; support" : ''));
      tile.title = String(task.id || '') + (task.title ? ' · ' + task.title : '') + ' · ' + originalLabel(task) + ' · ' + recoveredLabel(task);
      if (task.provisional) {const marker = node('span', 'recovery-marker'); marker.setAttribute('aria-hidden', 'true'); tile.append(marker);}
      tile.addEventListener('click', () => openDetail(task.id)); grid.append(tile);
    }
    const summary = data.summary || {};
    setText('worker-summary', number(summary.original_needs_repair) + " originals need repair · " + number(summary.empty_patches) + " empty patches");
  }

  function renderChart(data) {
    const container = byId('recovery-chart'); clear(container);
    const summary = data.summary || {};
    setText('chart-total', number(summary.provisional_product) + " provisional");
    const split = byId('recovery-split'); clear(split);
    const productTasks = tasks().filter((task) => task.provisional && task.feature_candidate !== false);
    const repairedProducts = productTasks.filter(repaired).length;
    const unchangedProducts = productTasks.length - repairedProducts;
    for (const [value, label] of [[unchangedProducts, "unchanged product"], [repairedProducts, "repaired product"], [summary.provisional_support, "support separately"]]) {
      const item = node('span'); item.append(node('strong', '', number(value)), document.createTextNode(' ' + label)); split.append(item);
    }
    const t0 = date(data.clock?.t0);
    const events = tasks().filter((task) => task.provisional && task.feature_candidate !== false).map((task) => {
      const green = date(task.delivery?.green_at), merged = date(task.delivery?.merged_at);
      return {at: green && merged ? new Date(Math.max(green.getTime(), merged.getTime())) : green || merged, id: task.id};
    }).filter((event) => event.at).sort((a, b) => a.at - b.at);
    if (!t0 || !events.length) {
      container.append(node('div', 'chart-empty', "No verified product milestones to chart."));
      container.setAttribute('aria-label', "No verified product milestones to chart."); return;
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
    container.setAttribute('aria-label', "Provisional recovery: " + events.length + " modules with merge and CI milestones, from " + timeLabel(data.clock.t0) + " to " + timeLabel(last.toISOString()) + ". This is not acceptance after the quality window.");
  }

  function renderTimeline(data) {
    const list = byId('timeline'); clear(list);
    const source = Array.isArray(data.timeline) ? data.timeline : [];
    const sorted = [...source].filter((item) => date(item.at)).sort((a, b) => date(a.at) - date(b.at));
    const shown = sorted.length > 5 ? [sorted[0], ...sorted.slice(-4)] : sorted;
    if (!shown.length) {const item = node('li'); item.append(node('span', 'timeline-point amber'), node('div', 'timeline-copy', "No milestones available")); list.append(item);}
    for (const event of shown) {
      const item = node('li'), point = node('span', 'timeline-point ' + (event.status === 'failed' ? 'coral' : event.status === 'pending' ? 'amber' : ''));
      point.setAttribute('aria-hidden', 'true');
      const copy = node('div', 'timeline-copy'); copy.append(node('strong', '', event.label || event.kind || "Recorded milestone"));
      if (event.task_id || event.kind) copy.append(node('p', '', [event.task_id, event.kind].filter(Boolean).join(' · ')));
      const timestamp = node('time', '', timeLabel(event.at)); timestamp.dateTime = date(event.at).toISOString();
      item.append(point, copy, timestamp); list.append(item);
    }
    const elapsed = secondsLabel(data.clock?.elapsed_seconds);
    setText('clock-note', "Since release: " + elapsed + '. ' + (data.phase === 'window_closed' ? "The delivery window ended before integration completed; final T1 remains unknown." : data.clock?.t1 ? "Closed: " + timeLabel(data.clock.t1) + '.' : "The clock remains open.") + " Review, repairs and CI included. " + (sorted.length > 5 ? sorted.length + " milestones in the exported JSON." : ''));
  }

  function renderTable() {
    const filter = byId('task-filter').value, query = byId('task-search').value.trim().toLocaleLowerCase("en-US");
    const filtered = tasks().filter((task) => {
      const searchable = [task.id, task.title, task.delivery?.pr_url, task.queue_status, task.review?.reviewer, task.attribution].filter(Boolean).join(' ').toLocaleLowerCase("en-US");
      return (!query || searchable.includes(query)) && (filter !== 'recovered' || task.provisional) && (filter !== 'pending' || !task.provisional) && (filter !== 'empty' || task.empty_patch) && (filter !== 'support' || task.feature_candidate === false);
    });
    const body = byId('task-rows'); clear(body);
    if (!filtered.length) {const row = node('tr', 'empty-table'), cell = node('td', '', "No tasks match this filter."); cell.colSpan = 6; row.append(cell); body.append(row);}
    for (const task of filtered) {
      const row = node('tr');
      const taskCell = node('td'), taskButton = node('button', 'task-name-button'); taskButton.type = 'button';
      taskButton.append(node('span', 'mono', task.id || "No ID"));
      const taskCopy = node('span'); taskCopy.append(node('span', 'task-title', task.title || (task.feature_candidate === false ? "Pilot support" : "Product module")), node('span', 'task-kind', task.empty_patch ? "No recovered implementation" : "Original patch preserved"));
      taskButton.append(taskCopy); taskButton.addEventListener('click', () => openDetail(task.id)); taskCell.append(taskButton);
      const original = node('td'); original.append(pill(originalLabel(task), originalType(task)));
      const recovery = node('td'); recovery.append(pill(recoveredLabel(task), task.provisional ? 'mint-pill' : task.empty_patch ? 'neutral' : 'amber-pill'));
      const pr = node('td');
      if (safeURL(task.delivery?.pr_url)) {const path = new URL(task.delivery.pr_url).pathname; pr.append(link('#' + path.split('/').pop(), task.delivery.pr_url));} else pr.append(node('span', 'subtle', '—'));
      const ci = node('td');
      if (task.provisional && date(task.delivery?.green_at)) ci.append(node('span', 'ci-good', "Verified"));
      else ci.append(node('span', 'subtle', task.empty_patch ? "No candidate" : "Pending"));
      const detail = node('td'), open = node('button', 'detail-open', '↗'); open.type = 'button'; open.setAttribute('aria-label', "View details for " + task.id); open.addEventListener('click', () => openDetail(task.id)); detail.append(open);
      row.append(taskCell, original, recovery, pr, ci, detail); body.append(row);
    }
    setText('table-count', number(filtered.length) + ' / ' + number(tasks().length) + " tasks");
    setText('table-summary', "Product: " + number(state.snapshot?.summary?.provisional_product) + " provisional · support: " + number(state.snapshot?.summary?.provisional_support) + '.');
  }

  function renderBudget(data) {
    const budget = data.financials || {};
    setText('budget-reserved', money(budget.admitted_new_usd));
    setText('budget-authorized', money(budget.authorized_new_usd));
    setText('budget-reserved-note', "New setup and cohort reserves, where included in the source. These are neither charges nor available credit.");
    const fill = finite(budget.admitted_new_usd) && finite(budget.authorized_new_usd) && budget.authorized_new_usd > 0 ? Math.min(100, Math.max(0, budget.admitted_new_usd / budget.authorized_new_usd * 100)) : 0;
    byId('budget-fill').value = fill;
    setText('budget-ceiling', money(budget.model_exposure_ceiling_usd));
    setText('budget-ceiling-note', "Conservative committed: " + money(budget.estimated_model_committed_usd) + " · uncertain retained: " + money(budget.model_uncertain_retained_usd) + ". Not an invoice.");
    setText('budget-ceiling-source', budget.source_observed_at ? "Captured: " + timeLabel(budget.source_observed_at) : "Source time unavailable");
    setText('budget-invoice', finite(budget.actual_billed_total_usd) ? money(budget.actual_billed_total_usd) : "Pending");
    setText('budget-invoice-note', finite(budget.actual_billed_total_usd) ? "Source-reported cost; eligibility and acceptance are separate checks." : "No reconciled attributable cost. Local Codex, shared services and CI remain unassigned.");
    setText('metric-per-dollar', "No data");
  }

  function renderEvidence(data) {
    const target = byId('evidence-links'); clear(target);
    for (const [label, href] of [["Public report", data.evidence?.report_url], ["GitHub tracker", data.evidence?.tracking_url]]) {
      if (safeURL(href)) {const anchor = link(label, href, ''); anchor.append(node('span', '', '↗')); target.append(anchor);}
    }
    if (!target.childElementCount) target.append(node('span', 'subtle', "Evidence links unavailable."));
  }

  function facts(parent, title, pairs) {
    const section = node('section', 'detail-section'); section.append(node('h3', '', title));
    const list = node('dl', 'detail-facts');
    for (const [name, value, hash] of pairs) {const detail = node('dd', hash ? 'hash' : '', value === null || value === undefined || value === '' ? "No data" : String(value)); list.append(node('dt', '', name), detail);}
    section.append(list); parent.append(section);
  }

  function renderDetail(task) {
    setText('detail-title', task.id || "Task details");
    const target = byId('detail-content'); clear(target);
    if (task.title) target.append(node('p', '', task.title));
    target.append(node('p', '', task.feature_candidate === false ? "Supporting task. Counted separately from product deliveries." : "Product module. This classifies the task; it does not prove delivery."));
    facts(target, "Original worker outcome", [["Status", originalLabel(task)], ["Agent exit", task.agent_exit], ["Helper exit", task.check_exit], ["Patch", task.empty_patch ? "Empty · no implementation" : "Nonempty · acceptance required"]]);
    facts(target, "Independent recovery", [["Qualification", recoveredLabel(task)], ["Queue", task.queue_status], ["Attribution", task.attribution === 'coordinator_code_or_test_repair' ? "Coordinator: code or test repair" : task.attribution === 'trusted_local_unchanged_failed_worker_recovery' ? "Original worker bytes recovered unchanged" : "No attribution"], ["Reviewer", task.review?.reviewer], ["Review started", dateLabel(task.review?.started_at)], ["Review finished", dateLabel(task.review?.finished_at)], ["Recorded duration", secondsLabel(task.review?.duration_seconds)]]);
    facts(target, "Integration and quality", [['Merge', dateLabel(task.delivery?.merged_at)], ["Combined CI green", dateLabel(task.delivery?.green_at)], ["Expected quality-window end", dateLabel(task.delivery?.quality_until)], ["Reviewed commit", task.delivery?.head_sha, true], ["Merge commit", task.delivery?.merge_sha, true]]);
    const urls = node('section', 'detail-section'); urls.append(node('h3', '', "Evidence links")); const links = node('div', 'detail-link-list');
    for (const [label, href] of [["Original issue ↗", task.issue_url], ['Pull request ↗', task.delivery?.pr_url], ["Reviewed-head checks ↗", task.delivery?.head_checks_url], ["Combined-tree checks ↗", task.delivery?.combined_checks_url]]) if (safeURL(href)) links.append(link(label, href, ''));
    if (!links.childElementCount) links.append(node('p', 'subtle', "No verified links available."));
    urls.append(links); target.append(urls);
    facts(target, "Evidence SHA-256", [["Original patch", task.hashes?.original_patch, true], ["Reviewed patch", task.hashes?.reviewed_patch, true], ["Review receipt", task.hashes?.review_receipt, true], ["Coordinator delta", task.hashes?.coordinator_delta, true]]);
    target.append(node('p', 'detail-warning', "Recovery does not change the failed original worker outcome. Quality acceptance, actual cost and AFK comparison remain separate measurements. Exit −1 or 137 alone does not establish the termination cause."));
  }

  function openDetail(id) {
    const task = tasks().find((item) => item.id === id); if (!task) return;
    state.selected = id; renderDetail(task);
    const dialog = byId('task-dialog'); if (!dialog.open) dialog.showModal(); byId('detail-title').focus();
  }

  function render(data) {
    runContent(true);
    renderOverview(data); renderGrid(data); renderChart(data); renderTimeline(data); renderTable(); renderBudget(data); renderEvidence(data);
    if (state.selected && byId('task-dialog').open) {const selected = tasks().find((item) => item.id === state.selected); if (selected) renderDetail(selected);}
    byId('export-button').disabled = false;
  }

  function showError(message) {
    state.fetchFailed = true;
    const alert = byId('data-alert'); alert.hidden = false; alert.textContent = message + (state.snapshot ? " The last received snapshot is retained; it does not represent live state." : " No example data is displayed.");
    const connection = byId('connection-status'); clear(connection); connection.append(node('span', 'status-dot amber'), document.createTextNode("Source unavailable"));
    if (!state.snapshot) {setText('run-phase', "No snapshot"); setText('run-name', state.runId || "Waiting for a valid source"); if (state.runId) {setText('workspace-empty-title', 'Run evidence unavailable'); setText('workspace-empty-copy', 'The selected run could not be read. Refresh to try again, or choose another configured run. No previous project data is displayed.');}}
    announce("Could not refresh the source.");
  }

  async function refresh() {
    if (!state.projectId || !state.runId) {loadCatalog(); return;}
    if (state.fetching) return;
    state.fetching = true; byId('refresh-button').disabled = true;
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 15000), version = ++state.requestVersion;
    state.controller = controller;
    const query = new URLSearchParams({project: state.projectId, run: state.runId});
    try {
      const response = await fetch('/api/snapshot?' + query, {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('HTTP');
      const data = await response.json();
      if (version !== state.requestVersion) return;
      if (data?.schema_version !== 1 || data.available !== true || !Array.isArray(data.tasks) || !data.summary) throw new Error('SNAPSHOT');
      state.snapshot = data; state.fetchFailed = false; render(data);
      setText('last-fetch', "Read: " + timeLabel(new Date().toISOString()));
      announce("Snapshot updated: " + number(data.summary.provisional_product) + " provisional modules.");
    } catch {if (version === state.requestVersion) showError("Could not read the sanitized snapshot from the local server.");}
    finally {clearTimeout(timeout); if (version === state.requestVersion) {state.fetching = false; state.controller = null; byId('refresh-button').disabled = false;}}
  }

  byId('project-select').addEventListener('change', () => chooseProject(byId('project-select').value, null));
  byId('run-select').addEventListener('change', () => {
    const project = state.catalog?.projects.find((item) => item.id === state.projectId);
    if (!project?.runs.some((run) => run.id === byId('run-select').value)) return;
    state.runId = byId('run-select').value; persistSelection();
    resetSelection('Loading selected run…', 'Reading the reviewed evidence for this selection.'); refresh();
  });
  byId('refresh-button').addEventListener('click', refresh);
  byId('task-filter').addEventListener('change', renderTable);
  byId('task-search').addEventListener('input', renderTable);
  byId('close-detail').addEventListener('click', () => byId('task-dialog').close());
  byId('task-dialog').addEventListener('close', () => {state.selected = null;});
  byId('task-dialog').addEventListener('click', (event) => {const dialog = byId('task-dialog'); if (event.target === dialog && event.offsetX < 0) dialog.close();});
  byId('autorefresh-button').addEventListener('click', () => {
    state.autoRefresh = !state.autoRefresh; const button = byId('autorefresh-button'); button.setAttribute('aria-pressed', String(state.autoRefresh)); clear(button); button.append(node('span', 'status-dot ' + (state.autoRefresh ? 'mint' : 'amber')), document.createTextNode(state.autoRefresh ? "Auto-refresh · 10 s" : "Auto-refresh paused")); announce(state.autoRefresh ? "Auto-refresh enabled." : "Auto-refresh paused.");
  });
  byId('export-button').addEventListener('click', () => {
    if (!state.snapshot) return;
    const blob = new Blob([JSON.stringify(state.snapshot, null, 2) + '\n'], {type: 'application/json'}), url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'swarm-snapshot-' + String(state.snapshot.run_id || 'local').replace(/[^A-Za-z0-9_-]/g, '').slice(0,90) + '.json'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); announce("Sanitized snapshot exported.");
  });
  for (const anchor of document.querySelectorAll('.nav-item')) anchor.addEventListener('click', () => {for (const item of document.querySelectorAll('.nav-item')) {item.classList.toggle('is-active', item === anchor); if (item === anchor) item.setAttribute('aria-current', 'location'); else item.removeAttribute('aria-current');}});
  document.querySelector('.nav-item')?.setAttribute('aria-current', 'location');
  window.setInterval(() => {if (state.autoRefresh && !document.hidden) refresh();}, 10000);
  document.addEventListener('visibilitychange', () => {if (!document.hidden && state.autoRefresh) refresh();});
  loadCatalog();
})();
