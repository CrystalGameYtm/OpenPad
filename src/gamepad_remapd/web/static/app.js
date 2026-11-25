'use strict';
/* Web UI for the remapper. No dependencies and no inline scripts (strict CSP).
 * Device data (names, hex) never goes into innerHTML — only textContent. */

const $ = (s, r = document) => r.querySelector(s);
/* replaceChildren without "null"/"false" in the DOM: empty values are simply skipped. */
const _rc = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...k) { _rc.apply(this, k.flat(Infinity).filter(x => x != null && x !== false)); };
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k === 'style' && typeof v === 'string') {   // CSP forbids the style attribute: we set it via CSSOM
      for (const decl of v.split(';')) { const i = decl.indexOf(':'); if (i > 0) e.style.setProperty(decl.slice(0, i).trim(), decl.slice(i + 1).trim()); }
    }
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

const TARGETS = ['A', 'B', 'X', 'Y', 'LB', 'RB', 'LT', 'RT', 'BACK', 'START', 'GUIDE', 'LS', 'RS',
  'UP', 'DOWN', 'LEFT', 'RIGHT', 'M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'C', 'Z'];
/* Keyboard/mouse as a button target (the virtual uinput keyboard): KEY_<name>, MOUSE_<button>. */
const KEY_TARGETS = [
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'.split('').map(c => 'KEY_' + c),
  ...Array.from({ length: 12 }, (_, i) => 'KEY_F' + (i + 1)),
  'KEY_SPACE', 'KEY_ENTER', 'KEY_ESC', 'KEY_TAB', 'KEY_BACKSPACE', 'KEY_LEFTSHIFT', 'KEY_LEFTCTRL', 'KEY_LEFTALT',
  'KEY_UP', 'KEY_DOWN', 'KEY_LEFT', 'KEY_RIGHT', 'KEY_HOME', 'KEY_END', 'KEY_PAGEUP', 'KEY_PAGEDOWN', 'KEY_INSERT', 'KEY_DELETE',
  'KEY_GRAVE', 'KEY_MINUS', 'KEY_EQUAL', 'KEY_COMMA', 'KEY_DOT', 'KEY_SLASH',
  'KEY_VOLUMEUP', 'KEY_VOLUMEDOWN', 'KEY_MUTE', 'KEY_PLAYPAUSE', 'KEY_NEXTSONG', 'KEY_PREVIOUSSONG',
  'MOUSE_LEFT', 'MOUSE_RIGHT', 'MOUSE_MIDDLE', 'MOUSE_SIDE', 'MOUSE_EXTRA'];
const EXTRAS = ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'C', 'Z'];

const S = {
  devices: [], dev: null, profile: null, profileName: null, profiles: [], warnings: [],
  live: null, stats: {}, daemon: false, sniff: null, tab: 'buttons', sub: 'stats', dirty: true, sys: null,
  sel: null,            // the selected input name (diagram, chips and the Buttons tab stay in sync)
  unsaved: false,       // the live profile differs from the saved one
};

/* "Unsaved changes" marker: edits apply to the running profile at once; Save writes them to the profile file. */
function markUnsaved(v) {
  S.unsaved = v;
  const b = $('#unsaved'); if (b) b.hidden = !v;
  const s = $('#prof-save'); if (s) s.classList.toggle('primary', v);
}

/* ---------------------------------------------------------------- API */
async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { 'X-Lang': I18N.state.lang, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const t = await r.text();
  let j = null;
  try { j = JSON.parse(t); } catch { /* not JSON */ }
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

/* Debounce of hot changes: a slider can send hundreds of events/s, while the profile
 * is recompiled by the daemon only once every ~60 ms. Patches are merged (RFC 7386). */
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
    S.profile = r.data; S.warnings = r.warnings || []; S.dirty = true; markUnsaved(true);
  } catch (e) {
    toast(e.message, true);
    await loadProfile();            // we return the UI to the daemon's actual state
  }
}

/* ---------------------------------------------------------------- data */
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
  S.unsaved = false; S.dirty = true; renderTab();
}

/* ---------------------------------------------------------------- WebSocket */
function connectWs() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?lang=' + encodeURIComponent(I18N.state.lang));
  ws.onmessage = e => onEvent(JSON.parse(e.data));
  ws.onclose = () => { setDaemon(false); setTimeout(connectWs, 1000); };
}
function setDaemon(up) {
  S.daemon = up;
  const b = $('#daemon');
  b.textContent = up ? tx('daemon.up') : tx('daemon.down');
  b.className = 'badge ' + (up ? 'ok' : 'bad');
}
function onEvent(ev) {
  switch (ev.event) {
    case 'daemon':
      setDaemon(ev.connected);
      if (ev.connected) guard(loadDevices)();
      break;
    case 'devices':
      guard(async () => {                       // re-fetched so the texts come in the page language
        const had = S.dev;
        S.devices = await api('GET', '/api/devices'); pickDevice();
        if (had !== S.dev || !S.profile) await loadProfile();
      })();
      break;
    case 'state': if (ev.dev === S.dev) { S.live = ev; S.dirty = true; } break;
    case 'profile':
      if (ev.dev === S.dev && !patchTimer) { S.profile = ev.data; S.profileName = ev.name; S.dirty = true; }
      break;
    case 'stats': S.stats = ev.stats; if (S.tab === 'diag' && S.sub === 'stats') renderStats(); break;
  }
}

