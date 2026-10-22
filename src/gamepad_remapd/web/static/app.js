'use strict';
/* Web UI ремапера. Без залежностей і без inline-скриптів (суворий CSP).
 * Дані з пристроїв (назви, hex) ніколи не йдуть в innerHTML — лише textContent. */

const $ = (s, r = document) => r.querySelector(s);
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

const TARGETS = ['A', 'B', 'X', 'Y', 'LB', 'RB', 'LT', 'RT', 'BACK', 'START', 'GUIDE', 'LS', 'RS',
  'UP', 'DOWN', 'LEFT', 'RIGHT', 'M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'C', 'Z'];
const EXTRAS = ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'C', 'Z'];

const S = {
  devices: [], dev: null, profile: null, profileName: null, profiles: [], warnings: [],
  live: null, stats: {}, daemon: false, sniff: null, tab: 'profile', dirty: true,
};

/* ---------------------------------------------------------------- API */
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const t = await r.text();
  let j = null;
  try { j = JSON.parse(t); } catch { /* не JSON */ }
  if (!r.ok) throw new Error((j && j.error) || r.statusText);
  return j;
}
let toastTimer = null;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg; t.className = bad ? 'bad' : ''; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 5000 : 1800);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

/* Дебаунс гарячих змін: повзунок може слати сотні подій/с, а профіль
 * перекомпілюється демоном лише раз на ~60 мс. Патчі зливаються (RFC 7386). */
const pending = {};
let patchTimer = null;
function deepMerge(dst, src) {
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      if (!dst[k] || typeof dst[k] !== 'object') dst[k] = {};
      deepMerge(dst[k], v);
    } else dst[k] = v;
  }
}
function queuePatch(obj) {
  deepMerge(pending, obj);
  clearTimeout(patchTimer);
  patchTimer = setTimeout(flushPatch, 60);
}
async function flushPatch() {
  patchTimer = null;
  const p = JSON.parse(JSON.stringify(pending));
  for (const k of Object.keys(pending)) delete pending[k];
  if (!S.dev) return;
  try {
    const r = await api('PATCH', `/api/devices/${S.dev}/profile`, p);
    S.profile = r.data; S.warnings = r.warnings || []; S.dirty = true;
  } catch (e) {
    toast(e.message, true);
    await loadProfile();            // повертаємо UI до фактичного стану демона
  }
}

/* ---------------------------------------------------------------- дані */
const curDev = () => S.devices.find(d => d.id === S.dev) || null;

async function loadDevices() { S.devices = await api('GET', '/api/devices'); pickDevice(); }
function pickDevice() {
  if (!S.devices.some(d => d.id === S.dev)) {
    const open = S.devices.find(d => d.open) || S.devices[0];
    S.dev = open ? open.id : null;
    S.profile = null;
    if (S.dev && curDev().open && !curDev().sniff_only) loadProfile();
  }
  renderHeader(); renderTab();
}
async function loadProfile() {
  const d = curDev();
  if (!d || !d.open || d.sniff_only) { S.profile = null; renderTab(); return; }
  try {
    const r = await api('GET', `/api/devices/${S.dev}/profile`);
    S.profile = r.data; S.profileName = r.name; S.warnings = r.warnings || [];
    S.profiles = await api('GET', '/api/profiles');
  } catch (e) { toast(e.message, true); }
  S.dirty = true; renderTab();
}

/* ---------------------------------------------------------------- WebSocket */
function connectWs() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.onmessage = e => onEvent(JSON.parse(e.data));
  ws.onclose = () => { setDaemon(false); setTimeout(connectWs, 1000); };
}
function setDaemon(up) {
  S.daemon = up;
  const b = $('#daemon');
  b.textContent = up ? 'демон підключено' : 'демон недоступний';
  b.className = 'badge ' + (up ? 'ok' : 'bad');
}
function onEvent(ev) {
  switch (ev.event) {
    case 'daemon':
      setDaemon(ev.connected);
      if (ev.connected) guard(loadDevices)();
      break;
    case 'devices': {
      const had = S.dev;
      S.devices = ev.devices; pickDevice();
      if (had !== S.dev || !S.profile) guard(loadProfile)();
      break;
    }
    case 'state': if (ev.dev === S.dev) { S.live = ev; S.dirty = true; } break;
    case 'profile':
      if (ev.dev === S.dev && !patchTimer) { S.profile = ev.data; S.profileName = ev.name; S.dirty = true; }
      break;
    case 'stats': S.stats = ev.stats; if (S.tab === 'stats') renderStats(); break;
  }
}

