'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createNavigation} = require('../swarm_control/dashboard_static/navigation.js');

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(callback);
  }
  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback); }
  dispatchEvent(event) {
    for (const callback of this.listeners.get(event.type) || []) callback(event);
    return !event.defaultPrevented;
  }
}
class Event {
  constructor(type, values = {}) { this.type = type; this.defaultPrevented = false; Object.assign(this, values); }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.stopped = true; }
}
function matches(element, selector) {
  return selector.split(',').some((part) => {
    const match = part.trim().match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
    return Boolean(match && element.hasAttribute(match[1]) && (match[2] === undefined || element.getAttribute(match[1]) === match[2]));
  });
}
class Element extends Events {
  constructor(document, tagName, attributes = {}) {
    super(); this.document = document; this.tagName = tagName.toUpperCase(); this.attributes = new Map();
    this.children = []; this.parentElement = null; this.hidden = false; this.textContent = ''; this.disabled = false; this.value = '';
    this.classes = new Set(); this.classList = {toggle: (name, state) => state ? this.classes.add(name) : this.classes.delete(name)};
    for (const [name, value] of Object.entries(attributes)) this.setAttribute(name, value);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  append(child) { child.parentElement = this; this.children.push(child); return child; }
  contains(element) { return element === this || this.children.some((child) => child.contains(element)); }
  closest(selector) { return matches(this, selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) { return this.children.flatMap((child) => [...(matches(child, selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.document.activeElement = this; }
}
class Document extends Events {
  constructor() { super(); this.documentElement = new Element(this, 'html'); this.activeElement = null; this.title = ''; }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }
  getElementById(id) {
    const find = (node) => node.getAttribute('id') === id ? node : node.children.map(find).find(Boolean);
    return find(this.documentElement) || null;
  }
  element(tag, attributes, parent = this.documentElement) { return parent.append(new Element(this, tag, attributes)); }
}
function browser(href) {
  const win = new Events(); win.URL = URL; win.CustomEvent = Event; win.location = {href};
  const media = new Events(); win.matchMedia = () => media;
  const entries = [{url: href, state: null}]; let cursor = 0;
  win.history = {
    calls: [], get length() { return entries.length; }, get state() { return entries[cursor].state; },
    pushState(state, _, url) {
      this.calls.push(['push', url]); entries.splice(cursor + 1); entries.push({url: String(url), state}); cursor++; win.location.href = String(url);
    },
    replaceState(state, _, url) {
      this.calls.push(['replace', url]); entries[cursor] = {url: String(url), state}; win.location.href = String(url);
    },
    go(delta) {
      if (cursor + delta < 0 || cursor + delta >= entries.length) return;
      const before = new URL(win.location.href).hash; cursor += delta; win.location.href = entries[cursor].url;
      win.dispatchEvent(new Event('popstate'));
      if (before !== new URL(win.location.href).hash) win.dispatchEvent(new Event('hashchange'));
    },
    back() { this.go(-1); }, forward() { this.go(1); }
  };
  win.externalHash = (hash) => {
    const url = new URL(win.location.href); url.hash = hash;
    win.history.pushState(null, '', url.href); win.dispatchEvent(new Event('hashchange'));
  };
  win.fetch = () => { throw new Error('Navigation must never issue a request'); };
  return win;
}
function fixture(href = 'http://127.0.0.1:8787/?project=P1&run=R1#/projects') {
  const doc = new Document(); const win = browser(href); let events = [];
  win.addEventListener('swarm:route', (event) => events.push(event.detail));
  const nav = doc.element('aside', {id: 'primary-navigation'});
  const toggle = doc.element('button', {id: 'mobile-navigation-toggle'});
  const links = {};
  for (const screen of ['projects', 'plan', 'new-run', 'runs', 'run', 'project-settings', 'settings']) {
    links[screen] = doc.element('a', {'data-screen-link': screen, href: '#/' + screen, title: screen + ' view'}, nav);
  }
  const headings = {};
  for (const id of ['screen-title', 'screen-description', 'screen-context', 'navigation-status', 'screen-reader-status']) headings[id] = doc.element('div', {id});
  const pages = {};
  for (const screen of ['projects', 'plan', 'new-run', 'runs', 'run', 'project-settings', 'settings']) pages[screen] = doc.element('section', {'data-screen': screen});
  const shared = doc.element('div', {'data-screen': 'settings project-settings'});
  const tablist = doc.element('nav', {role: 'tablist'}); const tabs = {}; const panels = {};
  for (const tab of ['summary', 'workers', 'changes', 'costs', 'evidence']) {
    tabs[tab] = doc.element('a', {role: 'tab', 'data-run-tab': tab, href: '#/run/' + tab}, tablist);
    panels[tab] = doc.element('section', {'data-run-panel': tab}, pages.run);
  }
  const raw = doc.element('div', {'data-run-content': ''}, panels.summary); raw.hidden = true;
  const input = doc.element('input', {}, pages['new-run']); input.disabled = true; input.value = 'preserve draft';
  const router = createNavigation(win, doc);
  const click = (target, values = {}) => {
    const event = new Event('click', {target, button: 0, ...values}); doc.dispatchEvent(event);
    if (!event.stopped) target.dispatchEvent(event);
    return event;
  };
  return {doc, win, router, events, nav, toggle, links, headings, pages, shared, tabs, panels, raw, input, click};
}

test('initial read is passive, supports deep links, and isolates all screen and run panels', () => {
  const f = fixture('http://local/?project=P_1&run=R-2#/run/changes');
  assert.deepEqual(f.router.current(), {screen: 'run', tab: 'changes', step: null, project: 'P_1', run: 'R-2'});
  assert.equal(f.events.length, 0); assert.equal(f.win.history.calls.length, 0);
  assert.deepEqual(Object.entries(f.pages).filter(([, node]) => !node.hidden).map(([key]) => key), ['run']);
  assert.deepEqual(Object.entries(f.panels).filter(([, node]) => !node.hidden).map(([key]) => key), ['changes']);
  assert.equal(f.raw.hidden, true); assert.equal(f.input.disabled, true); assert.equal(f.input.value, 'preserve draft');
  assert.equal(f.doc.documentElement.getAttribute('data-swarm-tab'), 'changes');
  assert.equal(f.tabs.changes.getAttribute('aria-selected'), 'true');
});

test('legacy bookmarks normalize in place and malformed routes safely select Projects', () => {
  const cases = {'#overview': ['projects', null], '#run-history': ['runs', null], '#workers': ['run', 'workers'],
    '#tasks': ['run', 'changes'], '#budget': ['run', 'costs'], '#evidence': ['run', 'evidence'],
    '#/unknown': ['projects', null], '#/run/workers/extra': ['projects', null], '#/projects/extra': ['projects', null], '#/run/unknown': ['run', 'summary']};
  for (const [hash, expected] of Object.entries(cases)) {
    const f = fixture('http://local/?project=P1&run=R1' + hash);
    assert.deepEqual([f.router.current().screen, f.router.current().tab], expected, hash);
    assert.equal(f.win.history.length, 1); assert.equal(f.events.length, 0); assert.equal(f.router.current().project, 'P1');
  }
  const empty = fixture('http://local/?project=P1');
  assert.equal(empty.router.current().screen, 'projects'); assert.equal(new URL(empty.win.location.href).hash, '');
  for (const step of ['repository', 'missing']) assert.equal(fixture('http://local/#/project-settings/' + step).router.current().step, 'profile');
});

test('navigation preserves query context, changes only explicit IDs, and supports replace and no-op', () => {
  const f = fixture('http://local/?project=P1&run=R1&view=compact#/projects');
  f.router.navigate('run', {tab: 'workers'});
  assert.equal(new URL(f.win.location.href).search, '?project=P1&run=R1&view=compact');
  f.router.navigate('plan', {project: 'P2', run: undefined});
  assert.equal(f.router.current().run, 'R1'); assert.equal(f.router.current().project, 'P2');
  f.router.navigate('new-run', {run: null, replace: true});
  assert.equal(f.router.current().run, null); assert.equal(f.win.history.calls.at(-1)[0], 'replace');
  const count = f.events.length; const length = f.win.history.length;
  f.router.navigate('new-run'); assert.equal(f.events.length, count); assert.equal(f.win.history.length, length);
  assert.equal(f.doc.activeElement, f.headings['screen-title']);
});

test('real history traversal emits exactly once despite paired popstate and hashchange', () => {
  const f = fixture();
  f.router.navigate('run', {tab: 'workers'}); f.router.navigate('settings', {project: 'P2', run: 'R2'});
  f.win.history.back(); assert.equal(f.events.length, 3); assert.equal(f.router.current().tab, 'workers');
  assert.equal(f.router.current().project, 'P1'); assert.equal(f.router.current().run, 'R1');
  f.win.history.back(); assert.equal(f.events.length, 4); assert.equal(f.router.current().screen, 'projects');
  f.win.history.forward(); assert.equal(f.events.length, 5); assert.equal(f.router.current().tab, 'workers');
  f.win.externalHash('#budget'); assert.equal(f.events.length, 6); assert.equal(f.router.current().tab, 'costs');
  assert.equal(new URL(f.win.location.href).hash, '#/run/costs');
  f.win.dispatchEvent(new Event('hashchange')); assert.equal(f.events.length, 6);
});

test('replaceSelection synchronizes catalog selections without route events, focus, or new history entries', () => {
  const f = fixture('http://local/?project=P1&run=R1&view=compact#/run/workers');
  f.input.focus(); const length = f.win.history.length;
  f.router.replaceSelection({project: 'P2', run: 'R2'});
  assert.deepEqual(f.router.current(), {screen: 'run', tab: 'workers', step: null, project: 'P2', run: 'R2'});
  assert.equal(new URL(f.win.location.href).searchParams.get('view'), 'compact');
  assert.equal(new URL(f.win.location.href).hash, '#/run/workers');
  assert.equal(f.events.length, 0); assert.equal(f.doc.activeElement, f.input); assert.equal(f.win.history.length, length);
  const writes = f.win.history.calls.length;
  f.router.replaceSelection({project: 'P2', run: undefined}); assert.equal(f.win.history.calls.length, writes);
  // A legacy caller may have already replaced the URL outside the router.
  const outside = new URL(f.win.location.href); outside.searchParams.set('run', 'R3');
  f.win.history.replaceState(null, '', outside.href);
  assert.equal(f.router.current().run, 'R2');
  f.router.replaceSelection({}); assert.equal(f.router.current().run, 'R3');
  assert.equal(f.win.history.calls.length, writes + 1); assert.equal(f.events.length, 0);
  f.router.replaceSelection({run: null}); assert.equal(f.router.current().run, null);
  assert.equal(f.router.current().project, 'P2'); assert.equal(f.input.value, 'preserve draft');
  const empty = fixture('http://local/?view=compact');
  empty.router.replaceSelection({project: 'P4'});
  assert.equal(new URL(empty.win.location.href).hash, '');
  assert.equal(empty.router.current().project, 'P4'); assert.equal(empty.events.length, 0);
});

test('context refresh never writes history or navigates and labels are plain text', () => {
  const f = fixture(); const href = f.win.location.href;
  f.router.setContext({workspace: true, projectId: 'P1', runId: 'R1', projectLabel: '<img onerror=alert(1)>', runLabel: 'Latest'});
  assert.equal(f.win.location.href, href); assert.equal(f.events.length, 0); assert.equal(f.win.history.calls.length, 0);
  assert.equal(f.headings['screen-context'].textContent, '<img onerror=alert(1)>');
  f.router.setContext({projectId: 'P2', runId: null});
  assert.equal(f.headings['screen-context'].textContent, 'Project unavailable');
  f.router.navigate('plan', {project: 'P2'}); assert.equal(f.headings['screen-context'].textContent, 'Selected project');
  assert.equal(f.links.plan.getAttribute('title'), 'plan view');
});

test('run context appears only within Run details while other screens preserve the selected run query', () => {
  const f = fixture('http://local/?project=P1&run=R1#/run/summary');
  f.router.setContext({workspace: true, projectId: 'P1', runId: 'R1', projectLabel: 'Search app', runLabel: 'Search validation', configurable: true});
  assert.equal(f.headings['screen-context'].textContent, 'Search app / Search validation');
  for (const screen of ['projects', 'plan', 'new-run', 'runs', 'settings', 'project-settings']) {
    f.router.navigate(screen);
    assert.equal(f.headings['screen-context'].textContent, 'Search app', screen);
    assert.equal(f.router.current().run, 'R1');
    assert.equal(new URL(f.win.location.href).searchParams.get('run'), 'R1');
  }
  f.router.navigate('run'); assert.equal(f.headings['screen-context'].textContent, 'Search app / Search validation');
  f.router.navigate('run', {run: 'missing'}); assert.equal(f.headings['screen-context'].textContent, 'Search app / Run unavailable');
  f.router.navigate('projects'); assert.equal(f.headings['screen-context'].textContent, 'Search app');
});

test('pending repository enables Plan while New run and project settings explain missing preparation', () => {
  const f = fixture('http://local/#/projects');
  f.router.setContext({workspace: true, hasRepository: true, configurable: false});
  assert.equal(f.links.plan.getAttribute('aria-disabled'), null);
  assert.equal(f.links['new-run'].getAttribute('aria-disabled'), 'true');
  assert.equal(f.links['project-settings'].getAttribute('aria-disabled'), 'true');
  f.headings['navigation-status'].hidden = true;
  f.click(f.links['new-run']); assert.equal(f.router.current().screen, 'projects'); assert.equal(f.events.length, 0);
  assert.match(f.headings['navigation-status'].textContent, /prepared project/);
  assert.equal(f.headings['navigation-status'].hidden, false);
  f.click(f.links.plan); assert.equal(f.router.current().screen, 'plan'); assert.equal(f.router.current().project, null);
  assert.equal(f.headings['navigation-status'].hidden, true);
  f.router.setContext({projectId: 'P2', configurable: true});
  assert.equal(f.links['new-run'].getAttribute('aria-disabled'), null);
  assert.equal(f.links['project-settings'].getAttribute('aria-disabled'), null);
  f.router.setContext({workspace: false});
  assert.equal(f.links.plan.getAttribute('aria-disabled'), 'true');
  assert.equal(f.links.settings.getAttribute('aria-disabled'), 'true');
  f.router.navigate('new-run'); assert.equal(f.router.current().screen, 'new-run');
});

test('link contexts follow the current selection unless explicit overrides were authored', () => {
  const f = fixture(); f.router.setContext({workspace: true, projectId: 'P1', configurable: true});
  const pinned = f.doc.element('a', {'data-screen-link': 'run', 'data-run-tab': 'evidence', href: '?project=P3&run=R3#/run/evidence'});
  const card = f.doc.element('a', {'data-screen-link': 'plan', 'data-route-project': 'P4', 'data-route-run': '', href: '#/plan'});
  f.router.setContext({}); f.router.navigate('runs', {project: 'P2', run: 'R2'});
  f.click(f.links.run); assert.equal(f.router.current().project, 'P2'); assert.equal(f.router.current().run, 'R2');
  f.click(pinned); assert.equal(f.router.current().project, 'P3'); assert.equal(f.router.current().run, 'R3'); assert.equal(f.router.current().tab, 'evidence');
  f.click(card); assert.equal(f.router.current().project, 'P4'); assert.equal(f.router.current().run, null);
  assert.equal(f.click(f.links.runs, {ctrlKey: true}).defaultPrevented, false);
  pinned.setAttribute('target', '_blank'); assert.equal(f.click(pinned).defaultPrevented, false);
});

test('invalid and duplicate IDs are never exposed as resolved route context', () => {
  for (const query of ['project=P1&project=P2', 'project=../path', 'project=ghp_abcdefghijklmnopqrstuvwx', 'project=sk-proj-abcdefghijklmnopqrstuvwx', 'project=' + 'a'.repeat(65)]) {
    assert.equal(fixture('http://local/?' + query + '#/projects').router.current().project, null, query);
  }
  const f = fixture(); f.router.navigate('plan', {project: 'secret space', run: 'R2'});
  assert.equal(new URL(f.win.location.href).searchParams.has('project'), false); assert.equal(f.router.current().run, 'R2');
});

test('grouped screens and raw evidence visibility retain their independent state', () => {
  const f = fixture(); f.raw.hidden = false;
  f.router.navigate('settings'); assert.equal(f.shared.hidden, false); assert.equal(f.raw.hidden, false);
  f.router.navigate('project-settings', {step: 'tasks'}); assert.equal(f.shared.hidden, false); assert.equal(f.router.current().step, 'tasks');
  f.router.navigate('run', {tab: 'evidence'}); assert.equal(f.shared.hidden, true); assert.equal(f.raw.hidden, false);
  assert.equal(f.input.disabled, true); assert.equal(f.input.value, 'preserve draft');
});

test('run tabs support keyboard traversal without disturbing draft inputs', () => {
  const f = fixture('http://local/#/run/summary');
  const event = new Event('keydown', {target: f.tabs.summary, key: 'ArrowRight'}); f.doc.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true); assert.equal(f.router.current().tab, 'workers'); assert.equal(f.doc.activeElement, f.tabs.workers);
  assert.equal(f.tabs.workers.getAttribute('tabindex'), '0'); assert.equal(f.tabs.summary.getAttribute('tabindex'), '-1');
  f.doc.dispatchEvent(new Event('keydown', {target: f.tabs.workers, key: 'End'})); assert.equal(f.router.current().tab, 'evidence');
  f.doc.dispatchEvent(new Event('keydown', {target: f.tabs.evidence, key: 'Home'})); assert.equal(f.router.current().tab, 'summary');
  assert.equal(f.input.value, 'preserve draft');
});

test('keyboard routing skips hidden tabs and leaves repository source tabs to their own controller', () => {
  const f = fixture(); f.router.setContext({workspace: true, projectId: 'P1', configurable: true});
  const list = f.doc.element('nav', {role: 'tablist'});
  const hidden = f.doc.element('button', {role: 'tab', 'data-project-step': 'tasks'}, list); hidden.hidden = true;
  const account = f.doc.element('button', {role: 'tab'}, list); account.hidden = true;
  const profile = f.doc.element('button', {role: 'tab', 'data-project-step': 'profile'}, list);
  const disabled = f.doc.element('button', {role: 'tab', 'data-project-step': 'checks'}, list); disabled.disabled = true;
  const advanced = f.doc.element('button', {role: 'tab', 'data-project-step': 'advanced'}, list);
  f.router.navigate('project-settings', {step: 'advanced'});
  f.doc.dispatchEvent(new Event('keydown', {target: advanced, key: 'Home'}));
  assert.equal(f.router.current().step, 'profile'); assert.equal(f.doc.activeElement, profile);
  f.doc.dispatchEvent(new Event('keydown', {target: profile, key: 'ArrowLeft'}));
  assert.equal(f.router.current().step, 'advanced'); assert.equal(f.doc.activeElement, advanced);
  const sourceList = f.doc.element('nav', {role: 'tablist'});
  const source = f.doc.element('button', {role: 'tab'}, sourceList);
  f.doc.element('button', {role: 'tab'}, sourceList);
  const count = f.events.length;
  const event = new Event('keydown', {target: source, key: 'ArrowRight'}); f.doc.dispatchEvent(event);
  assert.equal(event.defaultPrevented, false); assert.equal(f.events.length, count); assert.equal(f.router.current().step, 'advanced');
});

test('mobile menu closes on Escape, outside click, and current route activation without submitting', () => {
  const f = fixture(); let submissions = 0; f.doc.addEventListener('submit', () => submissions++);
  f.click(f.toggle); assert.equal(f.toggle.getAttribute('aria-expanded'), 'true');
  f.doc.dispatchEvent(new Event('keydown', {key: 'Escape', target: f.links.projects}));
  assert.equal(f.toggle.getAttribute('aria-expanded'), 'false'); assert.equal(f.doc.activeElement, f.toggle);
  f.click(f.toggle); const outside = f.click(f.input);
  assert.equal(outside.defaultPrevented, true); assert.equal(outside.stopped, true); assert.equal(f.events.length, 0);
  f.click(f.toggle); f.click(f.links.projects); assert.equal(f.toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(submissions, 0); assert.equal(f.events.length, 0);
});

test('subscriptions are removable and destroy detaches event handlers', () => {
  const f = fixture(); const observed = []; const unsubscribe = f.router.subscribe((route) => observed.push(route));
  f.router.navigate('runs'); unsubscribe(); f.router.navigate('projects'); assert.equal(observed.length, 1);
  assert.throws(() => f.router.subscribe(null), TypeError);
  f.router.destroy(); f.win.externalHash('#evidence'); assert.equal(f.router.current().screen, 'projects');
});