/* ---------------------------------------------------------------- header */
function renderHeader() {
  const sel = $('#device');
  sel.replaceChildren(...S.devices.map(d => h('option', { value: d.id, selected: d.id === S.dev },
    `${d.id} · ${d.vid}:${d.pid} · ${d.name || '?'}${d.open ? ' ●' : d.layout ? '' : ' (' + tx('dev.no_layout') + ')'}`)));
  if (!S.devices.length) sel.replaceChildren(h('option', {}, tx('dev.none_found')));
  const b = $('#btn-toggle'), d = curDev();
  b.hidden = !d;
  if (d) b.textContent = d.open ? tx('btn.close') : (d.layout ? tx('btn.open') : tx('btn.open_sniff'));
  const pr = d && d.protocol, box = $('#protocol');
  box.hidden = !pr;
  if (pr) {
    $('#protocol-summary').textContent = tx('protocol.summary', { s: pr.summary });
    $('#protocol-items').replaceChildren(...pr.items.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
  }
  renderHideBadge(d);
  const auto = $('#auto-badge'), isAuto = !!(d && d.layout && String(d.layout).startsWith('auto_'));
  auto.hidden = !isAuto;
  if (isAuto) { auto.textContent = tx('hdr.auto'); auto.title = tx('hdr.auto_hint'); }
  $('#inputs').replaceChildren(...((d && d.inputs) || []).map(([, n]) =>
    h('span', { class: 'chip', 'data-n': n, role: 'button', tabindex: '0', title: tx('btn.select_title', { name: n }) }, n)));
}
function renderHideBadge(d) {
  const b = $('#hide-badge'), hs = d && d.open && d.options && d.hide_state;
  b.hidden = !hs;
  if (!hs) return;
  let text, cls;
  if (!hs.requested) [text, cls] = [tx('hide.off'), 'warn'];
  else if (hs.mode === 'hidden') [text, cls] = [tx('hide.hidden', { n: (hs.nodes || []).length }), 'ok'];
  else if (hs.mode === 'partial') [text, cls] = [tx('hide.partial'), 'warn'];
  else if (hs.mode === 'denied') [text, cls] = [tx('hide.denied'), 'bad'];
  else [text, cls] = [tx('hide.none'), 'ok'];
  b.textContent = text;
  b.className = 'badge ' + cls;
  b.title = tx('hide.open_settings');
}
$('#hide-badge').addEventListener('click', () => { S.tab = 'device'; renderTab(); });
$('#auto-badge').addEventListener('click', () => { S.tab = 'device'; renderTab(); });
$('#inputs').addEventListener('click', e => { const c = e.target.closest('.chip'); if (c) selectInput(c.dataset.n); });
$('#inputs').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const c = e.target.closest('.chip'); if (c) { e.preventDefault(); selectInput(c.dataset.n); }
});
$('#device').addEventListener('change', e => { S.dev = e.target.value; S.profile = null; S.live = null; S.sel = null; S.unsaved = false; guard(loadProfile)(); renderHeader(); });
$('#btn-rescan').addEventListener('click', guard(async () => { S.devices = await api('POST', '/api/rescan'); pickDevice(); }));
$('#btn-toggle').addEventListener('click', guard(async () => {
  const d = curDev();
  if (d.open) await api('POST', `/api/devices/${d.id}/close`);
  else await api('POST', `/api/devices/${d.id}/open`, { layout: d.layout });
  await loadDevices(); await loadProfile();
}));

/* ---------------------------------------------------------------- tabs */
/* Structure: each setting lives in its own tab.
 *   Device     — protocol/mode, output (XInput/DS4/DInput), hiding, rumble, layout
 *   Buttons    — button remapping (including to keys/mouse)
 *   Sticks     — dead zones, curves, sensitivity
 *   Triggers   — curves, modes, stages, button → analog
 *   Gyro       — gyro aiming, live readouts, calibration (only if there is a sensor)
 *   Profiles   — list, apply, import/export, JSON editor
 *   Diagnostics — latency, hex sniffer
 *   System     — SDL, unhide, udev, HidHide, keyboards
 * needs: 'none' — works without a device; 'open' — needs an open device; 'profile' — also needs a profile. */
const TABS = [
  { id: 'device', key: 'tab.device', needs: 'device' },
  { id: 'buttons', key: 'tab.buttons', needs: 'profile' },
  { id: 'sticks', key: 'tab.sticks', needs: 'profile' },
  { id: 'triggers', key: 'tab.triggers', needs: 'profile' },
  { id: 'gyro', key: 'tab.gyro', needs: 'profile', only: d => d.has_gyro },
  { id: 'profiles', key: 'tab.profiles', needs: 'profile' },
  { id: 'diag', key: 'tab.diag', needs: 'open' },
  { id: 'system', key: 'tab.system', needs: 'none' },
];
function renderNav() {
  const d = curDev(), nav = $('#tabs');
  const shown = TABS.filter(tb => !tb.only || (d && tb.only(d)));
  if (!shown.some(tb => tb.id === S.tab)) S.tab = shown[0].id;
  nav.replaceChildren(...shown.map(tb => h('button', { role: 'tab', 'data-tab': tb.id, 'aria-selected': tb.id === S.tab }, tx(tb.key))));
}
$('#tabs').addEventListener('click', e => {
  const t = e.target.closest('[data-tab]'); if (!t) return;
  S.tab = t.dataset.tab; renderTab();
});
function renderTab() {
  clearInterval(sniffTimer); sniffTimer = null;
  renderNav();
  const p = $('#panel'), d = curDev(), tab = TABS.find(t => t.id === S.tab);
  const bar = $('#profbar');
  bar.hidden = true;
  if (tab.needs === 'none') return renderSystem(p);
  if (!d) { p.replaceChildren(h('p', { class: 'muted' }, tx('msg.connect'))); return; }
  if (tab.id === 'device') return renderDevice(p, d);
  if (!d.open) {
    p.replaceChildren(h('p', { class: 'muted' }, d.error ? tx('err.prefix', { e: d.error }) : tx('msg.not_open')),
      d.layout ? null : h('p', { class: 'muted' }, tx('msg.no_layout_hint')));
    return;
  }
  if (tab.id === 'diag') return renderDiag(p);
  if (d.sniff_only) { p.replaceChildren(h('p', { class: 'muted' }, tx('msg.sniff_only'))); return; }
  if (!S.profile) { p.replaceChildren(h('p', { class: 'muted' }, tx('msg.loading_profile'))); return; }
  renderProfBar(bar);
  ({ buttons: renderButtons, sticks: renderSticks, triggers: renderTriggers, gyro: renderGyro, profiles: renderProfiles })[tab.id](p, d);
}

/* ---------------------------------------------------------------- sliders */
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

/* Curve: a mirror of the python functions from logic/curves.py (for preview only). */
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
  const sel = h('select', { 'aria-label': tx('curve.type') }, ['linear', 'power', 'smooth', 'expo'].map(t => h('option', { value: t, selected: t === curve.type }, t)));
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
    slider(tx('stick.inner_dz'), cfg.inner_deadzone, 0, 0.9, 0.01, P('inner_deadzone')),
    slider(tx('stick.outer_dz'), cfg.outer_deadzone, 0.1, 1, 0.01, P('outer_deadzone')),
    slider(tx('common.sensitivity'), cfg.sensitivity, 0.1, 4, 0.05, P('sensitivity')),
    h('div', { class: 'inline' },
      h('select', { 'aria-label': tx('stick.shape'), onchange: e => P('shape')(e.target.value) },
        ['radial', 'axial'].map(s => h('option', { value: s, selected: s === cfg.shape }, s === 'radial' ? tx('stick.radial') : tx('stick.axial')))),
      check(tx('stick.invert_x'), cfg.invert_x, P('invert_x')), check(tx('stick.invert_y'), cfg.invert_y, P('invert_y')),
      check('X↔Y', cfg.swap_xy, P('swap_xy'))),
    curveEditor(cfg.curve, P('curve')));
}