/* ---------------------------------------------------------------- шапка */
function renderHeader() {
  const sel = $('#device');
  sel.replaceChildren(...S.devices.map(d => h('option', { value: d.id, selected: d.id === S.dev },
    `${d.id} · ${d.vid}:${d.pid} · ${d.name || '?'}${d.open ? ' ●' : d.layout ? '' : ' (немає розкладки)'}`)));
  if (!S.devices.length) sel.replaceChildren(h('option', {}, 'пристроїв не знайдено'));
  const b = $('#btn-toggle'), d = curDev();
  b.hidden = !d;
  if (d) b.textContent = d.open ? 'Закрити' : (d.layout ? 'Відкрити' : 'Відкрити для sniffing');
  $('#inputs').replaceChildren(...((d && d.inputs) || []).map(([, n]) => h('span', { class: 'chip', 'data-n': n }, n)));
}
$('#device').addEventListener('change', e => { S.dev = e.target.value; S.profile = null; S.live = null; guard(loadProfile)(); renderHeader(); });
$('#btn-rescan').addEventListener('click', guard(async () => { S.devices = await api('POST', '/api/rescan'); pickDevice(); }));
$('#btn-toggle').addEventListener('click', guard(async () => {
  const d = curDev();
  if (d.open) await api('POST', `/api/devices/${d.id}/close`);
  else await api('POST', `/api/devices/${d.id}/open`, { layout: d.layout });
  await loadDevices(); await loadProfile();
}));

/* ---------------------------------------------------------------- вкладки */
$('#tabs').addEventListener('click', e => {
  const t = e.target.closest('[data-tab]'); if (!t) return;
  S.tab = t.dataset.tab;
  for (const b of $('#tabs').children) b.setAttribute('aria-selected', b === t);
  renderTab();
});
function renderTab() {
  clearInterval(sniffTimer); sniffTimer = null;
  const p = $('#panel'), d = curDev();
  if (!d) { p.replaceChildren(h('p', { class: 'muted' }, 'Підключіть геймпад або ресивер.')); return; }
  if (!d.open) {
    p.replaceChildren(h('p', { class: 'muted' }, d.error ? `Помилка: ${d.error}` : 'Пристрій не відкрито.'),
      d.layout ? null : h('p', { class: 'muted' }, 'Для цього VID:PID немає розкладки. Відкрийте пристрій у режимі sniffing, ' +
        'щоб знайти біти кнопок, і опишіть розкладку (див. docs/ARCHITECTURE.md).'));
    return;
  }
  if (S.tab === 'sniff') return renderSniff(p);
  if (S.tab === 'stats') { p.replaceChildren(h('div', { id: 'stats' })); return renderStats(); }
  if (d.sniff_only) { p.replaceChildren(h('p', { class: 'muted' }, 'Режим лише sniffing: профілі недоступні.')); return; }
  if (!S.profile) { p.replaceChildren(h('p', { class: 'muted' }, 'Завантаження профілю…')); return; }
  if (S.tab === 'buttons') return renderButtons(p, d);
  renderProfile(p, d);
}

/* ---------------------------------------------------------------- повзунки */
function slider(label, value, min, max, step, onInput, fmt = v => (+v).toFixed(2)) {
  const out = h('output', {}, fmt(value));
  const inp = h('input', { type: 'range', min, max, step, value, 'aria-label': label });
  inp.addEventListener('input', () => { out.textContent = fmt(inp.value); onInput(+inp.value); });
  return h('div', { class: 'row' }, h('span', {}, label), inp, out);
}
function check(label, value, onChange) {
  const c = h('input', { type: 'checkbox', checked: !!value });
  c.addEventListener('change', () => onChange(c.checked));
  return h('label', {}, c, ' ', label);
}

