import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the actual roster script with an empty/partial browser draft and a fixed Shanghai day.
const source = readFileSync(new URL('../liveops/static/roster.html', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
async function roster() {
  const nodes = new Map(), requests = [], scrolls = [], frames = [], storage = new Map();
  const node = key => {
    if (!nodes.has(key)) {
      const classes = new Set(), listeners = new Map(), attributes = new Map();
      nodes.set(key, {
      innerHTML: '', textContent: '', value: '', style: { setProperty() {} }, dataset: {},
      classList: { toggle(value, on) { if (on) classes.add(value); else classes.delete(value); },
        add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); } },
      addEventListener(type, callback) { listeners.set(type, [...(listeners.get(type) || []), callback]); },
      dispatch(type, event) { for (const callback of listeners.get(type) || []) callback(event); },
      setAttribute(name, value) { attributes.set(name, value); }, getAttribute(name) { return attributes.get(name); },
      appendChild() {}, focus() {}, remove() {},
      getBoundingClientRect() { return { top: key.includes('is-today') ? 300 : 0, left: 200, right: 430, bottom: 200, width: 230, height: 64 }; },
      getClientRects() { return [1]; },
      querySelector(selector) {
        const field = selector.match(/data-shift-field="(\w+)"/)?.[1];
        return field ? node(`menu-${field}`) : node(`${key}-${selector}`);
      },
      querySelectorAll() { return key === 'shiftMenu' ? ['time', 'anchor', 'ops'].map(field => {
        const button = node(`menu-${field}`); button.dataset.shiftField = field; return button;
      }) : []; },
      });
    }
    return nodes.get(key);
  };
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : ['2026-10-09T08:00:00+08:00'])); }
    static now() { return new Clock().getTime(); }
  }
  const context = vm.createContext({
    Date: Clock, console, setTimeout() {}, clearTimeout() {},
    ResizeObserver: class { observe() {} },
    requestAnimationFrame(callback) { frames.push(callback); },
    document: {
      getElementById: node, querySelector: node, querySelectorAll() { return []; },
      body: node('body'), documentElement: node('html'), addEventListener() {},
      createElement: node,
    },
    location: { origin: 'http://localhost' },
    localStorage: { getItem(key) { return storage.get(key); }, setItem(key, value) { storage.set(key, value); }, removeItem(key) { storage.delete(key); } },
    fetch: async (path, options = {}) => {
      requests.push({ path, method: options.method || 'GET', body: options.body && JSON.parse(options.body) });
      return { ok: true, json: async () => path === '/api/settings' ? { settings: {} }
        : options.method === 'POST' ? { item: { id: 100 } } : { items: [], days: {} } };
    },
  });
  const window = { scrollY: 0, innerWidth: 1280, innerHeight: 800, addEventListener() {},
    scrollTo(...args) { scrolls.push(args); } };
  window.parent = window;
  context.window = window;
  vm.runInContext(script + `
    globalThis.rosterTest = {
      shiftDates, interruptButtonHTML, isPastDay, emptyMonth, renderList, renderWeek,
      addShift, openCurrentRoster, focusToday, loadRemote, openShiftMenu, openShiftSubmenu, setCalendarShiftField,
      setData(value) { data=value;serverData=emptyMonth(currentMonth); },
      getData() { return data; },
      setMonth(value) { currentMonth=value; },
      setHistory(value) { historyExpanded=value; },
      sync() { return syncData(); }
    };
  `, context);
  await new Promise(resolve => setImmediate(resolve));
  return { ...context.rosterTest, node, storage, requests, scrolls, frames };
}

test('missing, blank, partial and invalid times never crash the interruption renderer', async () => {
  const ui = await roster(), day = { date: '2026/10/9' };
  for (const time of [undefined, null, '', '06:00', '06:00-', '25:00-26:00', '06:90-08:00', '06:00-06:00']) {
    assert.equal(ui.shiftDates(day, { time }), null);
    assert.match(ui.interruptButtonHTML(day, { time }, 9, 0), /disabled/);
  }
  const dates = ui.shiftDates(day, { time: '22:00-02:00' });
  assert.equal(dates.end.toISOString(), '2026-10-09T18:00:00.000Z');
  assert.equal(dates.end - dates.start, 4 * 3600000);
});

test('history is collapsed and read-only; today and future remain usable even with empty drafts', async () => {
  const ui = await roster(), data = ui.emptyMonth('2026-10');
  for (const day of [1, 9, 10]) data.days[day - 1].shifts.push({ time: '', anchor: '', ops: '' });
  ui.setData(data);
  ui.renderList();
  const html = ui.node('mainArea').innerHTML;
  assert.match(html, /<details class="history-days" >/);
  const historical = html.match(/<section[^>]*data-daycard="1"[\s\S]*?<\/section>/)[0];
  assert.match(historical, /is-past/);
  assert.doesNotMatch(historical, /data-edit=|tabindex="0"/);
  assert.match(historical, /data-add="shift"[^>]*disabled/);
  assert.match(historical, /data-action="delete"[^>]*disabled/);
  const today = html.match(/<section[^>]*data-daycard="9"[\s\S]*?<\/section>/)[0];
  assert.match(today, /is-today/);
  assert.match(today, /aria-current="date"/);
  assert.match(today, /未设置时段/);
  assert.match(today, /data-add="shift" data-day="9" >/);
});