/* ---------------------------------------------------------------- triggers */
function triggerCard(side, cfg) {
  const nm = side === 'left' ? 'LT' : 'RT', T = k => v => queuePatch({ triggers: { [side]: { [k]: v } } });
  const mode = h('select', { 'aria-label': tx('trig.mode_aria', { nm }), onchange: e => {
    cfg.mode = e.target.value; T('mode')(cfg.mode); renderTab();
  } }, [['analog', tx('trig.mode.analog')], ['digital', tx('trig.mode.digital')], ['off', tx('trig.mode.off')]]
    .map(([v, t]) => h('option', { value: v, selected: v === cfg.mode }, t)));
  const dl = h('datalist', { id: 'targets-' + side }, TARGETS.concat(KEY_TARGETS).map(t => h('option', { value: t })));
  const stageRow = (name, label, defAt) => {
    const st = (cfg.stages || {})[name] || { at: defAt, targets: [] };
    const inp = h('input', { type: 'text', value: st.targets.join(','), placeholder: tx('trig.placeholder'), list: 'targets-' + side,
      'aria-label': tx('trig.stage_target', { label, nm }), spellcheck: 'false' });
    inp.addEventListener('change', () => {
      const v = inp.value.split(',').map(x => x.trim().toUpperCase()).filter(Boolean);
      T('stages')({ [name]: { targets: v, at: st.at } });
    });
    return h('div', {}, h('div', { class: 'inline' }, h('span', { class: 'muted' }, label), inp),
      slider(tx('trig.at'), st.at, 0.02, 1, 0.01, v => { st.at = v; T('stages')({ [name]: { at: v, targets: st.targets } }); }));
  };
  return h('div', { class: 'card' }, h('h2', {}, tx('trig.title', { nm })),
    h('div', { class: 'inline' }, h('span', { class: 'muted' }, tx('common.mode')), mode,
      check(tx('trig.invert'), cfg.invert, T('invert'))),
    cfg.mode === 'digital' ? slider(tx('trig.threshold'), cfg.threshold, 0.02, 1, 0.01, T('threshold')) : null,
    cfg.mode !== 'off' ? [
      slider(tx('trig.dz_start'), cfg.inner_deadzone, 0, 0.9, 0.01, T('inner_deadzone')),
      slider(tx('trig.dz_end'), cfg.outer_deadzone, 0.1, 1, 0.01, T('outer_deadzone')),
      slider(tx('common.sensitivity'), cfg.sensitivity, 0.1, 4, 0.05, T('sensitivity')),
      cfg.mode === 'analog' ? curveEditor(cfg.curve, T('curve')) : null] : null,
    h('h3', {}, tx('trig.stages_h')),
    h('p', { class: 'muted' }, tx('trig.stages_help')),
    dl, stageRow('soft', tx('trig.soft'), 0.3), stageRow('full', tx('trig.full'), 0.9),
    h('h3', {}, tx('trig.btn_analog', { nm })),
    slider(tx('trig.btn_value', { nm }), cfg.button_value, 0, 1, 0.01, T('button_value'),
      v => Math.round(v * 100) + '%'));
}

function renderProfBar(bar) {
  bar.hidden = false;
  const sel = h('select', { id: 'prof-sel', 'aria-label': tx('common.profile') },
    S.profiles.map(x => h('option', { value: x.name, selected: x.name === S.profileName }, `${x.name}${x.source === 'user' ? ' ★' : ''}`)));
  bar.replaceChildren(h('span', { class: 'muted' }, tx('common.profile')), sel,
    h('button', { class: 'primary', onclick: guard(async () => {
      const r = await api('PUT', `/api/devices/${S.dev}/profile`, { name: sel.value });
      S.profile = r.data; S.profileName = r.name; S.warnings = r.warnings; S.dirty = true; S.unsaved = false; renderTab(); toast(tx('prof.applied'));
    }) }, tx('common.apply')),
    h('button', { id: 'prof-save', class: S.unsaved ? 'primary' : '', onclick: guard(async () => {
      const r = await api('PATCH', `/api/devices/${S.dev}/profile?save=1`, {});
      S.unsaved = false; toast(tx('prof.saved', { name: r.name })); S.profiles = await api('GET', '/api/profiles'); renderTab();
    }) }, tx('common.save')),
    h('button', { onclick: guard(async () => {
      const name = prompt(tx('prof.prompt_name')); if (!name) return;
      await api('PUT', `/api/profiles/${encodeURIComponent(name)}`, { ...S.profile, name });
      await api('PUT', `/api/devices/${S.dev}/profile`, { name });
      await loadProfile(); toast(tx('prof.created', { name }));
    }) }, tx('prof.save_as')),
    h('span', { id: 'unsaved', class: 'badge warn', hidden: !S.unsaved, role: 'status' }, tx('prof.unsaved')),
    S.warnings.length ? h('span', { class: 'warn' }, S.warnings.join('; ')) : null);
}

function renderSticks(p) {
  const pr = S.profile;
  p.replaceChildren(stickCard(tx('stick.left'), 'left', pr.sticks.left), stickCard(tx('stick.right'), 'right', pr.sticks.right),
    h('div', { class: 'card' }, h('h2', {}, tx('common.general')),
      check(tx('sticks.swap'), pr.swap_sticks, v => queuePatch({ swap_sticks: v }))));
}

function renderTriggers(p) {
  const pr = S.profile;
  p.replaceChildren(...['left', 'right'].map(side => triggerCard(side, pr.triggers[side])),
    h('div', { class: 'card' }, h('h2', {}, tx('common.general')),
      check(tx('trig.swap'), pr.swap_triggers, v => queuePatch({ swap_triggers: v }))));
}