/* Крива: дзеркало python-функцій з logic/curves.py (лише для попереднього перегляду). */
function curveFn(c) {
  switch (c.type) {
    case 'power': return t => Math.pow(t, c.exponent);
    case 'smooth': return t => (1 - c.strength) * t + c.strength * t * t * (3 - 2 * t);
    case 'expo': return t => Math.expm1(c.k * t) / Math.expm1(c.k);
    default: return t => t;
  }
}
function curveEditor(curve, onChange) {
  curve = curve || { type: 'linear' };
  const cv = h('canvas', { width: 144, height: 144 });
  const draw = c => {
    const g = cv.getContext('2d'), s = getComputedStyle(document.documentElement);
    g.clearRect(0, 0, 144, 144); g.strokeStyle = s.getPropertyValue('--line'); g.strokeRect(.5, .5, 143, 143);
    g.strokeStyle = s.getPropertyValue('--accent'); g.lineWidth = 2; g.beginPath();
    const f = curveFn(c);
    for (let i = 0; i <= 64; i++) { const t = i / 64, y = Math.min(1, Math.max(0, f(t))); g[i ? 'lineTo' : 'moveTo'](t * 142 + 1, 143 - y * 142); }
    g.stroke();
  };
  const defaults = { linear: {}, power: { exponent: 1.6 }, smooth: { strength: 1 }, expo: { k: 3 } };
  const sel = h('select', { 'aria-label': 'Тип кривої' }, ['linear', 'power', 'smooth', 'expo'].map(t => h('option', { value: t, selected: t === curve.type }, t)));
  const param = h('div');
  const build = c => {
    draw(c);
    const send = nc => { onChange(nc); draw(nc); };
    if (c.type === 'power') param.replaceChildren(slider('exponent', c.exponent, 0.4, 4, 0.05, v => send({ type: 'power', exponent: v })));
    else if (c.type === 'smooth') param.replaceChildren(slider('strength', c.strength, 0, 1, 0.05, v => send({ type: 'smooth', strength: v })));
    else if (c.type === 'expo') param.replaceChildren(slider('k', c.k, -8, 8, 0.25, v => send({ type: 'expo', k: v || 0.25 })));
    else param.replaceChildren();
  };
  sel.addEventListener('change', () => { const c = { type: sel.value, ...defaults[sel.value] }; onChange(c); build(c); });
  build(curve);
  return h('div', { class: 'curve' }, cv, h('div', { style: 'flex:1' }, sel, param));
}

function stickCard(title, side, cfg) {
  const P = k => v => queuePatch({ sticks: { [side]: { [k]: v } } });
  return h('div', { class: 'card' }, h('h2', {}, title),
    slider('Внутр. мертва зона', cfg.inner_deadzone, 0, 0.9, 0.01, P('inner_deadzone')),
    slider('Зовн. мертва зона', cfg.outer_deadzone, 0.1, 1, 0.01, P('outer_deadzone')),
    slider('Чутливість', cfg.sensitivity, 0.1, 4, 0.05, P('sensitivity')),
    h('div', { class: 'inline' },
      h('select', { 'aria-label': 'Форма зони', onchange: e => P('shape')(e.target.value) },
        ['radial', 'axial'].map(s => h('option', { value: s, selected: s === cfg.shape }, s === 'radial' ? 'радіальна' : 'по осях'))),
      check('інверт. X', cfg.invert_x, P('invert_x')), check('інверт. Y', cfg.invert_y, P('invert_y')),
      check('X↔Y', cfg.swap_xy, P('swap_xy'))),
    curveEditor(cfg.curve, P('curve')));
}

