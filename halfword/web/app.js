"use strict";
// Панель Halfword: одна страница с вкладками, чистый JS без зависимостей.
const TOKEN = document.querySelector('meta[name="st-token"]').content;
const view = document.getElementById("view");
let status = null;      // последний статус из SSE
let tab = null;         // открытая вкладка
let focusKey = null;    // к какой настройке прокрутить после открытия (#settings:llm_min_prob)
let live = {};          // обновители вкладки на новый статус
let timers = [];        // таймеры вкладки (сбрасываются при смене)
let cleanup = [];       // что убрать при смене вкладки (плавающие панели, наблюдатели)

// ---------- помощники ----------
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "hidden") e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
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
const pct = (x, d = 0) => (x * 100).toFixed(d).replace(".", ",") + "%";
const num = x => Number(x).toLocaleString("ru-RU");
function dur(s) {
  if (s < 60) return Math.round(s) + " с";
  if (s < 3600) return (s / 60).toFixed(1).replace(".", ",") + " мин";
  return (s / 3600).toFixed(1).replace(".", ",") + " ч";
}
function toDate(v) {
  if (typeof v === "number" && v > 1e9) return new Date(v * 1000);
  if (typeof v === "string" && /^\d{4}-\d\d-\d\d/.test(v)) return new Date(v);
  return null;
}
function when(v) {
  const d = toDate(v);
  return d ? d.toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : v;
}
const hhmm = t => new Date(t * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
function plain(v) {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "да" : "нет";
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${k}: ${plain(when(x))}`).join(", ");
  return String(when(v));
}
const N = (x, cls = "") => h("span", { class: "num " + cls }, x);
const store = {  // мелкие предпочтения вида; хранилище может быть недоступно
  get(k, d) { try { const v = localStorage.getItem("hw." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("hw." + k, JSON.stringify(v)); } catch (e) { /* без запоминания */ } },
};

let toastTimer;
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.lastChild.textContent = msg;
  t.className = "on" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = err ? "err" : ""), err ? 5000 : 2400);
}

async function api(path, body) {
  const opt = body === undefined
    ? { headers: { "X-Token": TOKEN } }
    : { method: "POST", headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* пустой ответ */ }
  if (!r.ok) { const e = new Error(data.error || "ошибка " + r.status); e.status = r.status; throw e; }
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

// подтверждение своим диалогом вместо confirm()
function ask(text, okLabel = "Да", danger = false) {
  return new Promise(resolve => {
    const dlg = h("dialog", { class: "ask", "aria-label": "Подтверждение" });
    const done = v => { dlg.close(); dlg.remove(); resolve(v); };
    const no = h("button", { class: "btn ghost", onclick: () => done(false) }, "Отмена");
    const yes = h("button", { class: "btn " + (danger ? "danger-fill" : "primary"), onclick: () => done(true) }, okLabel);
    dlg.append(h("p", {}, text), h("div", { class: "actions" }, no, yes));
    dlg.addEventListener("cancel", e => { e.preventDefault(); done(false); });
    document.body.append(dlg);
    dlg.showModal();
    (danger ? no : yes).focus();
  });
}

function switchBox(label, checked, onchange) {
  const i = h("input", { type: "checkbox", role: "switch", checked, onchange: () => onchange(i.checked, i) });
  return [h("label", { class: "switch" }, i, label), i];
}
function segmented(options, value, onchange, label) {
  const box = h("div", { class: "seg", role: "group", "aria-label": label });
  const paint = v => box.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === String(v)));
  options.forEach(([v, name]) => box.append(h("button", { type: "button", "data-v": v, onclick: () => { paint(v); onchange(v); } }, name)));
  paint(value);
  return box;
}
function pageHead(eyebrow, title, sub, ...right) {
  return h("header", { class: "page-head" },
    h("div", {}, h("div", { class: "label" }, eyebrow), h("h1", {}, title), sub ? h("p", {}, sub) : null),
    right.length ? h("div", { class: "actions" }, right) : null);
}
function sec(title, aside, ...body) {
  return h("section", { class: "sec" }, h("div", { class: "sec-h" }, h("h2", {}, title), aside ? h("div", { class: "aside" }, aside) : null), body);
}
const empty = (title, text) => h("div", { class: "empty" }, h("b", {}, title), text ? h("p", {}, text) : null);
function skeleton() {
  const bar = (w, ht, mt) => h("span", { class: "sk", style: `width:${w};height:${ht}px;margin-top:${mt}px` });
  return h("div", { "aria-busy": "true", "aria-label": "Загрузка" }, bar("120px", 12, 0), bar("280px", 36, 14), bar("100%", 180, 48), bar("70%", 14, 32), bar("55%", 14, 14));
}
function meter(f, dim) { return h("div", { class: "meter" + (dim ? " dim" : "") }, h("i", { style: `width:${Math.max(0, Math.min(1, f)) * 100}%` })); }
// replaceChildren печатает null как текст «null» — пустые места выкидываем
const put = (el, ...kids) => el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));
const keys = (...ks) => ks.map((k, i) => [i ? h("span", { class: "plus" }, "+") : null, h("kbd", {}, k)]);

// программа в чёрный список и обратно — из Обзора, Журнала, Настроек
async function blockExe(exe, on) {
  return act("/api/blacklist/exe", { exe, on }, on ? `${exe}: подсказки выключены` : `${exe}: подсказки включены`);
}

// ---------- вкладки ----------
const TABS = { overview, dict, settings, usage, train, sandbox };
const ink = document.querySelector(".nav-ink");
function moveInk() {
  const a = document.querySelector("#tabs a.on");
  if (a) ink.style.transform = `translateX(${a.offsetLeft}px) scaleX(${a.offsetWidth})`;
}
addEventListener("resize", debounce(moveInk, 100));
document.fonts && document.fonts.ready.then(moveInk);

function route() {
  const [name, arg] = (location.hash || "#overview").slice(1).split(":");
  tab = TABS[name] ? name : "overview";
  focusKey = arg || null;
  document.querySelectorAll("#tabs a").forEach(a => {
    const on = a.getAttribute("href") === "#" + tab;
    a.classList.toggle("on", on);
    if (on) { a.setAttribute("aria-current", "page"); a.scrollIntoView({ block: "nearest", inline: "nearest" }); } else a.removeAttribute("aria-current");
  });
  moveInk();
  timers.forEach(clearInterval); timers = []; live = {};
  cleanup.forEach(fn => fn()); cleanup = [];
  view.classList.remove("enter");
  view.replaceChildren(skeleton());
  const shown = () => { void view.offsetWidth; view.classList.add("enter"); };
  TABS[tab]().then(shown).catch(e => {
    view.replaceChildren(h("div", { class: "empty err" }, h("b", {}, "Не удалось загрузить вкладку"),
      h("p", {}, e.message + ". Проверьте, что Halfword запущен, и повторите."),
      h("button", { class: "btn", onclick: route }, "Повторить")));
    shown();
  });
}
addEventListener("hashchange", route);

// ---------- связь с программой ----------
let es = null;
function lostBanner(text, reload = true) {
  let b = document.getElementById("lost");
  if (!text) { if (b) b.remove(); return; }
  if (!b) {
    b = h("div", { id: "lost", role: "alert" }, h("span", {}), h("button", { class: "btn sm", onclick: () => location.reload() }, "Обновить"));
    document.querySelector(".top").after(b);
  }
  b.firstChild.textContent = text;
  b.lastChild.hidden = !reload;  // со старым ключом обновление не поможет — только ссылка из трея
}
const probe = debounce(async () => {
  const conn = document.getElementById("conn");
  conn.className = "conn"; conn.lastChild.textContent = "нет связи";
  try {
    await api("/api/status");
    if (es.readyState === EventSource.CLOSED) connect();  // программа на месте — поток статуса заново
  } catch (e) {
    if (e.status === 403) {  // после перезапуска у программы новый ключ, старая вкладка уже не пустит
      es.close();
      lostBanner("Halfword перезапущен — эта вкладка устарела. Откройте панель заново: трей → «Открыть панель…».", false);
    } else if (!e.status) {
      lostBanner("Halfword не отвечает. Если программа закрыта, запустите её ярлыком Halfword — панель подключится сама.");
      if (es.readyState === EventSource.CLOSED) setTimeout(connect, 5000);
    }
  }
}, 600);
function connect() {
  const conn = document.getElementById("conn");
  if (es) es.close();
  es = new EventSource("/api/events");
  es.addEventListener("status", ev => {
    const st = JSON.parse(ev.data);
    const ok = !st.error;
    conn.className = "conn" + (ok ? " live" : "");
    conn.lastChild.textContent = ok ? "на связи" : "занята";
    if (!ok) return;
    lostBanner(null);
    status = st;
    Object.values(live).forEach(fn => fn(st));
  });
  es.onerror = probe;
}

// ---------- Обзор ----------
const METRICS = [["saved", "нажатия"], ["accepted", "принято"], ["shown", "показано"], ["typed", "набрано"]];
const METRIC_NAMES = { saved: "сэкономлено нажатий", accepted: "принято подсказок", shown: "показано подсказок", typed: "набрано нажатий" };

async function overview() {
  const [st, ov] = await Promise.all([api("/api/status"), api("/api/overview")]);
  status = st;
  const enBox = switchBox("Подсказки", st.enabled, async on => {
    const d = await act("/api/enabled", { on }); if (d) paint(d); else enBox[1].checked = !on;
  });
  const llmBox = switchBox("LLM ✦ — продолжение фразы", st.llm_enabled, async on => {
    const d = await act("/api/llm", { on }); if (d) paint(d); else llmBox[1].checked = !on;
  });
  const pauseRow = h("div", { class: "sys-row" }), exeRow = h("div", { class: "sys-row" });
  const llmText = h("div", {}), diag = h("p", { class: "diag mono" }), acceptKey = h("kbd", {}, st.key);
  let sig = "";
  async function setPause(on) {
    const d = await act("/api/pause", { on }, on ? "Пауза на 1 час" : "Пауза снята");
    if (d) paint(d);
  }
  function paint(s) {
    status = s;
    enBox[1].checked = s.enabled; llmBox[1].checked = s.llm_enabled;
    acceptKey.textContent = s.key;
    llmText.textContent = [s.llm_status, s.llm_only_on_ac || !s.on_ac ? (s.on_ac ? "от сети" : "от батареи") : null,
      s.llm_ready && s.llm_ms ? `ответ ≈ ${num(s.llm_ms)} мс` : null].filter(Boolean).join(" · ");
    diag.textContent = s.mem_mb != null ? `диагностика · память процесса ${s.mem_mb} МБ` : "";
    // строки с кнопками перерисовываем только при изменении: иначе клик теряется на ежесекундном обновлении
    const next = JSON.stringify([s.pause_until, s.cfg_enabled, s.exe, s.exe_blocked, s.exe_no_caret]);
    if (next === sig) return;
    sig = next;
    const paused = s.pause_until > 0;
    pauseRow.replaceChildren(
      h("div", {}, h("div", { class: "sys-t" }, paused ? "Пауза до " + hhmm(s.pause_until) : "Пауза"),
        h("div", { class: "sys-s" }, paused ? "подсказки вернутся сами" : s.cfg_enabled ? "выключить подсказки на час" : "подсказки и так выключены")),
      h("button", { class: "btn sm " + (paused ? "primary" : "ghost"), disabled: !paused && !s.cfg_enabled, onclick: () => setPause(!paused) },
        paused ? "Снять паузу" : "На 1 час"));
    if (!s.exe) {
      exeRow.replaceChildren(h("div", {}, h("div", { class: "sys-t" }, "Программа"),
        h("div", { class: "sys-s" }, "здесь появится программа, где вы печатали последней")));
      return;
    }
    const [state, cls, note] = s.exe_blocked ? ["выключено", "bad", "в чёрном списке: подсказок нет, набор не запоминается"]
      : s.exe_no_caret ? [`курсор не найден ×${s.exe_no_caret}`, "accent", "сегодня здесь не нашёлся курсор — плашка может не появляться"]
      : ["работает", "ok", "последняя программа, где вы печатали"];
    exeRow.replaceChildren(
      h("div", {}, h("div", { class: "sys-t" }, h("span", { class: "mono" }, s.exe), " ", h("span", { class: "tag " + cls }, state)),
        h("div", { class: "sys-s" }, note)),
      h("button", { class: "btn sm ghost", onclick: () => blockExe(s.exe, !s.exe_blocked) }, s.exe_blocked ? "Включить здесь" : "Выключить здесь"));
  }
  paint(st); live.ov = paint;

  const hero = h("div", { class: "hero" });
  function paintHero() {
    const t = ov.today, w = ov.d7;
    const date = new Date().toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" });
    const label = h("div", { class: "label" }, "сегодня · " + date);
    if (!t.typed && !t.shown) {  // утро: вместо огромного нуля — что было вчера и за неделю
      const y = ov.daily[ov.daily.length - 2] || { saved: 0, typed: 0 };
      const yShare = y.typed + y.saved ? y.saved / (y.typed + y.saved) : 0;
      hero.replaceChildren(label,
        h("div", { class: "hero-empty" }, h("h2", {}, "Сегодня ещё не печатали"),
          h("p", {}, "Цифры появятся с первыми подсказками. Пока — как было раньше:")),
        h("div", { class: "hero-sub" },
          y.saved ? h("span", {}, "вчера ", h("b", {}, num(y.saved)), " нажатий · ", h("b", {}, pct(yShare, 1)), " набора") : h("span", {}, "вчера подсказок не было"),
          w.saved ? h("span", {}, "7 дней ", h("b", {}, num(w.saved)), " нажатий · ", h("b", {}, "≈ " + dur(w.secs))) : null));
      return;
    }
    // доля сэкономленного от набора не зависит от длины дня — её и сравниваем с неделей
    const delta = t.shown && w.shown ? (t.share - w.share) * 100 : null;
    hero.replaceChildren(label,
      h("div", { class: "hero-num" },
        h("span", { class: "big" }, num(t.saved)),
        h("div", { class: "hero-side" },
          delta != null ? h("span", { class: "delta" + (delta >= 0 ? " up" : ""), title: "доля сэкономленного от всего набора: сегодня против 7 дней" },
            (delta >= 0 ? "▲ +" : "▼ −") + Math.abs(delta).toFixed(1).replace(".", ",") + " п.п.") : null,
          h("span", { class: "unit" }, "нажатий сэкономлено", h("br"), `${pct(t.share, 1)} от всего набора`))),
      h("div", { class: "hero-sub" },
        h("span", {}, "принято ", h("b", {}, num(t.accepted)), " из ", h("b", {}, num(t.shown)), t.shown ? ` · ${pct(t.accept_rate)}` : ""),
        h("span", {}, "время ", h("b", {}, "≈ " + dur(t.secs))),
        t.cpm ? h("span", {}, "скорость ", h("b", {}, `${t.cpm} зн/мин`)) : null));
  }

  let metric = "saved";
  const chart = h("div", { class: "chart" });
  const drawChart = () => barChart(chart, ov.daily, metric);
  const ro = new ResizeObserver(debounce(drawChart, 60));
  ro.observe(chart); cleanup.push(() => ro.disconnect());

  // сравнение периодов одной таблицей
  const P = [["Сегодня", ov.today], ["7 дней", ov.d7], ["30 дней", ov.d30]];
  const row = (name, f, sub) => h("tr", { class: sub ? "sub" : "" }, h("td", {}, name), P.map(([, s]) => h("td", { class: "n" }, f(s))));
  const periods = h("div", { class: "scroll" }, h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, h("th", {}, ""), P.map(([n]) => h("th", { class: "n" }, n)))),
    h("tbody", {},
      row("Сэкономлено нажатий", s => num(s.saved)),
      row("Доля от всего набора", s => pct(s.share, 1)),
      row("Сэкономлено времени", s => dur(s.secs)),
      row("Принято подсказок", s => `${num(s.accepted)} / ${num(s.shown)}`),
      row("Доля принятых", s => (s.shown ? pct(s.accept_rate) : "—")),
      P.some(([, s]) => s.llm_shown) ? row("в т. ч. LLM ✦", s => (s.llm_shown ? `${num(s.llm_accepted)} / ${num(s.llm_shown)}` : "—"), true) : null,
      P.some(([, s]) => s.long_shown) ? row("в т. ч. пауза ✦✦", s => (s.long_shown ? `${num(s.long_accepted)} / ${num(s.long_shown)}` : "—"), true) : null,
      P.some(([, s]) => s.cpm) ? row("Скорость набора, зн/мин", s => (s.cpm ? num(s.cpm) : "—")) : null)));

  const keyRow = (k, text) => [h("span", { class: "k" }, k), h("span", {}, text)];
  view.replaceChildren(
    h("div", { class: "split" }, hero,
      h("div", { class: "panel sys" }, h("div", { class: "label" }, "система"), enBox[0], llmBox[0],
        h("div", { class: "sys-rows" }, pauseRow, exeRow), h("div", { class: "sys-foot" }, llmText))),
    sec("По дням, 30 дней", segmented(METRICS, metric, v => { metric = v; drawChart(); }, "Показатель графика"), chart),
    sec("Периоды", "принято / показано", periods),
    sec("Клавиши", "работают, пока видна подсказка", h("div", { class: "keys" },
      keyRow(acceptKey, "принять подсказку целиком"),
      keyRow(keys("Ctrl", "→"), "принять одно слово"),
      keyRow([keys("Alt", "↓"), h("span", { class: "plus" }, "/"), h("kbd", {}, "↑")], "другой вариант длинной подсказки ✦✦"),
      keyRow(keys("Esc"), "скрыть подсказку до следующей буквы"),
      keyRow(keys("Ctrl", "Alt", "F12"), "включить или выключить подсказки (всегда)"))),
    diag);
  paintHero(); drawChart();
  every(async () => { if (tab === "overview") try { Object.assign(ov, await api("/api/overview")); paintHero(); drawChart(); } catch (e) { /* ждём следующего */ } }, 15000);
}

function barChart(box, daily, key) {
  if (!daily.some(d => d[key])) {
    box.replaceChildren(empty("За 30 дней данных нет", "График заполнится, когда вы начнёте печатать с включёнными подсказками."));
    return;
  }
  const W = Math.max(280, box.clientWidth || 900), narrow = W < 560;
  const H = narrow ? 170 : 230, L = 40, B = 26, T = 10, R = 0;
  const max = Math.max(1, ...daily.map(d => d[key]));
  const step = (W - L - R) / daily.length, gap = Math.max(2, step * 0.42), bw = step - gap;
  const s = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${METRIC_NAMES[key]} по дням за 30 дней` });
  const gid = "g" + key;
  s.append(svg("defs", {}, svg("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
    svg("stop", { offset: "0", "stop-color": "var(--accent)", "stop-opacity": ".78" }),
    svg("stop", { offset: "1", "stop-color": "var(--accent)", "stop-opacity": ".1" }))));
  (max >= 4 ? [0, 0.5, 1] : [0, 1]).forEach(f => {
    const y = T + (H - B - T) * (1 - f);
    s.append(svg("line", { class: "gl", x1: L, x2: W - R, y1: y, y2: y }),
             svg("text", { class: "ax", x: L - 10, y: y + 3.5, "text-anchor": "end" }, f ? num(Math.round(max * f)) : "0"));
  });
  const cols = [];
  daily.forEach((d, i) => {
    const bh = (H - B - T) * d[key] / max, x = L + i * step + gap / 2, last = i === daily.length - 1;
    const col = svg("rect", { class: "col", x: L + i * step, y: T, width: step, height: H - B - T + 4, rx: 4 });
    cols.push(col); s.append(col);
    const bar = { x, y: H - B - Math.max(bh, 2), width: bw, height: Math.max(bh, 2), rx: Math.min(3, bw / 2) };
    if (d[key]) s.append(svg("rect", last ? { class: "today", ...bar } : { fill: `url(#${gid})`, ...bar }));
    else s.append(svg("rect", { class: "zero", x, y: H - B - 2, width: bw, height: 2, rx: 1 }));
    const every_ = narrow ? 7 : 5;
    if ((i % every_ === 0 && daily.length - 1 - i >= every_ / 1.6) || last) {
      const [, m, dd] = d.day.split("-");
      s.append(svg("text", { class: "ax", x: x + bw / 2, y: H - 6, "text-anchor": "middle" }, last ? "сегодня" : `${+dd}.${m}`));
    }
  });
  const tip = h("div", { class: "tip" });
  const fmtDay = day => new Date(day + "T12:00").toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "short" });
  s.addEventListener("pointermove", e => {
    const r = s.getBoundingClientRect();
    const i = Math.floor((e.clientX - r.left - L) / step);
    if (i < 0 || i >= daily.length) { s.dispatchEvent(new Event("pointerleave")); return; }
    cols.forEach((c, j) => c.classList.toggle("hl", j === i));
    const d = daily[i];
    tip.replaceChildren(h("span", { class: "muted" }, fmtDay(d.day)), h("b", {}, num(d[key])));
    tip.style.left = Math.min(W - 60, Math.max(60, L + (i + 0.5) * step)) + "px";
    tip.style.top = Math.max(T, H - B - (H - B - T) * d[key] / max) - 10 + "px";
    tip.classList.add("on");
  });
  s.addEventListener("pointerleave", () => { cols.forEach(c => c.classList.remove("hl")); tip.classList.remove("on"); });
  box.replaceChildren(s, tip);
}