function renderGyro(p, d) {
  const pr = S.profile, cards = [];
  {
    const g = pr.gyro, G = k => v => queuePatch({ gyro: { [k]: v } });
    const act = h('select', { 'aria-label': tx('gyro.act_aria'), onchange: e => G('activation')(e.target.value || null) },
      h('option', { value: '' }, tx('gyro.always')), (d.inputs || []).map(([, n]) => h('option', { value: n, selected: n === g.activation }, n)));
    cards.push(h('div', { class: 'card' }, h('h2', {}, tx('tab.gyro')),
      h('div', { class: 'inline' }, check(tx('gyro.enabled'), g.enabled, G('enabled')), act,
        h('select', { 'aria-label': tx('gyro.target_aria'), onchange: e => G('target')(e.target.value) },
          [['right_stick', tx('gyro.t.right_stick')], ['left_stick', tx('gyro.t.left_stick')], ['mouse', tx('gyro.t.mouse')]].map(([v, t]) => h('option', { value: v, selected: v === g.target }, t)))),
      g.target === 'mouse' ? slider(tx('gyro.mouse_sens'), g.mouse_sens, 0.5, 60, 0.5, G('mouse_sens'), v => (+v).toFixed(1)) : null,
      slider(tx('gyro.dz'), g.deadzone_dps, 0, 50, 0.5, G('deadzone_dps'), v => (+v).toFixed(1)),
      slider(tx('gyro.full'), g.full_dps, 20, 1000, 5, G('full_dps'), v => (+v).toFixed(0)),
      slider(tx('gyro.smooth'), g.smoothing, 0, 0.9, 0.01, G('smoothing')),
      h('div', { class: 'inline' }, check(tx('gyro.inv_yaw'), g.invert_yaw, G('invert_yaw')), check(tx('gyro.inv_pitch'), g.invert_pitch, G('invert_pitch'))),
      curveEditor(g.curve, G('curve'))));
    cards.push(h('div', { class: 'card' }, h('h2', {}, tx('gyro.live_h')),
      h('p', { class: 'muted' }, tx('gyro.live_help')),
      ['yaw', 'pitch', 'roll'].map(k => h('div', { class: 'gyrorow' }, h('span', {}, k), h('div', { class: 'bar' }, h('i', { id: 'gy-' + k })), h('output', { id: 'gyv-' + k }, '0'))),
      h('div', { class: 'inline' },
        h('button', { class: 'primary', onclick: guard(async ev => {
          const b = ev.currentTarget; b.disabled = true; b.textContent = tx('gyro.hold_still');
          try {
            const r = await api('POST', `/api/devices/${S.dev}/gyro/calibrate`, { save: true });
            S.profile = r.data; S.dirty = true; renderTab();
            toast(tx('gyro.calibrated', { noise: r.noise_dps, n: r.samples }));
          } finally { b.disabled = false; b.textContent = tx('gyro.calibrate'); }
        }) }, tx('gyro.calibrate')),
        h('span', { class: 'muted' }, `bias: ${(g.bias || [0, 0]).join(', ')}`))));
  }
  p.replaceChildren(...cards);
}

/* ---------------------------------------------------------------- protocol / operating mode */
function renderDevice(p, d) {
  const modes = d.modes || [], pr = d.protocol;
  const info = pr ? h('div', { class: 'card' }, h('h2', {}, tx('dev.cur_proto')), h('p', {}, pr.summary),
    h('dl', { class: 'kv' }, pr.items.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]))) : null;
  let choice;
  if (modes.length > 1) {
    choice = h('div', { class: 'card' }, h('h2', {}, tx('dev.mode_h')),
      h('p', { class: 'muted' }, tx('dev.mode_switchable')),
      modes.map(m => h('label', { class: 'modeopt' },
        h('input', { type: 'radio', name: 'proto-mode', value: m.id, checked: m.current,
          onchange: guard(async () => {
            await api('POST', `/api/devices/${d.id}/mode`, { layout: m.id });
            S.profile = null; await loadDevices(); await loadProfile(); toast(tx('dev.mode_changed', { label: m.label }));
          }) }),
        h('span', {}, h('b', {}, m.label), m.gyro ? ' · ' + tx('dev.gyro') : '', m.note ? h('small', { class: 'muted' }, m.note) : null))));
  } else {
    choice = h('div', { class: 'card' }, h('h2', {}, tx('dev.mode_h')),
      h('p', { class: 'muted' }, tx('dev.mode_fixed')));
  }
  const op = d.options, setOpt = body => guard(async () => {
    await api('POST', `/api/devices/${d.id}/options`, body);
    S.profile = null; await loadDevices(); await loadProfile(); toast(tx('dev.saved'));
  })();
  let outCard = null, hideCard = null;
  if (op) {
    outCard = h('div', { class: 'card' }, h('h2', {}, tx('dev.out_h')),
      h('p', { class: 'muted' }, tx('dev.out_help')),
      op.output_choices.map(c => h('label', { class: 'modeopt' },
        h('input', { type: 'radio', name: 'out-kind', value: c.id, checked: c.id === op.output_kind,
          onchange: ev => {
            if (c.warn && !confirm('⚠ ' + c.label + '\n\n' + c.warn + '\n\n' + tx('dev.out_confirm'))) {
              const cur = document.querySelector(`input[name="out-kind"][value="${op.output_kind}"]`);
              if (cur) cur.checked = true;                  // cancelled — we restore the previous choice
              return;
            }
            setOpt({ output_kind: c.id });
          } }),
        h('span', {}, h('b', {}, c.label), h('small', { class: 'muted' }, c.note),
          c.warn && c.id === op.output_kind ? h('small', { class: 'warn' }, '⚠ ' + c.warn) : null))));
    const hs = d.hide_state, vp = `${d.vid}:${d.pid}`, win = d.backend === 'hidapi' || d.backend === 'winhid';
    const st = [];
    if (d.open && hs && hs.requested) {
      const n = (hs.nodes || []).length;
      if (hs.mode === 'hidden') st.push(tx('dev.hs_hidden', { n }));
      else if (hs.mode === 'partial') st.push(tx('dev.hs_partial', { n, f: (hs.failed || []).length }));
      else if (hs.mode === 'denied') st.push(tx('dev.hs_denied'));
      else if (hs.mode === 'none') st.push(tx('dev.hs_none'));
    }
    hideCard = h('div', { class: 'card' }, h('h2', {}, tx('dev.hide_h')),
      h('label', { class: 'modeopt' },
        h('input', { type: 'checkbox', checked: op.hide, onchange: e => setOpt({ hide: e.target.checked }) }),
        h('span', {}, h('b', {}, tx('dev.hide_cb')),
          h('small', { class: 'muted' }, tx('dev.hide_cb_help')))),
      !op.hide ? h('p', { class: 'warn' }, tx('dev.hide_off_warn')) : null,
      st.length ? h('p', { class: 'muted' }, st.join(' · ')) : null,
      win ? h('p', { class: 'muted' }, tx('dev.win_hide_a'),
        h('code', {}, 'python -m gamepad_remapd hidhide'), tx('dev.win_hide_b'))
        : (hs && (hs.mode === 'hidden' || hs.mode === 'partial')) ? null
        : h('div', { class: 'muted' },
          h('p', {}, tx('dev.hide_linux_help')),
          h('p', {}, h('code', {}, `gamepad-remapd setup-hide --hide ${vp} --service`)),
          h('p', {}, tx('dev.once_a'), h('code', {}, 'python3 -m gamepad_remapd serve --privileged'), tx('dev.once_b'), h('code', {}, 'gamepad-remapd sdl-list'), tx('dev.once_c'), h('code', {}, 'gamepad-remapd setup-hide --uninstall'))));
  }
  p.replaceChildren(info, choice, outCard, hideCard, d.open ? rumbleCard(d) : null, layoutCard(d));
}

function rumbleCard(d) {
  let strong = 0.6, weak = 0.6;
  return h('div', { class: 'card' }, h('h2', {}, tx('rumble.h')),
    h('p', { class: 'muted' }, tx('rumble.help')),
    slider(tx('rumble.strong'), strong, 0, 1, 0.05, v => { strong = v; }, v => Math.round(v * 100) + '%'),
    slider(tx('rumble.weak'), weak, 0, 1, 0.05, v => { weak = v; }, v => Math.round(v * 100) + '%'),
    h('button', { onclick: guard(async () => {
      const r = await api('POST', `/api/devices/${d.id}/rumble`, { strong, weak, ms: 500 }); toast(tx('rumble.done', { ms: r.ms }));
    }) }, tx('rumble.test')));
}