function renderProfile(p, d) {
  const pr = S.profile;
  const sel = h('select', { id: 'prof-sel', 'aria-label': 'Профіль' },
    S.profiles.map(x => h('option', { value: x.name, selected: x.name === S.profileName }, `${x.name}${x.source === 'user' ? ' ★' : ''}`)));
  const el = [
    h('div', { class: 'card' }, h('h2', {}, 'Профіль'),
      h('div', { class: 'inline' }, sel,
        h('button', { class: 'primary', onclick: guard(async () => {
          const r = await api('PUT', `/api/devices/${S.dev}/profile`, { name: sel.value });
          S.profile = r.data; S.profileName = r.name; S.warnings = r.warnings; S.dirty = true; renderTab(); toast('Профіль застосовано');
        }) }, 'Застосувати'),
        h('button', { onclick: guard(async () => {
          const r = await api('PATCH', `/api/devices/${S.dev}/profile?save=1`, {});
          toast(`Збережено: ${r.name}`); S.profiles = await api('GET', '/api/profiles'); renderTab();
        }) }, 'Зберегти'),
        h('button', { onclick: guard(async () => {
          const name = prompt('Ім\'я нового профілю (літери, цифри, ._-):'); if (!name) return;
          await api('PUT', `/api/profiles/${encodeURIComponent(name)}`, { ...S.profile, name });
          await api('PUT', `/api/devices/${S.dev}/profile`, { name });
          await loadProfile(); toast(`Створено: ${name}`);
        }) }, 'Зберегти як…')),
      S.warnings.length ? h('p', { class: 'warn' }, S.warnings.join('; ')) : null),
    stickCard('Лівий стік', 'left', pr.sticks.left),
    stickCard('Правий стік', 'right', pr.sticks.right),
    h('div', { class: 'card' }, h('h2', {}, 'Тригери'),
      ['left', 'right'].map(s => [
        slider(`${s === 'left' ? 'LT' : 'RT'} внутр.`, pr.triggers[s].inner_deadzone, 0, 0.9, 0.01, v => queuePatch({ triggers: { [s]: { inner_deadzone: v } } })),
        slider(`${s === 'left' ? 'LT' : 'RT'} зовн.`, pr.triggers[s].outer_deadzone, 0.1, 1, 0.01, v => queuePatch({ triggers: { [s]: { outer_deadzone: v } } }))]),
      h('div', { class: 'inline' }, check('поміняти стіки', pr.swap_sticks, v => queuePatch({ swap_sticks: v })),
        check('поміняти тригери', pr.swap_triggers, v => queuePatch({ swap_triggers: v })))),
  ];
  if (d.has_gyro) {
    const g = pr.gyro, G = k => v => queuePatch({ gyro: { [k]: v } });
    const act = h('select', { 'aria-label': 'Кнопка активації гіро', onchange: e => G('activation')(e.target.value || null) },
      h('option', { value: '' }, 'завжди'), (d.inputs || []).map(([, n]) => h('option', { value: n, selected: n === g.activation }, n)));
    el.push(h('div', { class: 'card' }, h('h2', {}, 'Гіроскоп → стік'),
      h('div', { class: 'inline' }, check('увімкнено', g.enabled, G('enabled')), act),
      slider('Мертва зона °/с', g.deadzone_dps, 0, 50, 0.5, G('deadzone_dps'), v => (+v).toFixed(1)),
      slider('Повний хід °/с', g.full_dps, 20, 1000, 5, G('full_dps'), v => (+v).toFixed(0)),
      slider('Згладжування', g.smoothing, 0, 0.9, 0.01, G('smoothing')),
      h('div', { class: 'inline' }, check('інверт. yaw', g.invert_yaw, G('invert_yaw')), check('інверт. pitch', g.invert_pitch, G('invert_pitch'))),
      curveEditor(g.curve, G('curve'))));
  }
  p.replaceChildren(...el);
}

