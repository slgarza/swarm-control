(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const dialog = byId('setup-dialog'), mount = byId('setup-root');
  if (!dialog || !mount) return;
  const state = {csrf: null, available: false, busy: false, step: 0, path: '', repository: '', projectId: null,
    connections: {}, jobs: new Map(), jobContexts: new Map(), selectionVersion: 0, applicationSelectionKey: '', bindingVersion: 0, routeVersion: 0, boundProjectId: null, bindingProjectId: null, bindingFlight: null, publishBinding: null, publishPath: null,
    cliBinding: null, cliCommand: null, drafts: [], polling: new Set(), runtimeOverrides: new Map()};
  const steps = ['Accounts', 'Repository', 'Profile', 'Tasks', 'Checks & publish', 'Execution allowance', 'Advanced tools'];
  const titles = {'github.configure': 'Connect GitHub', 'modal.configure': 'Connect Modal',
    'openai.configure': 'Create OpenAI secret', 'environment.configure': 'Save personal .env', 'connections.verify': 'Verify connection',
    'tools.install-github': 'Install GitHub CLI', 'repository.retarget': 'Retarget fork profile',
    'repository.inspect': 'Inspect local repository', 'repository.clone': 'Clone repository', 'repository.connect': 'Connect prepared project',
    'planning.inspect': 'Inspect repository for planning', 'planning.generate': 'Generate AI work plan', 'planning.apply': 'Prepare approved AI plan', 'planning.get': 'Inspect retained plan', 'repository.select': 'Select project configuration',
    'project.initialize': 'Create project profile', 'tasks.import': 'Import issue drafts',
    'tasks.save': 'Save task', 'tasks.create-issue': 'Create GitHub issue', 'preparation.preview': 'Review preparation changes',
    'preparation.publish': 'Commit and push preparation', 'project.check': 'Run project checks',
    'project.configure-budget': 'Save authorized limits', 'project.deploy': 'Deploy gateway',
    'project.probe': 'Probe deployed gateway', 'cli.preview': 'Preview CLI command', 'cli.execute': 'Execute CLI command'};
  const terminal = new Set(['finished', 'succeeded', 'completed', 'failed', 'uncertain', 'interrupted', 'cancelled']);
  const escapeAttribute = (value) => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;');
  const field = (id, label, options = {}) => {
    const common = `id="setup-${id}" name="${id}" ${options.required === false ? '' : 'required'} ${options.attributes || ''}`;
    const value = escapeAttribute(options.value || '');
    let input;
    if (options.textarea) input = `<textarea ${common} rows="${options.rows || 3}" placeholder="${escapeAttribute(options.placeholder || '')}">${value}</textarea>`;
    else input = `<input ${common} type="${options.type || 'text'}" value="${value}" placeholder="${escapeAttribute(options.placeholder || '')}" ${options.type === 'password' ? 'autocomplete="new-password" data-setup-secret spellcheck="false"' : 'autocomplete="off"'}>`;
    return `<label class="setup-field" for="setup-${id}"><span>${label}</span>${input}${options.help ? `<small>${options.help}</small>` : ''}</label>`;
  };
  const check = (id, label, required = false, checked = false) => `<label class="setup-check" for="setup-${id}"><input id="setup-${id}" type="checkbox" ${required ? 'required' : ''} ${checked ? 'checked' : ''}><span>${label}</span></label>`;
  const submit = (label, quiet = false) => `<button class="button ${quiet ? 'button-quiet' : 'button-primary'}" data-setup-submit type="submit">${label}</button>`;
  const pathContext = '<p class="setup-path-context">Selected local project: <strong data-setup-path>No folder selected yet. Add one in Projects.</strong></p>';
  const review = (id, label) => check(id, label, true);
  const persistent = (id) => check(id, 'Remember in a private plaintext file on this computer. Uncheck to keep it only for this server session.', false, true);
  mount.innerHTML = `
    <header class="setup-header"><div><h2 id="setup-title">Setup &amp; connections</h2><p>Connect your accounts, prepare a project, then review a run. You can return here whenever you need.</p></div><button id="setup-close" class="setup-close" type="button" aria-label="Close setup">×</button></header>
    <p id="setup-feedback" class="setup-feedback" role="status" aria-live="polite">Opening a local setup session…</p>
    <div class="setup-layout"><nav class="setup-steps" role="tablist" aria-label="Project settings tabs" aria-orientation="horizontal">${steps.map((name, index) => `<button id="setup-tab-${index}" class="setup-step" role="tab" aria-selected="${index === 0}" aria-controls="setup-panel-${index}" tabindex="${index === 0 ? 0 : -1}" type="button"><span class="setup-step-number">${index + 1}</span>${name}</button>`).join('')}<p class="setup-side-note">Start locally with a disabled budget. Connecting accounts does not start coding agents.<br><br>Account limits and available quota stay unknown until independently verified.</p></nav>
    <div class="setup-content">
    <section id="setup-panel-0" role="tabpanel" aria-labelledby="setup-tab-0"><h3>Connect your own accounts</h3><p class="setup-lead">GitHub stores your code, Modal runs the workers, and OpenAI supplies the model. Existing terminal authentication can be checked without entering new keys.</p>
      <article class="setup-card"><div class="setup-card-title"><h4>GitHub</h4><span class="setup-provider-status" data-setup-provider="github">Not checked</span></div><p class="setup-copy">Use an account with access to your repository. Cloning reads code; forking and publishing write to GitHub.</p><button type="button" class="setup-secondary" data-setup-verify="github" data-setup-submit>Verify existing GitHub login</button><details class="setup-disclosure"><summary>Connect with a GitHub token</summary><form id="setup-github-form" class="setup-form">${field('github-token', 'GitHub token', {type: 'password'})}${persistent('github-persist')}<div class="setup-actions">${submit('Connect GitHub')}</div></form></details></article>
      <article class="setup-card"><div class="setup-card-title"><h4>Modal</h4><span class="setup-provider-status" data-setup-provider="modal">Not checked</span></div><p class="setup-copy">Connect the Modal workspace that will host your gateway and worker machines.</p><button type="button" class="setup-secondary" data-setup-verify="modal" data-setup-submit>Verify existing Modal login</button><details class="setup-disclosure"><summary>Connect with a Modal token pair</summary><form id="setup-modal-form" class="setup-form">${field('modal-id', 'Modal token ID', {type: 'password'})}${field('modal-secret', 'Modal token secret', {type: 'password'})}${persistent('modal-persist')}<div class="setup-actions">${submit('Connect Modal')}</div></form></details></article>
      <article class="setup-card"><div class="setup-card-title"><h4>Worker gateway secret</h4><span class="setup-provider-status" data-setup-provider="openai">Not checked</span></div><p class="setup-copy">Create the model secret used by this project’s worker gateway. Review the name and environment against its profile before deployment.</p><button type="button" class="setup-secondary" data-setup-verify="openai" data-setup-submit>Verify configured secret</button><details class="setup-disclosure"><summary>Create the Modal Secret from an OpenAI key</summary><form id="setup-openai-form" class="setup-form">${field('openai-key', 'OpenAI API key', {type: 'password'})}<div class="setup-grid">${field('secret-name', 'Modal Secret name', {value: 'swarm-control-openai'})}${field('secret-environment', 'Modal environment', {value: 'main'})}</div>${check('secret-replace', 'Replace an existing secret with this name. Leave unchecked to preserve it.')}${review('secret-confirm', 'I want to create this secret in my connected Modal workspace.')}<div class="setup-actions">${submit('Create Modal Secret')}</div></form></details></article>
      <p class="setup-help">Configured means credentials are present. Verified means an explicit connection check succeeded; it does not verify API quota, billing or permission for every future action. Password fields clear after submission.</p>
    </section>
    <section id="setup-panel-1" role="tabpanel" aria-labelledby="setup-tab-1" hidden><h3>Choose where your project lives</h3><p class="setup-lead">The folder is on the computer running Easy Run. Keep your application in its own folder, separate from Swarm Control.</p>
      <article class="setup-card"><h4>I already have a local repository</h4><form id="setup-inspect-form" class="setup-form">${field('local-path', 'Full local folder path', {placeholder: '/home/you/projects/my-app', help: 'Use a Git checkout with a GitHub origin, not a ZIP folder. In WSL, use a Linux path such as /mnt/c/Users/you/my-app.'})}<div class="setup-actions">${submit('Inspect and select folder')}</div></form><dl id="setup-repository-facts" class="setup-facts" hidden></dl></article>
      <article class="setup-card"><h4>Download a repository from GitHub</h4><form id="setup-clone-form" class="setup-form">${field('clone-repository', 'GitHub URL or owner/repository', {placeholder: 'https://github.com/your-account/your-project'})}${field('clone-destination', 'New local folder', {placeholder: '/home/you/projects/my-app', help: 'Choose a new folder. An existing folder will not be overwritten.'})}${check('clone-fork', 'Create my own GitHub fork before cloning')}${field('fork-owner', 'Fork owner or organization', {required: false, placeholder: 'Optional: defaults to your account'})}${review('clone-confirm', 'I reviewed the repository and destination. If fork is selected, create that repository in my GitHub account.')}<div class="setup-actions">${submit('Clone selected repository')}</div></form></article>
    </section>
    <section id="setup-panel-2" role="tabpanel" aria-labelledby="setup-tab-2" hidden><h3>Tell the workers how your project works</h3><p class="setup-lead">Create a local <code>swarm.toml</code> profile with the actual install and test commands for your project. Existing configuration is preserved by the server.</p>${pathContext}
      <form id="setup-project-form" class="setup-form"><div class="setup-grid">${field('project-repository', 'GitHub owner/repository', {placeholder: 'your-account/your-project'})}${field('base-branch', 'Base branch', {value: 'main'})}</div><label class="setup-field" for="setup-command-preset"><span>Command starting point</span><select id="setup-command-preset"><option value="custom">Choose your actual commands</option><option value="node">Node · npm ci / npm run check</option><option value="python">Python · pip install / unittest</option></select><small>These are starting points. Review them against your own repository before running.</small></label>${field('install', 'Install commands · argument arrays', {textarea: true, value: '[["npm", "ci", "--no-audit", "--no-fund"]]', help: 'JSON list of argument lists. Each command runs directly, without a shell.'})}${field('checks', 'Check commands · argument arrays', {textarea: true, value: '[["npm", "run", "check"]]', help: 'Use your real tests, lint and type checks. A command returning nonzero fails validation.'})}<div class="setup-grid">${field('instructions', 'Instruction files · one path per line', {textarea: true, value: 'README.md\nAGENTS.md', help: 'Paths relative to your project. The server checks that they exist.'})}${field('required-checks', 'Required GitHub Actions check names', {textarea: true, value: 'checks', help: 'One exact check name per line.'})}</div><details class="setup-disclosure"><summary>Worker image and gateway settings</summary><div class="setup-form">${field('image', 'Worker image', {value: 'node:24.6.0-bookworm-slim'})}<div class="setup-grid">${field('project-secret', 'Modal Secret name', {value: 'swarm-control-openai'})}${field('project-environment', 'Modal environment', {value: 'main'})}${field('gateway-app', 'Gateway application', {value: 'swarm-control-gateway'})}${field('worker-app', 'Worker application', {value: 'swarm-control-workers'})}${field('gateway-concurrency', 'Gateway concurrent requests', {type: 'number', value: '20', attributes: 'min="1" max="100" step="1"'})}</div></div></details><p class="setup-warning">This creates a disabled local profile with zero allowance. Set an authorized cumulative allowance in Project settings → Execution allowance.</p>${review('project-confirm', 'I reviewed these commands and instructions for this repository.')}<div class="setup-actions">${submit('Create local project profile')}</div></form>
    </section>
    <section id="setup-panel-3" role="tabpanel" aria-labelledby="setup-tab-3" hidden><h3>Give each agent a clear task</h3><p class="setup-lead">A task needs a real issue, a precise outcome, owned paths and any prerequisites. Importing an issue creates a draft for you to complete.</p>${pathContext}
      <article class="setup-card"><h4>Start from existing GitHub issues</h4><form id="setup-import-form" class="setup-form">${field('issues', 'Issue numbers', {placeholder: '12, 18, 24', help: 'Comma-separated issue numbers from the selected repository.'})}<div class="setup-actions">${submit('Import issue drafts', true)}</div></form><div id="setup-issue-drafts" class="setup-form"></div></article>
      <form id="setup-task-form" class="setup-form"><div class="setup-grid">${field('task-id', 'Task ID', {placeholder: 'T01', attributes: 'pattern="[A-Z][A-Z0-9_-]{1,39}" maxlength="40"'})}${field('task-issue', 'GitHub issue number', {type: 'number', attributes: 'min="1" step="1"'})}</div>${field('task-title', 'Short task title', {placeholder: 'Reject malformed search input'})}${field('task-prompt', 'What should the agent implement?', {textarea: true, rows: 4, placeholder: 'Describe the contract, expected behavior and relevant context.'})}${field('task-ownership', 'Owned paths · one per line', {textarea: true, placeholder: 'src/search\ntests/search', help: 'Keep work inside these paths. Shared paths reduce how much work can run in parallel.'})}${field('task-acceptance', 'Acceptance criteria · one per line', {textarea: true, placeholder: 'Malformed input returns the documented error.\nExisting valid searches keep working.'})}${field('task-dependencies', 'Prerequisite task IDs', {required: false, placeholder: 'T02, T03', help: 'These tasks must be merged and verified before this task can run.'})}<details class="setup-disclosure"><summary>Behavioral checks and artifact relationships</summary><div class="setup-form">${field('task-checks', 'Acceptance command argument arrays', {textarea: true, required: false, placeholder: '[["python3", "tests/acceptance_search.py"]]'})}${field('task-protected', 'Protected acceptance files · one per line', {textarea: true, required: false, help: 'These must match the frozen base before and after validation.'})}<div class="setup-grid">${field('task-produces', 'Produces · one artifact ID per line', {textarea: true, required: false, placeholder: 'contract:search-v1'})}${field('task-consumes', 'Consumes · one artifact ID per line', {textarea: true, required: false, placeholder: 'contract:session-v1'})}</div></div></details>${check('task-overwrite', 'Replace an existing task with this ID after reviewing its current contents.')}<div class="setup-actions">${submit('Save local task')}</div></form>
    </section>
    <section id="setup-panel-4" role="tabpanel" aria-labelledby="setup-tab-4" hidden><h3>Check locally, then review what to publish</h3><p class="setup-lead">Workers need a pushed base containing their instructions and tasks. Run your checks first, inspect the exact preparation changes, then decide whether to commit and push.</p>${pathContext}
      <form id="setup-check-form" class="setup-form">${review('check-confirm', 'I reviewed the project install/check commands and want to run them on this computer.')}<div class="setup-actions">${submit('Run project checks')}</div></form>
      <article class="setup-card"><h4>Publish the reviewed preparation</h4><form id="setup-publish-preview-form" class="setup-form"><div class="setup-actions">${submit('Preview preparation changes', true)}</div></form><div id="setup-publish-review" hidden><p class="setup-copy">Review the listed paths. This approval belongs to this exact preview; changing files requires another preview.</p><ul id="setup-changed-files" class="setup-copy"></ul><form id="setup-publish-form" class="setup-form">${field('publish-message', 'Commit message', {value: 'Prepare project for swarm tasks'})}${review('publish-confirm', 'Commit the reviewed preparation files and push them to the configured GitHub repository.')}<div class="setup-actions">${submit('Commit and push preparation')}</div></form></div></article>
    </section>
    <section id="setup-panel-5" role="tabpanel" aria-labelledby="setup-tab-5" hidden><h3>Choose your allowance before deploying</h3><p class="setup-lead">Enter the cumulative limits you are authorized to use. These limits are admission controls, not a provider invoice or an account balance.</p>${pathContext}
      <form id="setup-budget-form" class="setup-form"><div class="setup-grid">${field('model-budget', 'Cumulative model allowance · USD', {type: 'number', value: '0.00', attributes: 'min="0" max="10000" step="0.01"'})}${field('infra-budget', 'Cumulative infrastructure reserve · USD', {type: 'number', value: '0.00', attributes: 'min="0" max="10000" step="0.01"'})}${field('max-agents', 'Maximum live agents', {type: 'number', value: '10', attributes: 'min="1" max="100" step="1"'})}</div>${check('live-enabled', 'Enable paid operations for this project. Both allowances must be positive.')}${review('budget-confirm', 'I authorize the exact cumulative limits above. Existing allocations must be preserved.')}<p class="setup-help">An existing ledger may require the explicit migration workflow. The server will refuse an incompatible profile change; it will not reset past spending.</p><div class="setup-actions">${submit('Save authorized limits')}</div></form>
      <article class="setup-card"><h4>Deploy your model gateway</h4><p class="setup-copy">Deploy uses your connected Modal workspace and the project’s named OpenAI secret. Modal may charge for builds, storage and compute. It does not launch a coding run.</p><form id="setup-deploy-form" class="setup-form">${review('deploy-confirm', 'I reviewed this project, connected accounts and allowance, and authorize this potentially billed gateway deployment.')}<div class="setup-actions">${submit('Deploy gateway')}</div></form></article>
      <article class="setup-card"><h4>Check the deployed gateway</h4><form id="setup-probe-form" class="setup-form">${field('gateway-url', 'Gateway HTTPS URL', {type: 'url', placeholder: 'https://your-gateway.modal.run'})}${review('probe-confirm', 'I authorize the gateway probe and its possible provider/compute usage under the project allowance.')}<div class="setup-actions">${submit('Probe gateway', true)}</div></form></article><p class="setup-help">A verified connection or a successful probe does not establish quota for 100 workers. Review capacity and the independent-task frontier before launching.</p>
    </section>
    <section id="setup-panel-6" role="tabpanel" aria-labelledby="setup-tab-6" hidden><h3>Run controller commands from here</h3><p class="setup-lead">Choose an operation, preview its exact arguments and execute it explicitly. The server accepts supported Swarm Control commands; this is not a system shell.</p>${pathContext}
      <article class="setup-card"><h4>Build a command</h4><div class="setup-form"><label class="setup-field" for="setup-cli-operation"><span>Operation</span><select id="setup-cli-operation"><option value="analyze">Analyze dependencies</option><option value="preflight">Check run preparation</option><option value="plan">Plan selected tasks</option><option value="ops status">Inspect operational status</option></select></label><div class="setup-grid">${field('cli-tasks', 'Task IDs · optional for analysis', {required: false, placeholder: 'T01, T02'})}${field('cli-workers', 'Concurrent agents', {type: 'number', value: '2', attributes: 'min="1" max="100" step="1"'})}</div>${field('cli-base', 'Base ref', {value: 'origin/main'})}<div class="setup-actions"><button id="setup-build-command" class="button button-quiet" type="button">Use this command</button><button id="setup-open-builder" class="button button-quiet" type="button">Open run builder</button></div></div></article>
      <form id="setup-cli-preview-form" class="setup-form">${field('cli-command', 'Swarm Control command', {textarea: true, rows: 3, placeholder: 'swarm-control analyze --project /path/to/project/swarm.toml', help: 'Enter a supported controller command. Configure tokens in Settings, never in this field.'})}<div class="setup-actions">${submit('Preview command')}</div></form><div id="setup-cli-review" hidden><pre id="setup-cli-argv" class="setup-command" aria-label="Reviewed command arguments"></pre><form id="setup-cli-execute-form" class="setup-form">${review('cli-confirm', 'I reviewed these exact arguments and authorize their stated side effects and any configured cost.')}<div class="setup-actions">${submit('Execute reviewed command')}</div></form></div><pre id="setup-cli-output" class="setup-command" aria-live="polite" hidden></pre>
    </section>
    </div></div>
    <details class="setup-history"><summary>Setup activity · local operation history</summary><ul id="setup-jobs" class="setup-job-list"><li class="setup-empty">No setup operations recorded yet.</li></ul><p class="setup-help">Mapped arguments and outcomes only. Credential values and private command logs are not shown here. An uncertain operation must be inspected before trying again.</p></details>`;

  function element(tag, className, content) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (content !== undefined) result.textContent = content;
    return result;
  }
  const createIssueCard = element('div', 'setup-card');
  createIssueCard.innerHTML = '<h4>Need a new GitHub issue?</h4><p class="setup-copy">Write the task title and implementation request above, then create its issue. This writes to the selected GitHub repository and fills the issue number below.</p>' + check('create-issue-confirm', 'Create a GitHub issue using the current task title and implementation request.') + '<div class="setup-actions"><button id="setup-create-issue" class="button button-quiet" data-setup-submit type="button">Create GitHub issue from this task</button></div>';
  byId('setup-task-prompt').closest('label').after(createIssueCard);
  const environmentCard = element('article', 'setup-card');
  environmentCard.innerHTML = '<div class="setup-card-title"><h4>Your personal local .env</h4><span class="setup-provider-status">Private to this computer</span></div><p class="setup-copy">Each person uses their own accounts and keys. Import a local .env file, or enter keys below. The server saves it with private permissions outside every Git repository. It is never committed or pushed to GitHub.</p><p id="setup-env-location" class="setup-help">The private .env location will appear when the local server provides it.</p><form id="setup-env-form" class="setup-form">' + field('env-file', 'Choose your .env file', {type: 'file', required: false, attributes: 'accept=".env,text/plain" data-setup-secret'}) + '<details class="setup-disclosure"><summary>Or enter individual keys</summary><div class="setup-form">' + field('env-github', 'GitHub token (GH_TOKEN) · optional', {type: 'password', required: false}) + field('env-modal-id', 'Modal token ID (MODAL_TOKEN_ID) · optional', {type: 'password', required: false}) + field('env-modal-secret', 'Modal token secret (MODAL_TOKEN_SECRET) · optional', {type: 'password', required: false}) + field('env-openai', 'OpenAI API key (CODEX_API_KEY) · optional', {type: 'password', required: false}) + '</div></details><p class="setup-help">Supported keys: GH_TOKEN, MODAL_TOKEN_ID, MODAL_TOKEN_SECRET and CODEX_API_KEY (OPENAI_API_KEY is also accepted). Saving here enables local planning without uploading the key to Modal. Create a worker secret separately in Project settings → Execution allowance.</p>' + review('env-confirm', 'Save these credentials only in my private local .env file.') + '<div class="setup-actions">' + submit('Save my local .env') + '</div></form>';
  byId('setup-panel-0').querySelector('.setup-lead').after(environmentCard);
  const modelKeyHelp=element('p','setup-help');const modelKeyLink=element('a','','Create an OpenAI API key');modelKeyLink.href='https://platform.openai.com/api-keys';modelKeyLink.target='_blank';modelKeyLink.rel='noopener noreferrer';modelKeyHelp.append(modelKeyLink,document.createTextNode(' · Use your own account’s API key to generate work plans.'));environmentCard.querySelector('.setup-copy').after(modelKeyHelp);
  const toolsCard = element('article', 'setup-card');
  toolsCard.innerHTML = '<h4>Tools on this computer</h4><p id="setup-tools-status" class="setup-copy">Checking local Git, Python and GitHub CLI availability…</p><form id="setup-install-github-form" class="setup-form" hidden><p class="setup-copy">GitHub CLI is needed to verify your GitHub account, manage issues and publish changes. Install a private copy from the official release without administrator access.</p>' + review('install-github-confirm', 'Download and install the official GitHub CLI release for this computer. No sudo or system-wide change.') + '<div class="setup-actions">' + submit('Install GitHub CLI locally') + '</div></form>';
  environmentCard.before(toolsCard);
  const retargetCard = element('article', 'setup-card'); retargetCard.id = 'setup-retarget-card'; retargetCard.hidden = true;
  retargetCard.innerHTML = '<h4>This fork needs its own project profile</h4><p id="setup-retarget-note" class="setup-copy"></p><form id="setup-retarget-form" class="setup-form">' + field('retarget-repository', 'Actual GitHub owner/repository') + review('retarget-confirm', 'Retarget this local profile to the actual repository, reset its service identities, and preserve tasks/history with zero allowance and live execution disabled.') + '<p class="setup-help">An existing budget ledger prevents this reset. Existing issue IDs are preserved for review; create or select issues belonging to your fork before running its tasks.</p><div class="setup-actions">' + submit('Retarget local profile to this fork') + '</div></form>';
  byId('setup-panel-1').append(retargetCard);
  const connectPrepared = element('button', 'button button-primary', 'Connect this prepared project to workspace'); connectPrepared.id = 'setup-connect-prepared'; connectPrepared.type = 'button'; connectPrepared.dataset.setupSubmit = ''; connectPrepared.hidden = true;
  byId('setup-repository-facts').after(connectPrepared);
  connectPrepared.addEventListener('click', () => {try {action('repository.connect', {path: projectPath()});} catch (error) {feedback(errorMessage(error), true);}});
  const existingProfile = element('details', 'setup-disclosure'); existingProfile.id = 'setup-existing-profile'; existingProfile.hidden = true;
  existingProfile.innerHTML = '<summary>Review existing project commands and instructions</summary><p class="setup-copy">These settings are read from the selected profile. They are preserved; continue to Tasks or review checks before running them.</p><pre id="setup-existing-profile-values" class="setup-command"></pre>';
  byId('setup-project-form').before(existingProfile);
  const checkProfile = element('details', 'setup-disclosure'); checkProfile.id = 'setup-check-profile'; checkProfile.hidden = true;
  checkProfile.innerHTML = '<summary>Review the configured commands before running</summary><pre id="setup-check-profile-values" class="setup-command"></pre>';
  byId('setup-check-form').before(checkProfile);
  const budgetHelp = byId('setup-budget-form').querySelector('.setup-help');
  budgetHelp.textContent = 'Existing allocations are preserved. An authorized increase uses a recorded policy migration; cumulative limits cannot be reduced or reset. Saving these settings changes your local profile: review and publish the updated preparation before launching workers.';
  const budgetReview = element('button', 'setup-secondary', 'Review updated preparation'); budgetReview.id = 'setup-budget-review'; budgetReview.type = 'button'; budgetReview.hidden = true;
  budgetReview.addEventListener('click', () => showStep(4, true)); byId('setup-budget-form').after(budgetReview);
  byId('setup-openai-key').required = false;
  byId('setup-openai-key').placeholder = 'Leave blank to use your saved local model key';
  byId('setup-openai-key').closest('label').append(element('small', '', 'Optional when your personal .env already contains CODEX_API_KEY or OPENAI_API_KEY. Creating the Modal Secret is a separate explicit upload.'));
  function accountLinks(formId, links, copy) {
    const paragraph = element('p', 'setup-help');
    links.forEach(([label, href], index) => {if (index) paragraph.append(document.createTextNode(' · ')); const link = element('a', '', label); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; paragraph.append(link);});
    if (copy) paragraph.append(document.createElement('br'), document.createTextNode(copy));
    byId('setup-' + formId).closest('article').querySelector('.setup-copy').after(paragraph);
  }
  accountLinks('github-form', [['Create a GitHub token', 'https://github.com/settings/personal-access-tokens/new'], ['GitHub token help', 'https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens']], 'Limit access to the target repository. Source access needs repository/Contents read; issue setup needs Issues read/write. Publishing needs Contents write, PR integration needs Pull requests write, and workflow changes need their own workflow permissions. Identity verification does not prove all these permissions.');
  accountLinks('modal-form', [['Open Modal token settings', 'https://modal.com/settings/tokens'], ['Modal workspace/token help', 'https://modal.com/docs/guide/workspaces#create-a-token-for-a-workspace']], 'Create a token for the workspace you intend to use, then enter its ID and secret here.');
  accountLinks('openai-form', [['Create an OpenAI API key', 'https://platform.openai.com/api-keys']], 'Use a key from your own account. Saving a local key and creating its Modal Secret are separate actions.');
  byId('setup-task-overwrite').closest('label').querySelector('span').textContent = 'Update my unpublished draft with this ID. Versioned tasks and inherited task history are preserved.';
  mount.querySelector('.setup-header > div').append(byId('setup-feedback'));
  function redact(value) {
    return String(value).replace(/\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{15,})\b/g, '[credential hidden]')
      .replace(/(?:bearer\s+)[^\s]+/gi, 'Bearer [hidden]').slice(0, 16000);
  }
  function feedback(message, error = false) {
    const target = byId('setup-feedback'); target.textContent = message; target.dataset.error = String(error);
    const repositoryFeedback=byId('repository-feedback');if(repositoryFeedback){repositoryFeedback.textContent=message;repositoryFeedback.dataset.error=String(error);}
  }
  function value(id) {return byId('setup-' + id).value.trim();}
  function checked(id) {return byId('setup-' + id).checked;}
  function lines(id) {return value(id).split(/\r?\n/).map((item) => item.trim()).filter(Boolean);}
  function ids(id) {return value(id).split(/[\s,]+/).filter(Boolean);}
  function commands(id, optional = false) {
    const raw = value(id); if (optional && !raw) return null;
    let parsed; try {parsed = JSON.parse(raw);} catch {throw new Error('COMMAND_ARRAYS');}
    if (!Array.isArray(parsed) || !parsed.length || parsed.length > 16 || parsed.some((command) => !Array.isArray(command) || !command.length || command.some((part) => typeof part !== 'string' || !part || part.includes('\0')))) throw new Error('COMMAND_ARRAYS');
    return parsed;
  }
  function projectPath() {
    if (!state.path) throw new Error('PROJECT_PATH');
    return state.path;
  }
  function selectPath(path, repository, recommendations = {}, configuredRuntime = {}) {
    if (typeof path === 'string' && path.length <= 4096) {if (state.path !== path) {state.projectId = null;state.boundProjectId=null;} state.path = path;}
    if (typeof repository === 'string') state.repository = repository;
    for (const node of document.querySelectorAll('[data-setup-path]')) node.textContent = state.path || 'No folder selected yet. Add one in Projects.';
    if (state.path) byId('setup-local-path').value = state.path;
    if (state.repository) {
      byId('setup-project-repository').value = state.repository;
      if (state.runtimeOverrides.has('')) {
        state.runtimeOverrides.set(state.repository, state.runtimeOverrides.get(''));
        state.runtimeOverrides.delete('');
      }
      const overrides = state.runtimeOverrides.get(state.repository) || {};
      for (const [id, key] of [['gateway-app', 'gateway_app'], ['worker-app', 'worker_app']]) {
        const input = byId('setup-' + id);
        const selected = configuredRuntime[key] || overrides[key] || recommendations[key] || '';
        input.value = /^[A-Za-z0-9_-]{1,100}$/.test(selected) ? selected : '';
      }
    }
    for (const id of ['project-confirm', 'check-confirm', 'publish-confirm', 'budget-confirm', 'deploy-confirm', 'probe-confirm', 'cli-confirm', 'create-issue-confirm']) byId('setup-' + id).checked = false;
    state.publishBinding = null; byId('setup-publish-review').hidden = true;
    state.cliBinding = null; byId('setup-cli-review').hidden = true;
  }
  function updateButtons() {
    for(const button of document.querySelectorAll('[data-setup-submit]')) button.disabled=!state.available||!state.csrf||state.busy;
    window.dispatchEvent(new CustomEvent('swarm:setup-status',{detail:{available:state.available,busy:state.busy,connections:state.connections}}));
  }
  function showStep(index,focus=false,fromRoute=false) {
    state.step=Math.max(0,Math.min(steps.length-1,index));
    steps.forEach((_,position)=>{const active=position===state.step,tab=byId('setup-tab-'+position);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;byId('setup-panel-'+position).hidden=!active;});
    if(!fromRoute){const route=state.step===0?'settings':state.step===1?'projects':'project-settings';window.SwarmNavigation?.navigate(route,{step:['','','profile','tasks','checks','budget','advanced'][state.step]});}
    updateButtons();if(focus&&state.step>=2)byId('setup-tab-'+state.step).focus();
  }
  async function request(path, options = {}) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
        headers: {'Accept': 'application/json', ...(options.method ? {'Content-Type': 'application/json', 'X-Swarm-CSRF': state.csrf || ''} : {})}, ...options});
      if (!response.ok) {const error = new Error('HTTP_' + response.status); error.status = response.status; throw error;}
      return await response.json();
    } finally {clearTimeout(timer);}
  }
  function connectionStatus(data) {
    state.connections = data && typeof data === 'object' ? data : {};
    for (const name of ['github', 'modal', 'openai']) {
      const source = state.connections[name] || {}, target = mount.querySelector('[data-setup-provider="' + name + '"]');
      if(name==='openai'&&source.verified===true&&(source.secret_name!==value('secret-name')||source.environment!==value('secret-environment'))){target.dataset.state='unknown';target.textContent='Selected project secret not verified · last verified: '+redact(source.secret_name||'another secret');continue;}
      const status = source.verified === true ? 'verified' : source.configured === true ? 'configured' : 'unknown';
      target.dataset.state = status; target.textContent = status === 'verified' ? 'Verified' : status === 'configured' ? 'Configured · not verified' : 'Not verified';
      if (name === 'openai' && source.verified === true && source.api_key_verified === false) target.textContent = 'Modal Secret verified · model key not tested';
      if (name === 'openai' && source.local_key_configured === true && source.verified !== true) {target.dataset.state = 'configured'; target.textContent = 'Local key saved · secret not verified';}
      if (name === 'openai' && typeof source.secret_name === 'string') target.textContent += ' · ' + redact(source.secret_name).slice(0, 100);
      if (source.identity && typeof source.identity === 'string') target.textContent += ' · ' + redact(source.identity).slice(0, 100);
    }
    const storage = state.connections.storage || {};
    const location = storage.safeenvpath || storage.safe_env_path || storage.dotenv_path || storage.path;
    byId('setup-env-location').textContent = typeof location === 'string' ? 'Private local .env: ' + redact(location) : 'Credentials stay on this computer in the server’s private configuration directory.';
  }
  function errorMessage(error) {
    const code = typeof error === 'string' ? error : error && (error.code || error.message);
    const mapped = {COMMAND_ARRAYS: 'Enter commands as JSON argument arrays, for example [["npm", "run", "check"]].', PROJECT_PATH: 'Select a local repository folder in Projects → Add repository first.',
      ISSUE_NUMBERS: 'Enter positive issue numbers separated by commas.', BUDGET: 'Enter exact dollar amounts with at most two decimal places.', CLI_CHANGED: 'The command changed after preview. Preview the new command before execution.',
      PUBLISH_PREVIEW: 'Preview the preparation changes for this selected folder before publishing.', HTTP_403: 'The local session expired or this request is unavailable. Refresh setup before trying again.',
      config_exists: 'This repository already has a project profile. It was preserved; inspect it rather than creating another.',
      missing_instruction: 'One of the selected instruction files is missing. Choose files that exist in this repository.',
      origin_mismatch: 'The owner/repository must match this checkout’s actual GitHub origin.',
      preparation_changed: 'Preparation files changed. Review their current contents and prepare a fresh publication preview.',
      issue_not_open: 'Choose a real open issue in the selected repository. Pull requests are not task issues.',
      setup_busy: 'Another setup operation is running. Wait for its recorded result.',
      confirmation_required: 'Review and confirm the displayed operation before continuing.',
      storage_unsafe: 'The credential storage location is unsafe. Choose a private workspace outside Git repositories before saving keys.',
      storage_unavailable: 'Private credentials could not be saved. Check access to the local settings directory.',
      modal_not_verified: 'Verify your intended Modal workspace in Settings before creating a worker gateway secret.',
      invalid_credential: 'Enter a valid single-line credential, or import a file containing only the supported key names.',
      HTTP_409: 'Another operation or an existing attempt needs attention. Inspect setup history before trying again.', HTTP_404: 'This setup action is unavailable on this server. Restart Easy Run with the updated controller.',
      HTTP_400: 'The server rejected this operation. Check the selected folder, field formats and operation requirements.', HTTP_413: 'The request is too large. Use a smaller task or issue selection.'};
    return mapped[code] || 'The operation did not finish successfully. Check the selected project, connection status and required fields. Private details remain on the local server.';
  }
  function safeFacts(result) {
    if (!result || typeof result !== 'object') return [];
    const allowed = {path: 'Folder', repository: 'Repository', branch: 'Branch', base_branch: 'Base branch', provider: 'Provider', identity: 'Account', configured: 'Configured', verified: 'Verified', task_id: 'Task', id: 'Task', number: 'Issue', task_count: 'Tasks', imported_count: 'Drafts imported', exit_code: 'Exit code', passed: 'Passed', checks_passed: 'Checks passed', source_ready: 'Source ready', launch_ready: 'Launch ready', secret_name: 'Modal Secret', environment: 'Environment', profile_path: 'Profile', gateway_url: 'Gateway', max_live_agents: 'Maximum live agents', ledger_migrated: 'Policy migration recorded', gateway_redeployment_required: 'Gateway redeployment required'};
    const facts = [];
    for (const [key, label] of Object.entries(allowed)) {
      const content = result[key];
      if (['string', 'number', 'boolean'].includes(typeof content)) facts.push(label + ': ' + redact(content).slice(0, 600));
    }
    return facts;
  }
  function safeJob(job, fallbackAction) {
    if (!job || typeof job !== 'object' || typeof job.id !== 'string') throw new Error('JOB');
    const unsuccessful = job.result && (job.result.checks_passed === false || Number.isInteger(job.result.exit_code) && job.result.exit_code !== 0);
    const status = job.status === 'finished' && job.result ? (unsuccessful ? 'failed' : 'succeeded') : job.status;
    return {id: job.id.slice(0, 128), action: titles[job.action] ? job.action : fallbackAction,
      status: typeof status === 'string' && /^[a-z_]{1,30}$/.test(status) ? status : 'unknown',
      requested_at: typeof job.created_at_utc === 'string' ? job.created_at_utc : typeof job.requested_at_utc === 'string' ? job.requested_at_utc : typeof job.started_at_utc === 'string' ? job.started_at_utc : '',
      facts: safeFacts(job.result), error: job.error ? errorMessage({code: job.error_code}) : null};
  }
  function renderJobs() {
    const jobs=[...state.jobs.values()].filter(job=>!job.action.startsWith('planning.')).slice(-20).reverse();
    renderJobList(byId('setup-jobs'),jobs);
    renderJobList(byId('repository-jobs'),jobs.filter(job=>job.action.startsWith('repository.')));
  }
  function renderJobList(target,jobs) {
    if(!target)return;
    target.replaceChildren();
    if (!jobs.length) {target.append(element('li', 'setup-empty', 'No setup operations recorded yet.')); return;}
    for (const job of jobs) {
      const item = element('li', 'setup-job'), heading = element('div', 'setup-job-heading');
      heading.append(element('strong', '', titles[job.action] || 'Setup operation'));
      const status = element('span', 'setup-job-state', job.status.replaceAll('_', ' ')); status.dataset.state = job.status; heading.append(status); item.append(heading);
      if (job.requested_at) item.append(element('time', '', redact(job.requested_at)));
      if (job.facts.length) {const facts = element('div', 'setup-job-facts'); for (const fact of job.facts) facts.append(element('span', '', fact)); item.append(facts);}
      if (job.error) item.append(element('p', 'setup-copy', job.error));
      const inspect = element('button', 'setup-secondary', 'Inspect recorded result'); inspect.type = 'button';
      inspect.addEventListener('click', async () => {
        inspect.disabled = true;
        try {const data = await request('/api/setup/job?id=' + encodeURIComponent(job.id)); const current = safeJob(data.job || data, job.action); state.jobs.set(current.id, current); renderJobs();}
        catch {feedback('The recorded result is unavailable. Preserve this attempt and inspect local operation history before retrying.', true); inspect.disabled = false;}
      });
      item.append(inspect);
      target.append(item);
    }
  }
  async function refreshSetup(quiet = false) {
    try {
      const [session, data] = await Promise.all([request('/api/session'), request('/api/setup')]);
      state.csrf = typeof session.csrf_token === 'string' ? session.csrf_token : null;
      state.available = Boolean(state.csrf); connectionStatus(data.connections || data);
      renderTools(data.tools);
      state.busy = data.busy === true;
      for (const item of Array.isArray(data.operations) ? data.operations.slice(-20) : []) {
        try {const job = safeJob(item, item.action); const previous = state.jobs.get(job.id); if (!('result' in item) && previous) {job.facts = previous.facts; if (item.status === 'finished') job.status = previous.status;} state.jobs.set(job.id, job); if (!terminal.has(job.status)) pollJob(job.id, job.action);} catch { /* Invalid history entries do not become successful operations. */ }
      }
      renderJobs(); updateButtons();
      if (data.busy === true && !(Array.isArray(data.operations) && data.operations.some((job) => !terminal.has(job.status)))) setTimeout(() => refreshSetup(true), 1000);
      if (!quiet) feedback(state.available ? 'Local setup is ready. Choose the account or project action you need; nothing starts automatically.' : 'Open Easy Run in workspace mode to enable setup operations.', !state.available);
    } catch {
      state.available = false; state.csrf = null; updateButtons();
      if (!quiet) feedback('Setup is unavailable on this server. Restart Easy Run with the updated controller; existing evidence remains available.', true);
    }
  }
  function uuid() {return crypto.randomUUID().replaceAll('-', '');}
  async function action(name,options) {
    if(state.busy||!state.csrf||!state.available){const error=new Error('Wait for the active operation or refresh the controller session.');feedback(error.message,true);return {ok:false,error};}
    const requestId=uuid(),selectionVersion=state.selectionVersion;
    const body=JSON.stringify({action:name,options,confirmed:true,request_id:requestId});
    for(const input of mount.querySelectorAll('[data-setup-secret]'))input.value='';
    state.busy=true;updateButtons();feedback((titles[name]||'Operation')+' requested.');
    let raw=null;
    try {
      const data=await request('/api/setup/action',{method:'POST',body});raw=data.job;
      let job=safeJob(raw,name);state.jobContexts.set(job.id,selectionVersion);state.jobs.set(job.id,job);state.polling.add(job.id);renderJobs();
      let failures=0;
      while(!terminal.has(job.status)){
        await new Promise(resolve=>setTimeout(resolve,800));
        try{const next=await request('/api/setup/job?id='+encodeURIComponent(job.id));raw=next.job||next;job=safeJob(raw,name);state.jobs.set(job.id,job);renderJobs();failures=0;}
        catch(error){if(++failures>=5){const uncertain=new Error('The operation outcome is unknown. Inspect its recorded request before retrying.');uncertain.code='operation_uncertain';uncertain.result=raw?.result;uncertain.job_id=job.id;state.polling.delete(job.id);throw uncertain;}}
      }
      // The job may have become terminal just before its serving thread exits.
      // Observe the read-only busy gate before enabling another explicit action.
      let idle=false;
      for(let index=0;index<25;index+=1){const status=await request('/api/setup');if(status.busy!==true){idle=true;break;}await new Promise(resolve=>setTimeout(resolve,200));}
      if(!idle){const error=new Error('The operation is still finalizing. Inspect its recorded result before repeating it.');error.code='operation_uncertain';error.job_id=job.id;throw error;}
      const final=await request('/api/setup/job?id='+encodeURIComponent(job.id));raw=final.job||final;job=safeJob(raw,name);state.jobs.set(job.id,job);
      state.polling.delete(job.id);await finishJob(raw,name);
      if(!['finished','succeeded','completed'].includes(job.status)){const error=new Error(errorMessage({code:raw.error_code}));error.code=raw.error_code;error.result=raw.result;error.job_id=job.id;return {ok:false,error,job:raw};}
      return {ok:true,result:raw.result||{},job:raw};
    }catch(error){
      const uncertain=!error.status;state.jobs.set(requestId,{id:requestId,action:name,status:uncertain?'uncertain':'failed',requested_at:'',facts:[],error:uncertain?'The request may have started. Inspect operation history before repeating it.':errorMessage(error)});renderJobs();feedback(error.message||'Operation unavailable.',true);return {ok:false,error};
    }finally{state.busy=false;updateButtons();}
  }
  function pollJob(id, name) {
    if (state.polling.has(id)) return;
    state.polling.add(id); let failures = 0;
    const tick = async () => {
      try {
        const data = await request('/api/setup/job?id=' + encodeURIComponent(id));
        const raw = data.job || data, job = safeJob(raw, name); state.jobs.set(job.id, job); renderJobs(); failures = 0;
        if (terminal.has(job.status)) {state.polling.delete(id); await finishJob(raw, name); return;}
      } catch {
        failures += 1;
        if (failures >= 5) {state.polling.delete(id); state.busy = false; updateButtons(); feedback('Progress is unavailable. The operation may still be running; inspect recorded history before trying again.', true); return;}
      }
      setTimeout(tick, 1500);
    };
    setTimeout(tick, 800);
  }
  async function finishJob(job, name) {
    state.busy = false; updateButtons();
    if (!['finished', 'succeeded', 'completed'].includes(job.status)) {feedback(errorMessage({code: job.error_code}), true); return;}
    const result = job.result && typeof job.result === 'object' ? job.result : {};
    if(name.startsWith('planning.')){feedback((titles[name]||'Planning operation')+' completed.');await refreshSetup(true);return;}
    if(state.jobContexts.get(job.id)!==state.selectionVersion){feedback('A background operation completed. Its recorded result is available in activity.');return;}
    if (!['cli.preview'].includes(name)) {state.cliBinding = null; byId('setup-cli-review').hidden = true;}
    if (['tasks.save', 'project.configure-budget', 'project.check', 'repository.retarget', 'project.initialize'].includes(name)) {state.publishBinding = null; byId('setup-publish-review').hidden = true;}
    if (['repository.inspect', 'repository.clone', 'project.initialize', 'repository.retarget'].includes(name)) {
      selectPath(result.path || result.root || state.path, result.repository || state.repository,
        result.recommended_runtime, result.configuration?.runtime);
      if (result.base_branch) {byId('setup-base-branch').value = result.base_branch; byId('setup-cli-base').value = 'origin/' + result.base_branch;}
      renderRepositoryFacts(result);
    }
    const connectedId = result.connection?.project_id || result.project_id;
    if (typeof connectedId === 'string') state.projectId = connectedId;
    if(['repository.inspect','repository.clone','repository.connect','project.initialize','repository.retarget'].includes(name)){state.selectionVersion+=1;window.dispatchEvent(new CustomEvent('swarm:repository-selected',{detail:{path:state.path,repository:state.repository,project_id:state.projectId}}));}
    if (name === 'tasks.import') renderDrafts(result.drafts || result.tasks || []);
    if (name === 'tasks.create-issue' && Number.isInteger(result.number) && result.number > 0) {
      byId('setup-task-issue').value = String(result.number); byId('setup-create-issue-confirm').checked = false;
    }
    if (name === 'openai.configure' && typeof result.secret_name === 'string') byId('setup-project-secret').value = result.secret_name;
    if (name === 'project.deploy' && typeof result.gateway_url === 'string') byId('setup-gateway-url').value = result.gateway_url;
    if (name === 'project.configure-budget') byId('setup-budget-review').hidden = false;
    if (name === 'preparation.preview') renderPreparation(result);
    if (name === 'cli.preview') renderCommandPreview(result);
    if (name === 'cli.execute') {
      const target = byId('setup-cli-output'); target.hidden = false;
      target.textContent = 'Exit code: ' + (Number.isInteger(result.exit_code) ? result.exit_code : 'unknown') + (typeof result.output === 'string' ? '\n\n' + redact(result.output) : '\nNo public command output returned.');
      state.cliBinding = null; byId('setup-cli-review').hidden = true;
    }
    if (name === 'preparation.publish') {state.publishBinding = null; byId('setup-publish-review').hidden = true;}
    const failed = result.checks_passed === false || Number.isInteger(result.exit_code) && result.exit_code !== 0;
    feedback((titles[name] || 'Operation') + (failed ? ' returned a failed check or nonzero exit code. Review its recorded outcome below.' : ' completed. Review its recorded outcome below.'), failed);
    if(['repository.connect','project.initialize','repository.retarget','tasks.save','project.configure-budget','preparation.publish'].includes(name))window.dispatchEvent(new CustomEvent('swarm:setup-changed',{detail:{project_id:state.projectId}}));
    await refreshSetup(true);
  }
  function renderRepositoryFacts(result) {
    const target = byId('setup-repository-facts'); target.replaceChildren();
    for (const fact of safeFacts(result)) {const split = fact.indexOf(':'); target.append(element('dt', '', fact.slice(0, split)), element('dd', '', fact.slice(split + 1).trim()));}
    target.hidden = !target.childElementCount;
    byId('setup-connect-prepared').hidden = result.configuration_valid !== true;
    const mismatch = result.configured === true && (result.origin_matches_configuration === false || typeof result.configured_repository === 'string' && result.configured_repository !== result.repository);
    byId('setup-retarget-card').hidden = !mismatch;
    if (mismatch) {
      byId('setup-retarget-note').textContent = 'The checkout points to ' + redact(result.repository || state.repository) + ', while its profile names ' + redact(result.configured_repository || 'another repository') + '. Review the actual origin before resetting this fork’s local settings.';
      byId('setup-retarget-repository').value = result.repository || state.repository; byId('setup-retarget-confirm').checked = false;
    }
    renderExistingProfile(result);
  }
  function renderTools(tools) {
    const source = tools && typeof tools === 'object' ? tools : {};
    byId('setup-tools-status').textContent = [['git', 'Git'], ['python', 'Python'], ['github_cli', 'GitHub CLI']].map(([key, label]) => label + ': ' + (source[key]?.available === true ? 'available' : source[key]?.available === false ? 'missing' : 'not checked')).join(' · ');
    byId('setup-install-github-form').hidden = source.github_cli?.available !== false;
  }
  function renderExistingProfile(result) {
    const profile = result.configuration || result.profile;
    const configured = result.configured === true;
    byId('setup-project-form').hidden = configured;
    byId('setup-existing-profile').hidden = !configured;
    if (!configured) {byId('setup-check-profile').hidden = true; return;}
    let projection = null;
    if (profile && typeof profile === 'object') {
      const validation = profile.validation || profile, project = profile.project || profile;
      projection = {install: validation.install, checks: validation.checks, instructions: project.instructions, required_checks: validation.required_checks};
      const runtime = profile.runtime || {}, budget = profile.budget || {};
      if (typeof runtime.openai_secret === 'string') {byId('setup-secret-name').value = runtime.openai_secret; byId('setup-project-secret').value = runtime.openai_secret;}
      if (typeof runtime.environment === 'string') {byId('setup-secret-environment').value = runtime.environment; byId('setup-project-environment').value = runtime.environment;}
      for (const [fieldName, key] of [['model-budget', 'model_limit_cents'], ['infra-budget', 'infra_limit_cents']]) if (Number.isInteger(budget[key]) && budget[key] >= 0) byId('setup-' + fieldName).value = (budget[key] / 100).toFixed(2);
      if (Number.isInteger(budget.max_live_agents)) byId('setup-max-agents').value = String(budget.max_live_agents);
      if (typeof budget.live_enabled === 'boolean') byId('setup-live-enabled').checked = budget.live_enabled;
      if (Object.keys(runtime).length) projection.runtime = {image: runtime.image, openai_secret: runtime.openai_secret, environment: runtime.environment, gateway_concurrency: runtime.gateway_concurrency};
      if (Object.keys(budget).length) projection.budget = budget;
    }
    const content = projection && Object.values(projection).some((item) => item !== undefined) ? redact(JSON.stringify(projection, null, 2)) : 'An existing profile was found and preserved. Inspect the project configuration before running its commands.';
    byId('setup-existing-profile-values').textContent = content;
    byId('setup-check-profile-values').textContent = content;
    byId('setup-check-profile').hidden = false;
  }
  function renderDrafts(drafts) {
    state.drafts = Array.isArray(drafts) ? drafts.slice(0, 100) : [];
    const target = byId('setup-issue-drafts'); target.replaceChildren();
    if (!state.drafts.length) {target.append(element('p', 'setup-copy', 'No issue drafts returned.')); return;}
    target.append(element('p', 'setup-copy', 'Choose a draft, fill in owned paths and acceptance criteria, then save it explicitly.'));
    state.drafts.forEach((draft, index) => {
      const button = element('button', 'setup-secondary', 'Load #' + (draft.issue || draft.number || '?') + ' · ' + redact(draft.title || 'Issue draft').slice(0, 180)); button.type = 'button';
      button.addEventListener('click', () => loadDraft(index)); target.append(button);
    });
  }
  function loadDraft(index) {
    const draft = state.drafts[index]; if (!draft) return;
    for (const [fieldName, key] of [['task-id', 'id'], ['task-title', 'title'], ['task-prompt', 'prompt']]) byId('setup-' + fieldName).value = typeof draft[key] === 'string' ? draft[key] : '';
    byId('setup-task-issue').value = draft.issue || draft.number || '';
    byId('setup-task-ownership').value = ''; byId('setup-task-acceptance').value = '';
    byId('setup-task-dependencies').value = ''; byId('setup-task-checks').value = ''; byId('setup-task-protected').value = '';
    byId('setup-task-overwrite').checked = false; byId('setup-task-id').focus();
    feedback('Draft loaded. Define actual owned paths, acceptance and prerequisites before saving.');
  }
  function renderPreparation(result) {
    const binding = result.binding || result.review_binding;
    if (!binding) {feedback('The server did not return an exact preparation binding. Inspect the selected repository before publishing.', true); return;}
    state.publishBinding = binding; state.publishPath = state.path;
    const target = byId('setup-changed-files'); target.replaceChildren();
    const files = result.changed_files || result.files || [];
    for (const item of Array.isArray(files) ? files.slice(0, 200) : []) {
      const path = typeof item === 'string' ? item : item && item.path;
      if (typeof path === 'string') target.append(element('li', '', redact(path).slice(0, 600)));
    }
    if (!target.childElementCount) target.append(element('li', '', 'No file paths returned. Review the server preparation outcome before publishing.'));
    byId('setup-publish-confirm').checked = false; byId('setup-publish-review').hidden = false;
  }
  function renderCommandPreview(result) {
    if (!Array.isArray(result.argv) || !result.argv.every((item) => typeof item === 'string') || !result.review_binding) {feedback('The server did not return a bound command preview. Execution stays unavailable.', true); return;}
    state.cliBinding = result.review_binding; state.cliCommand = value('cli-command');
    byId('setup-cli-argv').textContent = result.argv.map((argument) => quote(redact(argument))).join(' ') + (typeof result.summary === 'string' ? '\n\n' + redact(result.summary) : '');
    byId('setup-cli-confirm').checked = false; byId('setup-cli-review').hidden = false;
  }
  function quote(argument) {return /^[A-Za-z0-9_./:@=-]+$/.test(argument) ? argument : "'" + argument.replaceAll("'", "'\\''") + "'";}
  function bindForm(id, name, options) {
    byId('setup-' + id).addEventListener('submit', (event) => {
      event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
      try {const selected = options(); action(name, selected);} catch (error) {feedback(errorMessage(error), true);}
    });
  }
  function dollars(id) {const raw = value(id); if (!/^\d+(?:\.\d{1,2})?$/.test(raw)) throw new Error('BUDGET'); return Math.round(Number(raw) * 100);}
  bindForm('github-form', 'github.configure', () => ({token: value('github-token'), persist: checked('github-persist')}));
  bindForm('modal-form', 'modal.configure', () => ({token_id: value('modal-id'), token_secret: value('modal-secret'), persist: checked('modal-persist')}));
  bindForm('openai-form', 'openai.configure', () => ({...(value('openai-key') ? {api_key: value('openai-key')} : {}), secret_name: value('secret-name'), environment: value('secret-environment'), replace: checked('secret-replace')}));
  byId('setup-env-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
    const input = byId('setup-env-file'), file = input.files && input.files[0];
    const keys = [['GH_TOKEN', 'env-github'], ['MODAL_TOKEN_ID', 'env-modal-id'], ['MODAL_TOKEN_SECRET', 'env-modal-secret'], ['CODEX_API_KEY', 'env-openai']];
    const selected = keys.filter(([, id]) => value(id));
    if (file && selected.length) {feedback('Choose a .env file or enter individual keys, then save one source at a time.', true); return;}
    if (!file && !selected.length) {feedback('Choose your .env file or enter at least one supported key.', true); return;}
    if (file && file.size > 32768) {input.value = ''; feedback('Choose a .env file smaller than 32 KiB.', true); return;}
    if (selected.some(([, id]) => /[\r\n\0]/.test(value(id)))) {feedback('Enter each credential as a single-line value.', true); return;}
    try {
      const dotenv = file ? await file.text() : selected.map(([key, id]) => key + '=' + value(id)).join('\n') + '\n';
      action('environment.configure', {dotenv_text: dotenv, persist: true});
    } catch {input.value = ''; feedback('The selected .env file could not be read. Choose a readable local text file.', true);}
  });
  bindForm('inspect-form', 'repository.inspect', () => ({path: value('local-path')}));
  bindForm('install-github-form', 'tools.install-github', () => ({}));
  bindForm('retarget-form', 'repository.retarget', () => ({path: projectPath(), repository: value('retarget-repository')}));
  bindForm('clone-form', 'repository.clone', () => ({repository: value('clone-repository'), destination: value('clone-destination'), fork: checked('clone-fork'), ...(value('fork-owner') ? {owner: value('fork-owner')} : {})}));
  bindForm('project-form', 'project.initialize', () => ({path: projectPath(), repository: value('project-repository'), base_branch: value('base-branch'), install: commands('install'), checks: commands('checks'), instructions: lines('instructions'), required_checks: lines('required-checks'), image: value('image'), openai_secret: value('project-secret'), environment: value('project-environment'), gateway_app: value('gateway-app'), worker_app: value('worker-app'), gateway_concurrency: Number(value('gateway-concurrency'))}));
  bindForm('import-form', 'tasks.import', () => {const numbers = ids('issues'); if (!numbers.length || numbers.some((item) => !/^[1-9]\d*$/.test(item))) throw new Error('ISSUE_NUMBERS'); return {path: projectPath(), issue_numbers: numbers.map(Number)};});
  bindForm('task-form', 'tasks.save', () => {
    const task = {id: value('task-id'), title: value('task-title'), prompt: value('task-prompt'), ownership: lines('task-ownership'), acceptance: lines('task-acceptance'), dependsOn: ids('task-dependencies'), issue: Number(value('task-issue')), status: 'ready'};
    const acceptance = commands('task-checks', true); if (acceptance) task.acceptance_checks = acceptance;
    const protectedFiles = lines('task-protected'); if (protectedFiles.length) task.acceptance_files = protectedFiles;
    for (const key of ['produces', 'consumes']) {const items = lines('task-' + key); if (items.length) task[key] = items;}
    return {path: projectPath(), task, overwrite: checked('task-overwrite')};
  });
  byId('setup-create-issue').addEventListener('click', () => {
    if (!checked('create-issue-confirm')) {feedback('Confirm creation of the GitHub issue before continuing.', true); return;}
    if (!value('task-title') || !value('task-prompt')) {feedback('Enter a task title and implementation request above before creating the issue.', true); return;}
    try {action('tasks.create-issue', {path: projectPath(), title: value('task-title'), body: value('task-prompt')});} catch (error) {feedback(errorMessage(error), true);}
  });
  bindForm('check-form', 'project.check', () => ({path: projectPath()}));
  bindForm('publish-preview-form', 'preparation.preview', () => ({path: projectPath()}));
  bindForm('publish-form', 'preparation.publish', () => {if (!state.publishBinding || state.publishPath !== projectPath()) throw new Error('PUBLISH_PREVIEW'); return {path: projectPath(), message: value('publish-message'), binding: state.publishBinding, push: true};});
  bindForm('budget-form', 'project.configure-budget', () => ({path: projectPath(), model_limit_cents: dollars('model-budget'), infra_limit_cents: dollars('infra-budget'), max_live_agents: Number(value('max-agents')), live_enabled: checked('live-enabled')}));
  bindForm('deploy-form', 'project.deploy', () => ({path: projectPath()}));
  bindForm('probe-form', 'project.probe', () => ({path: projectPath(), gateway_url: value('gateway-url')}));
  bindForm('cli-preview-form', 'cli.preview', () => ({command: value('cli-command')}));
  bindForm('cli-execute-form', 'cli.execute', () => {if (!state.cliBinding || state.cliCommand !== value('cli-command')) throw new Error('CLI_CHANGED'); return {command: state.cliCommand, review_binding: state.cliBinding};});
  for (const button of mount.querySelectorAll('[data-setup-verify]')) button.addEventListener('click', () => action('connections.verify', {provider: button.dataset.setupVerify}));
  byId('setup-command-preset').addEventListener('change', () => {
    const presets = {node: {install: [['npm', 'ci', '--no-audit', '--no-fund']], checks: [['npm', 'run', 'check']]}, python: {install: [['python3', '-m', 'pip', 'install', '-e', '.']], checks: [['python3', '-m', 'unittest', 'discover', '-s', 'tests']]}};
    const preset = presets[byId('setup-command-preset').value]; if (preset) for (const key of ['install', 'checks']) byId('setup-' + key).value = JSON.stringify(preset[key]);
  });
  byId('setup-build-command').addEventListener('click', () => {
    try {
      const operation = byId('setup-cli-operation').value, selected = ['swarm-control', ...operation.split(' ')];
      if (['plan', 'preflight', 'analyze'].includes(operation)) selected.push(...ids('cli-tasks'));
      selected.push('--project', projectPath().endsWith('swarm.toml') ? projectPath() : projectPath().replace(/\/$/, '') + '/swarm.toml');
      if (['plan', 'preflight', 'analyze'].includes(operation)) selected.push('--workers', value('cli-workers'), '--base', value('cli-base'));
      byId('setup-cli-command').value = selected.map(quote).join(' '); state.cliBinding = null; byId('setup-cli-review').hidden = true;
    } catch (error) {feedback(errorMessage(error), true);}
  });
  byId('setup-cli-command').addEventListener('input', () => {state.cliBinding = null; byId('setup-cli-review').hidden = true;});
  for (const [id, key] of [['gateway-app', 'gateway_app'], ['worker-app', 'worker_app']]) {
    byId('setup-' + id).addEventListener('input', () => {
      const overrides = {...state.runtimeOverrides.get(state.repository || '')};
      if (value(id)) overrides[key] = value(id); else delete overrides[key];
      state.runtimeOverrides.set(state.repository || '', overrides);
    });
  }
  for (const [formId, confirmation] of [['env-form', 'env-confirm'], ['clone-form', 'clone-confirm'], ['project-form', 'project-confirm'], ['publish-form', 'publish-confirm'], ['budget-form', 'budget-confirm'], ['probe-form', 'probe-confirm']]) {
    byId('setup-' + formId).addEventListener('input', (event) => {if (event.target.id !== 'setup-' + confirmation) byId('setup-' + confirmation).checked = false;});
  }
  byId('setup-clone-fork').addEventListener('change', () => {byId('setup-clone-form').querySelector('[type="submit"]').textContent = checked('clone-fork') ? 'Create fork and clone' : 'Clone selected repository'; byId('setup-clone-confirm').checked = false;});

  function clearSecrets() {for(const input of mount.querySelectorAll('[data-setup-secret]'))input.value='';}
  function closeSetup(){clearSecrets();window.SwarmNavigation?.navigate('projects');}
  byId('setup-close').addEventListener('click',closeSetup);
  for(let index=2;index<steps.length;index+=1)byId('setup-tab-'+index).dataset.projectStep=['','','profile','tasks','checks','budget','advanced'][index];

  const repositoryPanel=byId('setup-panel-1');repositoryPanel.removeAttribute('role');repositoryPanel.removeAttribute('aria-labelledby');
  repositoryPanel.querySelector('h3')?.remove();repositoryPanel.querySelector('.setup-lead')?.remove();
  const sourceTabs=element('div','screen-tabs');sourceTabs.setAttribute('role','tablist');sourceTabs.setAttribute('aria-label','Repository source');
  const localForm=byId('setup-inspect-form'),localCard=localForm.closest('article'),githubCard=byId('setup-clone-form').closest('article');
  const localPane=element('section','repository-source');localPane.id='repository-local-source';localPane.append(localForm);
  githubCard.id='repository-github-source';githubCard.classList.add('repository-source');
  for(const [name,label,pane] of [['github','GitHub URL',githubCard],['local','Local folder',localPane]]){
    const button=element('button','button button-quiet',label);button.id='repository-source-'+name;button.type='button';button.setAttribute('role','tab');button.setAttribute('aria-controls',pane.id);pane.setAttribute('role','tabpanel');pane.setAttribute('aria-labelledby',button.id);
    button.addEventListener('click',()=>{for(const candidate of [githubCard,localPane])candidate.hidden=candidate!==pane;for(const tab of sourceTabs.querySelectorAll('button'))tab.setAttribute('aria-selected',String(tab===button));});sourceTabs.append(button);
  }
  repositoryPanel.prepend(sourceTabs,githubCard,localPane);localPane.hidden=true;sourceTabs.firstElementChild.setAttribute('aria-selected','true');
  const facts=byId('setup-repository-facts');if(localCard.contains(facts))repositoryPanel.append(facts,connectPrepared);localCard.remove();
  byId('repository-source-host').append(repositoryPanel);
  const repositoryFeedback=element('p','setup-feedback');repositoryFeedback.id='repository-feedback';repositoryFeedback.setAttribute('role','status');repositoryFeedback.setAttribute('aria-live','polite');repositoryPanel.prepend(repositoryFeedback);
  const repositoryHistory=element('details','setup-history');repositoryHistory.innerHTML='<summary>Repository operation history</summary><ul id="repository-jobs" class="setup-job-list"></ul>';repositoryPanel.append(repositoryHistory);
  sourceTabs.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const tabs=[...sourceTabs.querySelectorAll('[role="tab"]')],index=tabs.indexOf(event.target);if(index<0)return;
    event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
    tabs[next].click();tabs[next].focus();
  });
  const selectedActions=element('div','screen-actions');
  const planButton=element('button','button button-primary','Plan work for this repository');planButton.id='setup-start-planning';planButton.type='button';planButton.disabled=true;
  planButton.addEventListener('click',()=>window.SwarmNavigation?.navigate('plan'));
  const manual=element('button','button button-quiet','Configure manually');manual.type='button';manual.addEventListener('click',()=>showStep(2));selectedActions.append(planButton,manual);repositoryPanel.append(selectedActions);
  const originalRenderFacts=renderRepositoryFacts;
  renderRepositoryFacts=function(result){originalRenderFacts(result);planButton.disabled=!state.path;};
  const gatewayCard=byId('setup-openai-form').closest('article');byId('setup-panel-5').querySelector('article').before(gatewayCard);
  gatewayCard.querySelector('[data-setup-verify="openai"]').textContent='Verify last configured secret';
  for(const id of ['secret-name','secret-environment'])byId('setup-'+id).addEventListener('input',()=>{byId('setup-secret-confirm').checked=false;connectionStatus(state.connections);});
  byId('setup-panel-0').querySelector('h3').textContent='Accounts and local credentials';
  byId('setup-panel-0').querySelector('.setup-lead').textContent='These accounts belong to this controller. The local OpenAI key powers planning; each project’s worker gateway secret is configured under its execution settings.';
  byId('setup-github-form').closest('article').querySelector('.setup-copy').textContent='Use your own token with access to this repository. AI planning requires a saved or session token and verified identity so issue creation uses the account you reviewed. Existing terminal login remains available for manual operations.';
  const tabLabels=['Accounts','Repository','Profile','Tasks','Checks & publish','Execution allowance','Advanced tools'];
  mount.querySelector('.setup-steps').setAttribute('aria-orientation','horizontal');
  steps.forEach((_,index)=>{const tab=byId('setup-tab-'+index);tab.replaceChildren(document.createTextNode(tabLabels[index]));if(index<2)tab.hidden=true;});
  const blocked=element('div','screen-empty');blocked.id='setup-project-unavailable';blocked.hidden=true;blocked.innerHTML='<h2>Select a project to configure</h2><p>Choose a connected project in the header, or add a repository in Projects and generate its work plan.</p>';
  mount.querySelector('.setup-layout').before(blocked);
  byId('setup-close').textContent='Done';byId('setup-close').className='button button-quiet';byId('setup-close').setAttribute('aria-label','Return to Projects');

  async function bindProject(projectId){
    if(!projectId)return null;
    if(state.boundProjectId===projectId&&state.path)return selectedRepository();
    if(state.bindingProjectId===projectId&&state.bindingFlight)return state.bindingFlight;
    state.bindingProjectId=projectId;
    state.bindingFlight=loadSelectedProject(projectId);
    try{return await state.bindingFlight;}finally{if(state.bindingProjectId===projectId){state.bindingProjectId=null;state.bindingFlight=null;}}
  }
  async function loadSelectedProject(projectId){
    const version=++state.bindingVersion;state.selectionVersion+=1;state.boundProjectId=null;state.path='';state.projectId=null;
    for(const node of document.querySelectorAll('[data-setup-path]'))node.textContent='Loading selected project…';
    state.publishBinding=null;state.cliBinding=null;byId('setup-publish-review').hidden=true;byId('setup-cli-review').hidden=true;
    try{
      const result=await request('/api/setup/project?project='+encodeURIComponent(projectId));
      if(version!==state.bindingVersion||window.SwarmApplication?.context().projectId!==projectId)throw new Error('Project selection changed.');
      if(result.project_id!==projectId||typeof result.path!=='string')throw new Error('Selected project configuration is unavailable.');
      selectPath(result.path,result.repository,result.recommended_runtime,result.configuration?.runtime);state.projectId=projectId;state.boundProjectId=projectId;
      renderRepositoryFacts(result);
      const runtime=result.configuration?.runtime;
      if(runtime?.openai_secret)byId('setup-secret-name').value=runtime.openai_secret;
      if(runtime?.environment)byId('setup-secret-environment').value=runtime.environment;
      return selectedRepository();
    }catch(error){if(version===state.bindingVersion){state.path='';state.projectId=null;feedback('The selected project could not be loaded. Select a usable project or reconnect its repository in Projects.',true);}throw error;}
  }
  function selectedRepository(){return {path:state.path,repository:state.repository,projectId:state.projectId};}
  async function renderRoute(route){
    if(!route)return;
    const routeVersion=++state.routeVersion;
    clearSecrets();
    const context=window.SwarmApplication?.context();
    blocked.hidden=true;
    if(route.screen==='projects'){showStep(1,false,true);return;}
    if(route.screen==='settings'){mount.querySelector('.setup-layout').hidden=false;byId('setup-title').textContent='Accounts and connections';showStep(0,false,true);await refreshSetup(true);return;}
    if(route.screen==='project-settings'){
      byId('setup-title').textContent='Project configuration';
      if(context?.projectId){try{await bindProject(context.projectId);}catch{blocked.hidden=false;}}
      else if(!context?.pendingRepository?.path||context.pendingRepository.path!==state.path)blocked.hidden=false;
      const current=window.SwarmNavigation?.current();
      if(routeVersion!==state.routeVersion||current?.screen!=='project-settings'||current.step!==route.step||window.SwarmApplication?.context().projectId!==context?.projectId)return;
      mount.querySelector('.setup-layout').hidden=!blocked.hidden;
      if(!blocked.hidden)return;
      showStep({profile:2,tasks:3,checks:4,budget:5,advanced:6}[route.step]||2,false,true);await refreshSetup(true);
    }
  }
  window.addEventListener('swarm:route',event=>renderRoute(event.detail));
  window.addEventListener('swarm:context',event=>{
    const context=event.detail;
    const selectionKey=context.projectId||context.pendingRepository?.path||'';
    if(selectionKey!==state.applicationSelectionKey){state.applicationSelectionKey=selectionKey;state.selectionVersion+=1;state.publishBinding=null;state.cliBinding=null;byId('setup-publish-review').hidden=true;byId('setup-cli-review').hidden=true;}
    if(context.projectId&&context.projectId!==state.boundProjectId&&window.SwarmNavigation?.current().screen==='project-settings')renderRoute(window.SwarmNavigation.current());
    else if(!context.projectId&&!context.pendingRepository?.path&&state.boundProjectId){state.bindingVersion+=1;state.boundProjectId=null;state.path='';state.projectId=null;}
  });
  window.addEventListener('swarm:repository-onboarding',()=>{showStep(1,false,true);byId('setup-clone-repository').focus();});
  window.addEventListener('swarm:open-setup',()=>window.SwarmNavigation?.navigate('settings'));
  byId('setup-open-builder').addEventListener('click',()=>window.dispatchEvent(new CustomEvent('swarm:setup-run',{detail:{project_id:state.projectId}})));
  window.SwarmSetup={selectedRepository,bindProject,
    status:()=>({available:state.available,busy:state.busy,connections:state.connections}),
    refresh:()=>refreshSetup(true),
    perform:async(name,options)=>{if(!state.csrf)await refreshSetup(true);const outcome=await action(name,options);if(!outcome.ok)throw outcome.error;return outcome.result;},
    readJob:id=>request('/api/setup/job?id='+encodeURIComponent(id)),
    history:()=>request('/api/setup')};
  refreshSetup(true);renderRoute(window.SwarmNavigation?.current());
})();