test('calendar preserves gray history without editable or add targets and frames the entire today', async () => {
  const ui = await roster(), data = ui.emptyMonth('2026-10');
  data.days[0].shifts.push({ time: '', anchor: '', ops: '' });
  ui.setData(data); ui.renderWeek();
  const html = ui.node('mainArea').innerHTML;
  const history = html.slice(html.indexOf('data-date="2026-10-01"'), html.indexOf('data-date="2026-10-02"'));
  assert.doesNotMatch(history, /data-edit=|data-add=/);
  assert.match(html, /week-day [^"]*is-today" data-date="2026-10-09" aria-current="date"/);
  assert.doesNotMatch(html, /history-days/);
});

test('adding a past shift is refused and adding an incomplete future shift renders safely', async () => {
  const ui = await roster();
  ui.addShift(1);
  assert.equal(ui.getData().days[0].shifts.length, 0);
  ui.addShift(10);
  assert.equal(ui.getData().days[9].shifts.length, 1);
  assert.match(ui.node('mainArea').innerHTML, /未设置时段/);
  await ui.sync();
  assert.equal(ui.requests.filter(item => item.method !== 'GET').length, 0);
});

test('remove only the accidental empty October 1 draft and preserve other historical data', async () => {
  const ui = await roster();
  ui.storage.set('live_schedule_drafts_2026-10', JSON.stringify([
    { date: '2026/10/1', shifts: [{ time: '', anchor: '', ops: '' }, { time: '06:00-10:00', anchor: '旧草稿', ops: '' }] },
    { date: '2026/10/2', shifts: [{ time: '', anchor: '', ops: '' }] },
  ]));
  await ui.loadRemote();
  assert.equal(ui.getData().days[0].shifts.length, 1);
  assert.equal(ui.getData().days[0].shifts[0].anchor, '旧草稿');
  assert.equal(ui.getData().days[1].shifts.length, 1);
  assert.equal(JSON.parse(ui.storage.get('live_schedule_drafts_2026-10'))[0].shifts.length, 1);
  await ui.sync();
  assert.equal(ui.requests.filter(item => item.method !== 'GET').length, 0);
});

test('calendar submenu saves time, host and promoter together and updates existing schedules', async () => {
  const ui = await roster(); ui.addShift(10);
  const card = ui.node('calendar-card'); card.dataset = { day: '10', shift: '0' };
  ui.node('mainArea').dispatch('click', { target: { closest(selector) {
    return selector === '[data-edit="shift"]' ? card : null;
  } } });
  assert.ok(ui.node('shiftMenu').classList.contains('show'));
  for (const [field, value] of [['time', '22:00-02:00'], ['ops', '测试投手C'], ['anchor', '测试主播A']]) {
    ui.node(`menu-${field}`).dispatch('pointerenter', {});
    assert.ok(ui.node('shiftSubmenu').classList.contains('show'));
    assert.equal(ui.node(`menu-${field}`).getAttribute('aria-expanded'), 'true');
    assert.equal(ui.setCalendarShiftField(field, value), true);
  }
  await ui.sync();
  const created = ui.requests.find(item => item.method === 'POST');
  assert.equal(created.body.start_time, '22:00');
  assert.equal(created.body.end_time, '02:00');
  assert.equal(created.body.host_name, '测试主播A');
  assert.equal(created.body.promoter, '测试投手C');
  assert.equal(ui.getData().days[9].shifts[0].id, 100);
  assert.equal(ui.setCalendarShiftField('time', '06:00-06:00'), false);
  assert.equal(ui.setCalendarShiftField('anchor', ''), false);
  ui.setCalendarShiftField('time', '21:00-01:00');
  ui.setCalendarShiftField('ops', '测试投手A'); await ui.sync();
  const updated = ui.requests.filter(item => item.method === 'PUT').at(-1);
  assert.equal(updated.body.start_time, '21:00');
  assert.equal(updated.body.promoter, '测试投手A');
});

test('calendar menu cannot edit history or apply settings to a different month', async () => {
  const ui = await roster(), data = ui.emptyMonth('2026-10');
  data.days[0].shifts.push({ time: '06:00-10:00', anchor: '测试主播A', ops: '测试投手A' });
  data.days[9].shifts.push({ time: '', anchor: '', ops: '' }); ui.setData(data);
  ui.openShiftMenu(ui.node('card'), 1, 0);
  assert.equal(ui.setCalendarShiftField('ops', '测试投手B'), false);
  ui.openShiftMenu(ui.node('card'), 10, 0); ui.setMonth('2026-11');
  assert.equal(ui.setCalendarShiftField('ops', '测试投手B'), false);
  assert.equal(ui.requests.filter(item => item.method !== 'GET').length, 0);
});

test('entering the roster restores current month and requests today visibility each time', async () => {
  const ui = await roster();
  ui.setMonth('2026-09'); ui.setHistory(true);
  await ui.openCurrentRoster();
  assert.ok(ui.requests.some(item => item.path.includes('year=2026&month=10')));
  assert.match(ui.node('mainArea').innerHTML, /<details class="history-days" >/);
  for (const callback of ui.frames.splice(0)) callback();
  const count = ui.scrolls.length;
  await ui.openCurrentRoster();
  for (const callback of ui.frames.splice(0)) callback();
  assert.ok(ui.scrolls.length > count);
  assert.equal(ui.scrolls.at(-1)[0].top, 156);
});