/* ---------------------------------------------------------------- ремапінг */
function renderButtons(p, d) {
  const bm = S.profile.button_map || {};
  const dl = h('datalist', { id: 'targets' }, TARGETS.map(t => h('option', { value: t })));
  const rows = (d.inputs || []).map(([bit, name]) => {
    const def = d.default_targets && d.default_targets[name] !== undefined ? d.default_targets[name]
      : (name.startsWith('DPAD_') ? name.slice(5) : null);
    const cur = bm[name] ? bm[name].join(',') : '';
    const inp = h('input', { type: 'text', value: cur, placeholder: def || '—', list: 'targets', 'aria-label': `Ціль для ${name}`, spellcheck: 'false' });
    inp.addEventListener('change', () => {
      const v = inp.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
      queuePatch({ button_map: { [name]: v.length === 0 ? null : (v[0] === 'NONE' ? 'NONE' : v) } });
    });
    return h('tr', { 'data-n': name }, h('td', { class: 'hex' }, name), h('td', { class: 'muted' }, def || '—'), h('td', {}, inp));
  });
  p.replaceChildren(h('p', { class: 'muted' }, 'Порожнє поле — ціль за замовчуванням з розкладки. Кілька цілей через кому (A,RT); NONE — вимкнути. Кількість одночасних кнопок не обмежена.'),
    dl, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Вхід'), h('th', {}, 'За замовч.'), h('th', {}, 'Ціль'))), h('tbody', {}, rows)));
}

/* ---------------------------------------------------------------- sniffer */
let sniffTimer = null;
function renderSniff(p) {
  const box = h('div', { id: 'sniffbox' });
  const act = (action, name) => guard(async () => { S.sniff = await api('POST', `/api/devices/${S.dev}/sniff`, { action, name }); paintSniff(); });
  const learnName = h('input', { type: 'text', placeholder: 'M1', value: 'M1', size: 6, 'aria-label': 'Ім\'я кнопки для learn' });
  p.replaceChildren(
    h('p', { class: 'muted' }, 'Hex-інспектор: 2.5 с НЕ торкайтесь геймпада (еталон спокою), потім натискайте M1–M4/C/Z та ін. ' +
      'Для навчання введіть ім\'я і натисніть «Вивчити», потім утримайте кнопку.'),
    h('div', { class: 'inline' }, h('button', { class: 'primary', onclick: act('start') }, 'Старт'),
      h('button', { onclick: act('reset') }, 'Скинути'), h('button', { onclick: act('stop') }, 'Стоп'),
      learnName, h('button', { onclick: guard(async () => act('learn', learnName.value.trim())()) }, 'Вивчити')), box);
  guard(async () => { S.sniff = await api('GET', `/api/devices/${S.dev}/sniff`); paintSniff(); })();
  sniffTimer = setInterval(guard(async () => {
    if (S.sniff && S.sniff.phase === 'off') return;
    S.sniff = await api('GET', `/api/devices/${S.dev}/sniff`); paintSniff();
  }), 400);
}
function paintSniff() {
  const box = $('#sniffbox'), s = S.sniff;
  if (!box || !s) return;
  if (s.phase === 'off') { box.replaceChildren(h('p', { class: 'muted' }, 'Sniffer вимкнено.')); return; }
  const recent = new Set(s.recent_changed || []);
  const hex = (s.hex || '').split(' ').filter(Boolean).map((b, i) => h('span', {}, recent.has(i) ? h('b', {}, b) : b, ' '));
  const rows = (s.candidates || []).map(c => h('tr', {}, h('td', {}, c.byte), h('td', {}, c.bit), h('td', { class: 'hex' }, c.mask),
    h('td', {}, c.presses), h('td', {}, c.down ? '●' : '')));
  const learned = s.layout_snippet || {};
  box.replaceChildren(
    s.phase === 'baseline' ? h('div', {}, h('span', {}, 'Еталон спокою… не торкайтесь'), h('div', { class: 'bar' }, h('i', { style: `width:${Math.round(s.progress * 100)}%` }))) : null,
    h('div', { class: 'hex' }, `${s.length} байт, ${s.reports} звітів`), h('div', { class: 'hex' }, hex),
    s.learning ? h('p', { class: 'warn' }, `Чекаю натискання для «${s.learning}»…`) : null,
    h('table', {}, h('thead', {}, h('tr', {}, ['байт', 'біт', 'маска', 'натисків', ''].map(x => h('th', {}, x)))), h('tbody', {}, rows)),
    (s.hints || []).map(x => h('p', { class: 'muted' }, x)),
    Object.keys(learned).length ? [h('h2', {}, 'Фрагмент "buttons" для розкладки'),
      h('textarea', { readonly: true, 'aria-label': 'JSON-фрагмент' }, JSON.stringify(learned, null, 2))] : null);
}

/* ---------------------------------------------------------------- затримка */
function renderStats() {
  const box = $('#stats'); if (!box) return;
  const st = S.stats[S.dev];
  if (!st || !st.count) { box.replaceChildren(h('p', { class: 'muted' }, 'Ще немає звітів.')); return; }
  const budget = 1500, p99 = st.p99_us || 0;
  box.replaceChildren(
    h('div', { class: 'card' }, h('h2', {}, 'Час обробки одного звіту (мкс)'),
      h('table', {}, [['звітів', st.count], ['середнє', st.mean_us], ['p50', st.p50_us], ['p99', st.p99_us], ['p99.9', st.p999_us],
        ['максимум', st.max_us], ['відкинуто фільтром', st.dropped]].map(([k, v]) => h('tr', {}, h('td', {}, k), h('td', { class: 'hex' }, v)))),
      h('div', { class: 'bar', title: 'p99 від бюджету 1500 мкс' }, h('i', { style: `width:${Math.min(100, p99 / budget * 100)}%` })),
      h('p', { class: 'muted' }, 'Це лише програмна обробка (parse → map → write). USB-інтервал опитування ресивера (зазвичай 1–8 мс) сюди не входить.'),
      h('button', { onclick: guard(async () => { await api('POST', `/api/devices/${S.dev}/stats/reset`); }) }, 'Скинути')));
}

/* ---------------------------------------------------------------- Canvas */
const cv = $('#pad'), g = cv.getContext('2d');
let css = null;
const color = n => (css || (css = getComputedStyle(document.documentElement))).getPropertyValue(n).trim();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { css = null; S.dirty = true; });

