"use strict";
// Тестова сторінка: усе через textContent/CSSOM (CSP забороняє inline-скрипти й style="").
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const ui = {
  gen: -1, desc: null, chips: new Map(), tested: new Set(), pressed: new Set(),
  axes: {}, sectors: {}, barEls: new Map(), barTested: new Set(),
  raw: null, prevRaw: null, changedAt: [], noisy: new Set(), calib: null, lastReport: 0,
};

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ---------------------------------------------------------------- список пристроїв
async function loadDevices() {
  const sel = $("device"), lsel = $("layout");
  const prev = sel.value, prevL = lsel.value;
  const { devices, layouts, auto, active } = await api("/api/devices");
  $("auto").checked = auto;
  sel.replaceChildren();
  for (const d of devices) {
    const o = el("option", "", `${d.path}  ${d.vid}:${d.pid}  ${d.backend}${d.interface != null ? " if" + d.interface : ""}  ${d.name || "?"}` +
      `  → ${d.layout ? d.layout + (d.generated ? " (авто)" : "") : "немає розкладки"}`);
    o.value = d.path;
    o.disabled = !d.layout;
    sel.append(o);
  }
  if (!devices.length) sel.append(el("option", "", "HID-пристроїв не знайдено"));
  if (auto && active && [...sel.options].some((o) => o.value === active)) sel.value = active;
  else if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  else { const f = [...sel.options].find((o) => !o.disabled); if (f) sel.value = f.value; }
  hsFillDevices(devices);
  lsel.replaceChildren(el("option", "", "авто (за VID/PID)"));
  lsel.firstChild.value = "";
  for (const l of layouts) {
    const o = el("option", "", l.id + (l.generated ? " (авто)" : ""));
    o.value = l.id;
    lsel.append(o);
  }
  lsel.value = [...lsel.options].some((o) => o.value === prevL) ? prevL : "";
}

// ---------------------------------------------------------------- пошук handshake
let hsRunning = false;
function hsFillDevices(devices) {
  const sel = $("hs-device");
  if (hsRunning || document.activeElement === sel) return;
  const prev = sel.value;
  sel.replaceChildren();
  for (const d of devices) {
    if (d.backend === "evdev") continue;                  // evdev не потребує handshake
    const o = el("option", "", `${d.path}  ${d.vid}:${d.pid}  if${d.interface ?? "?"}  ${d.usage || "—"}  ` +
      `${d.name || "?"}${d.layout ? "" : "  (немає розкладки)"}`);
    o.value = d.path;
    sel.append(o);
  }
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  else {
    const missing = devices.find((d) => d.backend !== "evdev" && !d.layout && d.usage);
    if (missing) sel.value = missing.path;
  }
}
function hsRender(st) {
  hsRunning = st.running;
  $("hs-start").hidden = st.running;
  $("hs-stop").hidden = !st.running;
  $("hs-device").disabled = $("hs-broad").disabled = st.running;
  const evs = st.events || [];
  const lastTry = [...evs].reverse().find((e) => e.kind === "try");
  const prompt = evs.find((e) => e.kind === "prompt");
  const banner = $("hs-banner");
  banner.hidden = !(st.running && prompt);
  if (prompt) banner.textContent = prompt.text;
  const log = $("hs-log");
  log.replaceChildren();
  for (const e of evs) {
    if (e.kind === "try") log.append(el("li", "", `${e.index}/${e.total} ${e.id} — ${e.name}`));
  }
  log.scrollTop = log.scrollHeight;
  const msg = $("hs-msg"), samples = $("hs-samples"), res = st.result;
  msg.className = "";
  if (st.running) msg.textContent = lastTry ? `Пробую ${lastTry.index}/${lastTry.total}: ${lastTry.name}…` : "Запуск…";
  else if (res) {
    msg.textContent = res.message;
    msg.className = res.ok ? "ok" : "bad";
  } else msg.textContent = "";
  samples.hidden = !(res && res.samples && res.samples.length);
  if (!samples.hidden) samples.textContent = res.samples.join("\n");
  $("hs-save").hidden = !(res && res.ok && res.layout && res.mode !== "no-handshake");
}
async function hsPoll() {
  if ($("hs").hidden && !hsRunning) return;
  try { hsRender(await api("/api/probe")); } catch (_) { /* сервер зайнятий */ }
}
$("btn-hs").onclick = () => { $("hs").hidden = !$("hs").hidden; if (!$("hs").hidden) hsPoll(); };
$("hs-start").onclick = async () => {
  const path = $("hs-device").value;
  if (!path) { $("hs-msg").textContent = "Немає інтерфейсу для пошуку."; return; }
  try { hsRender(await api("/api/probe", { path, broad: $("hs-broad").checked })); }
  catch (e) { $("hs-msg").textContent = e.message; $("hs-msg").className = "bad"; }
};
$("hs-stop").onclick = async () => { await api("/api/probe/stop", {}); };
$("hs-save").onclick = async () => {
  try {
    const r = await api("/api/probe/save", {});
    $("hs-msg").textContent = `Збережено ${r.saved} (розкладка ${r.layout}). Автовиявлення відкриє пристрій саме.`;
    $("hs-msg").className = "ok";
    $("hs-save").hidden = true;
    $("auto").checked = true;
    loadDevices().catch(() => {});
  } catch (e) { $("hs-msg").textContent = e.message; $("hs-msg").className = "bad"; }
};
setInterval(hsPoll, 500);