// ---------- форма настроек: общая для «Настроек» и «Обучения» ----------
function fmtValue(s, v) {
  if (s.type === "bool") return v ? "вкл." : "выкл.";
  if (s.type === "paths") return v && v.length ? v.join(", ") : "пусто";
  if (s.type === "enum") return (s.names && s.names[v]) || v;
  return v == null ? "—" : String(v).replace(".", ",");
}
// → { row(spec), value(key), setValue(key, v), focus(key) }; плавающая панель «Сохранить» — своя на вкладку
function makeForm(d, opts = {}) {
  const dirty = {}, inputs = {};
  const count = h("b", {}, "0");
  const saveBtn = h("button", { class: "btn primary", onclick: save }, "Сохранить");
  const bar = h("div", { class: "savebar", role: "region", "aria-label": "Несохранённые изменения" },
    h("span", { class: "mono" }, "изменено: ", count), h("button", { class: "link", onclick: reset }, "Сбросить"), saveBtn);
  document.body.insertBefore(bar, document.getElementById("toast"));
  cleanup.push(() => bar.remove());
  const paintBar = () => { const n = Object.keys(dirty).length; count.textContent = n; bar.classList.toggle("on", n > 0); };

  // [узел, элемент, прочитать, записать]
  function control(s) {
    const v = d.values[s.key];
    let i;
    if (s.type === "bool") {
      i = h("input", { type: "checkbox", role: "switch", checked: !!v, "aria-label": s.label });
      return [h("label", { class: "switch" }, i), i, () => i.checked, x => (i.checked = !!x)];
    }
    if (s.type === "paths") {
      i = h("textarea", { rows: 3, placeholder: "C:\\Users\\вы\\Documents\\Заметки", "aria-label": s.label });
      const set = x => (i.value = (x || []).join("\n"));
      set(v);
      return [i, i, () => i.value.split("\n").map(x => x.trim()).filter(Boolean), set];
    }
    if (s.type === "enum") {
      i = h("select", { "aria-label": s.label }, s.choices.map(c => h("option", { value: c }, (s.names && s.names[c]) || c))); i.value = v;
      return [i, i, () => i.value, x => (i.value = x)];
    }
    i = h("input", { type: "number", min: s.min, max: s.max, step: s.step || 1, value: v, "aria-label": s.label });
    return [h("span", { class: "range" }, i, h("span", { class: "mono" }, `${s.min} … ${s.max}`)), i,
      () => (i.value === "" ? NaN : Number(i.value)), x => (i.value = x)];
  }

  function row(s) {
    const [ctl, el, get, set] = control(s);
    const err = h("div", { class: "err", role: "alert" });
    const dflt = d.defaults ? d.defaults[s.key] : undefined;
    const back = dflt === undefined ? null :
      h("button", { type: "button", class: "link reset", title: "Вернуть значение по умолчанию", onclick: () => { set(dflt); changed(); } }, "↺ по умолчанию");
    const r = h("div", { class: "field" + (s.adv ? " adv" : ""), id: "f-" + s.key },
      h("div", { class: "f-label" }, s.label, s.restart ? h("span", { class: "tag accent" }, "после перезапуска") : null),
      h("div", { class: "hint" }, s.hint, dflt === undefined ? null : h("span", { class: "dflt" }, " По умолчанию: " + fmtValue(s, dflt).replace(/\.?$/, "."))),
      h("div", { class: "ctl" }, ctl, back, err));
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    function changed() {
      const val = get();
      if (same(val, d.values[s.key])) delete dirty[s.key]; else dirty[s.key] = val;
      r.classList.toggle("dirty", s.key in dirty);
      r.classList.toggle("custom", dflt !== undefined && !same(val, dflt));
      paintBar();
      if (opts.onChange) opts.onChange();
    }
    el.addEventListener("change", changed);
    if (s.type === "int" || s.type === "float") el.addEventListener("input", changed);
    r.classList.toggle("custom", dflt !== undefined && !same(get(), dflt));
    inputs[s.key] = { row: r, err, el, set, changed, spec: s };
    return r;
  }

  async function save() {
    saveBtn.disabled = true;
    const r = await act("/api/settings", { changes: dirty });
    saveBtn.disabled = false;
    if (!r) return;
    for (const k of Object.keys(inputs)) inputs[k].err.textContent = r.errors[k] || "";
    for (const [k, v] of Object.entries(r.applied)) { d.values[k] = v; delete dirty[k]; inputs[k].row.classList.remove("dirty"); }
    paintBar();
    const bad = Object.keys(r.errors).length;
    toast(bad ? `Не принято: ${bad} — см. поля с ошибкой` : "Настройки сохранены" + (r.restart.length ? " · часть заработает после перезапуска" : ""), !!bad);
    if (opts.onSave) opts.onSave();
  }
  function reset() {
    for (const k of Object.keys(dirty)) { inputs[k].set(d.values[k]); inputs[k].err.textContent = ""; inputs[k].changed(); }
    paintBar();
  }
  return {
    row,
    value: k => (k in dirty ? dirty[k] : d.values[k]),
    setValue(k, v) { if (inputs[k]) { inputs[k].set(v); inputs[k].changed(); } },
    focus(k) {
      const i = inputs[k];
      if (!i) return false;
      if (i.spec.adv && opts.reveal) opts.reveal();
      i.row.scrollIntoView({ block: "center" });
      i.row.classList.add("flash");
      setTimeout(() => i.row.classList.remove("flash"), 2200);
      i.el.focus({ preventScroll: true });
      return true;
    },
  };
}