function fitCanvas() {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth || 760;
  cv.width = Math.round(w * dpr); cv.height = Math.round(w * dpr * 440 / 760); S.dirty = true;
}
addEventListener('resize', fitCanvas);

function circle(x, y, r, fill, stroke, dash) {
  g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2);
  if (fill) { g.fillStyle = fill; g.fill(); }
  if (stroke) { g.setLineDash(dash || []); g.strokeStyle = stroke; g.lineWidth = 1.5; g.stroke(); g.setLineDash([]); }
}
function rrect(x, y, w, hh, r, fill, stroke) {
  g.beginPath(); g.roundRect(x, y, w, hh, r);
  if (fill) { g.fillStyle = fill; g.fill(); }
  if (stroke) { g.strokeStyle = stroke; g.lineWidth = 1.5; g.stroke(); }
}
function label(t, x, y, on) {
  g.fillStyle = on ? color('--accent-ink') : color('--muted'); g.font = '600 13px system-ui, sans-serif';
  g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(t, x, y);
}

function drawStick(cx, cy, R, ax, ay, raw, cfg, pressed) {
  circle(cx, cy, R + 8, color('--pad'), color('--line'));
  if (cfg) {                                    // мертві зони з активного профілю
    circle(cx, cy, cfg.inner_deadzone * R, color('--dz'), color('--bad'), [4, 3]);
    circle(cx, cy, cfg.outer_deadzone * R, null, color('--muted'), [2, 4]);
  }
  circle(cx, cy, R, null, pressed ? color('--lit') : color('--line'));
  if (raw && raw[0] != null) circle(cx + raw[0] * R, cy + raw[1] * R, 6, null, color('--warn'));   // сирий вхід
  circle(cx + ax * R, cy + ay * R, 9, pressed ? color('--lit') : color('--text'));                // після обробки
}

