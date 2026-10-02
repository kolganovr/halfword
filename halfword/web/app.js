"use strict";
// Панель Halfword: одна страница с вкладками, чистый JS без зависимостей.
const TOKEN = document.querySelector('meta[name="st-token"]').content;
const view = document.getElementById("view");
let status = null;      // последний статус из SSE
let tab = null;         // открытая вкладка
let live = {};          // обновители вкладки на новый статус
let timers = [];        // таймеры вкладки (сбрасываются при смене)

// ---------- помощники ----------
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
const svg = (tag, attrs, ...kids) => {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  kids.forEach(k => e.append(k.nodeType ? k : document.createTextNode(k)));
  return e;
};
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const num = x => Number(x).toLocaleString("ru-RU");
function dur(s) {
  if (s < 60) return Math.round(s) + " с";
  if (s < 3600) return (s / 60).toFixed(1) + " мин";
  return (s / 3600).toFixed(1) + " ч";
}
function when(v) {
  if (typeof v === "number" && v > 1e9) return new Date(v * 1000).toLocaleString("ru-RU");
  if (typeof v === "string" && /^\d{4}-\d\d-\d\d/.test(v)) return new Date(v).toLocaleString("ru-RU");
  return v;
}
function plain(v) {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "да" : "нет";
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${k}: ${plain(when(x))}`).join(", ");
  return String(when(v));
}

let toastTimer;
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "on" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), err ? 5000 : 2200);
}

async function api(path, body) {
  const opt = body === undefined
    ? { headers: { "X-Token": TOKEN } }
    : { method: "POST", headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* пустой ответ */ }
  if (!r.ok) throw new Error(data.error || "ошибка " + r.status);
  return data;
}
// запрос с показом ошибки пользователю
async function act(path, body, okMsg) {
  try {
    const d = await api(path, body);
    if (okMsg) toast(okMsg);
    return d;
  } catch (e) { toast(e.message, true); return null; }
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const every = (fn, ms) => { timers.push(setInterval(fn, ms)); };

function switchBox(label, checked, onchange) {
  const i = h("input", { type: "checkbox", checked, onchange: () => onchange(i.checked, i) });
  return [h("label", { class: "switch" }, i, label), i];
}

// ---------- вкладки ----------
const TABS = { overview, train, usage, settings, sandbox, dict };
function route() {
  const name = (location.hash || "#overview").slice(1);
  tab = TABS[name] ? name : "overview";
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.getAttribute("href") === "#" + tab));
  timers.forEach(clearInterval); timers = []; live = {};
  view.replaceChildren(h("p", { class: "muted" }, "Загрузка…"));
  TABS[tab]().catch(e => view.replaceChildren(h("div", { class: "card" }, "Не получилось загрузить: " + e.message)));
}
addEventListener("hashchange", route);

// ---------- SSE ----------
function connect() {
  const conn = document.getElementById("conn");
  const es = new EventSource("/api/events");
  es.addEventListener("status", ev => {
    const st = JSON.parse(ev.data);
    const ok = !st.error;
    conn.className = "conn" + (ok ? " live" : "");
    conn.lastChild.textContent = ok ? "на связи" : "программа не отвечает";
    if (!ok) return;
    status = st;
    Object.values(live).forEach(fn => fn(st));
  });
  es.onerror = () => { conn.className = "conn"; conn.lastChild.textContent = "нет связи"; };
}

// ---------- Обзор ----------
async function overview() {
  const [st, ov] = await Promise.all([api("/api/status"), api("/api/overview")]);
  status = st;
  const enBox = switchBox("Подсказки включены", st.enabled, async on => {
    const d = await act("/api/enabled", { on }); if (d) status = d; else enBox[1].checked = !on;
  });
  const llmBox = switchBox("LLM ✦ включена", st.llm_enabled, async on => {
    const d = await act("/api/llm", { on }); if (d) status = d; else llmBox[1].checked = !on;
  });
  const llmText = h("span", { class: "muted" }), mem = h("span", { class: "muted" });
  const paint = s => {
    enBox[1].checked = s.enabled; llmBox[1].checked = s.llm_enabled;
    llmText.textContent = s.llm_status;
    mem.textContent = s.mem_mb != null ? `Память процесса: ${s.mem_mb} МБ` : "";
  };
  paint(st); live.ov = paint;

  const period = (title, s) => h("div", { class: "card" },
    h("div", { class: "muted small" }, title),
    h("div", { class: "big" }, `${num(s.accepted)} `, h("span", { class: "muted small" }, `принято из ${num(s.shown)} (${pct(s.accept_rate)})`)),
    h("div", { class: "kv" }, h("span", {}, "Сэкономлено нажатий"), h("b", {}, `${num(s.saved)} (${pct(s.share, 1)})`)),
    h("div", { class: "kv" }, h("span", {}, "Сэкономлено времени"), h("b", {}, dur(s.secs))),
    s.cpm ? h("div", { class: "kv" }, h("span", {}, "Скорость набора"), h("span", {}, `~${s.cpm} зн/мин`)) : null,
    s.llm_shown ? h("div", { class: "kv" }, h("span", {}, "из них LLM ✦"), h("span", {}, `${s.llm_accepted} из ${s.llm_shown}, ${s.llm_saved} нажатий`)) : null,
    s.long_shown ? h("div", { class: "kv" }, h("span", {}, "из них в паузе ✦✦"), h("span", {}, `${s.long_accepted} из ${s.long_shown}, ${s.long_saved} нажатий`)) : null);

  const metric = h("select", { onchange: drawChart },
    h("option", { value: "saved" }, "Сэкономлено нажатий"),
    h("option", { value: "accepted" }, "Принято подсказок"),
    h("option", { value: "shown" }, "Показано подсказок"),
    h("option", { value: "typed" }, "Набрано нажатий"));
  const chart = h("div", {});
  function drawChart() { chart.replaceChildren(barChart(ov.daily, metric.value)); }
  drawChart();

  view.replaceChildren(
    h("h2", {}, "Обзор"),
    h("div", { class: "card" },
      h("div", { class: "row sp" }, h("div", { class: "row" }, enBox[0], llmBox[0]), mem),
      h("div", { class: "small", style: "margin-top:6px" }, llmText)),
    h("div", { class: "grid" }, period("Сегодня", ov.today), period("7 дней", ov.d7), period("30 дней", ov.d30)),
    h("div", { class: "card" }, h("div", { class: "row sp" }, h("b", {}, "По дням, 30 дней"), metric), chart));
  every(async () => { if (tab === "overview") try { Object.assign(ov, await api("/api/overview")); drawChart(); } catch (e) { /* ждём следующего */ } }, 15000);
}

function barChart(daily, key) {
  const W = 940, H = 190, L = 34, B = 22, T = 8;
  const max = Math.max(1, ...daily.map(d => d[key]));
  const bw = (W - L) / daily.length;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "График по дням" });
  [0, 0.5, 1].forEach(f => {
    const y = T + (H - B - T) * (1 - f);
    s.append(svg("line", { x1: L, x2: W, y1: y, y2: y, stroke: "var(--line)" }),
             svg("text", { x: L - 5, y: y + 4, "text-anchor": "end" }, num(Math.round(max * f))));
  });
  daily.forEach((d, i) => {
    const bh = (H - B - T) * d[key] / max;
    const x = L + i * bw;
    s.append(svg("rect", { class: "b", x: x + 2, y: H - B - bh, width: Math.max(1, bw - 4), height: Math.max(bh, d[key] ? 1 : 0), rx: 2 },
                 svg("title", {}, `${d.day}: ${num(d[key])}`)));
    if (i % 5 === 0 || i === daily.length - 1)
      s.append(svg("text", { x: x + bw / 2, y: H - 6, "text-anchor": "middle" }, d.day.slice(5)));
  });
  return s;
}

// ---------- Обучение ----------
async function train() {
  const d = await api("/api/train");
  const stateBox = h("div", {}), srcBox = h("div", {}), histBox = h("div", {});
  let running = false;

  function paintState(s) {
    if (!s) { stateBox.replaceChildren(h("p", { class: "muted" }, "Модуль обучения в этой сборке недоступен — переобучение запускается из меню трея.")); return; }
    const wasRunning = running;
    running = !!s.running;
    const startBtn = h("button", { class: "primary", disabled: running, onclick: async () => { await act("/api/train/start", {}, "Обучение запущено"); } }, "Переобучить");
    const cancelBtn = h("button", { disabled: !running, onclick: async () => { await act("/api/train/cancel", {}, "Отмена запрошена"); } }, "Отменить");
    const rollbackBtn = h("button", { class: "danger", disabled: running, onclick: async () => {
      if (confirm("Вернуть предыдущую модель?")) await act("/api/train/rollback", {}, "Откат выполнен"); } }, "Откатить");
    const p = Math.max(0, Math.min(1, (s.pct || 0) / 100));
    stateBox.replaceChildren(
      h("div", { class: "row" }, startBtn, cancelBtn, rollbackBtn),
      running ? h("div", { style: "margin-top:12px" },
        h("div", { class: "row sp small" }, h("span", {}, s.label || s.stage || "обучение…"), h("span", {}, pct(p))),
        h("div", { class: "bar big" }, h("i", { style: `width:${p * 100}%` })),
        s.started ? h("div", { class: "muted small" }, "Начато: " + when(s.started)) : null) : null,
      !running && s.last ? h("div", { class: "muted small", style: "margin-top:10px" }, "Последний запуск: " + plain(s.last)) : null,
      s.pending ? h("div", { class: "card", style: "margin:12px 0 0" },
        h("b", {}, "Есть отложенная модель"), " ", h("span", { class: "muted small" }, typeof s.pending === "object" ? plain(s.pending) : ""),
        h("div", { style: "margin-top:8px" }, h("button", { class: "primary", disabled: running, onclick: async () => {
          if (confirm("Применить отложенную модель?")) await act("/api/train/apply", {}, "Модель применена"); } }, "Применить отложенную"))) : null);
    if (wasRunning && !running) reloadLists();
  }

  function paintLists(d) {
    srcBox.replaceChildren(d.sources.length ? h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Источник"), h("th", { class: "n" }, "Текстов"), h("th", {}, "В обучении"))),
      h("tbody", {}, d.sources.map(s => h("tr", {}, h("td", {}, s.label || s.key), h("td", { class: "n" }, s.count == null ? "—" : num(s.count)),
        h("td", {}, s.enabled ? "да" : h("span", { class: "muted" }, "нет")))))) : h("p", { class: "muted" }, "Нет данных об источниках."));
    histBox.replaceChildren(d.history.length ? genericTable(d.history) : h("p", { class: "muted" }, "Запусков пока не было."));
  }
  async function reloadLists() { try { paintLists(await api("/api/train")); } catch (e) { /* без обновления */ } }

  view.replaceChildren(
    h("h2", {}, "Обучение"),
    h("div", { class: "card" }, stateBox),
    h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Источники"), srcBox),
    h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "История запусков"), h("div", { class: "scroll" }, histBox)));
  paintState(d.state); paintLists(d);
  live.train = st => paintState(st.train);
}

const HIST_LABELS = { started: "Начало", finished: "Конец", ts: "Время", t: "Время", reason: "Причина", result: "Итог", status: "Итог",
  secs: "Секунд", dur: "Секунд", saved_pct: "Экономия, %", tokens: "Токенов", words: "Слов", note: "Заметка" };
function genericTable(rows) {
  const cols = [...new Set(rows.flatMap(r => Object.keys(r)))].slice(0, 9);
  return h("table", {}, h("thead", {}, h("tr", {}, cols.map(c => h("th", {}, HIST_LABELS[c] || c)))),
    h("tbody", {}, rows.map(r => h("tr", {}, cols.map(c => h("td", {}, plain(r[c])))))));
}

// ---------- Журнал подсказок ----------
const OUT = [["accepted", "принял"], ["partial", "по словам"], ["typed", "напечатал сам"], ["diverged", "разошёлся"], ["dismissed", "Esc"], ["lost", "ушёл"]];
function stack(g) {
  const sp = OUT.filter(([k]) => g[k]).map(([k, name]) => {
    const f = g[k] / g.n;
    return h("span", { class: "o-" + k, style: `width:${f * 100}%`, title: `${name}: ${g[k]} (${pct(f)})` }, f >= 0.09 ? pct(f) : "");
  });
  return h("div", { class: "stack" }, sp);
}
async function usage() {
  let days = 7;
  const box = h("div", {});
  const sel = h("select", { onchange: () => { days = +sel.value; load(); } }, h("option", { value: 7 }, "7 дней"), h("option", { value: 30 }, "30 дней"));
  view.replaceChildren(h("div", { class: "row sp" }, h("h2", {}, "Журнал подсказок"), sel), box);
  async function load() {
    const u = await api("/api/usage?days=" + days);
    if (!u.total) { box.replaceChildren(h("div", { class: "card muted" }, "За этот период в журнале ничего нет.")); return; }
    const part = [h("p", { class: "muted small" }, `Подсказок в журнале: ${num(u.total)}. Тексты в журнал не пишутся.`),
      h("div", { class: "legend" }, OUT.map(([k, n]) => h("span", {}, h("i", { class: "o-" + k }), n)))];
    u.sources.forEach(s => {
      const rows = [...s.pos, { name: "всего", ...s.all }];
      part.push(h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, s.name + " — по месту в тексте"),
        h("div", { class: "scroll" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Место"), h("th", { class: "n" }, "Показано"), h("th", {}, "Исходы"), h("th", { class: "n" }, "Сэкон."))),
          h("tbody", {}, rows.map(r => h("tr", {}, h("td", {}, r.name), h("td", { class: "n" }, num(r.n)), h("td", {}, stack(r)),
            h("td", { class: "n" }, num(r.saved)))))))));
    });
    const srcName = k => (u.sources.find(s => s.src === k) || {}).name || k;
    if (u.hand.n) part.push(h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Напечатал сам то, что было на плашке"),
      h("p", {}, `${num(u.hand.n)} раз (≥4 совпавших символа); на быстром наборе, когда не успел заметить: ${pct(u.hand.fast_share)}.`),
      h("div", { class: "small muted" }, "По источнику: " + Object.entries(u.hand.by_src).map(([k, v]) => `${srcName(k)} ${v}`).join(", ")),
      u.late.n ? h("p", { class: "small muted" }, `Принял не сразу: ${u.late.n} раз, в среднем после ${u.late.avg_chars} символов руками.`) : null));
    if (Object.keys(u.miss).length) part.push(h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Чем расходишься с подсказкой"),
      ...Object.entries(u.miss).map(([src, list]) => h("div", { style: "margin-bottom:10px" }, h("div", { class: "small muted" }, srcName(src)),
        ...list.map(m => h("div", { class: "row small" }, h("span", { style: "width:130px" }, m.name),
          h("div", { class: "bar", style: "flex:1" }, h("i", { style: `width:${m.share * 100}%` })), h("span", { style: "width:90px;text-align:right" }, `${pct(m.share)} · ${m.n}`)))))));
    if (Object.keys(u.fix).length) part.push(h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Что делаешь после принятия"),
      ...Object.entries(u.fix).map(([src, f]) => h("div", { class: "small", style: "margin-bottom:8px" },
        h("b", {}, srcName(src)), ` (${f.n}): `, f.kinds.map(k => `${k.name} ${pct(k.share)}`).join(", "),
        f.what.length ? h("span", { class: "muted" }, " · что правил: " + f.what.map(w => `${w.name} ${w.n}`).join(", ")) : null))));
    part.push(h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "По программам"),
      h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Программа"), h("th", { class: "n" }, "Показано"), h("th", { class: "n" }, "Принято"), h("th", {}, "Источники (принято/показано)"))),
        h("tbody", {}, u.apps.map(a => h("tr", {}, h("td", {}, a.app), h("td", { class: "n" }, num(a.n)), h("td", { class: "n" }, `${num(a.accepted)} (${pct(a.n ? a.accepted / a.n : 0)})`),
          h("td", { class: "small" }, Object.entries(a.by_src).map(([k, v]) => `${srcName(k)} ${v.acc}/${v.n}`).join("  ")))))))));
    box.replaceChildren(...part);
  }
  await load();
}

// ---------- Настройки ----------
async function settings() {
  const [d, bl] = await Promise.all([api("/api/settings"), api("/api/blacklist")]);
  const dirty = {}, inputs = {};
  const saveBtn = h("button", { class: "primary", disabled: true, onclick: save }, "Сохранить");
  const groups = {};
  d.schema.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));

  function control(s) {
    const v = d.values[s.key];
    let i;
    if (s.type === "bool") { i = h("input", { type: "checkbox", checked: !!v }); return [h("label", { class: "switch" }, i), i, () => i.checked]; }
    if (s.type === "paths") {
      i = h("textarea", { rows: 3, placeholder: "C:\Users\вы\Documents\Заметки" }); i.value = (v || []).join("
");
      return [i, i, () => i.value.split("
").map(x => x.trim()).filter(Boolean)];
    }
    if (s.type === "enum") { i = h("select", {}, s.choices.map(c => h("option", { value: c }, c))); i.value = v; return [i, i, () => i.value]; }
    i = h("input", { type: "number", min: s.min, max: s.max, step: s.step || 1, value: v });
    return [h("span", {}, i, h("span", { class: "muted small" }, `  ${s.min}…${s.max}`)), i, () => (i.value === "" ? NaN : Number(i.value))];
  }
  const sections = Object.entries(groups).map(([g, list]) => h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, g),
    list.map(s => {
      const [node, el, get] = control(s);
      const err = h("div", { class: "err" });
      const row = h("div", { class: "field" },
        h("div", {}, s.label, s.restart ? h("span", { class: "badge warn" }, "после перезапуска") : null),
        h("div", { class: "hint" }, s.hint), h("div", { class: "ctl" }, node, err));
      const onchange = () => {
        const val = get();
        if (JSON.stringify(val) === JSON.stringify(d.values[s.key])) delete dirty[s.key]; else dirty[s.key] = val;
        row.classList.toggle("dirty", s.key in dirty);
        saveBtn.disabled = !Object.keys(dirty).length;
      };
      el.addEventListener("change", onchange);
      inputs[s.key] = { row, err, el };
      return row;
    })));

  async function save() {
    const r = await act("/api/settings", { changes: dirty });
    if (!r) return;
    for (const k of Object.keys(inputs)) inputs[k].err.textContent = r.errors[k] || "";
    for (const [k, v] of Object.entries(r.applied)) { d.values[k] = v; delete dirty[k]; inputs[k].row.classList.remove("dirty"); }
    saveBtn.disabled = !Object.keys(dirty).length;
    const bad = Object.keys(r.errors).length;
    toast(bad ? `Не принято: ${bad}` : "Настройки сохранены" + (r.restart.length ? " (часть — после перезапуска)" : ""), !!bad);
  }

  // чёрный список: недавние программы + текущий cfg.blacklist
  const checks = new Map();
  const names = [...new Set([...bl.recent.map(r => r.exe), ...bl.blacklist])];
  const info = Object.fromEntries(bl.recent.map(r => [r.exe, r]));
  const chips = h("div", { class: "chips" });
  const addChip = n => {
    const c = h("input", { type: "checkbox", checked: bl.blacklist.includes(n) });
    checks.set(n, c);
    const r = info[n];
    chips.append(h("label", { title: r ? `показано ${r.shown}, курсор не найден ${r.no_caret}` : "из чёрного списка" }, c, n,
      r && r.shown ? h("span", { class: "muted small" }, ` ${r.shown}`) : null));
  };
  names.forEach(addChip);
  const newName = h("input", { type: "text", placeholder: "program.exe" });
  const blSave = h("button", { class: "primary", onclick: async () => {
    const list = [...checks].filter(([, c]) => c.checked).map(([n]) => n);
    const r = await act("/api/blacklist", { blacklist: list }, "Чёрный список сохранён");
    if (r) bl.blacklist = r.blacklist;
  } }, "Сохранить список");
  const blAdd = () => {
    const n = newName.value.trim().toLowerCase();
    if (n && !checks.has(n)) { addChip(n); checks.get(n).checked = true; }
    newName.value = "";
  };
  newName.addEventListener("keydown", e => { if (e.key === "Enter") blAdd(); });

  view.replaceChildren(h("div", { class: "row sp" }, h("h2", {}, "Настройки"), saveBtn), ...sections,
    h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Чёрный список программ"),
      h("p", { class: "muted small" }, "В отмеченных программах подсказок нет и ничего не запоминается. Рядом с именем — сколько подсказок показано там за 30 дней."),
      chips, h("div", { class: "row", style: "margin-top:12px" }, newName, h("button", { onclick: blAdd }, "Добавить"), blSave)));
}

// ---------- Песочница ----------
async function sandbox() {
  const out = h("div", {});
  const ta = h("textarea", { placeholder: "Начни фразу, например: «Привет, как де»", maxlength: 2000 });
  async function run() {
    if (!ta.value) { out.replaceChildren(); return; }
    const r = await act("/api/sandbox", { text: ta.value });
    if (!r) return;
    const s = r.suggestion;
    out.replaceChildren(
      h("div", { class: "card" }, h("div", { class: "sug" }, h("span", { class: "typed" }, ta.value.slice(-80)), s ? h("span", { class: "ins" }, s.insert) : h("span", { class: "muted" }, " — подсказки нет")),
        s ? h("div", { class: "muted small", style: "margin-top:6px" }, `уровень: ${s.level} · уверенность ${s.confidence} · ${s.whole_word ? "слово целиком" : "общая основа"} · ${r.ms} мс`) : h("div", { class: "muted small", style: "margin-top:6px" }, `${r.ms} мс`),
        h("div", { class: "muted small" }, `контекст: «${r.context.c2}» «${r.context.c1}» · начало слова: «${r.context.prefix}»`)),
      h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Топ-8 кандидатов"),
        r.top.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Слово"), h("th", {}, "Вероятность"), h("th", { class: "n" }, "Опора (пары/тройки)"))),
          h("tbody", {}, r.top.map(t => h("tr", {}, h("td", {}, t.word),
            h("td", {}, h("div", { class: "row small" }, h("div", { class: "bar", style: "width:140px" }, h("i", { style: `width:${Math.min(1, t.p) * 100}%` })), pct(t.p, 1))),
            h("td", { class: "n" }, num(t.support)))))) : h("span", { class: "muted" }, "Кандидатов нет.")));
  }
  ta.addEventListener("input", debounce(run, 350));
  view.replaceChildren(h("h2", {}, "Песочница"), h("p", { class: "muted small" }, "Проверка n-граммной модели на любом тексте. LLM здесь не участвует, введённый текст нигде не сохраняется."), ta, out);
  ta.focus();
}

// ---------- Словарь ----------
async function dict() {
  const wordsBox = h("div", {}), banBox = h("div", {}), snBox = h("div", {});
  let prefix = "";
  async function load() {
    const d = await api("/api/dict?prefix=" + encodeURIComponent(prefix));
    paintWords(d); paintBanned(d.banned); paintSnippets(d.snippets);
  }
  function paintWords(d) {
    wordsBox.replaceChildren(
      h("div", { class: "muted small" }, `Найдено ${num(d.found)} из ${num(d.total)}; показаны самые частые (до 200).`),
      d.words.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Слово"), h("th", { class: "n" }, "Раз набрано"), h("th", {}, ""))),
        h("tbody", {}, d.words.map(w => h("tr", {}, h("td", {}, w.word), h("td", { class: "n" }, num(w.n)),
          h("td", { class: "n" },
            h("button", { class: "mini", onclick: async () => { if (confirm(`Забыть «${w.word}» везде (слова, пары, тройки)?`)) { const r = await act("/api/dict/forget", { word: w.word }, "Забыто"); if (r) load(); } } }, "Забыть"), " ",
            h("button", { class: "mini danger", onclick: async () => { const r = await act("/api/dict/ban", { word: w.word }, "Больше не подсказывается"); if (r) load(); } }, "Запретить")))))) : h("p", { class: "muted" }, "Ничего не найдено."));
  }
  function paintBanned(list) {
    banBox.replaceChildren(list.length ? h("div", { class: "chips" }, list.map(w => h("span", {}, w, " ",
      h("button", { class: "mini", onclick: async () => { const r = await act("/api/dict/unban", { word: w }, "Разрешено"); if (r) load(); } }, "Разрешить")))) : h("span", { class: "muted" }, "Пусто."));
  }
  function paintSnippets(sn) {
    const abbr = h("input", { type: "text", placeholder: "спс", style: "width:120px", maxlength: 40 });
    const text = h("input", { type: "text", placeholder: "спасибо", style: "flex:1;min-width:200px", maxlength: 2000 });
    const add = async () => { const r = await act("/api/snippets", { abbr: abbr.value, text: text.value }, "Сниппет сохранён"); if (r) load(); };
    text.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
    const rows = Object.entries(sn);
    snBox.replaceChildren(
      rows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Сокращение"), h("th", {}, "Текст"), h("th", {}, ""))),
        h("tbody", {}, rows.map(([a, t]) => h("tr", {}, h("td", {}, a), h("td", {}, t),
          h("td", { class: "n" }, h("button", { class: "mini danger", onclick: async () => { const r = await act("/api/snippets", { delete: a }, "Удалено"); if (r) load(); } }, "Удалить")))))) : h("p", { class: "muted" }, "Сниппетов нет."),
      h("div", { class: "row", style: "margin-top:10px" }, abbr, text, h("button", { class: "primary", onclick: add }, "Добавить")));
  }
  const q = h("input", { type: "text", placeholder: "Начало слова…", style: "width:260px" });
  q.addEventListener("input", debounce(() => { prefix = q.value; load(); }, 250));
  view.replaceChildren(h("h2", {}, "Словарь"),
    h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Слова, набранные руками"), q, h("div", { style: "margin-top:10px" }, wordsBox)),
    h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Запрещённые слова"), h("p", { class: "muted small" }, "Не подсказываются."), banBox),
    h("div", { class: "card" }, h("h3", { style: "margin-top:0" }, "Сниппеты"), h("p", { class: "muted small" }, "Свои сокращения: «спс» → «спасибо»."), snBox));
  await load();
}

connect();
route();
