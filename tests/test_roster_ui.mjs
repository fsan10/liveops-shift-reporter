import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Exercise the actual roster script with an empty/partial browser draft and a fixed Shanghai day.
const source = readFileSync(new URL('../liveops/static/roster.html', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
async function roster(storage = new Map()) {
  const nodes = new Map(), requests = [], scrolls = [], frames = [];
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
      colorOf, ensurePersonColors, renderAll, applyPeople,
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

test('roster restores the multicolor palette in list, calendar, sidebar and person menu', async () => {
  const ui = await roster(), data = ui.emptyMonth('2026-10');
  const anchors = ['主播甲', '主播乙', '主播丙', '主播丁', '主播戊'];
  const promoters = ['投手甲', '投手乙', '投手丙'];
  data.days[8].shifts = anchors.map((anchor, index) => ({
    time: `${6 + index * 3}:00-${9 + index * 3}:00`, anchor, ops: promoters[index % 3],
  }));
  ui.setData(data); ui.renderAll();
  const colors = [...anchors, ...promoters].map(ui.colorOf);
  assert.equal(new Set(colors).size, 8);
  assert.deepEqual(colors, ['#B3D502', '#FFC539', '#39C39F', '#4F83F9', '#FF6A9D', '#C8B8E7', '#A3BFEF', '#8FD1E1']);
  assert.doesNotMatch(ui.node('mainArea').innerHTML, /#D8D4E2/);
  for (const color of colors) assert.ok(ui.node('mainArea').innerHTML.includes(`background:${color}`));
  ui.renderWeek();
  for (const color of colors.slice(0, 5)) assert.ok(ui.node('mainArea').innerHTML.includes(`background:${color}22`));
  assert.ok(ui.node('anchorStats').innerHTML.includes(`background:${colors[0]}`));
  ui.applyPeople(anchors, promoters);
  ui.openShiftMenu(ui.node('card'), 9, 0); ui.openShiftSubmenu('ops');
  assert.ok(ui.node('shiftPersonOptions').innerHTML.includes(`background:${colors[5]}`));
});

test('sanitized empty personnel pools preserve the palette for newly configured example staff', async () => {
  const ui = await roster();
  ui.applyPeople([], []);
  ui.applyPeople(['示例主播甲', '示例主播乙'], ['示例投手甲', '示例投手乙']);
  const names = ['示例主播甲', '示例主播乙', '示例投手甲', '示例投手乙'];
  const colors = names.map(ui.colorOf);
  assert.deepEqual(colors, ['#B3D502', '#FFC539', '#C8B8E7', '#A3BFEF']);
  const data = ui.emptyMonth('2026-10');
  data.days[8].shifts = [{ time: '06:00-10:00', anchor: names[0], ops: names[2] }];
  ui.setData(data); ui.renderAll();
  ui.applyPeople([], []);
  assert.deepEqual(names.map(ui.colorOf), colors);
  assert.ok(ui.node('mainArea').innerHTML.includes('background:#B3D502'));
  ui.renderWeek();
  assert.ok(ui.node('mainArea').innerHTML.includes('background:#B3D50222'));
});

test('person colors survive reloads, pool changes and switching to months with different staff order', async () => {
  const ui = await roster(), data = ui.emptyMonth('2026-10');
  data.days[8].shifts = [{ anchor: '主播甲', ops: '投手甲' }, { anchor: '主播乙', ops: '投手乙' }];
  ui.ensurePersonColors(data.days);
  const before = ['主播甲', '主播乙', '投手甲', '投手乙'].map(ui.colorOf);
  const reloaded = await roster(ui.storage), nextMonth = ui.emptyMonth('2026-11');
  nextMonth.days[0].shifts = [{ anchor: '新增主播', ops: '新增投手' }, ...data.days[8].shifts.toReversed()];
  reloaded.ensurePersonColors(nextMonth.days);
  reloaded.applyPeople(['新增主播', '主播乙'], ['新增投手']);
  assert.deepEqual(['主播甲', '主播乙', '投手甲', '投手乙'].map(reloaded.colorOf), before);
  assert.notEqual(reloaded.colorOf('新增主播'), '#D8D4E2');
  assert.notEqual(reloaded.colorOf('新增投手'), '#D8D4E2');
  assert.notEqual(reloaded.colorOf('不在名单的历史人员'), '#D8D4E2');
  assert.equal(reloaded.colorOf('不在名单的历史人员'), ui.colorOf('不在名单的历史人员'));
  reloaded.applyPeople(['候选新主播'], ['候选新投手']);
  const candidateColors = ['候选新主播', '候选新投手'].map(reloaded.colorOf);
  nextMonth.days[1].shifts = [{ anchor: '候选新主播', ops: '候选新投手' }];
  reloaded.ensurePersonColors(nextMonth.days);
  assert.deepEqual(['候选新主播', '候选新投手'].map(reloaded.colorOf), candidateColors);
});

test('invalid or unavailable browser color cache cannot break roster rendering', async () => {
  for (const cached of ['broken JSON', 'null', '[]', '{"主播甲":"url(example)","主播乙":42}']) {
    const storage = new Map([['liveops_roster_person_colors_v1', cached]]), ui = await roster(storage);
    const data = ui.emptyMonth('2026-10'); data.days[8].shifts = [{ anchor: '主播甲', ops: '投手甲' }];
    ui.setData(data); ui.renderAll();
    assert.notEqual(ui.colorOf('主播甲'), '#D8D4E2');
    assert.match(ui.colorOf('__proto__'), /^#[\dA-F]{6}$/);
    storage.set = () => { throw new Error('Storage unavailable'); };
    assert.doesNotThrow(() => ui.ensurePersonColors([{ shifts: [{ anchor: '新增主播' }] }]));
  }
});

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