function draw() {
  requestAnimationFrame(draw);
  if (!S.dirty) return;
  S.dirty = false;
  const k = cv.width / 760;
  g.setTransform(k, 0, 0, k, 0, 0);
  g.clearRect(0, 0, 760, 440);
  const live = S.live, on = new Set(live ? live.out : []);
  const ax = live ? live.ax : [0, 0, 0, 0, 0, 0], raw = live && live.raw;
  const cfgL = S.profile && S.profile.sticks.left, cfgR = S.profile && S.profile.sticks.right;
  const pad = color('--pad'), line = color('--line'), lit = color('--lit');

  g.beginPath(); g.roundRect(40, 80, 680, 300, 110); g.fillStyle = pad; g.fill(); g.strokeStyle = line; g.lineWidth = 2; g.stroke();
  // тригери та бампери
  for (const [name, x, v] of [['LT', 70, ax[4] / 255], ['RT', 580, ax[5] / 255]]) {
    rrect(x, 14, 110, 24, 6, null, line); rrect(x, 14, 110 * Math.min(1, Math.max(v, on.has(name) ? 1 : 0)), 24, 6, lit);
    label(name, x + 55, 26, v > 0.5 || on.has(name));
  }
  for (const [name, x] of [['LB', 70], ['RB', 580]]) { rrect(x, 46, 110, 26, 8, on.has(name) ? lit : null, line); label(name, x + 55, 59, on.has(name)); }
  // стіки: LS зверху-ліворуч, RS знизу-праворуч (розкладка Xbox)
  drawStick(175, 185, 52, ax[0] / 32767, ax[1] / 32767, raw && [raw[0], raw[1]], cfgL, on.has('LS'));
  drawStick(475, 290, 52, ax[2] / 32767, ax[3] / 32767, raw && [raw[2], raw[3]], cfgR, on.has('RS'));
  // D-pad
  for (const [n, x, y, w, hh] of [['UP', 270, 240, 30, 38], ['DOWN', 270, 307, 30, 38], ['LEFT', 232, 278, 38, 30], ['RIGHT', 300, 278, 38, 30]])
    rrect(x, y, w, hh, 5, on.has(n) ? lit : null, line);
  // лицьові кнопки
  for (const [n, x, y] of [['A', 585, 232], ['B', 632, 185], ['X', 538, 185], ['Y', 585, 138]]) { circle(x, y, 24, on.has(n) ? lit : null, line); label(n, x, y, on.has(n)); }
  // службові
  for (const [n, x, y] of [['BACK', 335, 170], ['START', 425, 170], ['GUIDE', 380, 125]]) { circle(x, y, 15, on.has(n) ? lit : null, line); label(n === 'GUIDE' ? 'G' : n === 'BACK' ? '⧉' : '☰', x, y, on.has(n)); }
  // додаткові кнопки (M1–M6, C, Z), яких немає в XInput
  EXTRAS.forEach((n, i) => { const x = 108 + i * 68; rrect(x, 396, 58, 28, 8, on.has(n) ? lit : null, line); label(n, x + 29, 410, on.has(n)); });

  // чіпи вхідних бітів: BigInt — маска довільної довжини (без ліміту rollover)
  const d = curDev();
  if (d && d.inputs) {
    const v = BigInt('0x' + ((live && live.src) || '0'));
    for (const c of $('#inputs').children) {
      const ent = d.inputs.find(i => i[1] === c.dataset.n);
      c.classList.toggle('on', !!ent && ((v >> BigInt(ent[0])) & 1n) === 1n);
    }
    if (S.tab === 'buttons') for (const tr of document.querySelectorAll('#panel tr[data-n]')) {
      const ent = d.inputs.find(i => i[1] === tr.dataset.n);
      tr.style.background = ent && ((v >> BigInt(ent[0])) & 1n) === 1n ? color('--dz') : '';
    }
  }
}

/* ---------------------------------------------------------------- старт */
fitCanvas(); draw(); connectWs();
guard(loadDevices)();