// ---------- Обучение ----------
const REASONS = { manual: "вручную", schedule: "по расписанию", auto: "автоматически", corpus: "новые тексты", start: "при запуске",
  first: "первый запуск", apply_pending: "применена отложенная", rollback: "откат модели" };
const RESULTS = { accepted: ["принята", "ok"], rejected: ["отклонена", "accent"], cancelled: ["отменено", ""], error: ["ошибка", "bad"],
  rollback: ["откат", ""] };
function resultTag(r) {
  const [name, cls] = RESULTS[r.result] || [r.result || "—", ""];
  return h("span", { class: "tag " + cls, title: r.error || null }, name);
}
const metricTxt = m => (typeof m === "number" ? m.toFixed(1).replace(".", ",") + "%" : "—");
function runLine(r) {
  if (!r || typeof r !== "object") return r ? String(r) : "";
  const parts = [when(r.when || r.ts || r.started || r.t)];
  if (r.result) parts.push((RESULTS[r.result] || [r.result])[0]);
  if (typeof r.metric === "number") parts.push("метрика " + metricTxt(r.metric));
  return parts.filter(Boolean).join(" · ");
}
// почему источник не участвует в обучении
function sourceNote(s) {
  if (s.count == null || s.enabled) return "";
  if (!s.count) return s.key === "notes" ? "добавьте папки ниже" : s.key === "telegram" ? "экспорт не найден" : "файлов нет";
  return "выключено: вес 0";
}

