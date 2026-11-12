/* Screen routing owns visibility and history, never task or provider actions. */
(function (factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = {createNavigation: factory};
  else window.SwarmNavigation = factory(window, document);
})(function createNavigation(win, doc) {
  'use strict';
  const screens = new Set(['projects', 'plan', 'new-run', 'runs', 'run', 'project-settings', 'settings']);
  const runTabs = ['summary', 'workers', 'changes', 'costs', 'evidence'];
  const projectSteps = ['profile', 'tasks', 'checks', 'budget', 'advanced'];
  const legacy = {overview: ['projects'], 'run-history': ['runs'], workers: ['run', 'workers'],
    tasks: ['run', 'changes'], budget: ['run', 'costs'], evidence: ['run', 'evidence']};
  const titles = {projects: 'Projects', plan: 'Plan work', 'new-run': 'New run', runs: 'Runs',
    run: 'Run details', 'project-settings': 'Project settings', settings: 'Settings'};
  const descriptions = {projects: 'Choose a repository or connect another.',
    plan: 'Turn a goal into a task plan you can review.',
    'new-run': 'Select ready tasks and review the execution limits.',
    runs: 'Open a saved draft or recorded execution.',
    run: 'Inspect configuration and the recorded outcome.',
    'project-settings': 'Prepare this project and review its configuration.',
    settings: 'Manage the accounts used by this local controller.'};
  const linkSelector = '[data-screen-link], [data-run-tab], [data-project-step]';
  const linkSpecs = new WeakMap();
  const linkTitles = new WeakMap();
  const removers = [];
  const context = {initialized: false, workspace: false, projectId: null, runId: null,
    hasRepository: false, configurable: false, repository: '', projectLabel: '', runLabel: '', hasEvidence: false};
  let route;
  let lastURL = null;
  let menuOpen = false;
  let guardMessage = '';
  const menu = doc.getElementById('primary-navigation');
  const menuToggle = doc.getElementById('mobile-navigation-toggle');
  const media = typeof win.matchMedia === 'function' ? win.matchMedia('(max-width: 980px)') : null;

  function listen(target, type, callback, options) {
    target.addEventListener(type, callback, options);
    removers.push(() => target.removeEventListener(type, callback, options));
  }

  function identifier(value) {
    if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(value)) return null;
    if (/(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})/i.test(value)) return null;
    return value;
  }

  function queryID(params, name) {
    const values = params.getAll(name);
    return values.length === 1 ? identifier(values[0]) : null;
  }

  function pathFor(value) {
    if (value.screen === 'run') return '#/run/' + value.tab;
    if (value.screen === 'project-settings') return '#/project-settings/' + value.step;
    return '#/' + value.screen;
  }

  function destination(screen, options = {}) {
    screen = screens.has(screen) ? screen : 'projects';
    return {screen, tab: screen === 'run' ? (runTabs.includes(options.tab) ? options.tab : 'summary') : null,
      step: screen === 'project-settings' ? (projectSteps.includes(options.step) ? options.step : 'profile') : null};
  }

  function parse(url) {
    const raw = url.hash.slice(1);
    const parts = legacy[raw] || (raw.startsWith('/') ? raw.slice(1).split('/') : []);
    let next = destination(parts[0], {tab: parts[1], step: parts[1]});
    if (parts.length > 2 || (parts.length > 1 && !['run', 'project-settings'].includes(parts[0]))) next = destination('projects');
    return {...next, project: queryID(url.searchParams, 'project'), run: queryID(url.searchParams, 'run')};
  }

  function snapshot() { return {...route}; }

  function routeURL(next, options = {}) {
    const url = new win.URL(win.location.href);
    patchSelection(url, options);
    url.hash = pathFor(next);
    return url;
  }

  function patchSelection(url, options) {
    for (const key of ['project', 'run']) {
      if (Object.prototype.hasOwnProperty.call(options, key) && options[key] !== undefined) {
        const value = identifier(options[key]);
        if (value) url.searchParams.set(key, value);
        else url.searchParams.delete(key);
      }
    }
  }

  function text(id, value) {
    const element = doc.getElementById(id);
    if (element) element.textContent = value;
  }

  function contextLabel() {
    if (route.project && context.initialized && route.project !== context.projectId) return 'Project unavailable';
    if (!context.projectId) return context.hasRepository ? 'Repository selected; project not prepared yet' : 'No project selected';
    const project = context.projectLabel || context.repository || 'Selected project';
    if (route.screen !== 'run') return project;
    if (route.run && route.run !== context.runId) return project + ' / Run unavailable';
    return context.runId ? project + ' / ' + (context.runLabel || 'Selected run') : project;
  }

  function unavailable(screen) {
    if (!context.initialized) return '';
    if (['plan', 'new-run', 'project-settings', 'settings'].includes(screen) && !context.workspace) {
      return 'This view is read only. Open workspace mode to configure projects.';
    }
    if (screen === 'plan' && !context.projectId && !context.hasRepository) return 'Choose or connect a repository before planning work.';
    if (['new-run', 'project-settings'].includes(screen) && !context.projectId) return 'Choose a prepared project first.';
    if (screen === 'new-run' && !context.configurable) return 'Connect this project to a prepared local repository before creating a run.';
    return '';
  }

  function specFor(link) {
    if (!linkSpecs.has(link)) {
      const options = {};
      const raw = link.getAttribute('href') || '';
      if (raw.includes('?')) {
        const url = new win.URL(raw, win.location.href);
        for (const key of ['project', 'run']) if (url.searchParams.has(key)) options[key] = queryID(url.searchParams, key);
      }
      linkSpecs.set(link, options);
    }
    const options = {...linkSpecs.get(link)};
    for (const key of ['project', 'run']) {
      if (link.hasAttribute('data-route-' + key)) options[key] = link.getAttribute('data-route-' + key) || null;
    }
    const tab = link.getAttribute('data-run-tab');
    const step = link.getAttribute('data-project-step');
    if (tab) options.tab = tab;
    if (step) options.step = step;
    return {screen: link.getAttribute('data-screen-link') || (tab ? 'run' : 'project-settings'), options};
  }

  function render() {
    const html = doc.documentElement;
    html.setAttribute('data-swarm-screen', route.screen);
    html.setAttribute('data-swarm-tab', route.tab || '');
    html.setAttribute('data-swarm-step', route.step || '');
    html.setAttribute('data-swarm-workspace', String(context.workspace));
    html.setAttribute('data-swarm-project', String(Boolean(context.projectId)));
    for (const element of doc.querySelectorAll('[data-screen]')) {
      if (!element.hasAttribute('data-run-content')) element.hidden = !element.getAttribute('data-screen').split(/\s+/).includes(route.screen);
    }
    for (const element of doc.querySelectorAll('[data-run-panel]')) {
      if (!element.hasAttribute('data-run-content')) element.hidden = route.screen !== 'run' || !element.getAttribute('data-run-panel').split(/\s+/).includes(route.tab);
    }
    for (const link of doc.querySelectorAll(linkSelector)) {
      if (!linkTitles.has(link)) linkTitles.set(link, link.getAttribute('title'));
      const spec = specFor(link);
      const target = destination(spec.screen, spec.options);
      const active = target.screen === route.screen && (!spec.options.tab || target.tab === route.tab)
        && (!spec.options.step || target.step === route.step)
        && (!Object.prototype.hasOwnProperty.call(spec.options, 'project') || identifier(spec.options.project) === route.project);
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
      if (link.getAttribute('role') === 'tab') {
        link.setAttribute('aria-selected', String(active));
        link.setAttribute('tabindex', active ? '0' : '-1');
      }
      const reason = unavailable(target.screen);
      if (reason) {
        link.setAttribute('aria-disabled', 'true');
        link.setAttribute('aria-description', reason);
        link.setAttribute('title', reason);
      } else {
        link.removeAttribute('aria-disabled');
        link.removeAttribute('aria-description');
        const title = linkTitles.get(link);
        if (title === null) link.removeAttribute('title');
        else link.setAttribute('title', title);
      }
      if (link.tagName === 'A') link.setAttribute('href', routeURL(target, spec.options).href);
    }
    text('screen-title', titles[route.screen]);
    text('screen-description', descriptions[route.screen]);
    text('screen-context', contextLabel());
    doc.title = titles[route.screen] + ' | Swarm Control';
  }

  function closeMenu(restoreFocus = false) {
    const wasOpen = menuOpen;
    menuOpen = false;
    doc.documentElement.setAttribute('data-navigation-open', 'false');
    if (menuToggle) menuToggle.setAttribute('aria-expanded', 'false');
    if (restoreFocus && wasOpen && menuToggle) menuToggle.focus();
  }

  function apply(emit) {
    const url = new win.URL(win.location.href);
    const next = parse(url);
    if (url.hash && url.hash !== pathFor(next)) {
      url.hash = pathFor(next);
      win.history.replaceState(win.history.state, '', url.href);
    }
    const key = url.href;
    if (key === lastURL) return false;
    route = next;
    lastURL = key;
    closeMenu();
    render();
    if (emit) win.dispatchEvent(new win.CustomEvent('swarm:route', {detail: snapshot()}));
    return true;
  }

  function go(screen, options = {}, focusHeading = true) {
    const status = doc.getElementById('navigation-status');
    if (guardMessage && status?.textContent === guardMessage) {
      status.textContent = '';
      status.hidden = true;
    }
    guardMessage = '';
    const next = destination(screen, options);
    const url = routeURL(next, options);
    if (url.href === win.location.href) { closeMenu(); return snapshot(); }
    win.history[options.replace === true ? 'replaceState' : 'pushState']({swarmNavigation: true}, '', url.href);
    apply(true);
    if (focusHeading) {
      const heading = doc.getElementById('screen-title');
      if (heading) {
        if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
        heading.focus();
      }
    }
    return snapshot();
  }

  function nearest(event, selector) {
    const target = event.target?.closest ? event.target : event.target?.parentElement;
    return target?.closest(selector) || null;
  }

  function routedTab(tab) {
    return tab && ['data-screen-link', 'data-run-tab', 'data-project-step'].some((name) => tab.hasAttribute(name));
  }

  function hiddenTab(tab) {
    for (let element = tab; element; element = element.parentElement) if (element.hidden) return true;
    return false;
  }

  function activate(link) {
    const spec = specFor(link);
    const reason = unavailable(destination(spec.screen, spec.options).screen);
    if (reason) {
      guardMessage = reason;
      text('navigation-status', reason);
      const status = doc.getElementById('navigation-status');
      if (status) status.hidden = false;
      text('screen-reader-status', reason);
      return;
    }
    go(spec.screen, spec.options, !link.hasAttribute('data-run-tab') && !link.hasAttribute('data-project-step'));
  }

  listen(doc, 'click', (event) => {
    if (event.defaultPrevented || (event.button !== undefined && event.button !== 0)
        || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (menuOpen && menu && !menu.contains(event.target) && !menuToggle.contains(event.target)) {
      event.preventDefault();
      event.stopPropagation();
      closeMenu();
      return;
    }
    const link = nearest(event, linkSelector);
    if (!link || link.hasAttribute('download') || (link.getAttribute('target') && link.getAttribute('target') !== '_self')) return;
    event.preventDefault();
    activate(link);
  }, true);

  listen(doc, 'keydown', (event) => {
    if (event.key === 'Escape' && menuOpen) {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    const tab = nearest(event, '[role="tab"]');
    if (!routedTab(tab)) return;
    const list = tab?.closest('[role="tablist"]');
    if (!list || !['ArrowLeft', 'ArrowRight', 'Home', 'End', ' '].includes(event.key)) return;
    const tabs = [...list.querySelectorAll('[role="tab"]')].filter((item) => routedTab(item) && !hiddenTab(item)
      && item.disabled !== true && item.getAttribute('aria-disabled') !== 'true');
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1]
      : event.key === 'ArrowLeft' ? tabs[(index - 1 + tabs.length) % tabs.length]
        : event.key === 'ArrowRight' ? tabs[(index + 1) % tabs.length] : tab;
    activate(next);
    next.focus();
  });

  listen(win, 'popstate', () => apply(true));
  listen(win, 'hashchange', () => apply(true));
  if (menu && menuToggle) {
    doc.documentElement.setAttribute('data-navigation-menu', 'ready');
    menuToggle.setAttribute('aria-controls', 'primary-navigation');
    menuToggle.setAttribute('aria-expanded', 'false');
    listen(menuToggle, 'click', () => {
      menuOpen = !menuOpen;
      menuToggle.setAttribute('aria-expanded', String(menuOpen));
      doc.documentElement.setAttribute('data-navigation-open', String(menuOpen));
      if (menuOpen) menu.querySelector(linkSelector)?.focus();
    });
    if (media?.addEventListener) listen(media, 'change', () => closeMenu());
    else if (media?.addListener) {
      const change = () => closeMenu();
      media.addListener(change);
      removers.push(() => media.removeListener(change));
    }
  }

  apply(false);
  return {
    current: snapshot,
    navigate: (screen, options = {}) => go(screen, options),
    replaceSelection(options = {}) {
      const url = new win.URL(win.location.href);
      patchSelection(url, options);
      if (url.href !== win.location.href) win.history.replaceState(win.history.state, '', url.href);
      apply(false);
      return snapshot();
    },
    setContext(value = {}) {
      const oldProject = context.projectId;
      const oldRun = context.runId;
      for (const key of ['projectId', 'runId']) if (Object.prototype.hasOwnProperty.call(value, key)) context[key] = identifier(value[key]);
      if (oldProject !== context.projectId) {
        context.repository = '';
        context.projectLabel = '';
        context.runLabel = '';
      } else if (oldRun !== context.runId) context.runLabel = '';
      for (const key of ['workspace', 'hasRepository', 'hasEvidence']) if (Object.prototype.hasOwnProperty.call(value, key)) context[key] = value[key] === true;
      if (Object.prototype.hasOwnProperty.call(value, 'configurable')) context.configurable = value.configurable === true;
      else if (oldProject !== context.projectId) context.configurable = Boolean(context.projectId);
      for (const key of ['repository', 'projectLabel', 'runLabel']) {
        if (Object.prototype.hasOwnProperty.call(value, key)) context[key] = typeof value[key] === 'string' ? value[key].slice(0, 240) : '';
      }
      context.initialized = true;
      render();
      return snapshot();
    },
    subscribe(callback) {
      if (typeof callback !== 'function') throw new TypeError('A navigation subscriber must be a function.');
      const listener = (event) => callback(event.detail);
      win.addEventListener('swarm:route', listener);
      return () => win.removeEventListener('swarm:route', listener);
    },
    destroy() { removers.splice(0).forEach((remove) => remove()); closeMenu(); }
  };
});