function layoutCard(d) {
  const box = h('div');
  const run = save => guard(async () => {
    const r = await api('POST', `/api/devices/${d.id}/autolayout`, { save });
    box.replaceChildren(
      (r.notes || []).map(n => h('p', { class: 'muted' }, '• ' + n)),
      h('textarea', { readonly: true, 'aria-label': tx('layout.generated_aria'), rows: 10 }, JSON.stringify(r.layout, null, 2)),
      r.saved ? h('p', {}, tx('layout.saved', { saved: r.saved, id: r.id })) : null,
      h('details', {}, h('summary', {}, tx('layout.fields')), h('pre', { class: 'hex' }, r.fields || '')));
    if (save) { await loadDevices(); }
  })();
  return h('div', { class: 'card' }, h('h2', {}, tx('layout.h')),
    h('p', {}, tx('layout.current'), h('b', {}, d.layout || '—')),
    h('p', { class: 'muted' }, tx('layout.help')),
    h('div', { class: 'inline' }, h('button', { onclick: () => run(false) }, tx('layout.show')),
      h('button', { class: 'primary', onclick: () => run(true) }, tx('layout.create'))), box);
}

/* ---------------------------------------------------------------- remapping */
/* Overview (the table) and details (the card with the editor of the selected input). The diagram, the chips under it
 * and the table select the same input, so one editor serves all of them. */
const defaultTarget = (d, name) => d.default_targets && d.default_targets[name] !== undefined ? d.default_targets[name]
  : (name.startsWith('DPAD_') ? name.slice(5) : null);
const targetText = (bm, name) => bm[name] ? (bm[name] === 'NONE' ? 'NONE' : bm[name].join(',')) : '';

function selectInput(name) {
  const d = curDev();
  if (!d || !d.inputs || !d.inputs.some(i => i[1] === name)) return;
  S.sel = name; S.dirty = true;
  if (S.tab !== 'buttons' && d.open && !d.sniff_only && S.profile) S.tab = 'buttons';
  renderTab();
  const det = $('#detail'); if (det) det.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function renderDetail(d) {
  const box = h('div', { id: 'detail', class: 'card detail', 'aria-live': 'polite' });
  const name = S.sel;
  if (!name || !(d.inputs || []).some(i => i[1] === name)) {
    box.append(h('p', { class: 'muted' }, tx('btn.select_hint')));
    return box;
  }
  const bm = S.profile.button_map || {}, def = defaultTarget(d, name);
  const inp = h('input', { type: 'text', value: targetText(bm, name), placeholder: def || '—', list: 'targets', 'aria-label': tx('btn.target_for', { name }), spellcheck: 'false' });
  const setCell = v => { const c = document.querySelector(`#panel tr[data-n="${CSS.escape(name)}"] .cur`); if (c) c.textContent = v || '—'; };
  inp.addEventListener('change', () => {
    const v = inp.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
    queuePatch({ button_map: { [name]: v.length === 0 ? null : (v[0] === 'NONE' ? 'NONE' : v) } });
    setCell(v.join(','));
  });
  const reset = h('button', { 'aria-label': tx('btn.reset_one', { name }), title: tx('btn.reset_title'), onclick: () => {
    inp.value = ''; queuePatch({ button_map: { [name]: null } }); setCell(''); } }, tx('btn.reset_short'));
  box.append(h('h3', {}, name), h('p', { class: 'muted' }, tx('btn.default_is', { v: def || '—' })),
    h('div', { class: 'inline' }, inp, reset));
  return box;
}

function renderButtons(p, d) {
  const bm = S.profile.button_map || {};
  const dl = h('datalist', { id: 'targets' }, TARGETS.concat(KEY_TARGETS).map(t => h('option', { value: t })));
  const rows = (d.inputs || []).map(([bit, name]) => {
    const def = defaultTarget(d, name);
    const tr = h('tr', { 'data-n': name, class: name === S.sel ? 'sel' : '', onclick: () => selectInput(name) },
      h('td', { class: 'hex' }, h('button', { type: 'button', class: 'link', 'aria-pressed': name === S.sel, onclick: e => { e.stopPropagation(); selectInput(name); } }, name)),
      h('td', { class: 'muted' }, def || '—'), h('td', { class: 'cur' }, targetText(bm, name) || '—'));
    return tr;
  });
  p.replaceChildren(h('p', { class: 'muted' }, tx('btn.help')),
    dl, renderDetail(d),
    h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, tx('btn.col_in')), h('th', {}, tx('btn.col_def')), h('th', {}, tx('btn.col_target')))), h('tbody', {}, rows)),
    h('div', { class: 'inline' }, h('button', { onclick: guard(async () => {
      if (!confirm(tx('btn.reset_all_confirm'))) return;
      const patch = {}; for (const k of Object.keys(S.profile.button_map || {})) patch[k] = null;
      queuePatch({ button_map: patch }); setTimeout(renderTab, 200);
    }) }, tx('btn.reset_all'))));
}