async function train() {
  const [d, sd] = await Promise.all([api("/api/train"), api("/api/settings")]);
  const stateBox = h("div", { class: "panel" }), srcBox = h("div", {}), histBox = h("div", {});
  let running = false;

  function paintState(s) {
    if (!s) {
      stateBox.replaceChildren(empty("Переобучение из панели недоступно", "В этой сборке нет модуля обучения — переобучение запускается из меню в трее."));
      return;
    }
    const wasRunning = running;
    running = !!s.running;
    const startBtn = h("button", { class: "btn primary", disabled: running, onclick: () => act("/api/train/start", {}, "Обучение запущено") }, "Переобучить");
    const cancelBtn = running ? h("button", { class: "btn ghost", onclick: () => act("/api/train/cancel", {}, "Отмена запрошена") }, "Отменить") : null;
    const rollbackBtn = h("button", { class: "link danger", disabled: running, onclick: async () => {
      if (await ask("Вернуть предыдущую модель? Текущая будет заменена той, что работала до последнего обучения.", "Откатить", true))
        await act("/api/train/rollback", {}, "Откат выполнен");
    } }, "Откатить");
    const p = Math.max(0, Math.min(1, (s.pct || 0) / 100));
    stateBox.replaceChildren(h("div", { class: "status" },
      h("div", {},
        h("h2", {}, h("i", { class: "dot" + (running ? " busy" : "") }), running ? "Идёт обучение" : s.pending ? "Есть новая модель" : "Модель в работе"),
        h("div", { class: "meta" }, running ? (s.started ? "начато " + when(s.started) : "") : s.last ? "последний запуск: " + runLine(s.last) : "запусков ещё не было")),
      h("div", { class: "actions" }, rollbackBtn, cancelBtn, startBtn),
      running ? h("div", { class: "progress" },
        h("div", { class: "row" }, h("span", {}, s.label || s.stage || "обучение…"), N(pct(p))),
        h("div", { class: "track", role: "progressbar", "aria-valuenow": Math.round(p * 100), "aria-valuemin": 0, "aria-valuemax": 100 },
          h("i", { style: `width:${p * 100}%` }))) : null,
      s.pending ? h("div", { class: "banner" },
        h("div", {}, h("p", {}, "Отложенная модель ждёт применения"), typeof s.pending === "object" ? h("div", { class: "muted" }, plain(s.pending)) : null),
        h("button", { class: "btn primary", disabled: running, onclick: async () => {
          if (await ask("Применить отложенную модель? Подсказки сразу пойдут от неё.", "Применить")) await act("/api/train/apply", {}, "Модель применена");
        } }, "Применить")) : null));
    if (wasRunning && !running) reloadLists();
  }

  function paintLists(d) {
    srcBox.replaceChildren(d.sources.length ? h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", { class: "w" }, "Источник"), h("th", { class: "n" }, "Файлов"), h("th", {}, "В обучении"))),
      h("tbody", {}, d.sources.map(s => h("tr", {}, h("td", {}, s.label || s.key), h("td", { class: "n" }, s.count == null ? "—" : num(s.count)),
        h("td", {}, h("span", { class: "src-state" }, s.enabled ? h("span", { class: "tag ok" }, "да") : h("span", { class: "tag" }, "нет"),
          sourceNote(s) ? h("span", { class: "muted small" }, sourceNote(s)) : null)))))) :
      empty("Нет данных об источниках", "Папки с текстами задаются ниже."));
    histBox.replaceChildren(d.history.length ? historyTable(d.history) : empty("Запусков пока не было", "Нажмите «Переобучить», когда добавите тексты."));
    if (d.error) histBox.prepend(h("p", { class: "err" }, d.error));
  }
  async function reloadLists() { try { paintLists(await api("/api/train")); } catch (e) { /* без обновления */ } }

  const form = makeForm(sd, { onSave: reloadLists });
  const fields = h("div", { class: "form show-adv" }, sd.schema.filter(s => s.tab === "train").map(form.row));
  view.replaceChildren(
    pageHead("модель n-грамм", "Обучение", "Модель учится на ваших текстах в отдельном процессе. Новая модель заменяет старую, только если подсказывает не хуже."),
    stateBox,
    sec("Источники", "тексты не читаются панелью — только число файлов", srcBox),
    sec("Что брать в обучение", "применится при следующем обучении", fields),
    sec("История запусков", d.history.length ? `последние ${d.history.length}` : null, h("div", { class: "scroll" }, histBox)));
  paintState(d.state); paintLists(d);
  live.train = st => paintState(st.train);
  if (focusKey) form.focus(focusKey);
}