// ---------------------------------------------------------------- опис розкладки -> DOM
function setup(desc) {
  ui.gen = desc.gen; ui.desc = desc;
  ui.chips.clear(); ui.tested.clear(); ui.pressed.clear(); ui.axes = {}; ui.sectors = {};
  ui.barEls.clear(); ui.barTested.clear(); ui.raw = ui.prevRaw = null; ui.changedAt = [];
  const box = $("buttons");
  box.replaceChildren();
  for (const b of desc.buttons) {
    const c = el("span", "chip", b.target && b.target !== b.name ? `${b.name} → ${b.target}` : b.name);
    if (!b.target) c.title = "ціль не задана в розкладці";
    box.append(c);
    ui.chips.set(b.name, c);
  }
  const inSticks = new Set(Object.values(desc.sticks).flat());
  const bars = $("bars");
  bars.replaceChildren();
  const barNames = [...Object.values(desc.triggers), ...Object.keys(desc.fields).filter(
    (n) => !inSticks.has(n) && !Object.values(desc.triggers).includes(n))];
  for (const n of barNames) {
    const row = el("div", "bar"), track = el("div", "track"), fill = el("div", "fill");
    const val = el("span", "val", "0");
    track.append(fill);
    row.append(el("span", "name", n), track, val);
    bars.append(row);
    ui.barEls.set(n, { row, fill, val });
  }
  const w = $("warn");
  w.hidden = !desc.generated;
  w.textContent = desc.generated
    ? "Розкладку складено автоматично з HID-дескриптора: імена кнопок — припущення. Звіряйте з написами на геймпаді."
    : "";
  $("st-info").textContent = `${desc.device.path} · ${desc.device.vid}:${desc.device.pid} · ` +
    `${desc.device.name || "?"} · розкладка ${desc.layout}`;
  $("btn-open").hidden = true; $("btn-close").hidden = false;
  $("log").replaceChildren();
  renderHexSkeleton(0);
  updateProgress();
  drawSticks();
}

function updateProgress() {
  const total = ui.chips.size;
  $("progress").textContent = total ? `перевірено ${ui.tested.size} з ${total}` : "";
}

// ---------------------------------------------------------------- стіки
const OCT = 8;
function stickNorm(name) {
  const f = ui.desc.fields[name], v = ui.axes[name];
  if (!f || v === undefined) return 0;
  const half = Math.max(f.center - f.min, f.max - f.center) || 1;
  return Math.max(-1, Math.min(1, (v - f.center) / half));
}
function drawStick(cv, side, pair) {
  const g = cv.getContext("2d"), W = cv.width, H = cv.height, R = W / 2 - 14;
  const cs = getComputedStyle(document.body);
  const line = cs.getPropertyValue("--line") || "#ccc", ok = cs.getPropertyValue("--ok") || "green";
  const acc = cs.getPropertyValue("--accent") || "blue", fg = cs.getPropertyValue("--fg") || "#000";
  g.clearRect(0, 0, W, H);
  g.translate(W / 2, W / 2);
  const secs = ui.sectors[side] || (ui.sectors[side] = new Set());
  g.lineWidth = 8;
  for (let i = 0; i < OCT; i++) {
    g.beginPath();
    const a0 = (i / OCT) * Math.PI * 2 - Math.PI / OCT + 0.05, a1 = a0 + (Math.PI * 2) / OCT - 0.1;
    g.arc(0, 0, R + 5, a0, a1);
    g.strokeStyle = secs.has(i) ? ok : line;
    g.stroke();
  }
  g.lineWidth = 1; g.strokeStyle = line;
  g.beginPath(); g.arc(0, 0, R * 0.5, 0, 7); g.moveTo(-R, 0); g.lineTo(R, 0); g.moveTo(0, -R); g.lineTo(0, R); g.stroke();
  if (pair) {
    const x = stickNorm(pair[0]), y = stickNorm(pair[1]);
    g.beginPath(); g.arc(x * R, y * R, 9, 0, 7); g.fillStyle = acc; g.fill();
    g.fillStyle = fg; g.font = "12px ui-monospace, monospace"; g.textAlign = "center";
    g.fillText(`${side === "left" ? "L" : "R"}  x ${x.toFixed(2)}  y ${y.toFixed(2)}`, 0, R + 28);
  } else {
    g.fillStyle = line; g.font = "12px sans-serif"; g.textAlign = "center";
    g.fillText("немає стіка", 0, 4);
  }
  g.setTransform(1, 0, 0, 1, 0, 0);
}
function drawSticks() {
  if (!ui.desc) return;
  drawStick($("cv-left"), "left", ui.desc.sticks.left);
  drawStick($("cv-right"), "right", ui.desc.sticks.right);
}
function trackSector(side, pair) {
  if (!pair) return;
  const x = stickNorm(pair[0]), y = stickNorm(pair[1]);
  if (Math.hypot(x, y) < 0.8) return;
  const a = (Math.atan2(y, x) + Math.PI * 2) % (Math.PI * 2);
  (ui.sectors[side] || (ui.sectors[side] = new Set())).add(Math.round(a / (Math.PI * 2 / OCT)) % OCT);
}