/* ---------------------------------------------------------------- profiles */
function download(name, text) {
  const a = h('a', { href: URL.createObjectURL(new Blob([text], { type: 'application/json' })), download: name });
  document.body.append(a); a.click(); a.remove();
}
function renderProfiles(p, d) {
  const ta = h('textarea', { 'aria-label': tx('prof.json_aria'), rows: 14, spellcheck: 'false' }, JSON.stringify(S.profile, null, 2));
  const msg = h('p', { class: 'muted' });
  const parse = () => { try { return JSON.parse(ta.value); } catch (e) { msg.className = 'warn'; msg.textContent = tx('prof.bad_json', { e: e.message }); return null; } };
  const rows = S.profiles.map(x => h('tr', {}, h('td', {}, x.name + (x.name === S.profileName ? ' ●' : '')),
    h('td', { class: 'muted' }, x.source === 'user' ? tx('prof.user') : tx('prof.builtin')),
    h('td', {}, h('div', { class: 'inline' },
      h('button', { onclick: guard(async () => {
        const r = await api('PUT', `/api/devices/${S.dev}/profile`, { name: x.name });
        S.profile = r.data; S.profileName = r.name; S.warnings = r.warnings; S.dirty = true; S.unsaved = false; renderTab(); toast(tx('prof.applied_x', { name: x.name }));
      }) }, tx('common.apply')),
      h('button', { onclick: guard(async () => { const r = await api('GET', `/api/profiles/${encodeURIComponent(x.name)}`); download(x.name + '.json', JSON.stringify(r, null, 2)); }) }, tx('common.export')),
      x.source === 'user' ? h('button', { onclick: guard(async () => {
        if (!confirm(tx('prof.delete_confirm', { name: x.name }))) return;
        await api('DELETE', `/api/profiles/${encodeURIComponent(x.name)}`);
        S.profiles = await api('GET', '/api/profiles'); renderTab(); toast(tx('prof.deleted'));
      }) }, tx('common.delete')) : null))));
  const file = h('input', { type: 'file', accept: 'application/json,.json', 'aria-label': tx('prof.import_aria') });
  file.addEventListener('change', guard(async () => {
    const f = file.files[0]; if (!f) return;
    const data = JSON.parse(await f.text());
    if (!data.name) data.name = f.name.replace(/\.json$/i, '');
    await api('PUT', `/api/profiles/${encodeURIComponent(data.name)}`, data);
    S.profiles = await api('GET', '/api/profiles'); renderTab(); toast(tx('prof.imported', { name: data.name }));
  }));
  p.replaceChildren(
    h('div', { class: 'card' }, h('h2', {}, tx('tab.profiles')), h('table', {}, h('tbody', {}, rows)),
      h('div', { class: 'inline' }, h('span', { class: 'muted' }, tx('prof.import_label')), file)),
    h('div', { class: 'card' }, h('h2', {}, tx('prof.json_h')),
      h('p', { class: 'muted' }, tx('prof.json_help')),
      ta, msg,
      h('div', { class: 'inline' },
        h('button', { onclick: guard(async () => {
          const data = parse(); if (!data) return;
          const r = await api('POST', '/api/profiles-check', { data, layout: d.layout });
          msg.className = r.warnings.length ? 'warn' : 'muted';
          msg.textContent = r.warnings.length ? tx('prof.warnings', { w: r.warnings.join('; ') }) : tx('prof.ok');
        }) }, tx('prof.check')),
        h('button', { class: 'primary', onclick: guard(async () => {
          const data = parse(); if (!data) return;
          await api('POST', '/api/profiles-check', { data, layout: d.layout });
          await api('PUT', `/api/profiles/${encodeURIComponent(data.name || S.profileName)}`, { ...data, name: data.name || S.profileName });
          const r = await api('PUT', `/api/devices/${S.dev}/profile`, { name: data.name || S.profileName });
          S.unsaved = false; S.profile = r.data; S.profileName = r.name; S.warnings = r.warnings; S.profiles = await api('GET', '/api/profiles');
          S.dirty = true; renderTab(); toast(tx('prof.json_saved'));
        }) }, tx('prof.save_apply')))));
}

/* ---------------------------------------------------------------- diagnostics */
function renderDiag(p) {
  const sub = h('div', { class: 'seg', role: 'tablist' },
    [['stats', tx('diag.latency')], ['sniff', tx('diag.sniffer')]].map(([id, t]) => h('button', {
      role: 'tab', 'aria-selected': S.sub === id, onclick: () => { S.sub = id; renderTab(); } }, t)));
  const body = h('div');
  p.replaceChildren(sub, body);
  if (S.sub === 'sniff') renderSniff(body);
  else { body.append(h('div', { id: 'stats' })); renderStats(); }
}

/* ---------------------------------------------------------------- system */
async function renderSystem(p) {
  p.replaceChildren(h('p', { class: 'muted' }, tx('common.loading')));
  let info;
  try { info = await api('GET', '/api/system'); } catch (e) { p.replaceChildren(h('p', { class: 'warn' }, e.message)); return; }
  S.sys = info;
  const d = curDev(), vp = d ? `${d.vid}:${d.pid}` : '054c:09cc';
  const out = h('pre', { class: 'hex out' });
  const show = t => { out.textContent = t; };
  const sysCard = h('div', { class: 'card' }, h('h2', {}, tx('tab.system')),
    h('dl', { class: 'kv' }, [[tx('sys.platform'), info.platform], [tx('sys.backend'), info.backend], [tx('sys.config_dir'), info.config_dir],
      [tx('sys.devices_dir'), info.devices_dir]].flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])));
  const hideCard = h('div', { class: 'card' }, h('h2', {}, tx('sys.hide_h')),
    info.posix ? [
      h('p', { class: 'muted' }, info.hidden_nodes.length ? tx('sys.hidden_count', { n: info.hidden_nodes.length }) : tx('sys.hidden_none')),
      info.hidden_nodes.length ? h('pre', { class: 'hex' }, info.hidden_nodes.join('\n')) : null,
      h('div', { class: 'inline' },
        h('button', { onclick: guard(async () => {
          const r = await api('POST', '/api/system/unhide');
          show(r.restored.length || r.failed.length ? `${tx('sys.restored', { n: r.restored.length })}\n${r.failed.length ? tx('sys.failed', { list: r.failed.join(', ') }) : ''}` : tx('sys.nothing_restore'));
          renderSystem(p);
        }) }, tx('sys.unhide_btn')),
        h('button', { onclick: guard(async ev => {
          const b = ev.currentTarget; b.disabled = true; show(tx('sys.sdl_polling'));
          try { const r = await api('POST', '/api/system/sdl-list'); show(r.text || tx('sys.empty')); } finally { b.disabled = false; }
        }) }, tx('sys.sdl_btn'))),
      h('p', { class: 'muted' }, tx('sys.autosetup')),
      h('p', {}, h('code', {}, `gamepad-remapd setup-hide --hide ${vp} --service`)),
      h('p', { class: 'muted' }, tx('sys.once_a'), h('code', {}, 'python3 -m gamepad_remapd serve --privileged'),
        tx('dev.once_c'), h('code', {}, 'gamepad-remapd setup-hide --uninstall'))]
      : h('p', { class: 'muted' }, tx('sys.win_note')),
    out);
  const udevGroup = h('input', { type: 'text', placeholder: tx('sys.group_ph'), 'aria-label': tx('sys.group_aria'), size: 16 });
  const udevVp = h('input', { type: 'text', value: vp, 'aria-label': 'VID:PID', size: 10 });
  const udevHide = h('input', { type: 'checkbox' });
  const rules = h('textarea', { readonly: true, 'aria-label': tx('sys.udev_aria'), rows: 8 });
  const udevCard = info.posix ? h('div', { class: 'card' }, h('h2', {}, tx('sys.udev_h')),
    h('p', { class: 'muted' }, tx('sys.udev_help')),
    h('div', { class: 'inline' }, udevVp, h('label', {}, udevHide, tx('sys.udev_hide_only')), udevGroup,
      h('button', { onclick: guard(async () => {
        const vpv = udevVp.value.trim();
        const r = await api('POST', '/api/system/udev-rules', udevHide.checked
          ? { hide: [vpv], group: udevGroup.value.trim() || null } : { vid_pid: vpv ? [vpv] : [] });
        rules.value = r.text;
      }) }, tx('sys.generate')),
      h('button', { onclick: () => rules.value && download('70-gamepad-remapd.rules', rules.value) }, tx('sys.download'))), rules) : null;
  const hhOut = h('pre', { class: 'hex' });
  const hidhideCard = !info.posix ? h('div', { class: 'card' }, h('h2', {}, 'HidHide (Windows)'),
    h('p', { class: 'muted' }, tx('sys.hh_help')),
    d ? h('button', { onclick: guard(async () => {
      const r = await api('POST', '/api/system/hidhide', { vid: d.vid, pid: d.pid });
      hhOut.textContent = r.commands.length ? r.commands.join('\n') : tx('sys.no_devices');
    }) }, tx('sys.hh_show', { vp })) : h('p', { class: 'muted' }, tx('sys.connect_pad')), hhOut) : null;
  const kbBox = h('div');
  const kbCard = info.posix ? h('div', { class: 'card' }, h('h2', {}, tx('sys.kb_h')),
    h('p', { class: 'muted' }, tx('sys.kb_state', { mode: info.keyboard.enabled ? tx('sys.on') : tx('sys.off'), toggle: info.keyboard.toggle.join('+') })),
    h('button', { onclick: guard(async () => {
      const r = await api('GET', '/api/system/keyboards');
      kbBox.replaceChildren(r.note ? h('p', { class: 'muted' }, r.note) : null, h('table', {}, h('tbody', {}, r.candidates.map(c =>
        h('tr', {}, h('td', {}, c.selected ? '★' : ''), h('td', { class: 'hex' }, `${c.vid}:${c.pid}`), h('td', {}, c.name), h('td', { class: 'muted' }, c.paths.join(', ')))))));
    }) }, tx('sys.kb_find')), kbBox) : null;
  p.replaceChildren(sysCard, hideCard, udevCard, hidhideCard, kbCard);
}