function historyTable(rows) {
  const delta = r => (typeof r.metric === "number" && typeof r.prev_metric === "number" ? r.metric - r.prev_metric : null);
  return h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, h("th", {}, "Когда"), h("th", {}, "Причина"), h("th", {}, "Итог"), h("th", { class: "n", title: "доля сэкономленных нажатий на отложенной выборке" }, "Метрика"),
      h("th", { class: "n" }, "Δ, п.п."), h("th", { class: "n" }, "Слов"), h("th", { class: "n" }, "Длительность"))),
    h("tbody", {}, rows.map(r => {
      const dl = delta(r);
      return h("tr", {},
        h("td", { class: "num small" }, plain(r.when || r.ts || r.started || r.t)),
        h("td", { class: "muted" }, REASONS[r.reason] || r.reason || "—"),
        h("td", {}, resultTag(r)),
        h("td", { class: "n" }, metricTxt(r.metric)),
        h("td", { class: "n " + (dl == null ? "faint" : dl >= 0 ? "" : "muted") }, dl == null ? "—" : (dl >= 0 ? "+" : "−") + Math.abs(dl).toFixed(2).replace(".", ",")),
        h("td", { class: "n" }, r.words ? num(r.words) : "—"),
        h("td", { class: "n" }, typeof (r.secs ?? r.dur) === "number" && (r.secs ?? r.dur) > 0 ? dur(r.secs ?? r.dur) : "—"));
    })));
}

// ---------- Журнал подсказок ----------
// шесть исходов сводим в три группы: их цвета различимы, подробности — во всплывающей подсказке
const OUT_NAMES = { accepted: "Tab целиком", partial: "по словам (Ctrl+→)", typed: "напечатал её сам", dismissed: "Esc",
  lost: "ушёл: клик, другое окно", diverged: "начал печатать другое" };
const GROUPS3 = [["took", "принял", ["accepted", "partial"]], ["skip", "не стал брать", ["typed", "dismissed", "lost"]], ["miss", "мимо", ["diverged"]]];
const took = g => ((g.accepted || 0) + (g.partial || 0)) / (g.n || 1);
function stack3(g) {
  const parts = GROUPS3.map(([k, name, outs]) => ({ k, name, outs, n: outs.reduce((a, o) => a + (g[o] || 0), 0) })).filter(x => x.n);
  return h("div", { class: "stack", role: "img", "aria-label": parts.map(p => `${p.name} ${pct(p.n / g.n)}`).join(", ") },
    parts.map(p => h("span", { class: "o-" + p.k, style: `flex:${p.n / g.n} 1 0`,
      title: `${p.name}: ${p.n} (${pct(p.n / g.n)})\n` + p.outs.filter(o => g[o]).map(o => `· ${OUT_NAMES[o]}: ${g[o]}`).join("\n") })));
}
// 2–4 вывода из журнала, у каждого — что с этим сделать
function insights(u, blocked) {
  const out = [];
  const fix = (src, pos) => src === "ngram" ? ["Частота подсказок", "#settings:freq"]
    : src === "long" ? (pos === "line" ? ["Подсказки с новой строки", "#settings:llm_after_enter"] : ["Пауза до ✦✦", "#settings:llm_pause_ms"])
    : ["Порог LLM", "#settings:llm_min_prob"];
  for (const s of u.sources) {
    const base = took(s.all);
    if (s.all.n >= 30 && base < 0.15) {
      out.push({ w: 0.3 - base, text: `${s.name}: принимаете всего ${pct(base)} из ${num(s.all.n)} подсказок — они больше мешают, чем помогают.`, go: fix(s.src) });
      continue;
    }
    const worst = s.pos.filter(r => r.n >= 15 && took(r) < base * 0.6).sort((a, b) => took(a) - took(b))[0];
    if (worst) out.push({ w: base - took(worst), text: `${s.name}, ${worst.name}: принимаете ${pct(took(worst))} — в среднем по источнику ${pct(base)}.`, go: fix(s.src, worst.pos) });
  }
  for (const a of u.apps) {
    if (a.app !== "?" && a.n >= 20 && a.accepted / a.n < 0.1 && !blocked.includes(a.app.toLowerCase()))
      out.push({ w: 0.2, text: `${a.app}: принято ${num(a.accepted)} из ${num(a.n)}. Похоже, здесь подсказки не нужны.`, exe: a.app });
  }
  if (u.hand.n >= 10 && u.hand.fast_share >= 0.3)
    out.push({ w: u.hand.fast_share / 3, text: `${pct(u.hand.fast_share)} подсказок, которые вы напечатали сами, пришлись на быстрый набор — их не успевают заметить.`,
      go: ["Длина на быстром наборе", "#settings:fast_min_insert"] });
  const form = (u.miss.ngram || []).find(m => m.kind === "form");
  if (form && form.share >= 0.35 && form.n >= 10)
    out.push({ w: 0.05, text: `n-граммы чаще всего ошибаются формой слова (${pct(form.share)} расхождений): слово угадано, окончание нет. Принимайте по словам — Ctrl+→ — и правьте окончание.` });
  return out.sort((a, b) => b.w - a.w).slice(0, 4);
}

