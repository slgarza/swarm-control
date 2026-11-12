/* Local workspace and evidence dashboard. Launch requires a saved draft and explicit confirmation. */
'use strict';

(() => {
  const byId = (id) => document.getElementById(id);
  const state = {snapshot: null, autoRefresh: true, fetching: false, selected: null, fetchFailed: false, catalog: null, catalogLoading: false, catalogFlight: null, projectId: null, runId: null, requestVersion: 0, controller: null, workspace: false, context: null, contextVersion: 0, contextController: null, csrf: null, selectedTasks: new Set(), preview: null, previewFingerprint: null, planning: false, planVersion: 0, connecting: false, launchEnabled: false, runDetail: null, launchChecks: new Set(), launchRequests: new Set(), launchStates: new Map(), launchInputs: new Map()};
  const countFormat = new Intl.NumberFormat("en-US");
  const moneyFormat = new Intl.NumberFormat("en-US", {style: 'currency', currency: 'USD', maximumFractionDigits: 2});
  const dateFormat = new Intl.DateTimeFormat("en-US", {dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC'});
  const timeFormat = new Intl.DateTimeFormat("en-US", {hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC'});
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const number = (value) => finite(value) ? countFormat.format(value) : 'Unknown';
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
  const runStatus = (status) => ({preview: 'Plan preview', draft: 'Draft · not launched', launching: 'Launch requested', interrupted: 'Runner interrupted', runner_finished: 'Runner finished · review outcomes', historical: 'Recorded evidence'}[status] || 'Status unavailable');

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
    for (const id of ['metric-workers', 'metric-validated', 'metric-recovered', 'metric-active', 'budget-reserved', 'budget-authorized', 'budget-ceiling', 'budget-invoice', 'metric-per-dollar']) setText(id, 'Unknown');
    byId('refresh-button').disabled = false; byId('data-alert').hidden = true;
    setText('workspace-empty-title', title); setText('workspace-empty-copy', copy);
    setText('run-name', state.runId || 'No run selected'); setText('run-phase', 'No snapshot');
    byId('run-phase').className = 'pill neutral';
    clear(byId('connection-status')); byId('connection-status').append(node('span', 'status-dot amber'), document.createTextNode(state.runId ? 'Loading source' : 'No run evidence'));
    setText('snapshot-time', 'Source pending'); setText('nav-worker-count', 'Unknown');
    setText('last-fetch', 'Not read yet'); runContent(false);
    state.runDetail = null; byId('run-configuration').hidden = true; clear(byId('run-configuration-content')); delete byId('run-configuration-content').dataset.runKey; updateEasyRunGuide();
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
    setText('workspace-summary', workspaceSummary(project));
    persistSelection();
    resetSelection(state.runId ? 'Loading selected run…' : state.workspace ? 'Ready to prepare your first run' : 'No run evidence yet', state.runId ? 'Reading the reviewed evidence for this selection.' : state.workspace ? 'Click New run, select a ready task, keep Concurrent agents at 1 and click Check plan. Saving a draft gives you a separate run record.' : 'This project has no recorded runs in the local catalog. Run agents through the controller, then load their reviewed evidence here.');
    if (state.workspace) {resetBuilder(); renderHistory(project.runs); loadProjectContext(project.id);}
    if (state.runId) refresh();
  }

  async function loadCatalog(preserveForm = false, preferredProject = null) {
    if (state.catalogLoading) {await state.catalogFlight; return loadCatalog(preserveForm, preferredProject);}
    state.catalogLoading = true; let finishCatalog; state.catalogFlight = new Promise((resolve) => {finishCatalog = resolve;});
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/catalog', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('CATALOG');
      const data = await response.json(), validId = (id) => typeof id === 'string' && id.length > 0;
      if (!Array.isArray(data?.projects) || data.projects.some((project) => !validId(project.id) || !Array.isArray(project.runs) || project.runs.some((run) => !validId(run.id)) || new Set(project.runs.map((run) => run.id)).size !== project.runs.length) || new Set(data.projects.map((project) => project.id)).size !== data.projects.length) throw new Error('CATALOG');
      state.catalog = data;
      configureWorkspace(data.mode === 'workspace');
      selectOptions(byId('project-select'), data.projects, 'No configured projects');
      const query = new URL(window.location.href).searchParams;
      const wanted = preferredProject || state.projectId || query.get('project') || data.default_project_id;
      const project = data.projects.find((item) => item.id === wanted) || data.projects.find((item) => item.id === data.default_project_id) || data.projects[0];
      if (project && preserveForm && state.workspace && state.projectId === project.id) {
        byId('project-select').value = project.id;
        selectOptions(byId('run-select'), project.runs, 'No runs yet');
        if (!project.runs.some((run) => run.id === state.runId)) state.runId = null;
        if (state.runId) byId('run-select').value = state.runId;
        else addRunPlaceholder();
        setText('workspace-summary', workspaceSummary(project)); renderHistory(project.runs);
        if (state.runId) refresh();
      }
      else if (project) chooseProject(project.id, state.runId || query.get('run') || data.default_run_id);
      else {state.projectId = null; state.runId = null; persistSelection(); selectOptions(byId('run-select'), [], 'No runs yet'); resetSelection(state.workspace ? 'Connect your first repository' : 'No configured projects', state.workspace ? 'Use Connect repository above and paste the full local folder path. Your code stays in its own repository. You can also try the included local demo.' : 'Add your projects to the local server catalog to inspect their reviewed run evidence. No example projects or results are displayed.'); setText('workspace-summary', 'No projects are configured on this local server.'); if (state.workspace) toggleConnect(true, false);}
      return data;
    } catch {resetSelection('Workspace unavailable', 'The local server did not provide a valid project catalog. Refresh to try reading it again. No example projects or results are displayed.'); showError('Could not read the local project catalog.'); return null;}
    finally {clearTimeout(timeout); state.catalogLoading = false; finishCatalog();}
  }

  function workspaceSummary(project) {
    return number(state.catalog.projects.length) + ' configured repositor' + (state.catalog.projects.length === 1 ? 'y' : 'ies') + ' · ' + number(project.runs.length) + ' saved run(s).' + (state.workspace ? ' Prepare a draft or inspect earlier evidence.' : ' This viewer is read only.');
  }

  function addRunPlaceholder() {
    if (!byId('run-select').options.length || byId('run-select').disabled) return;
    const option = node('option', '', 'Select a saved run'); option.value = ''; option.disabled = true; option.selected = true; byId('run-select').prepend(option);
  }

  function configureWorkspace(enabled) {
    const changed = state.workspace !== enabled; state.workspace = enabled;
    for (const element of document.querySelectorAll('[data-workspace-only]')) element.hidden = !enabled;
    setText('local-mode-label', enabled ? 'Local workspace' : 'Read-only evidence');
    setText('workspace-intro-copy', enabled ? 'Choose a repository. Prepare a new run or open an earlier run. Saving a draft does not deploy agents.' : 'Choose a repository, then a run to inspect its reviewed results. Only repositories configured on this local server are listed.');
    if (!enabled) {byId('run-builder').hidden = true; byId('run-configuration').hidden = true; byId('connect-repository').hidden = true;} updateEasyRunGuide();
    if (enabled && (changed || !state.csrf)) loadSession();
  }

  async function loadSession() {
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/session', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('SESSION');
      const data = await response.json();
      if (data.mode !== 'workspace' || typeof data.csrf_token !== 'string' || !data.csrf_token) throw new Error('SESSION');
      state.csrf = data.csrf_token; state.launchEnabled = data.launch_enabled === true; byId('connect-submit').disabled = state.connecting; updateBuilder(); if (state.runDetail) renderRunConfiguration(state.runDetail);
    } catch {state.csrf = null; byId('connect-submit').disabled = true; connectionResult('Could not open a local session. Refresh the workspace to connect or save a draft.', true); builderError('Could not prepare a local planning session. Refresh the workspace before saving a draft.');}
    finally {clearTimeout(timeout);}
  }

  function builderError(message) {
    const box = byId('run-form-error'); box.hidden = !message; box.textContent = message || '';
  }

  function resetBuilder() {
    state.contextController?.abort(); state.contextVersion += 1; state.planVersion += 1;
    state.context = null; state.selectedTasks.clear(); state.preview = null; state.previewFingerprint = null; state.planning = false;
    byId('run-form').reset(); byId('run-options').disabled = true;
    byId('new-run-button').disabled = true; byId('new-run-button').setAttribute('aria-expanded', 'false');
    byId('run-builder').hidden = true; byId('run-preview').hidden = true; clear(byId('run-preview')); builderError('');
    clear(byId('run-task-options')); clear(byId('worker-presets')); byId('builder-unavailable').hidden = true;
    byId('preview-run-button').disabled = true; byId('save-run-button').disabled = true;
  }

  async function loadProjectContext(projectId) {
    const version = ++state.contextVersion, controller = new AbortController(); state.contextController = controller;
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/api/project?' + new URLSearchParams({project: projectId}), {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('PROJECT');
      const data = await response.json();
      if (version !== state.contextVersion || projectId !== state.projectId) return;
      if (data.project_id !== projectId || !Array.isArray(data.tasks)) throw new Error('PROJECT');
      state.context = data;
      if (!data.configurable) {
        setText('history-note', 'This repository has historical evidence only. Use Connect repository to add its project configuration and prepare new runs.');
        byId('new-run-button').disabled = true; return;
      }
      const cap = data.limits?.max_workers;
      if (!Number.isInteger(cap) || cap < 1 || cap > 100) throw new Error('CAPACITY');
      byId('new-run-button').disabled = false; byId('run-options').disabled = false;
      byId('run-workers').max = String(cap); byId('run-workers').value = String(data.defaults?.workers || 1);
      byId('run-timeout').value = String(data.defaults?.timeout || 300); byId('run-base').value = data.defaults?.base || 'HEAD';
      byId('run-synchronized').checked = data.defaults?.synchronized === true; byId('run-overlap').checked = data.defaults?.allow_overlap === true;
      byId('run-context').value = data.defaults?.context_mode || 'full';
      setText('worker-limit', 'Repository policy: up to ' + cap + ' agents');
      for (const count of [1, 2, 5, 10, 25, 50, 100]) {
        const button = node('button', '', String(count)); button.type = 'button'; button.disabled = count > cap; button.dataset.workers = String(count); button.setAttribute('aria-label', count + ' concurrent agent' + (count === 1 ? '' : 's')); button.setAttribute('aria-pressed', String(count === Number(byId('run-workers').value)));
        if (count > cap) button.title = 'Above this repository’s configured limit';
        button.addEventListener('click', () => {byId('run-workers').value = String(count); invalidatePreview(); updateBuilder();}); byId('worker-presets').append(button);
      }
      renderTaskChoices(); updateBuilder(); updateEasyRunGuide(); if (state.runDetail) updateLaunchStatus(state.runDetail);
      if (!state.runId) toggleBuilder(true, false);
    } catch (error) {
      if (version !== state.contextVersion || projectId !== state.projectId) return;
      byId('new-run-button').disabled = true;
      setText('history-note', 'Repository settings could not be read. Refresh the workspace or check the registered project configuration with the CLI.');
    }
    finally {clearTimeout(timeout);}
  }

  function toggleBuilder(open, focus = true) {
    if (open && !state.context?.configurable) return;
    byId('run-builder').hidden = !open; byId('new-run-button').setAttribute('aria-expanded', String(open));
    if (open && focus) {byId('run-builder').scrollIntoView({behavior: 'smooth', block: 'start'}); byId('run-workers').focus({preventScroll: true});}
  }

  function renderTaskChoices() {
    const target = byId('run-task-options'); clear(target);
    const query = byId('run-task-search').value.trim().toLocaleLowerCase('en-US');
    const available = (state.context?.tasks || []).filter((task) => [task.id, task.title].join(' ').toLocaleLowerCase('en-US').includes(query));
    if (!available.length) {target.append(node('p', 'task-picker-empty', query ? 'No tasks match this search.' : 'No tasks are registered in this repository. Prepare its backlog before creating a run.')); return;}
    for (const task of available) {
      const label = node('label', 'run-task-option'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.value = task.id; checkbox.checked = state.selectedTasks.has(task.id); checkbox.setAttribute('aria-label', 'Select ' + task.id + ': ' + task.title);
      checkbox.addEventListener('change', () => {if (checkbox.checked) state.selectedTasks.add(task.id); else state.selectedTasks.delete(task.id); invalidatePreview(); updateBuilder();});
      const copy = node('span', 'run-task-copy'), heading = node('span', 'run-task-heading'); heading.append(node('strong', 'mono', task.id), node('span', '', task.title));
      const metadata = []; if (task.dependencies?.length) metadata.push('Depends on: ' + task.dependencies.join(', ')); if (task.has_task_context) metadata.push('Task packet available');
      copy.append(heading); if (metadata.length) copy.append(node('span', 'run-task-metadata', metadata.join(' · ')));
      label.append(checkbox, copy, pill(task.status || 'Status unknown', task.status === 'ready' ? 'mint-pill' : 'neutral')); target.append(label);
    }
  }

  function readOptions() {
    const budget = byId('run-budget').value, workers = Number(byId('run-workers').value), required = byId('run-required-parallel').value, idle = byId('run-no-progress').value;
    return {task_ids: [...state.selectedTasks].sort(), workers, timeout: Number(byId('run-timeout').value), base: byId('run-base').value.trim(), model_budget: budget === '' ? null : Number(budget), synchronized: byId('run-synchronized').checked, allow_overlap: byId('run-overlap').checked, context_mode: byId('run-context').value, require_parallel: required === '' ? (workers === 100 ? 100 : null) : Number(required), no_progress_timeout: idle === '' ? null : Number(idle), ...(byId('run-label').value.trim() ? {label: byId('run-label').value.trim()} : {})};
  }

  function validateOptions(options) {
    if (!state.context?.configurable) return 'Connect a project configuration to prepare a run.';
    if (!options.task_ids.length) return 'Select at least one task.';
    if (!Number.isInteger(options.workers) || options.workers < 1 || options.workers > state.context.limits.max_workers) return 'Choose an agent count within this repository’s limit.';
    if (options.workers > options.task_ids.length) return 'Select at least ' + options.workers + ' tasks for ' + options.workers + ' concurrent agents.';
    if (options.task_ids.length > state.context.limits.max_workers) return 'This repository permits at most ' + state.context.limits.max_workers + ' tasks in one run.';
    if (options.synchronized && options.workers !== options.task_ids.length) return 'A synchronized run needs exactly one selected task per agent.';
    if (!Number.isInteger(options.timeout) || options.timeout < 30 || options.timeout > 1800) return 'Choose a coding time limit from 30 to 1800 seconds.';
    if (options.require_parallel !== null && (!Number.isInteger(options.require_parallel) || options.require_parallel < 1 || options.require_parallel > options.workers)) return 'The required independent frontier must be between 1 and the selected agent count.';
    if (options.no_progress_timeout !== null && (!Number.isInteger(options.no_progress_timeout) || options.no_progress_timeout < 30 || options.no_progress_timeout > options.timeout)) return 'Choose a no-progress stop from 30 seconds up to the coding time limit, or leave it empty for warnings.';
    if (!options.base || options.base.startsWith('-')) return 'Choose a valid source reference.';
    if (options.model_budget !== null && (!finite(options.model_budget) || options.model_budget <= 0 || options.model_budget > 180 || Math.abs(options.model_budget * 100 - Math.round(options.model_budget * 100)) > 0.000001)) return 'Use a total model reservation between USD0.01 and USD180.00, in whole cents.';
    if (options.context_mode === 'task' && state.context.tasks.some((task) => options.task_ids.includes(task.id) && !task.has_task_context)) return 'Every selected task needs a repository-provided packet to use task context.';
    return null;
  }

  function invalidatePreview() {
    state.preview = null; state.previewFingerprint = null; state.planVersion += 1;
    byId('run-preview').hidden = true; clear(byId('run-preview')); byId('save-run-button').disabled = true; builderError('');
  }

  function updateBuilder() {
    const options = readOptions(), problem = validateOptions(options);
    const frontier = state.previewFingerprint === JSON.stringify(options) ? state.preview?.preflight?.dependency_analysis?.safe_frontier_count : null;
    setText('run-task-count', number(options.task_ids.length) + ' selected · ' + number(state.context?.tasks?.length || 0) + ' available' + (options.task_ids.length ? ' · ' + number(options.workers || 0) + ' requested slots' + (finite(frontier) ? ' · ' + number(frontier) + ' independently ready' : ' · check plan for readiness') : ''));
    for (const button of byId('worker-presets').querySelectorAll('button')) button.setAttribute('aria-pressed', String(Number(button.dataset.workers) === options.workers));
    byId('preview-run-button').disabled = state.planning || Boolean(problem) || !state.csrf;
    byId('save-run-button').disabled = state.planning || !state.preview || state.previewFingerprint !== JSON.stringify(options) || Boolean(problem) || !state.csrf;
    byId('preview-run-button').textContent = state.planning ? 'Checking…' : 'Check plan';
    if (problem && options.task_ids.length) builderError(problem); updateEasyRunGuide();
  }

  const blockerCopy = {
    task_dependency_graph_invalid: 'The task graph has a cycle, missing prerequisite or invalid resource declaration. Resolve the graph before dispatch.', task_dependencies_not_ready: 'Selected tasks depend on unfinished work. Complete and merge their prerequisites, then check a new source base.', independent_frontier_below_required_parallelism: 'Fewer independent tasks are ready than this run requires. Inspect the parallelism review below.', dependency_source_scan_incomplete: 'The bounded source scan is incomplete. Review the remaining source before asserting the required independent capacity.',
    target_origin_mismatch_or_unavailable: 'The registered repository origin needs verification.', checkout_head_differs_from_selected_base: 'The checkout differs from the selected source reference.', target_checkout_has_uncommitted_changes: 'The repository has uncommitted changes.', task_metadata_not_ready: 'Some selected tasks are not ready.', configured_worker_capacity_insufficient: 'The requested agent count exceeds the repository policy.', configured_gateway_capacity_insufficient: 'The configured gateway capacity is below this run’s concurrency.', selected_tasks_already_have_durable_claims: 'Some tasks already belong to an earlier run.', budget_profile_policy_mismatch: 'The project budget policy differs from the retained accounting policy.', existing_active_or_awaiting_tasks: 'Earlier tasks are still active or waiting for integration.', durable_unreserved_allowance_not_observed: 'Remaining local admission allowance has not been observed.', local_infrastructure_reserve_insufficient: 'Remaining infrastructure admission allowance is insufficient.', local_model_reserve_insufficient: 'Remaining model admission allowance is insufficient.', cohort_model_reserve_not_selected: 'Select a total model reservation before a live launch.', github_readiness_not_checked: 'Current GitHub source readiness has not been checked.', provider_quota_not_verified: 'Current provider quota needs verification.', deployed_gateway_capacity_not_verified: 'The deployed gateway capacity needs verification.', owner_launch_authorization_not_verified: 'The live launch still needs explicit owner authorization.', live_execution_disabled: 'Live execution is disabled in the repository policy.'
  };

  function renderPlan(container, plan) {
    const preflight = plan.preflight || {}, heading = node('div', 'plan-heading-row');
    heading.append(node('h3', '', plan.id ? 'Saved plan' : 'Plan review'), pill(preflight.preparation_checks_passed === true ? 'Source checks passed' : 'Preparation needs attention', preflight.preparation_checks_passed === true ? 'mint-pill' : 'amber-pill')); container.append(heading);
    const factList = node('div', 'plan-facts');
    for (const [label, value] of [['Concurrent agents', number(plan.workers)], ['Selected tasks', number(plan.task_ids?.length)], ['Coding limit', secondsLabel(plan.timeout)], ['Model reservation', money(plan.model_budget)], ['Infrastructure admission', money(finite(preflight.minimum_infrastructure_reserve_cents) ? preflight.minimum_infrastructure_reserve_cents / 100 : null)]]) {const fact = node('div', 'plan-fact'); fact.append(node('span', '', label), node('strong', '', value)); factList.append(fact);}
    container.append(factList, node('p', '', ['preview', 'draft'].includes(plan.status) ? 'This is a local plan. Agent execution has not started, and no additional spending has been authorized. Reservations are limits, not invoices.' : 'These are the saved planning observations, not a new readiness check. Execution outcomes, delivery acceptance and invoices remain separate evidence.'));
    renderReadiness(container, preflight, plan);
    const blockers = [...new Set([...(preflight.local_workload_blockers || []), ...(preflight.local_admission_blockers || []), ...(preflight.launch_blockers || [])])];
    if (blockers.length) {const details = node('details', 'plan-checks'), summary = node('summary', '', blockers.length + ' item(s) to resolve before a live launch'); details.open = preflight.preparation_checks_passed !== true; const list = node('ul', 'plan-blockers'); for (const blocker of blockers) list.append(node('li', '', blockerCopy[blocker] || String(blocker).replaceAll('_', ' '))); details.append(summary, list); container.append(details);}
  }

  function renderReadiness(container, preflight, plan) {
    const graph = preflight.dependency_analysis;
    if (graph && Number.isInteger(graph.safe_frontier_count)) {
      const section = node('section', 'dependency-review'), heading = node('div', 'plan-heading-row');
      const enough = graph.safe_frontier_count >= plan.workers && !graph.error_count && graph.source_scan_incomplete !== true;
      heading.append(node('h3', '', 'Parallelism review'), pill(enough ? 'Frontier available' : 'Dependencies need attention', enough ? 'mint-pill' : 'amber-pill')); section.append(heading);
      facts(section, 'Task graph', [['Ready now', number(graph.ready_count)], ['Independent ready frontier', number(graph.safe_frontier_count)], ['Longest dependency chain', number(graph.critical_path_length)], ['Largest theoretical wave', number(graph.max_wave_width)]]);
      const missing = Math.max(0, plan.workers - graph.safe_frontier_count);
      section.append(node('p', 'field-help', missing ? 'Requested ' + number(plan.workers) + ' agents; ' + number(graph.safe_frontier_count) + ' tasks form a conservative independent ready frontier. ' + number(missing) + ' more independent tasks are needed for this stage.' : 'The declared graph has enough independent ready tasks for the requested slots. This is static preparation evidence; semantic independence and provider capacity still need verification.'));
      if (graph.critical_path?.length > 1) section.append(node('p', 'dependency-chain mono', 'Dependency chain: ' + graph.critical_path.slice(0, 12).join(' → ') + (graph.critical_path.length > 12 ? ' …' : '')));
      if (graph.wave_sizes?.length) section.append(node('p', 'field-help', 'Theoretical wave sizes: ' + graph.wave_sizes.slice(0, 12).join(' → ') + (graph.wave_sizes.length > 12 ? ' …' : '') + '. Later waves require merged prerequisites and a new reviewed source base.'));
      const explanations = {new_owned_paths_not_in_base: 'New output paths are absent from the base, as expected for new work', shared_ownership: 'Tasks write overlapping paths', read_write_dependency_review: 'A task reads paths another task will change', static_import_dependency_review: 'A source import may couple these tasks', missing_import_with_provider: 'An import may need another task to produce its module', missing_input_with_provider: 'A missing input has a possible producer', missing_input_at_base: 'A declared input is absent from the selected base', static_source_unavailable: 'Frozen source could not be scanned completely', source_parse_unavailable: 'A source file needs manual import review'};
      const rows = [...(graph.errors || []), ...(graph.review_hints || [])];
      if (rows.length) {const details = node('details', 'dependency-details'); details.append(node('summary', '', number(graph.error_count || 0) + ' graph error(s) · ' + number(graph.review_hint_count || 0) + ' review hint(s)')); const list = node('ul'); for (const row of rows.slice(0, 20)) list.append(node('li', '', (explanations[row.code] || row.code.replaceAll('_', ' ')) + (row.task_ids?.length ? ': ' + row.task_ids.join(', ') : ''))); details.append(list); section.append(details);}
      container.append(section);
    }
    const context = preflight.context_readiness;
    if (context && Number.isInteger(context.instruction_count)) {
      const details = node('details', 'context-readiness'); details.append(node('summary', '', 'Context and acceptance readiness'));
      facts(details, 'Frozen context', [['Required documents', number(context.instruction_count)], ['Required instruction size', number(context.instruction_bytes) + ' bytes'], ['Tasks with focused context', number(context.tasks_with_context) + ' / ' + number(context.task_count)], ['Tasks with configured acceptance checks', number(context.tasks_with_acceptance_checks) + ' / ' + number(context.task_count)]]);
      const warnings = {task_context_selectors_missing: 'Some tasks have no focused contract excerpts.', task_acceptance_checks_missing: 'Some tasks have no separate configured acceptance checks.', large_required_instruction_payload: 'The required global instruction payload is large. Review it before adding more context.', large_context_payload_review_required: 'The instruction and prompt payload needs a context-size review.', context_token_count_unmeasured: 'Token usage has not been measured; byte counts are not token counts.'};
      for (const code of context.warnings || []) details.append(node('p', 'field-help', warnings[code] || code.replaceAll('_', ' ')));
      container.append(details);
    }
  }

  async function prepareRun(save) {
    const options = readOptions(), problem = validateOptions(options); if (problem || !state.csrf || state.planning) {builderError(problem || 'A local planning session is required. Refresh the workspace.'); return;}
    if (save && (!state.preview || state.previewFingerprint !== JSON.stringify(options))) {builderError('Check the current plan before saving its draft.'); return;}
    const projectId = state.projectId, version = ++state.planVersion, fingerprint = JSON.stringify(options); state.planning = true; byId('run-options').disabled = true; builderError(''); updateBuilder();
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(save ? '/api/runs' : '/api/runs/preview', {method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Swarm-CSRF': state.csrf}, body: JSON.stringify({project_id: projectId, options})});
      const result = await response.json();
      if (version !== state.planVersion || projectId !== state.projectId) return;
      if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : 'The local server rejected this plan.');
      if (result.project_id !== projectId || !result.preflight || result.authorizes_spend !== false || !['preview', 'draft'].includes(result.status)) throw new Error('The server did not return a valid local plan.');
      state.preview = result; state.previewFingerprint = fingerprint;
      const panel = byId('run-preview'); clear(panel); panel.hidden = false; renderPlan(panel, result);
      if (save) {
        if (!result.id || result.status !== 'draft') throw new Error('The saved draft could not be confirmed. Refresh run history before retrying.');
        state.runId = result.id; persistSelection(); announce('Draft saved. No agents were launched.');
        await loadCatalog(); toggleBuilder(false, false);
      } else announce('Plan checked. Review the remaining launch requirements before saving a draft.');
    } catch (error) {
      if (version !== state.planVersion || projectId !== state.projectId) return;
      state.preview = null; state.previewFingerprint = null;
      builderError(save ? 'Could not confirm the saved draft. Refresh run history before trying again. No agents are launched by this action.' : error.name === 'AbortError' ? 'The local plan check took too long. Refresh the workspace before retrying.' : error.message);
    } finally {clearTimeout(timeout); if (version === state.planVersion && projectId === state.projectId) {state.planning = false; byId('run-options').disabled = !state.context?.configurable; updateBuilder();}}
  }

  function renderHistory(runs) {
    const body = byId('history-rows'); clear(body);
    if (!runs.length) {const row = node('tr', 'empty-table'), cell = node('td', '', 'No saved runs for this repository. Prepare a new run to create a draft.'); cell.colSpan = 5; row.append(cell); body.append(row);}
    for (const run of runs) {
      const row = node('tr'), name = node('td'); name.append(node('span', 'history-label', run.label || run.id), node('span', 'history-id mono', run.id));
      const status = node('td'); status.append(pill(runStatus(run.status), run.status === 'draft' ? 'amber-pill' : 'neutral'));
      const openCell = node('td'), open = node('button', 'button button-quiet', 'View'); open.type = 'button'; open.setAttribute('aria-label', 'View run ' + (run.label || run.id)); open.addEventListener('click', () => selectRun(run.id)); openCell.append(open);
      row.append(name, status, node('td', '', finite(run.workers) ? number(run.workers) : 'Not recorded'), node('td', '', dateLabel(run.created_at_utc)), openCell); body.append(row);
    }
    setText('history-note', number(runs.length) + ' saved run(s). A draft has not started; recorded evidence does not by itself establish successful delivery.');
  }

  async function refreshHistory() {
    if (!state.workspace || !state.projectId) return;
    const projectId = state.projectId; byId('refresh-history-button').disabled = true;
    try {
      const response = await fetch('/api/runs?' + new URLSearchParams({project: projectId}), {cache: 'no-store', credentials: 'same-origin', headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('RUNS'); const data = await response.json();
      if (projectId !== state.projectId) return;
      if (!Array.isArray(data.runs)) throw new Error('RUNS');
      const project = state.catalog.projects.find((item) => item.id === projectId); project.runs = data.runs;
      selectOptions(byId('run-select'), project.runs, 'No runs yet'); if (state.runId && project.runs.some((run) => run.id === state.runId)) byId('run-select').value = state.runId;
      else if (!state.runId) addRunPlaceholder();
      renderHistory(data.runs); setText('workspace-summary', workspaceSummary(project));
    } catch {if (projectId === state.projectId) setText('history-note', 'Could not refresh saved runs. Previously displayed records are retained.');}
    finally {byId('refresh-history-button').disabled = false;}
  }

  function selectRun(id) {
    const project = state.catalog?.projects.find((item) => item.id === state.projectId); if (!project?.runs.some((run) => run.id === id)) return;
    state.runId = id; byId('run-select').value = id; persistSelection(); toggleBuilder(false, false);
    resetSelection('Loading selected run…', 'Reading the recorded configuration and reviewed evidence for this selection.'); refresh();
  }

  function renderRunConfiguration(run) {
    state.runDetail = run; updateEasyRunGuide(); const target = byId('run-configuration-content'), key = launchKey(run.project_id, run.id);
    if (target.dataset.runKey === key && run.status === 'draft' && target.querySelector('.browser-launch')?.contains(document.activeElement)) {updateLaunchStatus(run); return;}
    clear(target); target.dataset.runKey = key; byId('run-configuration').hidden = false;
    setText('configuration-status', runStatus(run.status));
    setText('run-configuration-title', run.label || run.id || 'Run configuration'); setText('run-name', run.label || run.id); setText('run-phase', runStatus(run.status));
    const draft = run.status === 'draft';
    target.append(node('p', '', draft ? 'This saved draft has not launched any agents. Its configuration is preserved; another run creates a separate record.' : run.has_snapshot ? 'Recorded evidence is shown below when its curated source is available. Missing configuration fields stay unknown.' : 'This run has no imported delivery snapshot. Its execution status does not establish merged or accepted work.'));
    if (run.status === 'historical' && run.has_snapshot) {setText('run-configuration-title', 'Historical evidence'); return;}
    if (run.preflight) renderPlan(target, run);
    facts(target, 'Frozen run settings', [['Agents', run.workers], ['Selected task IDs', run.task_ids?.join(', ')], ['Source commit', run.base_sha, true], ['Coding time limit', finite(run.timeout) ? secondsLabel(run.timeout) : null], ['Required independent tasks', finite(run.require_parallel) ? run.require_parallel : 'No additional minimum'], ['No-source-progress stop', finite(run.no_progress_timeout) ? secondsLabel(run.no_progress_timeout) : 'Warnings only'], ['Instruction context', run.context_mode === 'task' ? 'Repository-provided task packet' : run.context_mode === 'full' ? 'Full project instructions' : null], ['Created', run.created_at_utc ? dateLabel(run.created_at_utc) : null]]);
    const execution = node('details', 'run-execution'); execution.open = !draft; execution.append(node('summary', '', 'Execution observations' + (draft ? ' · not launched' : '')));
    const reported = run.execution?.source === 'local_worker_reports' ? run.execution : null;
    const firstStart = run.started_at_utc || reported?.first_agent_started_at_utc;
    facts(execution, 'Recorded execution', [['Launch requested', run.launch_requested_at_utc ? dateLabel(run.launch_requested_at_utc) : null], ['First observed agent start', firstStart ? dateLabel(firstStart) : null], ['Runner finished', run.finished_at_utc ? dateLabel(run.finished_at_utc) : null], ['Runner exit code', Number.isInteger(run.runner_exit_code) ? run.runner_exit_code : null]]);
    const outcomeLabels = {validated: 'Validated patch', needs_repair: 'Needs repair', needs_recovery: 'Needs artifact recovery', rejected: 'Rejected', failed: 'Failed', interrupted: 'Interrupted', not_dispatched: 'Not dispatched', launch_failed: 'Launch failed', working: 'Working', preparing: 'Preparing', checking: 'Checking', ready: 'Prepared', unknown: 'Unknown'};
    const outcomeRows = Object.entries(reported?.worker_outcomes || run.worker_outcomes || {}).filter(([key, count]) => Object.hasOwn(outcomeLabels, key) && Number.isInteger(count) && count >= 0).map(([key, count]) => [outcomeLabels[key], number(count)]);
    if (outcomeRows.length) facts(execution, 'Recorded worker outcomes', outcomeRows);
    execution.append(node('p', 'field-help', outcomeRows.length ? 'Worker outcomes and a runner exit code do not establish accepted or merged deliveries.' : 'No per-worker outcomes have been recorded. Missing counts remain unknown.'));
    target.append(execution);
    if (reported) {
      const progress = node('section', 'reported-progress');
      facts(progress, 'Reported worker progress', [['Observed task reports', finite(reported.observed_tasks) ? number(reported.observed_tasks) : null], ['Expected tasks', finite(reported.expected_tasks) ? number(reported.expected_tasks) : null], ['Missing task reports', finite(reported.missing_tasks) ? number(reported.missing_tasks) : null], ['Observed at', reported.observed_at_utc ? dateLabel(reported.observed_at_utc) : null]]);
      const phaseLabels = {provision: 'Provision', prepare: 'Prepare', barrier: 'Start barrier', agent: 'Agent session', collect: 'Collect patch', check: 'Validation', acceptance: 'Task acceptance checks', recover: 'Recover artifacts', artifacts: 'Read artifacts', terminate: 'Terminate sandbox'};
      const phaseRows = Object.entries(reported.phase_counts || {}).filter(([key, count]) => Object.hasOwn(phaseLabels, key) && Number.isInteger(count) && count >= 0).map(([key, count]) => [phaseLabels[key], number(count)]);
      if (phaseRows.length) facts(progress, 'Last reported phase', phaseRows);
      if (reported.task_observations?.length) {
        const details = node('details', 'worker-observations'), summary = node('summary', '', 'Per-task progress and observed stop reasons'); details.append(summary);
        const scroll = node('div', 'table-scroll'), table = node('table'), head = node('thead'), tr = node('tr'), body = node('tbody');
        for (const text of ['Task', 'Outcome', 'Last phase', 'Source changes', 'Observed stop']) {const cell = node('th', '', text); cell.scope = 'col'; tr.append(cell);} head.append(tr);
        const reasons = {child_exited: 'Agent process exited', coding_deadline_reached: 'Coding limit reached', configured_no_source_progress: 'Configured source-progress stop', supervisor_interrupted: 'Supervisor interrupted', spawn_failed: 'Could not start agent'};
        for (const task of reported.task_observations.slice(0, 100)) {const row = node('tr'); row.append(node('td', 'mono', task.task_id), node('td', '', outcomeLabels[task.status] || 'Unknown'), node('td', '', phaseLabels[task.phase] || 'Not observed'), node('td', '', finite(task.source_change_count) ? number(task.source_change_count) : 'Not observed'), node('td', '', (reasons[task.terminal_reason] || 'Unknown') + (task.recovered_candidate_available ? ' · Recovery candidate needs review' : ''))); body.append(row);}
        table.append(head, body); scroll.append(table); details.append(scroll); progress.append(details);
      }
      progress.append(node('p', 'field-help', 'These are the last local worker reports. Phase counts are not active machines, continuous useful activity or accepted deliveries. Unobserved work remains unknown.')); target.append(progress);
    }
    if (draft && run.launch_command) {
      const guide = node('details', 'launch-guide'); guide.append(node('summary', '', 'Inspect this draft from the terminal'), node('p', '', 'This command inspects the saved run without starting agents. Use it from an activated controller environment. Replace a workspace placeholder, if shown, with the same workspace file used by this dashboard.'), node('code', '', run.launch_command));
      const copy = node('button', 'button button-quiet', 'Copy inspect command'); copy.type = 'button'; copy.addEventListener('click', async () => {try {await navigator.clipboard.writeText(run.launch_command); copy.textContent = 'Copied'; announce('Inspect command copied.');} catch {copy.textContent = 'Select and copy the command above';}}); guide.append(copy); target.append(guide);
    }
    if (state.workspace && (draft || state.launchStates.has(key) || ['launching', 'runner_finished', 'interrupted'].includes(run.status))) renderBrowserLaunch(target, run);
    if (!run.has_snapshot) {
      runContent(false); byId('workspace-empty').hidden = true; setText('snapshot-time', 'No delivery snapshot'); setText('last-fetch', 'Read: ' + timeLabel(new Date().toISOString()));
      clear(byId('connection-status')); byId('connection-status').append(node('span', 'status-dot amber'), document.createTextNode(draft ? 'Draft · no agents launched' : 'Run record available'));
    }
  }

  function updateEasyRunGuide() {
    if (!state.workspace) return;
    const connected = Boolean(state.projectId), planned = Boolean(state.preview || state.runDetail), saved = Boolean(state.runId), launched = Boolean(state.runDetail && !['draft', 'historical'].includes(state.runDetail.status));
    for (const [id, done, current] of [['step-connect', connected, !connected], ['step-plan', planned, connected && !planned], ['step-save', saved, planned && !saved], ['step-run', launched, saved && !launched]]) {
      byId(id).classList.toggle('is-done', done); if (current) byId(id).setAttribute('aria-current', 'step'); else byId(id).removeAttribute('aria-current');
    }
    setText('easy-run-next', !connected ? 'Next: connect a local repository folder.' : launched ? 'Launch observations appear in this run. Keep the controller terminal open while workers run.' : saved ? 'Next: review the saved draft and its launch requirements below.' : planned ? 'Next: click Save draft to preserve these settings.' : 'Next: select ready tasks, choose the number of concurrent agents and click Check plan.');
  }

  function toggleConnect(open, focus = true) {
    byId('connect-repository').hidden = !open; byId('connect-toggle').setAttribute('aria-expanded', String(open));
    if (open && focus) byId('connect-path').focus();
  }

  function connectionResult(message, error = false) {
    const box = byId('connect-result'); box.hidden = !message; box.textContent = message; box.classList.toggle('error-message', error);
  }

  async function connectRepository() {
    if (state.connecting || !state.csrf) return;
    const path = byId('connect-path').value.trim(), label = byId('connect-label').value.trim();
    if (/^[a-z]+:\/\//i.test(path) || /^git@/i.test(path)) {connectionResult('Download or clone the repository first, then paste its full local folder path. A GitHub URL cannot be connected here.', true); return;}
    if (/^['"]|['"]$/.test(path)) {connectionResult('Paste the folder path without surrounding quotes. Spaces in folder names are allowed.', true); return;}
    state.connecting = true; byId('connect-submit').disabled = true; byId('connect-submit').textContent = 'Connecting…'; connectionResult('Checking the local project configuration…');
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch('/api/projects', {method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Swarm-CSRF': state.csrf}, body: JSON.stringify({path, ...(label ? {label} : {})})});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : 'The project could not be connected. Check its local path, swarm.toml and Git origin.');
      if (typeof data.project_id !== 'string' || data.configurable !== true) throw new Error('The server did not confirm a connected project. Refresh the workspace before trying again.');
      state.projectId = data.project_id; state.runId = null; state.runDetail = null; persistSelection();
      const catalog = await loadCatalog(false, data.project_id);
      if (!catalog?.projects.some((project) => project.id === data.project_id)) throw new Error('The repository connected, but the refreshed workspace could not confirm it. Refresh the workspace before trying another connection.');
      connectionResult('Repository connected. Select ready tasks below to prepare a run.'); byId('connect-path').value = ''; byId('connect-label').value = ''; toggleConnect(false, false); announce('Repository connected. Prepare a new run.');
    } catch (error) {connectionResult(error.name === 'AbortError' ? 'The connection check took too long. Refresh the workspace to see whether it connected before retrying.' : error.message, true);}
    finally {clearTimeout(timeout); state.connecting = false; byId('connect-submit').disabled = !state.csrf; byId('connect-submit').textContent = 'Connect repository';}
  }

  const launchKey = (project, run) => String(project) + ':' + String(run);
  const pendingKey = (run) => 'swarm-control-launch:' + launchKey(run.project_id, run.id);
  function pendingLaunch(run) {try {return window.sessionStorage.getItem(pendingKey(run)) === 'pending';} catch {return false;}}
  function rememberPending(run, pending) {try {if (pending) window.sessionStorage.setItem(pendingKey(run), 'pending'); else window.sessionStorage.removeItem(pendingKey(run));} catch { /* In-memory duplicate protection remains available. */ }}
  function gatewayOrigin(value) {
    if (!/^https:\/\/[A-Za-z0-9-]+\.modal\.run\/?$/.test(value)) return null;
    try {return new URL(value).origin;} catch {return null;}
  }

  function launchProblem(run) {
    if (!state.launchEnabled) return 'This server does not support browser launch. Use the CLI execution guide.';
    if (!state.csrf) return 'Refresh the workspace to establish a local session.';
    if (run.status !== 'draft') return 'This run has already been attempted. Review its observations before preparing another draft.';
    if (state.context?.project_id !== run.project_id) return 'Reading this repository’s current policy…';
    if (state.context?.live_execution_enabled !== true) return 'Live execution is disabled for this repository. Follow docs/TWO_AGENT_WALKTHROUGH.md to configure your own services and authorize a bounded budget. Planning and drafts work without cloud accounts.';
    if (!finite(run.model_budget) || run.model_budget <= 0) return 'This draft has no model reservation. Prepare a new run with a total model reservation before launching.';
    if (run.preflight?.preparation_checks_passed !== true) return 'The saved source checks need attention. Resolve the listed preparation items, then prepare and check a new draft.';
    return null;
  }

  function launchObservation(run) {
    const observed = state.launchStates.get(launchKey(run.project_id, run.id));
    if (run.status === 'launching') return {status: 'running', error: observed?.evidence_status === 'unavailable' ? observed.error : null, evidence_status: observed?.evidence_status};
    if (run.status === 'runner_finished') return {status: 'finished', error: observed?.error || null, evidence_status: observed?.evidence_status, runner_exit_code: run.runner_exit_code};
    if (run.status === 'interrupted') return {status: 'failed', error: 'The runner was interrupted. Inspect its preserved evidence before preparing another draft.'};
    if (observed && observed.status !== 'idle') return observed;
    if (pendingLaunch(run)) return {status: 'uncertain', error: 'The launch response is unknown. Refresh run history and inspect the controller terminal before preparing another draft. This request will not be sent again automatically.'};
    return observed || {status: 'idle', error: null};
  }

  function updateLaunchStatus(run) {
    const form = byId('browser-launch-form'); if (!form || form.dataset.runKey !== launchKey(run.project_id, run.id)) return;
    const observation = launchObservation(run), problem = launchProblem(run), blocked = observation.status !== 'idle', button = byId('launch-run-button');
    const statusCopy = {starting: 'Launch requested. Checking the saved source, policy and budget gates…', running: 'The runner is active. Keep the controller terminal open. Closing this browser tab does not stop workers.', finished: observation.runner_exit_code === 0 ? 'Runner finished. Review worker outcomes, patches and checks; accepted deliveries are recorded separately.' : 'Runner finished with an error or an unknown exit status. Inspect the preserved worker outcomes and controller terminal.', failed: observation.error || 'Launch failed. Inspect the controller terminal and preserved evidence before preparing another draft.', uncertain: observation.error};
    const gatewayValue = byId('launch-gateway').value.trim(), gatewayProblem = gatewayValue && !gatewayOrigin(gatewayValue) ? 'Enter your verified HTTPS Modal gateway origin, for example https://your-gateway.modal.run. Other domains, ports, paths and API keys are not accepted.' : null;
    byId('launch-gateway').setAttribute('aria-invalid', String(Boolean(gatewayProblem)));
    const evidenceProblem = observation.evidence_status === 'unavailable' ? observation.error || 'The runner status was observed, but its private launch evidence could not be fully recorded. Inspect the controller terminal and preserved history before preparing another draft.' : observation.error;
    setText('browser-launch-status', evidenceProblem || statusCopy[observation.status] || problem || gatewayProblem || 'Ready for your explicit confirmation. The controller will recheck the saved source and all ordinary launch gates.');
    byId('browser-launch-status').classList.toggle('error-message', ['failed', 'uncertain'].includes(observation.status) || Boolean(evidenceProblem) || Boolean(gatewayProblem && !problem));
    byId('launch-gateway').disabled = blocked || Boolean(problem); byId('launch-confirm').disabled = blocked || Boolean(problem);
    button.disabled = blocked || Boolean(problem) || !byId('launch-confirm').checked || !gatewayOrigin(byId('launch-gateway').value.trim());
    button.textContent = observation.status === 'starting' ? 'Requesting launch…' : observation.status === 'running' ? 'Runner active' : blocked ? 'Launch attempt recorded' : 'Run agents';
  }

  function renderBrowserLaunch(target, run) {
    const key = launchKey(run.project_id, run.id), section = node('section', 'launch-guide browser-launch');
    section.append(node('h3', '', 'Run this saved draft'), node('p', '', 'Run agents starts real coding workers on Modal using your configured services. Review the total model reservation and infrastructure admission above, resolve the launch requirements and keep the controller terminal open.'));
    const form = node('form'); form.id = 'browser-launch-form'; form.dataset.runKey = key;
    const field = node('div', 'run-field'), label = node('label', '', 'Verified gateway HTTPS origin'); label.htmlFor = 'launch-gateway';
    const input = node('input'); input.id = 'launch-gateway'; input.type = 'url'; input.required = true; input.placeholder = 'https://your-verified-gateway.modal.run'; input.autocomplete = 'off'; input.spellcheck = false; input.setAttribute('aria-describedby', 'gateway-help');
    const savedInput = state.launchInputs.get(key) || {gateway: '', confirmed: false}; input.value = savedInput.gateway;
    field.append(label, input); const help = node('p', 'field-help', 'Use your verified https://…modal.run origin, without a port, path or API key. No service credentials are entered in this panel.'); help.id = 'gateway-help'; field.append(help);
    const confirmation = node('label', 'launch-confirmation'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.id = 'launch-confirm'; checkbox.required = true; checkbox.checked = savedInput.confirmed;
    confirmation.append(checkbox, node('span', '', 'I authorize this run within the configured model and infrastructure allowances, and have verified my gateway and provider capacity.'));
    const button = node('button', 'button button-primary', 'Run agents'); button.id = 'launch-run-button'; button.type = 'submit'; button.disabled = true;
    const status = node('p', 'launch-status'); status.id = 'browser-launch-status'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const recordInput = () => {state.launchInputs.set(key, {gateway: input.value, confirmed: checkbox.checked}); updateLaunchStatus(run);}; input.addEventListener('input', () => {checkbox.checked = false; recordInput();}); checkbox.addEventListener('change', recordInput);
    form.append(field, confirmation, button, status); form.addEventListener('submit', (event) => {event.preventDefault(); if (form.reportValidity()) launchRun(run);}); section.append(form); target.append(section); updateLaunchStatus(run); checkLaunch(run);
  }

  async function checkLaunch(run) {
    if (!state.launchEnabled) return;
    const key = launchKey(run.project_id, run.id); if (state.launchChecks.has(key)) return; state.launchChecks.add(key);
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/launch?' + new URLSearchParams({project: run.project_id, run: run.id}), {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) return; const data = await response.json();
      if (data.project_id !== run.project_id || data.run_id !== run.id || !['idle', 'starting', 'running', 'finished', 'failed'].includes(data.status)) return;
      const previous = state.launchStates.get(key); if (state.launchRequests.has(key) && data.status === 'idle') return;
      state.launchStates.set(key, data); if (data.status !== 'idle') rememberPending(run, false);
      if (state.projectId !== run.project_id || state.runId !== run.id) return; updateLaunchStatus(state.runDetail || run);
      if (previous?.status !== data.status && ['finished', 'failed', 'running'].includes(data.status)) {refreshHistory(); refresh();}
    } catch { /* Missing observations never make an uncertain launch retryable. */ }
    finally {clearTimeout(timeout); state.launchChecks.delete(key);}
  }

  async function launchRun(run) {
    const key = launchKey(run.project_id, run.id), gateway = gatewayOrigin(byId('launch-gateway').value.trim());
    if (launchProblem(run) || !gateway || !byId('launch-confirm').checked || launchObservation(run).status !== 'idle') return;
    rememberPending(run, true); state.launchRequests.add(key); state.launchStates.set(key, {status: 'starting', error: null}); updateLaunchStatus(run);
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch('/api/runs/launch', {method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Swarm-CSRF': state.csrf}, body: JSON.stringify({project_id: run.project_id, run_id: run.id, gateway_url: gateway, confirmed: true})});
      const data = await response.json();
      if (!response.ok) {
        if ([400, 403, 404, 409].includes(response.status)) {rememberPending(run, false); state.launchStates.set(key, {status: 'failed', error: typeof data.error === 'string' ? data.error : 'The controller rejected the launch. Resolve its requirements before preparing another draft.'});}
        else throw new Error('UNKNOWN');
      } else {
        if (response.status !== 202 || data.project_id !== run.project_id || data.run_id !== run.id || !['starting', 'running'].includes(data.status)) throw new Error('UNKNOWN');
        state.launchStates.set(key, data); rememberPending(run, false); announce('Launch requested. Keep the controller terminal open.');
      }
    } catch {state.launchStates.set(key, {status: 'uncertain', error: 'The launch response is unknown. Refresh run history and inspect the controller terminal. This request will not be sent again automatically.'});}
    finally {clearTimeout(timeout); state.launchRequests.delete(key); if (state.projectId === run.project_id && state.runId === run.id) {updateLaunchStatus(run); checkLaunch(run); refreshHistory(); refresh();}}
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
    const style = getComputedStyle(container);
    const width = container.clientWidth ? Math.max(80, container.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)) : 600;
    const height = container.clientHeight ? Math.max(80, container.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)) : 180;
    const left = 36, right = width - 12, baseline = height - 30;
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', 'aria-hidden': 'true'});
    const x = (time) => left + Math.max(0, Math.min(1, (time - start) / (end - start))) * (right - left);
    const y = (value) => baseline - Math.min(max, value) / max * (baseline - 16);
    for (let tick = 0; tick <= 4; tick++) {
      const value = Math.round(max * tick / 4), atY = y(value);
      svg.append(svgNode('line', {x1: left, x2: right, y1: atY, y2: atY, stroke: 'var(--line)', 'stroke-width': '.6', 'stroke-dasharray': '3 4'}));
      svg.append(svgNode('text', {x: left - 8, y: atY + 3, fill: 'var(--muted)', 'font-size': 12, 'text-anchor': 'end', 'font-family': 'var(--font-ui)'}, value));
    }
    let line = 'M ' + left + ' ' + y(0), value = 0;
    for (const event of events) {line += ' H ' + x(event.at.getTime()) + ' V ' + y(++value);}
    line += ' H ' + x(end);
    const area = line + ' L ' + x(end) + ' ' + y(0) + ' H ' + left + ' Z';
    svg.append(svgNode('path', {d: area, fill: 'var(--positive-soft)'}), svgNode('path', {d: line, fill: 'none', stroke: 'var(--mint)', 'stroke-width': 1.7, 'stroke-linejoin': 'round'}));
    svg.append(svgNode('circle', {cx: x(end), cy: y(value), r: 3, fill: 'var(--mint)'}));
    const intervals = width < 360 ? 2 : 3;
    for (let tick = 0; tick <= intervals; tick++) {const at = start + (end - start) * tick / intervals; svg.append(svgNode('text', {x: x(at), y: height - 7, fill: 'var(--muted)', 'font-size': 12, 'text-anchor': tick === 0 ? 'start' : tick === intervals ? 'end' : 'middle', 'font-family': 'var(--font-ui)'}, timeFormat.format(new Date(at))));}
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
      if (safeURL(task.delivery?.pr_url)) {const path = new URL(task.delivery.pr_url).pathname; pr.append(link('#' + path.split('/').pop(), task.delivery.pr_url));} else pr.append(node('span', 'subtle', 'Unknown'));
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
    if (!state.projectId || !state.runId) {if (!state.workspace) loadCatalog(); return;}
    if (state.fetching) return;
    state.fetching = true; byId('refresh-button').disabled = true;
    const controller = new AbortController(), timeout = window.setTimeout(() => controller.abort(), 15000), version = ++state.requestVersion;
    state.controller = controller;
    const query = new URLSearchParams({project: state.projectId, run: state.runId});
    try {
      if (state.workspace) {
        const response = await fetch('/api/run?' + query, {cache: 'no-store', credentials: 'same-origin', signal: controller.signal, headers: {'Accept': 'application/json'}});
        if (!response.ok) throw new Error('RUN');
        const run = await response.json(); if (version !== state.requestVersion) return;
        if (run.project_id !== state.projectId || run.id !== state.runId) throw new Error('RUN');
        renderRunConfiguration(run);
        if (run.has_snapshot !== true) {announce('Saved run configuration loaded. No delivery snapshot is available.'); return;}
      }
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

  byId('connect-toggle').addEventListener('click', () => toggleConnect(byId('connect-repository').hidden));
  byId('connect-cancel').addEventListener('click', () => toggleConnect(false, false));
  byId('connect-form').addEventListener('submit', (event) => {event.preventDefault(); if (byId('connect-form').reportValidity()) connectRepository();});
  byId('project-select').addEventListener('change', () => chooseProject(byId('project-select').value, null));
  byId('run-select').addEventListener('change', () => {
    if (state.workspace) {selectRun(byId('run-select').value); return;}
    const project = state.catalog?.projects.find((item) => item.id === state.projectId);
    if (!project?.runs.some((run) => run.id === byId('run-select').value)) return;
    state.runId = byId('run-select').value; persistSelection();
    resetSelection('Loading selected run…', 'Reading the reviewed evidence for this selection.'); refresh();
  });
  byId('refresh-button').addEventListener('click', () => state.workspace ? loadCatalog(true) : refresh());
  byId('new-run-button').addEventListener('click', () => toggleBuilder(byId('run-builder').hidden));
  byId('close-builder').addEventListener('click', () => toggleBuilder(false, false));
  byId('refresh-history-button').addEventListener('click', refreshHistory);
  byId('run-form').addEventListener('submit', (event) => {event.preventDefault(); if (byId('run-form').reportValidity()) prepareRun(false);});
  byId('save-run-button').addEventListener('click', () => prepareRun(true));
  byId('run-task-search').addEventListener('input', renderTaskChoices);
  byId('select-matching-run-tasks').addEventListener('click', () => {
    const query = byId('run-task-search').value.trim().toLocaleLowerCase('en-US'), cap = state.context?.limits?.max_tasks || state.context?.limits?.max_workers || 100;
    const matches = (state.context?.tasks || []).filter((task) => task.status === 'ready' && [task.id, task.title].join(' ').toLocaleLowerCase('en-US').includes(query));
    state.selectedTasks = new Set(matches.slice(0, cap).map((task) => task.id)); invalidatePreview(); renderTaskChoices(); updateBuilder(); announce(number(state.selectedTasks.size) + ' matching ready task definitions selected. Check the plan to inspect their dependencies.');
  });
  byId('clear-run-tasks').addEventListener('click', () => {state.selectedTasks.clear(); invalidatePreview(); renderTaskChoices(); updateBuilder();});
  for (const id of ['run-workers', 'run-label', 'run-budget', 'run-base', 'run-timeout', 'run-context', 'run-synchronized', 'run-overlap', 'run-required-parallel', 'run-no-progress']) byId(id).addEventListener('input', () => {invalidatePreview(); updateBuilder();});
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
  window.setInterval(() => {if (state.autoRefresh && !document.hidden) refresh(); if (state.workspace && state.runDetail && !document.hidden) checkLaunch(state.runDetail);}, 10000);
  document.addEventListener('visibilitychange', () => {if (!document.hidden && state.autoRefresh) refresh();});
  window.addEventListener('swarm:setup-changed', (event) => {if (state.workspace) loadCatalog(true, event.detail?.project_id);});
  window.addEventListener('swarm:setup-run', (event) => {if (state.workspace) {loadCatalog(true, event.detail?.project_id).then(() => toggleBuilder(true));}});
  if (window.ResizeObserver) {
    let chartWidth = 0;
    new ResizeObserver(([entry]) => {
      const nextWidth = Math.round(entry.contentRect.width);
      if (nextWidth > 0 && nextWidth !== chartWidth) {chartWidth = nextWidth; if (state.snapshot) renderChart(state.snapshot);}
    }).observe(byId('recovery-chart'));
  }
  loadCatalog();
})();
