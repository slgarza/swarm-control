(() => {
  'use strict';
  const el = id => document.getElementById(id);
  if (!el('plan-view')) return;
  const state = {key: '', version: 0, source: null, inspection: null, proposal: null,
    applied: null, busy: false, historyVersion: 0, lastJob: null};
  const node = (tag, cls, text) => {const item = document.createElement(tag); if (cls) item.className = cls; if (text !== undefined) item.textContent = String(text); return item;};
  const money = value => typeof value === 'number' && Number.isFinite(value) ? '$' + value.toFixed(2) : 'unknown';
  const context = () => window.SwarmApplication?.context() || {};
  const sourceKey = source => source?.projectId ? 'project:' + source.projectId : source?.path ? 'path:' + source.path : '';
  const options = () => state.source?.projectId ? {project_id: state.source.projectId} : {path: state.source?.path};
  const messages = {
    planning_source_changed: 'Repository files changed after inspection or review. Inspect again and review a new plan before approving changes.',
    planning_accounts_changed: 'The connected account changed after review. Inspect the retained request before authorizing a new plan.',
    planning_budget_insufficient: 'The planning limit is below the conservative request bound. Review that bound before increasing the limit.',
    planning_attempt_held: 'This planning request already has a retained attempt. Inspect its recorded result; it will not be repeated automatically.',
    planning_apply_held: 'An issue or file preparation attempt is already recorded. Inspect its progress before any manual recovery; automatic replay is blocked.',
    planning_github_token_required: 'Save your own GitHub token in Settings and verify that account before generating a plan.',
    planning_invalid_response: 'The model response could not be validated. Its request reservation remains recorded. Inspect the attempt before authorizing another request.',
    planning_validation_blocked: 'The proposed plan has dependency or ownership blockers. Review the reported problems before preparing tasks.',
    operation_uncertain: 'The request outcome is unknown. Inspect its recorded operation before repeating it.'
  };
  function feedback(text, bad = false) {
    el('planner-feedback').textContent = text; el('planner-feedback').hidden = !text;
    el('planner-feedback').dataset.state = bad ? 'error' : 'info';
  }
  function update() {
    const setup = window.SwarmSetup?.status() || {}, inspected = state.inspection, ready = inspected?.readiness || {};
    const goalBytes=new TextEncoder().encode(el('planner-goal').value).length;
    el('planner-goal').setCustomValidity(goalBytes>4096?'Use a shorter goal (up to 4096 UTF-8 bytes).':'');
    const max = Number(el('planner-task-limit').value), workers = Number(el('planner-workers').value);
    const formReady = Boolean(inspected && ready.local_model_key_configured && ready.github_verified && ready.github_credential_captured
      && el('planner-goal').value.trim() && goalBytes<=4096 && workers >= 1 && workers <= max && max <= 100
      && Number(el('planner-budget').value) >= inspected.call_cost_bound_usd && el('planner-generate-confirm').checked);
    for (const id of ['planner-goal', 'planner-task-limit', 'planner-workers', 'planner-budget', 'planner-generate-confirm', 'planner-apply-confirm']) el(id).disabled = state.busy;
    el('planner-inspect').disabled = state.busy || setup.busy || !state.source || !setup.available;
    el('planner-generate').disabled = state.busy || setup.busy || !setup.available || !formReady;
    el('planner-apply').disabled = state.busy || setup.busy || !setup.available || !state.proposal?.can_apply
      || state.proposal.state !== 'review_required' || !el('planner-apply-confirm').checked;
    el('planner-open-checks').disabled = state.busy || !state.applied?.project_id;
    el('planner-open-run').disabled = state.busy || !state.applied?.project_id;
  }
  function clearReview() {
    state.proposal = null; state.applied = null;
    el('planner-review').hidden = true; el('planner-applied').hidden = true;
    el('planner-apply-confirm').checked = false;
  }
  function synchronize() {
    const current = context();
    const source = current.projectId&&current.hasRepository ? {projectId: current.projectId, repository: current.repository}
      : current.pendingRepository?.path ? {path: current.pendingRepository.path, repository: current.pendingRepository.repository} : null;
    const key = sourceKey(source);
    // Materialization registers the same checkout. Preserve its reviewed receipt
    // while the catalog changes from an unprepared folder to a connected project.
    const materialized = state.applied?.project_id && source?.projectId === state.applied.project_id;
    if (key !== state.key) {
      state.key = key; state.source = source; state.version += 1;
      state.inspection = null; el('planner-inspection').hidden = true; el('planner-generate-confirm').checked = false;
      if (!materialized) {clearReview(); feedback(''); el('planner-goal').value = '';}
      el('planner-history').replaceChildren();
      if (window.SwarmNavigation?.current().screen === 'plan') history();
    } else state.source = source;
    el('planner-no-repository').hidden = Boolean(source) && current.workspace;
    el('planner-workspace').hidden = !source || !current.workspace;
    el('planner-project').textContent = source?.repository || source?.path || '';
    update();
  }
  function list(target, values, cls = '') {
    const ul = node('ul', cls);
    for (const value of values || []) ul.append(node('li', '', finding(value)));
    target.append(ul);
  }
  function finding(value) {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object') return String(value);
    if (value.code === 'insufficient_independent_frontier') return 'Only ' + value.available + ' tasks can safely start together; ' + value.requested + ' agents were requested. Review the prerequisites and choose fewer concurrent agents or a goal with more independent work.';
    const labels = {ownership_overlap: 'Tasks share owned files', dependency_cycle: 'Tasks contain a prerequisite cycle',
      unknown_dependency: 'A prerequisite task does not exist', self_dependency: 'A task depends on itself',
      invalid_ownership: 'Owned paths are missing or invalid', overlapping_ownership: 'Tasks share owned paths',
      new_owned_paths_not_in_base: 'Some tasks create new files; review their shared contracts'};
    return (labels[value.code] || String(value.code || 'Review finding').replaceAll('_', ' '))
      + (value.task_ids?.length ? ': ' + value.task_ids.join(', ') : '')
      + (value.dependency_id ? ' · prerequisite ' + value.dependency_id : '')
      + (value.path ? ' · ' + value.path : '');
  }
  function renderInspection(value) {
    state.inspection = value; el('planner-inspection').hidden = false;
    const ready = value.readiness || {}, missing = [];
    if (!ready.local_model_key_configured) missing.push('save a local OpenAI key');
    if (!ready.github_credential_captured) missing.push('save your GitHub token');
    if (!ready.github_verified) missing.push('verify your GitHub account');
    const target = el('planner-readiness'); target.replaceChildren();
    target.append(document.createTextNode(missing.length ? 'Before generating a plan: ' + missing.join(', ') + '. ' : 'Planning accounts ready' + (ready.github_identity ? ' · GitHub: ' + ready.github_identity : '') + '.'));
    if (missing.length) {const link = node('a', '', 'Open account settings'); link.href = '#/settings'; link.dataset.screenLink = 'settings'; target.append(link);}
    el('planner-context-size').textContent = (value.selected_bytes ?? 0).toLocaleString() + ' bytes selected, up to 64 KiB of tracked project context. Review filenames before sending.';
    el('planner-context-files').replaceChildren();
    for (const file of value.files || []) el('planner-context-files').append(node('li', 'mono', file.path + ' · ' + (file.selected ? 'included' : 'inventory only')));
    el('planner-cost-bound').textContent = 'Conservative request reservation: ' + money(value.call_cost_bound_usd) + '. Actual billed cost remains unknown until reconciled. This is separate from worker execution allowance.';
    el('planner-generate-confirm').checked = false; update();
  }
  function renderApplied(result) {
    state.applied = result; el('planner-applied').hidden = false;
    el('planner-apply-form').hidden = true;
    el('planner-applied-copy').textContent = (result.task_ids?.length || 0) + ' reviewed tasks and GitHub issues prepared for ' + result.repository + '. Review the preparation files and publish them before launch. Worker execution remains separately authorized.';
    const links = node('div', 'planner-issue-links');
    for (const issue of result.issues || []) {
      // A model or malformed transport must never supply an executable URL.
      let url; try {url = new URL(issue.url);} catch {continue;}
      if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.username || url.password) continue;
      const a = node('a', '', '#' + issue.number + ' · ' + issue.task_id); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; links.append(a);
    }
    el('planner-applied').querySelector('.planner-issue-links')?.remove(); el('planner-applied').append(links); update();
  }
  function renderProposal(value) {
    state.proposal = value; el('planner-apply-confirm').checked = false;
    const plan = value.plan;
    el('planner-review').hidden = !plan;
    if (plan) {
      el('planner-summary').textContent = plan.summary;
      const reservation = value.planning_reservation || {};
      el('planner-accounting').textContent = 'Planning request ' + value.plan_id + ' · ' + value.model + ' · reserved ' + money(reservation.reserved_usd)
        + ' · actual billed cost: ' + money(reservation.invoice_cost_usd) + (value.usage ? ' · reported tokens: ' + value.usage.input_tokens + ' input / ' + value.usage.output_tokens + ' output' : '');
      const analysis = value.analysis || {}, capacity = analysis.capacity || {};
      const frontier = Array.isArray(analysis.parallel_frontier) ? analysis.parallel_frontier.length : analysis.parallel_frontier;
      const target = el('planner-analysis'); target.replaceChildren();
      target.append(node('p', 'planner-capacity', 'Independent frontier: ' + (frontier ?? 'unknown') + ' tasks · requested agents: ' + (analysis.requested_workers ?? 'unknown')));
      if (capacity.requires_capacity_review) target.append(node('p', 'planner-warning', 'The existing execution policy needs capacity review before this many agents can launch. Existing limits are preserved.'));
      if (analysis.errors?.length) {target.append(node('h3', '', 'Preparation blocked')); list(target, analysis.errors, 'planner-errors');}
      if (analysis.warnings?.length) {target.append(node('h3', '', 'Review before approval')); list(target, analysis.warnings);}
      el('planner-commands').textContent = JSON.stringify({install: plan.install, checks: plan.checks, instructions: plan.instructions}, null, 2);
      const tasks = el('planner-tasks'); tasks.replaceChildren();
      for (const task of plan.tasks || []) {
        const detail = node('details', 'planner-task'); const summary = node('summary', '', task.id + ' · ' + task.title); detail.append(summary);
        detail.append(node('p', '', task.prompt), node('h4', '', 'Owned paths')); list(detail, task.ownership, 'mono');
        detail.append(node('h4', '', 'Acceptance criteria')); list(detail, task.acceptance);
        detail.append(node('p', 'field-help', 'Prerequisites: ' + (task.dependsOn?.join(', ') || 'none'))); tasks.append(detail);
      }
      el('planner-apply-form').hidden = value.state !== 'review_required';
    }
    if (value.state === 'applied' && value.apply_result) renderApplied(value.apply_result);
    else if (value.state !== 'review_required') {
      feedback('Recorded state: ' + value.state + '. ' + (messages[value.apply_error_code || value.error_code] || 'The attempt remains recorded. No request was repeated.')
        , true);
      if (Array.isArray(value.apply_progress) && value.apply_progress.length) {
        const progress = node('ul', 'planner-apply-progress');
        for (const item of value.apply_progress) progress.append(node('li', '', item.task_id + ' · ' + item.state + (Number.isInteger(item.number) ? ' · GitHub issue #' + item.number : '')));
        el('planner-feedback').append(progress);
      }
    }
    update();
  }
  async function failure(error, version) {
    if (version !== state.version) return;
    const result = error.result;
    feedback(messages[error.code] || error.message || 'The planning operation did not complete. Inspect its recorded result before trying again.', true);
    if (result?.plan_id) {renderProposal(result); retainedButton(result.plan_id);}
    if (error.job_id) state.lastJob = error.job_id;
  }
  function retainedButton(planId) {
    const button = node('button', 'button button-quiet', 'Inspect retained plan'); button.type = 'button';
    button.addEventListener('click', () => operation(async version => {
      const result = await window.SwarmSetup.perform('planning.get', {plan_id: planId});
      if (version !== state.version || !matches(result)) return;
      renderProposal(result);
    }));
    el('planner-feedback').append(document.createTextNode(' '), button);
  }
  function matches(result) {return Boolean(result && (state.source?.projectId ? result.project_id === state.source.projectId || result.apply_result?.project_id === state.source.projectId : result.path === state.source?.path));}
  async function operation(work) {
    if (state.busy) return;
    const version = state.version; state.busy = true; update();
    try {await work(version);} catch (error) {await failure(error, version);}
    finally {state.busy = false; update(); history();}
  }
  async function history() {
    if (!state.source || window.SwarmNavigation?.current().screen !== 'plan') return;
    const version = state.version, sequence = ++state.historyVersion, target = el('planner-history');
    try {
      const data = await window.SwarmSetup.history();
      const jobs = (data.operations || []).filter(job => ['planning.generate', 'planning.apply'].includes(job.action)).slice(-20).reverse();
      const results = await Promise.all(jobs.map(job => window.SwarmSetup.readJob(job.id).then(value => value.job || value).catch(() => null)));
      if (version !== state.version || sequence !== state.historyVersion) return;
      target.replaceChildren();
      for (const job of results.filter(job => job && matches(job.result))) {
        const row = node('div', 'planner-history-row'); row.append(node('p', '', (job.action === 'planning.apply' ? 'Prepare tasks' : 'Generate plan') + ' · ' + job.status));
        if (job.result?.plan_id) {const button = node('button', 'button button-quiet', 'Open recorded plan'); button.type = 'button';
          button.addEventListener('click', () => operation(async current => {const value = await window.SwarmSetup.perform('planning.get', {plan_id: job.result.plan_id}); if (current === state.version && matches(value)) renderProposal(value);})); row.append(button);}
        target.append(row);
      }
      if (!target.childElementCount) target.append(node('p', 'field-help', 'No recorded planning requests for this repository. Inspection makes no model request.'));
    } catch {if (version === state.version) target.textContent = 'Planning history could not be read. Refresh this view before repeating an uncertain request.';}
  }
  el('planner-inspect-form').addEventListener('submit', event => {
    event.preventDefault(); operation(async version => {
      clearReview(); feedback('Inspecting tracked repository files locally…');
      const result = await window.SwarmSetup.perform('planning.inspect', options());
      if (version !== state.version) return; renderInspection(result); feedback('Repository inspected. Review the selected context, enter a goal and authorize a planning request.');
    });
  });
  el('planner-generate-form').addEventListener('submit', event => {
    event.preventDefault(); if (el('planner-generate').disabled || !event.currentTarget.reportValidity()) return;
    const selected = {...options(), goal: el('planner-goal').value.trim(), context_binding: state.inspection.context_binding,
      max_tasks: Number(el('planner-task-limit').value), workers: Number(el('planner-workers').value), plan_budget_usd: Number(el('planner-budget').value)};
    el('planner-generate-confirm').checked = false;
    operation(async version => {
      clearReview(); feedback('Generating and validating the work plan. The request is recorded; it will not be retried automatically.');
      const result = await window.SwarmSetup.perform('planning.generate', selected);
      if (version !== state.version) return; renderProposal(result);
      feedback(result.can_apply ? 'Plan ready for your review. Inspect every task and command before preparing GitHub issues.' : 'The plan has blockers. Review the dependency and ownership findings.', !result.can_apply);
    });
  });
  el('planner-apply-form').addEventListener('submit', event => {
    event.preventDefault(); if (el('planner-apply').disabled || !event.currentTarget.reportValidity()) return;
    const approved = state.proposal, workers = approved.analysis.requested_workers;
    el('planner-apply-confirm').checked = false;
    operation(async version => {
      feedback('Preparing the approved tasks and GitHub issues. Do not repeat this request while its outcome is pending.');
      const result = await window.SwarmSetup.perform('planning.apply', {plan_id: approved.plan_id, review_binding: approved.review_binding});
      if (version !== state.version) return;
      state.proposal = {...approved, state: 'applied', can_apply: false, apply_result: result}; renderApplied({...result, workers});
      await window.SwarmApplication.refreshProjects(result.project_id);
      feedback('Approved work prepared. Review checks and publish the preparation files, then choose your execution allowance.');
    });
  });
  for (const id of ['planner-goal', 'planner-task-limit', 'planner-workers', 'planner-budget']) el(id).addEventListener('input', () => {
    el('planner-generate-confirm').checked = false;
    // A changed request cannot approve a proposal from the previous goal.
    if (state.proposal && !state.applied) clearReview(); update();
  });
  el('planner-generate-confirm').addEventListener('change', update); el('planner-apply-confirm').addEventListener('change', update);
  el('planner-open-checks').addEventListener('click', () => window.SwarmNavigation?.navigate('project-settings', {step: 'checks', project: state.applied.project_id, run: null}));
  el('planner-open-run').addEventListener('click', () => operation(async () => {
    await window.SwarmApplication.prepareTasks(state.applied.project_id, state.applied.task_ids, state.applied.workers || state.proposal?.analysis?.requested_workers || 1);
  }));
  window.addEventListener('swarm:context', synchronize);
  window.addEventListener('swarm:setup-status', update);
  window.addEventListener('swarm:route', event => {if (event.detail.screen === 'plan') {synchronize(); history();}});
  synchronize();
})();