async function usage() {
  let days = 7;
  const box = h("div", {});
  view.replaceChildren(
    pageHead("что происходит с подсказками", "Журнал подсказок", "Каждая показанная подсказка и что вы с ней сделали. Тексты в журнал не пишутся — только исходы.",
      segmented([[7, "7 дней"], [30, "30 дней"]], days, v => { days = +v; load(); }, "Период")),
    box);
  async function load() {
    const [u, bl] = await Promise.all([api("/api/usage?days=" + days), api("/api/blacklist")]);
    if (!u.total) { box.replaceChildren(empty("За этот период в журнале пусто", "Поработайте с включёнными подсказками — исходы появятся здесь.")); return; }
    const srcName = k => (u.sources.find(s => s.src === k) || {}).name || k;
    const part = [];

    const ins = insights(u, bl.blacklist);
    part.push(sec("Главное", `за ${days} дней`, ins.length ? h("div", { class: "insights" }, ins.map(i => h("div", { class: "insight" },
      h("p", {}, i.text),
      i.go ? h("a", { class: "btn sm ghost", href: i.go[1] }, i.go[0] + " →") : null,
      i.exe ? h("button", { class: "btn sm ghost", onclick: async () => { if (await blockExe(i.exe, true)) load(); } }, "Выключить здесь") : null))) :
      h("p", { class: "muted" }, "Явных проблем не видно: подсказки принимают ровно по всем местам и программам.")));

    const tot = Object.fromEntries(GROUPS3.map(([k, , outs]) => [k, u.sources.reduce((a, s) => a + outs.reduce((b, o) => b + (s.all[o] || 0), 0), 0)]));
    const totN = u.sources.reduce((a, s) => a + s.all.n, 0) || 1;
    part.push(h("div", { class: "legend-bar" }, h("div", { class: "legend" },
      h("span", {}, N(num(u.total)), "подсказок"),
      GROUPS3.map(([k, n, outs]) => h("span", { title: outs.map(o => OUT_NAMES[o]).join(", ") }, h("i", { class: "o-" + k }), n, N(pct(tot[k] / totN)))))));
    u.sources.forEach(s => {
      const rows = [...s.pos.map(r => [r, false]), [{ name: "всего", ...s.all }, true]];
      part.push(sec(s.name, "по месту в тексте", h("div", { class: "scroll" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", {}, "Место"), h("th", { class: "n" }, "Показано"), h("th", { class: "w" }, "Исходы"), h("th", { class: "n" }, "Принято"), h("th", { class: "n" }, "Сэкон."))),
        h("tbody", {}, rows.map(([r, total]) => h("tr", { class: total ? "total" : "" }, h("td", {}, r.name), h("td", { class: "n" }, num(r.n)),
          h("td", { class: "w" }, stack3(r)), h("td", { class: "n" }, pct(took(r))), h("td", { class: "n" }, num(r.saved)))))))));
    });
    if (u.hand.n) part.push(sec("Напечатал сам то, что было на плашке", null, h("div", { class: "facts" },
      h("div", { class: "fact" }, h("div", { class: "label" }, "раз"), h("div", { class: "v" }, num(u.hand.n)),
        h("p", {}, "≥ 4 совпавших символа. По источнику: " + Object.entries(u.hand.by_src).map(([k, v]) => `${srcName(k)} ${v}`).join(", "))),
      h("div", { class: "fact" }, h("div", { class: "label" }, "на быстром наборе"), h("div", { class: "v" }, pct(u.hand.fast_share)),
        h("p", {}, "Печатали слишком быстро, чтобы заметить подсказку.")),
      u.late.n ? h("div", { class: "fact" }, h("div", { class: "label" }, "принял не сразу"), h("div", { class: "v" }, num(u.late.n)),
        h("p", {}, `В среднем после ${String(u.late.avg_chars).replace(".", ",")} символов, набранных руками.`)) : null)));

    // источники — столбцы, виды расхождения или правки — строки
    const crossTable = (title, aside, bySrc, cell) => {
      const srcs = Object.keys(bySrc);
      if (!srcs.length) return null;
      const kinds = [];
      srcs.forEach(src => bySrc[src].forEach(k => { if (!kinds.some(x => x.kind === k.kind)) kinds.push(k); }));
      return sec(title, aside, h("div", { class: "scroll" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", { class: "w" }, ""), srcs.map(src => h("th", { class: "n" }, srcName(src))))),
        h("tbody", {}, kinds.map(k => h("tr", {}, h("td", {}, k.name), srcs.map(src => {
          const x = bySrc[src].find(y => y.kind === k.kind);
          return h("td", { class: "n" }, x ? cell(x) : h("span", { class: "faint" }, "—"));
        })))))));
    };
    part.push(crossTable("Чем расходишься с подсказкой", "доля расхождений · сколько раз", u.miss, m => `${pct(m.share)} · ${m.n}`));
    const fixKinds = Object.fromEntries(Object.entries(u.fix).map(([src, f]) => [src, f.kinds]));
    const fixSec = crossTable("Что делаешь после принятия", "доля принятых", fixKinds, k => pct(k.share));
    if (fixSec) {
      const what = Object.entries(u.fix).filter(([, f]) => f.what.length).map(([src, f]) => `${srcName(src)}: ${f.what.map(w => `${w.name} ${w.n}`).join(", ")}`);
      if (what.length) fixSec.append(h("p", { class: "small muted after" }, "что правили — " + what.join("; ")));
      part.push(fixSec);
    }
    part.push(sec("По программам", null, h("div", { class: "scroll" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "Программа"), h("th", { class: "n" }, "Показано"), h("th", { class: "n" }, "Принято"), h("th", { class: "w" }, ""),
        h("th", {}, "Источники, принято / показано"), h("th", {}, ""))),
      h("tbody", {}, u.apps.map(a => {
        const f = a.n ? a.accepted / a.n : 0;
        const off = bl.blacklist.includes(a.app.toLowerCase());
        return h("tr", {}, h("td", { class: "mono" }, a.app), h("td", { class: "n" }, num(a.n)), h("td", { class: "n" }, `${num(a.accepted)} · ${pct(f)}`),
          h("td", { class: "w" }, meter(f)),
          h("td", { class: "small muted" }, Object.entries(a.by_src).map(([k, v]) => `${srcName(k)} ${v.acc}/${v.n}`).join(" · ")),
          h("td", { class: "row-act" }, a.app === "?" ? null : h("button", { class: "link" + (off ? "" : " danger"),
            onclick: async () => { if (await blockExe(a.app, !off)) load(); } }, off ? "включить" : "выключить здесь")));
      }))))));
    box.replaceChildren(...part.filter(Boolean));
  }
  await load();
}