// ---------------------------------------------------------------- сирий звіт
function renderHexSkeleton(len) {
  const box = $("hex");
  box.replaceChildren();
  ui.cells = [];
  for (let i = 0; i < len; i++) {
    const c = el("div", "b"), idx = el("i", "", String(i)), v = el("span", "", "00");
    c.append(idx, v);
    box.append(c);
    ui.cells.push({ c, v });
  }
}
const covered = () => new Set(ui.desc ? ui.desc.covered_bytes : []);

function onRaw(hex, now) {
  const bytes = [];
  for (let i = 0; i < hex.length; i += 2) bytes.push(parseInt(hex.substr(i, 2), 16));
  if (!ui.cells || ui.cells.length !== bytes.length) { renderHexSkeleton(bytes.length); ui.prevRaw = null; }
  const prev = ui.prevRaw, known = covered();
  if (prev) {
    for (let i = 0; i < bytes.length; i++) {
      if (bytes[i] === prev[i]) continue;
      ui.changedAt[i] = now;
      if (ui.calib) { if (!known.has(i)) ui.calib.add(i); continue; }
      if (!known.has(i) && !ui.noisy.has(i)) {
        const bits = [];
        for (let b = 0; b < 8; b++) if (((bytes[i] ^ prev[i]) >> b) & 1) bits.push(`${b}${(bytes[i] >> b) & 1 ? "↑" : "↓"}`);
        logLine(`байт ${i}: ${prev[i].toString(16).padStart(2, "0")} → ${bytes[i].toString(16).padStart(2, "0")}  (біти ${bits.join(" ")}) — розкладка про нього не знає`, "unk");
      }
    }
  }
  ui.prevRaw = bytes;
  ui.raw = bytes;
  $("rawinfo").textContent = `${bytes.length} байт`;
}
function paintHex(now) {
  if (!ui.raw || !ui.cells) return;
  const known = covered();
  ui.raw.forEach((b, i) => {
    const cell = ui.cells[i];
    if (!cell) return;
    cell.v.textContent = b.toString(16).padStart(2, "0");
    cell.c.title = `байт ${i}: 0b${b.toString(2).padStart(8, "0")} (${b})`;
    const recent = ui.changedAt[i] !== undefined && now - ui.changedAt[i] < 450;
    const noisy = ui.noisy.has(i);
    cell.c.classList.toggle("known", known.has(i));
    cell.c.classList.toggle("noisy", noisy);
    cell.c.classList.toggle("flash", recent && (known.has(i) || noisy));
    cell.c.classList.toggle("unk", recent && !known.has(i) && !noisy);
  });
}

// ---------------------------------------------------------------- журнал
function logLine(text, cls) {
  const li = el("li", cls || "");
  li.append(el("span", "t", new Date().toLocaleTimeString("uk-UA", { hour12: false })), document.createTextNode(text));
  const log = $("log");
  log.prepend(li);
  while (log.children.length > 300) log.lastChild.remove();
}