/* ---------------------------------------------------------------- sniffer */
let sniffTimer = null;
function renderSniff(p) {
  const box = h('div', { id: 'sniffbox' });
  const act = (action, name) => guard(async () => { S.sniff = await api('POST', `/api/devices/${S.dev}/sniff`, { action, name }); paintSniff(); });
  const learnName = h('input', { type: 'text', placeholder: 'M1', value: 'M1', size: 6, 'aria-label': tx('sniff.name_aria') });
  p.replaceChildren(
    h('p', { class: 'muted' }, tx('sniff.help')),
    h('div', { class: 'inline' }, h('button', { class: 'primary', onclick: act('start') }, tx('sniff.start')),
      h('button', { onclick: act('reset') }, tx('common.reset')), h('button', { onclick: act('stop') }, tx('sniff.stop')),
      learnName, h('button', { onclick: guard(async () => act('learn', learnName.value.trim())()) }, tx('sniff.learn'))), box);
  guard(async () => { S.sniff = await api('GET', `/api/devices/${S.dev}/sniff`); paintSniff(); })();
  sniffTimer = setInterval(guard(async () => {
    if (S.sniff && S.sniff.phase === 'off') return;
    S.sniff = await api('GET', `/api/devices/${S.dev}/sniff`); paintSniff();
  }), 400);
}
function paintSniff() {
  const box = $('#sniffbox'), s = S.sniff;
  if (!box || !s) return;
  if (s.phase === 'off') { box.replaceChildren(h('p', { class: 'muted' }, tx('sniff.off'))); return; }
  const recent = new Set(s.recent_changed || []);
  const hex = (s.hex || '').split(' ').filter(Boolean).map((b, i) => h('span', {}, recent.has(i) ? h('b', {}, b) : b, ' '));
  const rows = (s.candidates || []).map(c => h('tr', {}, h('td', {}, c.byte), h('td', {}, c.bit), h('td', { class: 'hex' }, c.mask),
    h('td', {}, c.presses), h('td', {}, c.down ? '●' : '')));
  const learned = s.layout_snippet || {};
  box.replaceChildren(
    s.phase === 'baseline' ? h('div', {}, h('span', {}, tx('sniff.baseline')), h('div', { class: 'bar' }, h('i', { style: `width:${Math.round(s.progress * 100)}%` }))) : null,
    h('div', { class: 'hex' }, tx('sniff.info', { len: s.length, n: s.reports })), h('div', { class: 'hex' }, hex),
    s.learning ? h('p', { class: 'warn' }, tx('sniff.waiting', { name: s.learning })) : null,
    h('table', {}, h('thead', {}, h('tr', {}, [...tx('sniff.cols').split('|'), ''].map(x => h('th', {}, x)))), h('tbody', {}, rows)),
    (s.hints || []).map(x => h('p', { class: 'muted' }, x)),
    Object.keys(learned).length ? [h('h2', {}, tx('sniff.snippet_h')),
      h('textarea', { readonly: true, 'aria-label': tx('sniff.snippet_aria') }, JSON.stringify(learned, null, 2))] : null);
}