// ---------- Настройки ----------
async function settings() {
  const [d, bl] = await Promise.all([api("/api/settings"), api("/api/blacklist")]);
  const specs = d.schema.filter(s => s.tab !== "train");
  const wrap = h("div", { class: "settings" + (store.get("adv", false) ? " show-adv" : "") });
  const setAdv = on => { wrap.classList.toggle("show-adv", on); advBox[1].checked = on; store.set("adv", on); };
  const advBox = switchBox("Расширенные", store.get("adv", false), on => setAdv(on));
  const form = makeForm(d, { onChange: paintPreset, reveal: () => setAdv(true) });

  // частота подсказок одним переключателем вместо порогов
  const presetBtns = d.presets.map(p => h("button", { type: "button", "data-v": p.key,
    onclick: () => Object.entries(p.values).forEach(([k, v]) => form.setValue(k, v)) }, p.name));
  const custom = h("span", { class: "seg-note mono" }, "сейчас свои значения");
  function paintPreset() {
    const cur = d.presets.find(p => Object.entries(p.values).every(([k, v]) => Math.abs(Number(form.value(k)) - v) < 1e-6));
    presetBtns.forEach(b => b.setAttribute("aria-pressed", !!cur && b.dataset.v === cur.key));
    custom.hidden = !!cur;
  }
  const freq = sec("Частота подсказок", null,
    h("p", { class: "lead" }, "Один переключатель вместо порогов уверенности и длины. Точная настройка — в «Расширенных». Применится после «Сохранить»."),
    h("div", { class: "preset" }, h("div", { class: "seg", role: "group", "aria-label": "Частота подсказок" }, presetBtns), custom));
  freq.id = "grp-freq";

  const groups = {};
  specs.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
  const sections = Object.entries(groups).map(([g, list], i) => {
    const node = sec(g, null, list.map(form.row));
    node.id = "grp-" + i;
    if (list.every(s => s.adv)) node.classList.add("adv");
    return node;
  });
  sections.push(Object.assign(sec("Обучение", null, h("p", { class: "lead" }, "Папки с текстами, веса Telegram и Claude, автообучение — во вкладке ",
    h("a", { href: "#train" }, "«Обучение»"), ".")), { id: "grp-train" }));

  // чёрный список: что выключено — сверху, недавние программы — таблицей; всё применяется сразу
  const blBody = h("div", {});
  function paintBl() {
    const blocked = bl.blacklist;
    const recent = bl.recent.filter(r => !blocked.includes(r.exe));
    blBody.replaceChildren(
      h("div", { class: "label group-t" }, "выключено · " + blocked.length),
      blocked.length ? h("div", { class: "chips" }, blocked.map(n => h("span", { class: "chip on" }, n,
        h("button", { type: "button", title: "Включить подсказки", "aria-label": `Включить подсказки в ${n}`, onclick: () => toggleBl(n, false) }, "×")))) :
        h("p", { class: "muted" }, "Пусто — подсказки работают во всех программах."),
      h("div", { class: "joined" }, newName, h("button", { class: "btn ghost", onclick: addBl }, "Выключить")),
      h("div", { class: "label group-t" }, "недавние программы · 30 дней"),
      recent.length ? h("div", { class: "scroll" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", { class: "w" }, "Программа"), h("th", { class: "n" }, "Показано"),
          h("th", { class: "n", title: "сколько раз не нашёлся курсор — там плашка может не появляться" }, "Курсор не найден"), h("th", {}, ""))),
        h("tbody", {}, recent.map(r => h("tr", {}, h("td", { class: "mono" }, r.exe), h("td", { class: "n" }, num(r.shown)),
          h("td", { class: "n" + (r.no_caret ? "" : " faint") }, r.no_caret ? num(r.no_caret) : "—"),
          h("td", { class: "row-act" }, h("button", { class: "link danger", onclick: () => toggleBl(r.exe, true) }, "выключить здесь"))))))) :
        h("p", { class: "muted" }, "Пока пусто: здесь появятся программы, где вы печатали."));
  }
  async function toggleBl(n, on) {
    const r = await blockExe(n, on);
    if (r) { bl.blacklist = r.blacklist; paintBl(); }
  }
  const newName = h("input", { type: "text", placeholder: "program.exe", "aria-label": "Имя программы" });
  async function addBl() {
    const n = newName.value.trim().toLowerCase();
    if (!n) return;
    await toggleBl(n.endsWith(".exe") ? n : n + ".exe", true);
    newName.value = "";
  }
  newName.addEventListener("keydown", e => { if (e.key === "Enter") addBl(); });
  const blSec = sec("Чёрный список программ", null,
    h("p", { class: "lead" }, "В выключенных программах подсказок нет и набор не запоминается. Применяется сразу."), blBody);
  blSec.id = "grp-bl";
  paintBl();

  const all = [freq, ...sections, blSec];
  const toc = h("nav", { class: "toc", "aria-label": "Группы настроек" }, all.map(s =>
    h("button", { type: "button", "data-id": s.id, class: s.classList.contains("adv") ? "adv" : null,
      onclick: () => s.scrollIntoView({ behavior: "smooth", block: "start" }) }, s.querySelector("h2").textContent)));
  const io = new IntersectionObserver(es => {
    const vis = es.filter(e => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (vis) toc.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.id === vis.target.id));
  }, { rootMargin: "-80px 0px -60% 0px" });
  all.forEach(s => io.observe(s));
  toc.firstChild.classList.add("on");
  cleanup.push(() => io.disconnect());

  wrap.append(h("aside", {}, toc), h("div", {}, all));
  view.replaceChildren(
    pageHead("как ведут себя подсказки", "Настройки", "Изменения применяются после «Сохранить». У каждого поля указано значение по умолчанию.", advBox[0]),
    wrap);
  paintPreset();
  if (focusKey === "freq") freq.scrollIntoView({ block: "start" });
  else if (focusKey && !form.focus(focusKey)) location.hash = "#train:" + focusKey;  // поле живёт во вкладке «Обучение»
}

// ---------- Песочница ----------
const ctxWord = w => (w.startsWith("<") ? "начало" : `«${w}»`);
// значок источника на плашке — как в App._show
const MARKS = { llm: "✦", long: "✦✦", layout: "⇄", snippet: "✎" };
// плашка так, как её рисует overlay.py: текст подсказки, клавиша в рамке, значок; ведущий пробел — отступом
function ovChip(insert, level) {
  const body = insert.replace(/^ +/, "");
  return h("span", { class: "ov" + (body.length < insert.length ? " gap" : "") },
    h("span", { class: "ov-t" }, body), h("span", { class: "ov-k" }, status ? status.key : "Tab"),
    MARKS[level] ? h("span", { class: "ov-m" }, MARKS[level]) : null);
}
async function sandbox() {
  let theme = null;  // тема превью: с сервера (как у плашки сейчас), можно переключить
  const screen = h("div", { class: "screen light", "aria-live": "polite" });
  const themeSeg = segmented([["light", "светлая"], ["dark", "тёмная"]], "light", v => { theme = v; paintTheme(); }, "Тема плашки");
  const paintTheme = () => { screen.className = "screen " + (theme || "light"); themeSeg.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === (theme || "light"))); };
  // строка «вашей программы»: хвост набранного, курсор и плашка рядом с ним
  function paintScreen(chip) {
    if (!ta.value) { put(screen, h("span", { class: "scr-none" }, "Начните печатать — здесь подсказка появится так, как на экране")); return; }
    const tail = ta.value.split("\n").pop().slice(-60);
    put(screen, h("span", { class: "scr-typed" }, tail), h("i", { class: "scr-caret", "aria-hidden": "true" }),
      chip || h("span", { class: "scr-none" }, "  подсказки нет"));
  }
  const why = h("p", { class: "why" }), metas = h("div", { class: "metas" }), top = h("div", {});
  const llmRes = h("span", { class: "llm-res" });
  const ta = h("textarea", { placeholder: "Привет, как де", maxlength: 2000, "aria-label": "Текст для проверки", spellcheck: "false" });
  const llmBtn = h("button", { class: "btn sm ghost", disabled: true, onclick: askLLM }, "Спросить LLM ✦");
  let seq = 0;  // ответ на устаревший текст не должен затереть свежий
  async function run() {
    const my = ++seq;
    llmBtn.disabled = !ta.value.trim();
    llmRes.replaceChildren();
    if (!ta.value) {
      paintScreen();
      why.replaceChildren(); metas.replaceChildren(); top.replaceChildren(); return;
    }
    const r = await act("/api/sandbox", { text: ta.value });
    if (!r || my !== seq) return;
    if (!theme && r.theme) { theme = r.theme; paintTheme(); }
    const s = r.suggestion, e = r.explain;
    paintScreen(s ? ovChip(s.insert, s.level) : null);
    why.className = "why" + (s ? " ok" : "");
    why.replaceChildren(h("b", {}, s ? "Будет подсказка: " : "Подсказки нет: "), e.why + ".");
    const m = (k, v) => h("span", {}, k + " ", h("b", {}, v));
    put(metas,
      s ? m("как", s.level_name) : null,
      e.conf != null ? m("уверенность", pct(e.conf)) : null, e.need != null ? m("порог", pct(e.need)) : null,
      m("перед словом", `${ctxWord(r.context.c2)} ${ctxWord(r.context.c1)}`), r.context.prefix ? m("начало слова", `«${r.context.prefix}»`) : null,
      m("время", `${String(r.ms).replace(".", ",")} мс`));
    top.replaceChildren(r.top.length ? h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "Слово"), h("th", { class: "w" }, "Вероятность"), h("th", { class: "n", title: "сколько раз слово встречалось после тех же слов" }, "Опора"), h("th", {}, ""))),
      h("tbody", {}, r.top.map((t, i) => h("tr", { class: i ? "" : "first" }, h("td", { class: "mono" }, t.word),
        h("td", { class: "w" }, h("div", { class: "p-cell" }, meter(Math.min(1, t.p), i > 0), N(pct(t.p, 1)))),
        h("td", { class: "n" }, num(t.support)),
        h("td", { class: "row-act" }, h("button", { class: "link danger", title: "Больше никогда не подсказывать это слово",
          onclick: async () => { if (await act("/api/dict/ban", { word: t.word }, `«${t.word}» больше не подсказывается`)) run(); } }, "запретить")))))) :
      empty("Кандидатов нет", "Модель не знает продолжений для этого контекста."));
  }
  async function askLLM() {
    const asked = ta.value;
    llmBtn.disabled = true;
    llmRes.replaceChildren(h("span", { class: "muted" }, "думает…"));
    const r = await act("/api/sandbox/llm", { text: asked });
    if (ta.value !== asked) return;  // текст уже другой — ответ к нему не относится, run() всё перерисовал
    llmBtn.disabled = !ta.value.trim();
    if (!r) { llmRes.replaceChildren(); return; }
    if (!r.ready) llmRes.replaceChildren(h("span", { class: "muted" }, "LLM недоступна — " + r.status));
    else if (r.insert) {
      paintScreen(ovChip(r.insert, "llm"));
      llmRes.replaceChildren(h("span", { class: "muted" }, `✦ на плашке выше · вероятность ${pct(r.prob)} при пороге ${pct(r.need)} · ${num(r.ms)} мс`));
    } else llmRes.replaceChildren(h("span", { class: "muted" }, `LLM не уверена: вероятность ниже порога ${pct(r.need)} · ${num(r.ms)} мс`));
  }
  ta.addEventListener("input", debounce(run, 300));
  paintScreen(); paintTheme();
  view.replaceChildren(
    pageHead("проверка модели", "Песочница", "Что подскажет модель на любой текст и почему. LLM ✦ — по кнопке. Введённое нигде не сохраняется."),
    h("div", { class: "composer" }, ta, screen,
      h("div", { class: "screen-bar" }, h("span", { class: "label" }, "так плашка выглядит у курсора"), themeSeg)), why, metas,
    h("div", { class: "llm-ask" }, llmBtn, llmRes),
    sec("Топ-8 кандидатов n-грамм", "по вероятности", top));
  ta.focus();
}

