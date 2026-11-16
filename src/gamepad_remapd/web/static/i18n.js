'use strict';
/* Tiny i18n for the web clients. Strings live in JSON files (web/static/locales/<code>.json and
 * <config>/locales/<code>.json), served by /api/locale/<code>. Language choice, in order:
 * the picker (localStorage) -> "language" in daemon.json -> the browser languages -> English.
 * Missing keys fall back to English, then to the key itself. */
const I18N = (() => {
  const st = { lang: 'en', dict: {}, base: {}, list: [{ code: 'en', name: 'English' }], configured: 'auto' };
  const fill = (s, v) => (v ? s.replace(/\{(\w+)\}/g, (m, k) => (k in v ? v[k] : m)) : s);
  const tx = (key, vars) => fill(st.dict[key] ?? st.base[key] ?? key, vars);
  const getJson = async url => { const r = await fetch(url); if (!r.ok) throw new Error(r.statusText); return r.json(); };
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { /* storage unavailable */ } return null; };

  function pick() {
    const codes = st.list.map(l => l.code);
    const wanted = [store('lang'), st.configured !== 'auto' ? st.configured : null, ...(navigator.languages || [navigator.language])];
    for (const w of wanted) {
      if (!w) continue;
      if (codes.includes(w)) return w;
      const base = w.split('-')[0];
      if (codes.includes(base)) return base;
    }
    return 'en';
  }
  /* "**bold**" in a string becomes <b>; everything else stays plain text (never innerHTML). */
  function rich(el, text) {
    const parts = text.split('**');
    el.replaceChildren(...parts.map((p, i) => {
      if (i % 2) { const b = document.createElement('b'); b.textContent = p; return b; }
      return document.createTextNode(p);
    }));
  }
  function apply(root) {
    for (const e of root.querySelectorAll('[data-i18n]')) rich(e, tx(e.dataset.i18n));
    for (const a of ['title', 'aria-label', 'placeholder'])
      for (const e of root.querySelectorAll(`[data-i18n-${a}]`)) e.setAttribute(a, tx(e.getAttribute(`data-i18n-${a}`)));
  }
  async function init() {
    try { const m = await getJson('/api/locales'); st.list = m.available; st.configured = m.configured; } catch { /* English only */ }
    st.lang = pick();
    try { st.base = await getJson('/api/locale/en'); } catch { /* keys will show */ }
    st.dict = st.lang === 'en' ? st.base : await getJson('/api/locale/' + st.lang).catch(() => ({}));
    document.documentElement.lang = st.lang;
    apply(document);
    const sel = document.getElementById('lang');
    if (sel) {
      sel.replaceChildren(...st.list.map(l => { const o = document.createElement('option'); o.value = l.code; o.textContent = l.name; o.selected = l.code === st.lang; return o; }));
      sel.hidden = st.list.length < 2;
      sel.addEventListener('change', () => { store('lang', sel.value); location.reload(); });
    }
  }
  return { init, tx, apply, state: st };
})();
const tx = I18N.tx;