/* ---------------------------------------------------------------- latency */
function renderStats() {
  const box = $('#stats'); if (!box) return;
  const st = S.stats[S.dev];
  if (!st || !st.count) { box.replaceChildren(h('p', { class: 'muted' }, tx('stats.none'))); return; }
  const budget = 1500, p99 = st.p99_us || 0, SC = tx('stats.cols').split('|');
  box.replaceChildren(
    h('div', { class: 'card' }, h('h2', {}, tx('stats.h')),
      h('table', {}, [[SC[0], st.count], [SC[1], st.mean_us], ['p50', st.p50_us], ['p99', st.p99_us], ['p99.9', st.p999_us],
        [SC[2], st.max_us], [SC[3], st.dropped]].map(([k, v]) => h('tr', {}, h('td', {}, k), h('td', { class: 'hex' }, v)))),
      h('div', { class: 'bar', title: tx('stats.budget') }, h('i', { style: `width:${Math.min(100, p99 / budget * 100)}%` })),
      h('p', { class: 'muted' }, tx('stats.note')),
      h('button', { onclick: guard(async () => { await api('POST', `/api/devices/${S.dev}/stats/reset`); }) }, tx('common.reset'))));
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

/* Clickable areas of the diagram (the same geometry as draw() below, in the 760x440 canvas space). */
const SHAPES = [
  { n: 'LT', x: 70, y: 14, w: 110, h: 24 }, { n: 'RT', x: 580, y: 14, w: 110, h: 24 },
  { n: 'LB', x: 70, y: 46, w: 110, h: 26 }, { n: 'RB', x: 580, y: 46, w: 110, h: 26 },
  { n: 'LS', cx: 175, cy: 185, r: 60 }, { n: 'RS', cx: 475, cy: 290, r: 60 },
  { n: 'UP', x: 270, y: 240, w: 30, h: 38 }, { n: 'DOWN', x: 270, y: 307, w: 30, h: 38 },
  { n: 'LEFT', x: 232, y: 278, w: 38, h: 30 }, { n: 'RIGHT', x: 300, y: 278, w: 38, h: 30 },
  { n: 'A', cx: 585, cy: 232, r: 24 }, { n: 'B', cx: 632, cy: 185, r: 24 }, { n: 'X', cx: 538, cy: 185, r: 24 }, { n: 'Y', cx: 585, cy: 138, r: 24 },
  { n: 'BACK', cx: 335, cy: 170, r: 15 }, { n: 'START', cx: 425, cy: 170, r: 15 }, { n: 'GUIDE', cx: 380, cy: 125, r: 15 },
  ...EXTRAS.map((n, i) => ({ n, x: 150 + i * 58, y: 396, w: 54, h: 28 })),
];
const shapeAt = (x, y) => SHAPES.find(s => s.r ? Math.hypot(x - s.cx, y - s.cy) <= s.r : (x >= s.x && x <= s.x + s.w && y >= s.y && y <= s.y + s.h));
/* A diagram button -> the device input that feeds it (D-pad inputs are called DPAD_UP, …). */
const inputFor = (d, n) => ((d && d.inputs) || []).map(i => i[1]).find(x => x === n || x === 'DPAD_' + n) || null;
const canvasPoint = e => { const r = cv.getBoundingClientRect(); return [(e.clientX - r.left) * 760 / r.width, (e.clientY - r.top) * 440 / r.height]; };
cv.addEventListener('click', e => {
  const s = shapeAt(...canvasPoint(e)); if (!s) return;
  const name = inputFor(curDev(), s.n);
  if (name) selectInput(name); else toast(tx('btn.not_on_device', { name: s.n }));
});
cv.addEventListener('mousemove', e => { cv.style.cursor = shapeAt(...canvasPoint(e)) ? 'pointer' : ''; });

function drawStick(cx, cy, R, ax, ay, raw, cfg, pressed) {
  circle(cx, cy, R + 8, color('--pad'), color('--line'));
  if (cfg) {                                    // dead zones from the active profile
    circle(cx, cy, cfg.inner_deadzone * R, color('--dz'), color('--bad'), [4, 3]);
    circle(cx, cy, cfg.outer_deadzone * R, null, color('--muted'), [2, 4]);
  }
  circle(cx, cy, R, null, pressed ? color('--lit') : color('--line'));
  if (raw && raw[0] != null) circle(cx + raw[0] * R, cy + raw[1] * R, 6, null, color('--warn'));   // raw input
  circle(cx + ax * R, cy + ay * R, 9, pressed ? color('--lit') : color('--text'));                // after processing
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
  // triggers and bumpers
  for (const [name, x, v] of [['LT', 70, ax[4] / 255], ['RT', 580, ax[5] / 255]]) {
    rrect(x, 14, 110, 24, 6, null, line); rrect(x, 14, 110 * Math.min(1, Math.max(v, on.has(name) ? 1 : 0)), 24, 6, lit);
    label(name, x + 55, 26, v > 0.5 || on.has(name));
  }
  for (const [name, x] of [['LB', 70], ['RB', 580]]) { rrect(x, 46, 110, 26, 8, on.has(name) ? lit : null, line); label(name, x + 55, 59, on.has(name)); }
  // sticks: LS top-left, RS bottom-right (Xbox layout)
  drawStick(175, 185, 52, ax[0] / 32767, ax[1] / 32767, raw && [raw[0], raw[1]], cfgL, on.has('LS'));
  drawStick(475, 290, 52, ax[2] / 32767, ax[3] / 32767, raw && [raw[2], raw[3]], cfgR, on.has('RS'));
  // D-pad
  for (const [n, x, y, w, hh] of [['UP', 270, 240, 30, 38], ['DOWN', 270, 307, 30, 38], ['LEFT', 232, 278, 38, 30], ['RIGHT', 300, 278, 38, 30]])
    rrect(x, y, w, hh, 5, on.has(n) ? lit : null, line);
  // face buttons
  for (const [n, x, y] of [['A', 585, 232], ['B', 632, 185], ['X', 538, 185], ['Y', 585, 138]]) { circle(x, y, 24, on.has(n) ? lit : null, line); label(n, x, y, on.has(n)); }
  // system buttons
  for (const [n, x, y] of [['BACK', 335, 170], ['START', 425, 170], ['GUIDE', 380, 125]]) { circle(x, y, 15, on.has(n) ? lit : null, line); label(n === 'GUIDE' ? 'G' : n === 'BACK' ? '⧉' : '☰', x, y, on.has(n)); }
  // extra buttons (M1–M6, C, Z) that XInput lacks: a row in the center of the 760 px canvas
  EXTRAS.forEach((n, i) => { const x = 150 + i * 58; rrect(x, 396, 54, 28, 8, on.has(n) ? lit : null, line); label(n, x + 27, 410, on.has(n)); });

  // ring around the selected input
  if (S.sel) {
    const s = SHAPES.find(x => x.n === S.sel || 'DPAD_' + x.n === S.sel);
    if (s) {
      g.strokeStyle = color('--accent'); g.lineWidth = 3;
      g.beginPath();
      if (s.r) g.arc(s.cx, s.cy, s.r + 4, 0, Math.PI * 2); else g.roundRect(s.x - 4, s.y - 4, s.w + 8, s.h + 8, 10);
      g.stroke();
    }
  }
  // input bit chips: BigInt — an arbitrary-length mask (no rollover limit)
  const d = curDev();
  if (d && d.inputs) {
    const v = BigInt('0x' + ((live && live.src) || '0'));
    for (const c of $('#inputs').children) {
      const ent = d.inputs.find(i => i[1] === c.dataset.n);
      c.classList.toggle('on', !!ent && ((v >> BigInt(ent[0])) & 1n) === 1n);
      c.classList.toggle('sel', c.dataset.n === S.sel);
    }
    if (S.tab === 'gyro' && live && live.gyro) live.gyro.forEach((v, i) => {
      const k = ['yaw', 'pitch', 'roll'][i], bar = document.getElementById('gy-' + k), out = document.getElementById('gyv-' + k);
      if (!bar || v == null) return;
      bar.style.setProperty('width', Math.min(100, Math.abs(v) / 5) + '%');       // 500 °/s = full bar
      out.textContent = v + ' ' + tx('unit.dps');
    });
    if (S.tab === 'buttons') for (const tr of document.querySelectorAll('#panel tr[data-n]')) {
      const ent = d.inputs.find(i => i[1] === tr.dataset.n);
      tr.style.background = ent && ((v >> BigInt(ent[0])) & 1n) === 1n ? color('--dz') : '';
    }
  }
}

/* ---------------------------------------------------------------- startup */
I18N.init().finally(() => {
  fitCanvas(); draw(); connectWs();
  guard(loadDevices)();
});