// ---------------------------------------------------------------- стан від сервера
function onState(s) {
  const now = performance.now();
  const badge = $("st-state");
  const labels = { starting: "підключення…", running: "працює", error: "помилка", gone: "відключено" };
  badge.textContent = labels[s.status.state] || s.status.state;
  badge.className = "badge " + s.status.state;
  if (s.status.message) $("st-info").textContent = s.status.message;
  if (s.status.state === "running" && s.n === 0 && now - ui.lastReport > 4000)
    $("st-rate").textContent = "звітів ще немає — натисніть будь-яку кнопку (2.4 ГГц-геймпад засинає) або спробуйте «Знайти handshake»";
  if (!s.n) return;
  ui.lastReport = now;
  $("st-rate").textContent = `${s.hz} Гц · ${s.n} звітів`;

  for (const e of s.events) {
    if (e.down) ui.tested.add(e.name);
    logLine(`${e.down ? "+" : "−"} ${e.name}${e.target ? " → " + e.target : ""}`, e.down ? "down" : "");
  }
  const pressed = new Set(s.buttons);
  for (const [name, chip] of ui.chips) {
    chip.classList.toggle("down", pressed.has(name));
    chip.classList.toggle("tested", ui.tested.has(name) && !pressed.has(name));
  }
  if (s.events.length) updateProgress();

  ui.axes = s.axes;
  trackSector("left", ui.desc.sticks.left);
  trackSector("right", ui.desc.sticks.right);
  for (const [n, b] of ui.barEls) {
    const f = ui.desc.fields[n], v = s.axes[n];
    if (v === undefined) continue;
    const frac = Math.max(0, Math.min(1, (v - f.min) / ((f.max - f.min) || 1)));
    b.fill.style.width = `${(frac * 100).toFixed(1)}%`;
    b.val.textContent = String(v);
    if (frac > 0.9) ui.barTested.add(n);
    b.row.classList.toggle("tested", ui.barTested.has(n));
  }
  onRaw(s.raw, now);
}

// ---------------------------------------------------------------- SSE
let es = null;
function connect() {
  es = new EventSource("/api/events");
  es.addEventListener("state", async (m) => {
    const s = JSON.parse(m.data);
    if (s.gen !== ui.gen) {
      try { const d = await api("/api/current"); if (d.gen !== undefined) setup(d); } catch (_) { /* ігноруємо */ }
      if (s.gen !== ui.gen) return;
    }
    onState(s);
  });
  es.addEventListener("idle", () => {
    if (ui.desc) { ui.desc = null; ui.gen = -1; $("btn-open").hidden = false; $("btn-close").hidden = true; }
    $("st-state").textContent = $("auto").checked ? "шукаю геймпад…" : "не відкрито";
    $("st-state").className = "badge";
    $("st-info").textContent = $("auto").checked ? "Увімкніть геймпад і натисніть будь-яку кнопку — він відкриється сам." : "";
    $("st-rate").textContent = "";
  });
  es.onerror = () => { $("st-state").textContent = "немає зв'язку з сервером"; $("st-state").className = "badge error"; };
}

// ---------------------------------------------------------------- кнопки
$("btn-refresh").onclick = () => loadDevices().catch((e) => logLine("помилка: " + e.message));
$("btn-open").onclick = async () => {
  const path = $("device").value;
  if (!path) return;
  try { setup(await api("/api/open", { path, layout: $("layout").value || null })); $("auto").checked = false; }
  catch (e) { $("st-state").textContent = "помилка"; $("st-state").className = "badge error"; $("st-info").textContent = e.message; }
};
$("btn-close").onclick = async () => { await api("/api/close", {}); $("auto").checked = false; };
$("auto").onchange = async () => {
  try { await api("/api/auto", { on: $("auto").checked }); } catch (e) { logLine("помилка: " + e.message); }
};
$("btn-clear-log").onclick = () => $("log").replaceChildren();
$("btn-calib").onclick = () => {
  if (ui.calib) return;
  ui.calib = new Set();
  $("calib-state").textContent = "калібрую… не торкайтеся геймпада";
  setTimeout(() => {
    for (const i of ui.calib) ui.noisy.add(i);
    $("calib-state").textContent = `приглушено байтів: ${ui.noisy.size}`;
    ui.calib = null;
  }, 2000);
};
$("btn-clear-noise").onclick = () => { ui.noisy.clear(); $("calib-state").textContent = ""; };

function frame(now) { paintHex(now); drawSticks(); requestAnimationFrame(frame); }

(async () => {
  try { await loadDevices(); } catch (e) { logLine("помилка: " + e.message); }
  try { const d = await api("/api/current"); if (d.gen !== undefined) setup(d); } catch (_) { /* нічого не відкрито */ }
  connect();
  requestAnimationFrame(frame);
  // Список пристроїв оновлюється сам (hotplug); не чіпаємо його, поки користувач вибирає.
  setInterval(() => { if (document.activeElement !== $("device")) loadDevices().catch(() => {}); }, 2000);
})();