// ---------- Словарь ----------
async function dict() {
  const wordsBox = h("div", {}), banBox = h("div", {}), snBox = h("div", {}), found = h("span", {});
  let prefix = "", editing = null;  // editing — сокращение, которое сейчас правят
  async function load() {
    const d = await api("/api/dict?prefix=" + encodeURIComponent(prefix));
    paintWords(d); paintBanned(d.banned); paintSnippets(d.snippets);
  }
  const ban = async w => { if (await act("/api/dict/ban", { word: w }, `«${w}» больше не подсказывается`)) load(); };
  function paintWords(d) {
    found.textContent = d.total ? `найдено ${num(d.found)} из ${num(d.total)} · показаны самые частые, до 200` : "";
    wordsBox.replaceChildren(d.words.length ? h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", { class: "w" }, "Слово"), h("th", { class: "n" }, "Раз набрано"), h("th", {}, ""))),
      h("tbody", {}, d.words.map(w => h("tr", {}, h("td", { class: "w mono" }, w.word), h("td", { class: "n" }, num(w.n)),
        h("td", { class: "row-act" }, h("div", { class: "actions" },
          h("button", { class: "link", title: "Стереть из ваших слов; вернётся, если наберёте снова", onclick: async () => {
            if (await ask(`Забыть «${w.word}» — слово и сочетания с ним? Если наберёте его снова, оно опять запомнится.`, "Забыть", true)) {
              const r = await act("/api/dict/forget", { word: w.word }, "Забыто"); if (r) load();
            }
          } }, "забыть"),
          h("button", { class: "link danger", title: "Никогда не подсказывать", onclick: () => ban(w.word) }, "запретить"))))))) :
      empty(prefix ? "Ничего не найдено" : "Словарь пуст", prefix ? "Попробуйте начало покороче." : "Слова появятся, когда вы начнёте печатать с включёнными подсказками."));
  }
  const banInput = h("input", { type: "text", placeholder: "слово", maxlength: 30, "aria-label": "Слово, которое не подсказывать" });
  const addBan = () => { const w = banInput.value.trim(); if (w) { banInput.value = ""; ban(w); } };
  banInput.addEventListener("keydown", e => { if (e.key === "Enter") addBan(); });
  function paintBanned(list) {
    banBox.replaceChildren(list.length ? h("div", { class: "chips" }, list.map(w => h("span", { class: "chip" }, w,
      h("button", { type: "button", title: "Разрешить", "aria-label": `Разрешить «${w}»`, onclick: async () => { const r = await act("/api/dict/unban", { word: w }, "Разрешено"); if (r) load(); } }, "×")))) :
      h("p", { class: "muted" }, "Пусто."),
      h("div", { class: "joined" }, banInput, h("button", { class: "btn ghost", onclick: addBan }, "Запретить")));
  }
  const abbr = h("input", { type: "text", placeholder: "спс", maxlength: 40, "aria-label": "Сокращение", class: "short" });
  const text = h("input", { type: "text", placeholder: "спасибо", maxlength: 2000, "aria-label": "Текст", class: "grow" });
  const addBtn = h("button", { class: "btn primary", onclick: () => saveSnip() }, "Добавить");
  const cancelBtn = h("button", { class: "link", hidden: true, onclick: () => edit(null) }, "Отмена");
  let snippets = {};
  function edit(a) {
    editing = a;
    abbr.value = a || ""; text.value = a ? snippets[a] : "";
    addBtn.textContent = a ? "Сохранить" : "Добавить";
    cancelBtn.hidden = !a;
    snBox.querySelectorAll(".snip").forEach(el => el.classList.toggle("editing", el.dataset.a === a));
    if (a) text.focus();
  }
  async function saveSnip() {
    const a = abbr.value.trim();
    if (!editing && a in snippets && !(await ask(`Сниппет «${a}» уже есть: «${snippets[a]}». Заменить?`, "Заменить"))) return;
    const r = await act("/api/snippets", { abbr: a, text: text.value, old: editing }, editing ? "Сниппет изменён" : "Сниппет сохранён");
    if (r) { edit(null); load(); }
  }
  text.addEventListener("keydown", e => { if (e.key === "Enter") saveSnip(); if (e.key === "Escape") edit(null); });
  function paintSnippets(sn) {
    snippets = sn;
    const rows = Object.entries(sn);
    snBox.replaceChildren(
      rows.length ? h("div", { class: "snips" }, rows.map(([a, t]) => h("div", { class: "snip" + (a === editing ? " editing" : ""), "data-a": a },
        h("span", { class: "abbr" }, a), h("span", { class: "arrow", "aria-hidden": "true" }, "→"), h("span", {}, t),
        h("span", { class: "row-act" }, h("span", { class: "actions" },
          h("button", { class: "link", onclick: () => edit(a) }, "изменить"),
          h("button", { class: "link danger", onclick: async () => {
            if (await ask(`Удалить сниппет «${a}»?`, "Удалить", true)) { const r = await act("/api/snippets", { delete: a }, "Удалено"); if (r) { if (editing === a) edit(null); load(); } }
          } }, "удалить")))))) : h("p", { class: "muted" }, "Сниппетов нет."),
      h("div", { class: "joined" }, abbr, text, cancelBtn, addBtn));
  }
  const q = h("input", { type: "search", placeholder: "Начало слова…", "aria-label": "Поиск по словарю" });
  q.addEventListener("input", debounce(() => { prefix = q.value; load(); }, 250));
  const icon = svg("svg", { viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "aria-hidden": "true" },
    svg("circle", { cx: 7, cy: 7, r: 4.75 }), svg("path", { d: "M10.5 10.5 14 14", "stroke-linecap": "round" }));
  view.replaceChildren(
    pageHead("что модель знает о вас", "Словарь", "Слова, которые вы набирали руками. «Забыть» стирает слово из ваших — оно вернётся, если наберёте снова. «Запретить» — не подсказывать никогда, ни n-граммам, ни LLM."),
    h("div", { class: "search" }, icon, q),
    sec("Слова", found, wordsBox),
    sec("Запрещённые слова", "не подсказываются никогда", h("p", { class: "lead" }, "Можно запретить любое слово, даже то, которого нет в списке выше: например, неудачную подсказку LLM."), banBox),
    sec("Сниппеты", "свои сокращения: «спс» → «спасибо»", snBox));
  await load();
}

connect();
route();
