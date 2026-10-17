"""Журнал подсказок: что стало с каждой показанной подсказкой — без текста.

Строка в data/usage.jsonl на подсказку: источник (n-граммы / LLM ✦ / пауза ✦✦), место в тексте, программа,
длина, исход и сколько символов совпало с тем, что напечатали руками; для принятых — что поправили после.
Подсказка отслеживается по нажатиям, а не по буферу: пока печатаешь ровно её, она жива, даже если плашка скрылась.

Исходы (out):
  accepted  — Tab или Ctrl+→ до конца;     partial — часть по Ctrl+→, дальше сам;
  typed     — напечатал её целиком руками (не заметил или не стал принимать);
  diverged  — начал печатать другое (match — сколько символов до этого совпало);
  erased    — стёр текст перед подсказкой;  dismissed — Esc;  lost — клик, стрелки, смена окна.
Чем отличалось (miss — для diverged/partial, fix_kind — правка после принятия):
  form — то же слово в другой форме (общая основа), word — другое слово, punct — знак препинания,
  case — регистр, short — закончил раньше (точка, Enter, перестал печатать).
Правка после принятия (fix): kept — не трогал; cut — стёр хвост и пошёл дальше; edit — стёр и переписал;
  ctrlbs — Ctrl+Backspace; undo — сочетание клавиш (Ctrl+Z и т.п.) сразу после принятия.

Исправление раскладки — отдельные строки с src = "layout" (текст не пишется, только форма):
  dir — en2ru / ru2en; len — 1-2 / 3-4 / 5+ букв в самом длинном слове; chain — слов в исправлении;
  mode — suggest / auto (режим на тот момент); how — inword (плашка посреди слова), delim (плашка после разделителя),
  auto (исправлено само), hotkey (ручная клавиша);
  out — accepted (исправлено: Tab, само или клавишей), ignored (плашку не приняли), undone (отменили Backspace
  или повторной клавишей), never (после второй отмены слово ушло в исключения), missed (человек стёр закрытое слово
  ≥3 букв и набрал взамен его перевод, а детектор молчал — только dir и len);
  switch_ok — раскладка после исправления сменилась (проверка через ~150 мс).
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .model import TRAILING_WORD_RE as PREFIX_RE, WORD_CH

WORD = re.compile(rf"[{WORD_CH}\-'’]")
SENT_END = ".!?…"
MAX_OPEN = 4            # сколько подсказок отслеживать одновременно (n-граммы меняются на каждом нажатии)
DIVERGE_WAIT = 30       # сколько символов ждать конца слова, в котором разошлись
FIX_SETTLE = 12         # столько символов после самой глубокой точки правки — правка закончена
FIX_KEPT = 20           # столько символов дальше без стирания — принятое оставлено как есть
FIX_TIMEOUT_S = 30
UNDO_S = 5
MAX_BYTES = 20_000_000  # дальше usage.jsonl → usage.1.jsonl
LAYOUT_PUNCT = ".,;:!?()\"'"


def place(buf: str) -> str:
    """Место, где показана подсказка: word — посреди слова, space — после пробела посреди предложения,
    sent — после конца предложения, line — в начале строки, punct — сразу после знака."""
    if not buf or buf.endswith("\n"):
        return "line"
    if WORD.match(buf[-1]):
        return "word"
    if not buf[-1].isspace():
        return "punct"
    body = buf.rstrip(" \t")
    if not body or body.endswith("\n"):
        return "line"
    return "sent" if body[-1] in SENT_END else "space"


def _common(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _word_at(s: str, i: int) -> str:
    j = i
    while j < len(s) and WORD.match(s[j]):
        j += 1
    return s[i:j]


def diff_kind(sugg: str, real: str) -> str:
    """Чем напечатанное (real), отличается от подсказки (sugg), в месте первого расхождения."""
    i = _common(sugg, real)
    s = i
    while s > 0 and WORD.match(sugg[s - 1]):
        s -= 1
    a = sugg[i:i + 1]
    b = real[i:i + 1]
    if not b or b == "\n" or (b in SENT_END and not WORD.match(a or " ")):
        return "short"
    if not (a and WORD.match(a)) and not WORD.match(b):
        return "punct"  # «мы пошли» → «мы, пошли»
    if s == i and not WORD.match(b):
        return "punct" if b.strip() else "short"  # слово подсказки не стал писать
    sw, rw = _word_at(sugg, s), _word_at(real, s)
    if not rw:
        return "short"
    if sw.lower() == rw.lower():
        return "case"
    cp = _common(sw.lower(), rw.lower())
    if cp >= 3 and abs(len(sw) - len(rw)) <= 3 and cp >= min(len(sw), len(rw)) - 3:
        return "form"
    return "word"


def len_bucket(n: int) -> str:
    return "1-2" if n <= 2 else "3-4" if n <= 4 else "5+"


def layout_info(original: str, to_cyr: bool, words: int, mode: str, how: str) -> dict:
    """Форма исправления раскладки для журнала: направление, длина самого длинного слова, цепочка, режим, способ."""
    longest = max((len(w.strip(LAYOUT_PUNCT)) for w in original.split()), default=0)
    return dict(dir="en2ru" if to_cyr else "ru2en", len=len_bucket(longest), chain=words, mode=mode, how=how)


class MissWatch:
    """Пропуск раскладки: стёр Backspace-ами законченное слово и набрал взамен ровно его перевод.

    Слова держим только в памяти, пока идёт сравнение; наружу отдаются направление и длина.
    convert(слово) → (перевод, в кириллицу?); silent(текст) → детектор по такому тексту промолчал.
    """

    def __init__(self, convert, silent):
        self.convert, self.silent = convert, silent
        self.run = ""      # стёртое подряд Backspace-ами
        self.watch = None  # {"target", "got", "dir", "len"}: сравниваем набранное с переводом стёртого

    def bs(self, removed: str):
        self.watch = None
        self.run = removed + self.run

    def other(self):
        self.run, self.watch = "", None

    def char(self, ch: str) -> dict | None:
        """→ {"dir", "len"}, когда пропуск замечен."""
        if self.watch is None and self.run:
            self.watch, self.run = self._arm(self.run), ""
        w = self.watch
        if w is None:
            return None
        w["got"] += ch
        got, target = w["got"], w["target"]
        if got == target or target.startswith(got):
            return None  # переводит дальше (или слово напечатано, ждём разделитель)
        if got.startswith(target) and len(got) == len(target) + 1 and not ch.isalnum():
            self.watch = None
            return {"dir": w["dir"], "len": w["len"]}
        self.watch = None
        return None

    def _arm(self, run: str) -> dict | None:
        if run[-1:].isalnum():
            return None  # слово было не закончено
        word = run.strip().rstrip(LAYOUT_PUNCT + " ")
        if len(word.split()) != 1 or sum(c.isalpha() for c in word) < 3:
            return None
        conv, to_cyr = self.convert(word)
        if conv == word or sum(c.isalpha() for c in conv) < 3 or not self.silent(run):
            return None
        return {"target": conv, "got": "", "dir": "en2ru" if to_cyr else "ru2en",
                "len": len_bucket(len(word))}


class Episode:
    def __init__(self, full: str, src: str, pos: str, app: str, now: float, gap: int, idle: int, variants: int,
                 conf: float | None = None, lvl: str = "", plen: int = 0):
        self.full = full            # подсказка целиком от точки показа
        self.conf, self.lvl, self.plen = conf, lvl, plen  # уверенность, уровень, длина префикса — для калибровки
        self.typed = ""             # что набрано от точки показа (руками и принятием)
        self.acc = 0                # символов вставлено принятием
        self.src = self.src0 = src
        self.pos, self.app = pos, app
        self.t0 = now
        self.first = None           # мс до первого нажатия после показа
        self.keys = 0
        self.gap, self.idle = gap, idle
        self.words0 = len(full.split())
        self.variants = variants
        self.cycles = 0
        self.div = None             # индекс расхождения (ждём конца слова)
        self.out = None
        self.miss = None
        # правка после принятия: fix_text — принятое, tail — текст от той же точки сейчас
        self.fix_text = ""
        self.tail = ""
        self.low = 0               # насколько глубоко стирал в принятое (≤ 0)
        self.fix_t = 0.0

    def record(self, now: float) -> dict:
        r = dict(t=int(self.t0), app=self.app, src=self.src, pos=self.pos, words=self.words0,
                 chars=len(self.full), out=self.out, match=_common(self.full, self.typed),
                 acc=self.acc, keys=self.keys, vis=int((now - self.t0) * 1000), gap=self.gap, idle=self.idle)
        if self.src0 != self.src:
            r["src0"] = self.src0  # сначала показал один источник, продлил другой
        if self.first is not None:
            r["first"] = self.first
        if self.src == "long":
            r["var"], r["cyc"] = self.variants, self.cycles
        if self.miss:
            r["miss"] = self.miss
        if self.conf is not None:
            r["conf"], r["lvl"], r["plen"] = round(self.conf, 3), self.lvl, self.plen
        return r


class Usage:
    def __init__(self, path: Path | None, clock=time.time):
        self.path = path
        self.clock = clock
        self.open: list[Episode] = []
        self.cur: Episode | None = None     # та, что сейчас на плашке
        self.fix: Episode | None = None     # принятая, ждём правок
        self.rows: list[dict] = []
        self.lay: dict | None = None        # плашка ⇄ на экране: {"t", "app", "info", "stale"}
        self.last_key = 0.0
        self.gaps: list[float] = []
        self.swap = False

    # --- события от приложения ---
    def shown(self, buf: str, insert: str, level: str, app: str, variants: int = 0, conf: float | None = None):
        now = self.clock()
        src = {"llm": "llm", "long": "long"}.get(level, "ngram")
        for ep in list(self.open):
            if ep.lvl == "fix" and not (level == "fix" and ep.full == insert):
                self._close(ep, "lost")  # исправление слова не продлевается: другое слово или другой уровень
        for ep in self.open:
            if level == "fix" and ep.lvl == "fix":
                self.cur = ep  # то же исправление пересчитано на следующей букве: один показ
                self.swap = False
                return
            t = ep.typed + insert
            if ep.div is None and (t.startswith(ep.full) or ep.full.startswith(t)):
                if len(t) > len(ep.full):
                    ep.full = t   # подсказка растёт (пауза) или её продлил другой источник
                    ep.src = src  # вставлять будешь уже его текст
                ep.variants = max(ep.variants, variants)
                self.cur = ep
                self.swap = False
                return
        if self.swap and self.cur in self.open and self.cur.div is None:
            ep = self.cur  # Alt+↓/↑: тот же показ, другой вариант
            ep.full = ep.typed + insert
            ep.cycles += 1
            ep.variants = max(ep.variants, variants)
            self.swap = False
            return
        self.swap = False
        gaps = sorted(self.gaps)
        gap = int(gaps[len(gaps) // 2]) if gaps else 0
        idle = int((now - self.last_key) * 1000) if self.last_key else 0
        m = PREFIX_RE.search(buf[-40:])
        ep = Episode(insert, src, place(buf), app, now, gap, idle, variants, conf, level, len(m.group(0)) if m else 0)
        self.open.append(ep)
        self.cur = ep
        if len(self.open) > MAX_OPEN:
            self._close(self.open[0], "lost")

    def char(self, ch: str):
        self._key()
        if self.lay:
            if self.lay["info"]["how"] == "inword" and not ch.isspace() and not self.lay["stale"]:
                self.lay["stale"] = True  # слово дописывают: плашка пересчитается, пока не покажут снова — ждём
            else:
                self._lay_close("ignored")
        for ep in list(self.open):
            self._step(ep)
            if ep.lvl == "fix":
                continue  # insert заменяет набранное слово, а не продолжает его: сверять нечего
            ep.typed += ch
            self._check(ep)
        if self.fix:
            self.fix.tail += ch
            self._fix_check(self.fix, ch)

    def bs(self):
        self._key()
        self._lay_close("ignored")
        for ep in list(self.open):
            self._step(ep)
            if ep.lvl == "fix":
                continue
            if not ep.typed:
                self._close(ep, "erased")
                continue
            ep.typed = ep.typed[:-1]
            if ep.div is not None and len(ep.typed) <= ep.div:
                ep.div = None  # опечатку стёр — снова печатаешь подсказку
        if self.fix:
            f = self.fix
            if not f.tail:
                f.low = -len(f.fix_text)
                self._fix_done(f, "edit")  # стёр всё принятое и дальше
            else:
                f.tail = f.tail[:-1]
                f.low = min(f.low, len(f.tail) - len(f.fix_text))

    def accept(self, text: str):
        """Tab / Ctrl+→: text вставлен в позицию курсора."""
        self._lay_close("ignored")
        ep = self.cur if self.cur in self.open else None
        if self.fix:
            self._fix_done(self.fix, None)
        for o in list(self.open):
            if o is not ep:
                self._close(o, "lost")
        if not ep:
            return
        self._step(ep)
        ep.typed += text
        ep.acc += len(text)
        if ep.typed.rstrip().startswith(ep.full.rstrip()):
            self._close(ep, "accepted", keep_for_fix=True)  # приняли до конца — ждём правок
            ep.fix_text = ep.tail = ep.typed
            ep.fix_t = self.clock()
            self.fix = ep

    def cycle(self):
        if self.cur:
            self.swap = True

    def dismiss(self):
        self._lay_close("ignored")
        if self.cur in self.open:
            self._close(self.cur, "dismissed")
        self.cur = None

    def reset(self, reason: str = ""):
        """Клик, стрелки, сочетания клавиш, смена окна: текст перед курсором уже не тот."""
        self._lay_close("ignored")
        for ep in list(self.open):
            self._close(ep, "lost")
        self.cur = None
        self.swap = False
        self.gaps.clear()
        self.last_key = 0.0
        if self.fix:
            f = self.fix
            depth = len(f.tail) - len(f.fix_text)
            quick = self.clock() - f.fix_t < UNDO_S and depth <= 2
            if reason == "ctrl-bs" and depth <= 2:
                self._fix_done(f, "ctrlbs")
            elif reason == "shortcut" and quick:
                self._fix_done(f, "undo")
            else:
                self._fix_done(f, None)

    # --- исправление раскладки ---
    def layout_shown(self, info: dict, app: str):
        """Плашка ⇄ на экране. Пока слово дописывают, это один показ: пересчитанная плашка его не дробит."""
        lay = self.lay
        if lay and all(lay["info"][k] == info[k] for k in ("dir", "how", "mode")):
            lay["info"], lay["stale"] = info, False
            return
        self._lay_close("ignored")
        self.lay = {"t": self.clock(), "app": app, "info": info, "stale": False}

    def layout_done(self, out: str, info: dict, app: str, **extra) -> dict:
        """Исправление состоялось (accepted: Tab, само или клавишей) или его отменили (undone / never)."""
        lay, self.lay = self.lay, None
        t0 = lay["t"] if lay and out == "accepted" and info["how"] in ("inword", "delim") else self.clock()
        return self._lay_row(out, info, app, t0, **extra)

    def layout_missed(self, app: str, dir_: str, len_: str):
        self._lay_row("missed", dict(dir=dir_, len=len_), app, self.clock())

    def _lay_row(self, out: str, info: dict, app: str, t0: float, **extra) -> dict:
        r = dict(t=int(t0), app=app, src="layout", out=out, **info,
                 **({"vis": int((self.clock() - t0) * 1000)} if out in ("accepted", "ignored") else {}), **extra)
        self.rows.append(r)
        return r

    def _lay_close(self, out: str):
        lay, self.lay = self.lay, None
        if lay:
            self._lay_row(out, lay["info"], lay["app"], lay["t"])

    def flush(self):
        if not self.rows or not self.path:
            self.rows.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_name(self.path.stem + ".1.jsonl"))
        except OSError:
            pass
        with self.path.open("a", encoding="utf-8") as f:
            for r in self.rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.rows.clear()

    # --- внутреннее ---
    def _key(self):
        now = self.clock()
        if self.last_key:
            g = (now - self.last_key) * 1000
            if g < 1500:
                self.gaps = (self.gaps + [g])[-6:]
        self.last_key = now
        if self.fix and now - self.fix.fix_t > FIX_TIMEOUT_S:
            self._fix_done(self.fix, None)

    def _step(self, ep: Episode):
        ep.keys += 1
        if ep.first is None:
            ep.first = int((self.clock() - ep.t0) * 1000)

    def _check(self, ep: Episode):
        full, typed = ep.full, ep.typed
        if ep.div is None:
            if typed.startswith(full.rstrip()) and full.strip():
                self._close(ep, "partial" if ep.acc else "typed")
            elif not full.startswith(typed):
                ep.div = _common(full, typed)
            else:
                return
        # разошлись: ждём, пока допишешь слово, чтобы понять — форма, другое слово или знак
        tail = typed[ep.div:]
        if len(tail) > DIVERGE_WAIT or (len(tail) > 1 and not WORD.match(tail[-1])) or not WORD.match(tail[:1] or " "):
            ep.miss = diff_kind(full, typed)
            self._close(ep, "partial" if ep.acc else "diverged")

    def _close(self, ep: Episode, out: str, keep_for_fix: bool = False):
        if ep in self.open:
            self.open.remove(ep)
        if ep.out:
            return
        if out == "lost" and ep.div is not None:
            ep.miss = diff_kind(ep.full, ep.typed)
            out = "partial" if ep.acc else "diverged"
        ep.out = out
        if self.cur is ep:
            self.cur = None
        if not keep_for_fix:
            self.rows.append(ep.record(self.clock()))

    def _fix_check(self, f: Episode, ch: str):
        depth = len(f.tail) - len(f.fix_text)
        if f.low == 0:
            if depth >= FIX_KEPT:
                self._fix_done(f, "kept")
            return
        if depth - f.low >= FIX_SETTLE and not WORD.match(ch):
            self._fix_done(f, None)

    def _fix_done(self, f: Episode, kind: str | None):
        """Чем закончилась принятая подсказка: оставил, стёр хвост, переписал."""
        if self.fix is f:
            self.fix = None
        r = f.record(self.clock())
        r["fix_del"] = -f.low
        if kind is None:
            if f.low == 0:
                kind = "kept"
            else:
                k = diff_kind(f.fix_text, f.tail)
                kind = "cut" if k == "short" else "edit"
                r["fix_kind"] = k
        r["fix"] = kind
        self.rows.append(r)


# --- отчёт ---
SRC_NAMES = {"ngram": "n-граммы", "llm": "LLM ✦", "long": "пауза ✦✦"}
POS_NAMES = {"word": "в слове", "space": "посреди предл.", "sent": "после точки", "line": "начало строки",
             "punct": "после знака"}
MISS_NAMES = {"form": "форма слова", "word": "другое слово", "punct": "знак", "case": "регистр", "short": "закончил раньше"}


def load(path: Path, days: int | None = None) -> list[dict]:
    since = time.time() - days * 86400 if days else 0
    rows = []
    for p in (path.with_name(path.stem + ".1.jsonl"), path):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t", 0) >= since:
                rows.append(r)
    return rows


def _split_layout(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    return [r for r in rows if r.get("src") != "layout"], [r for r in rows if r.get("src") == "layout"]


def layout_summary(lay: list[dict]) -> dict:
    """Агрегаты по строкам src == "layout": показано/принято в suggest, само, вручную, отмены, пропуски, переключение."""
    shown = [r for r in lay if r.get("how") in ("inword", "delim") and r["out"] in ("accepted", "ignored")]
    acc = [r for r in shown if r["out"] == "accepted"]
    auto = [r for r in lay if r.get("how") == "auto" and r["out"] == "accepted"]
    hand = [r for r in lay if r.get("how") == "hotkey" and r["out"] == "accepted"]
    undone = [r for r in lay if r["out"] == "undone"]
    undone_auto = [r for r in undone if r.get("how") == "auto"]
    sw = [r["switch_ok"] for r in lay if r.get("switch_ok") is not None]
    return dict(n=len(lay), shown=len(shown), accepted=len(acc), auto=len(auto), hotkey=len(hand),
                undone=len(undone), undone_auto=len(undone_auto),
                never=sum(r["out"] == "never" for r in lay), missed=sum(r["out"] == "missed" for r in lay),
                switch_n=len(sw), switch_ok=sum(bool(x) for x in sw),
                by_dir=dict(Counter(r["dir"] for r in lay if r["out"] == "accepted")),
                switch_fail_apps=dict(Counter(r["app"] or "?" for r in lay if r.get("switch_ok") is False)))


def layout_report(lay: list[dict]) -> str:
    a = layout_summary(lay)

    def pct(x, n):
        return f"{x / n:.0%}" if n else "—"

    out = ["\nРаскладка (EN ⇄ RU): "
           f"показано {a['shown']}, принято {a['accepted']} ({pct(a['accepted'], a['shown'])}); "
           f"само {a['auto']}, клавишей {a['hotkey']}; отменено {a['undone']} "
           f"({pct(a['undone_auto'], a['auto'])} автоисправлений), в исключения {a['never']}; "
           f"пропущено (оценка) {a['missed']}; переключение ок {pct(a['switch_ok'], a['switch_n'])}"]
    if a["switch_fail_apps"]:
        top = sorted(a["switch_fail_apps"].items(), key=lambda x: -x[1])[:5]
        out.append("  не переключилась: " + ", ".join(f"{k} ×{v}" for k, v in top))
    return "\n".join(out)


def report(rows: list[dict]) -> str:
    rows, lay = _split_layout(rows)
    if not rows and not lay:
        return "Журнал подсказок пока пуст (data/usage.jsonl)."
    if not rows:
        return layout_report(lay).lstrip("\n")
    out = [f"Подсказок в журнале: {len(rows)}"]
    head = (f"{'':16}{'показано':>9}{'принято':>9}{'по сл.':>7}{'сам':>6}{'мимо':>6}{'Esc':>5}"
            f"{'совп. до ухода':>15}{'сэкон.':>8}")
    for src in ("ngram", "llm", "long"):
        rs = [r for r in rows if r["src"] == src]
        if not rs:
            continue
        out.append(f"\n{SRC_NAMES[src]} — по месту в тексте")
        out.append(head)
        for pos in ("word", "space", "sent", "line", "punct", None):
            g = [r for r in rs if pos is None or r["pos"] == pos]
            if not g or (pos and len(g) < 3):
                continue
            out.append(_line("всего" if pos is None else POS_NAMES[pos], g))
    # не заметил: напечатал совпадающее руками, пока плашка была видна
    hand = [r for r in rows if r["out"] in ("typed", "diverged") and r["match"] >= 4]
    if hand:
        fast = sum(1 for r in hand if r.get("first", 9999) < 300 and 0 < r.get("gap", 0) < 250)
        out.append(f"\nНапечатал руками то, что было на плашке (≥4 совпавших символа): {len(hand)}; "
                   f"из них на быстром наборе (не успел заметить) {fast / len(hand):.0%}")
        by = Counter(SRC_NAMES[r["src"]] for r in hand)
        out.append("  по источнику: " + ", ".join(f"{k} {v}" for k, v in by.most_common()))
    late = [r for r in rows if r["out"] in ("accepted", "partial") and r["match"] - r["acc"] >= 4]
    if late:
        out.append(f"Принял не сразу (сначала ≥4 символа руками): {len(late)}, "
                   f"в среднем {sum(r['match'] - r['acc'] for r in late) / len(late):.0f} символов")
    miss = [r for r in rows if r.get("miss")]
    if miss:
        out.append("\nГде разошлись с подсказкой (по первому неверному слову):")
        for src in ("ngram", "llm", "long"):
            c = Counter(r["miss"] for r in miss if r["src"] == src)
            if c:
                tot = sum(c.values())
                out.append(f"  {SRC_NAMES[src]:10} " + ", ".join(f"{MISS_NAMES[k]} {v / tot:.0%}"
                                                                for k, v in c.most_common()))
    fixed = [r for r in rows if "fix" in r]
    if fixed:
        out.append("\nПосле принятия:")
        for src in ("ngram", "llm", "long"):
            g = [r for r in fixed if r["src"] == src]
            if not g:
                continue
            c = Counter(r["fix"] for r in g)
            k = Counter(r["fix_kind"] for r in g if r.get("fix_kind"))
            out.append(f"  {SRC_NAMES[src]:10} " + ", ".join(f"{FIX_NAMES[x]} {v / len(g):.0%}" for x, v in c.most_common())
                       + ("  · что правил: " + ", ".join(f"{MISS_NAMES[x]} {v}" for x, v in k.most_common()) if k else ""))
    apps = defaultdict(lambda: Counter())
    for r in rows:
        apps[r["app"]][r["src"]] += 1
        apps[r["app"]]["acc_" + r["src"]] += r["out"] in ("accepted", "partial")
    top = sorted(apps.items(), key=lambda x: -sum(v for k, v in x[1].items() if not k.startswith("acc_")))[:6]
    out.append("\nПо программам (показано / принято):")
    for app, c in top:
        out.append(f"  {app or '?':22} " + "  ".join(f"{SRC_NAMES[s]} {c[s]}/{c['acc_' + s]}"
                                                    for s in ("ngram", "llm", "long") if c[s]))
    if lay:
        out.append(layout_report(lay))
    return "\n".join(out)


FIX_NAMES = {"kept": "оставил", "cut": "стёр хвост", "edit": "переписал", "ctrlbs": "Ctrl+Backspace", "undo": "отменил"}


def _line(title: str, g: list[dict]) -> str:
    n = len(g)
    c = Counter(r["out"] for r in g)
    left = [r for r in g if r["out"] in ("typed", "diverged", "dismissed", "lost")]
    match = sum(r["match"] for r in left) / len(left) if left else 0
    saved = sum(max(0, r["acc"] - 1) for r in g)
    return (f"  {title:14}{n:9}{c['accepted'] / n:9.0%}{c['partial'] / n:7.0%}{c['typed'] / n:6.0%}"
            f"{c['diverged'] / n:6.0%}{c['dismissed'] / n:5.0%}{match:12.1f} зн{saved:8}")


# --- сводка словарём для веб-панели: те же срезы, что в report ---
OUTCOMES = ("accepted", "partial", "typed", "diverged", "dismissed", "lost")  # erased считаем как lost


def _group(g: list[dict]) -> dict:
    n = len(g)
    c = Counter("lost" if r["out"] == "erased" else r["out"] for r in g)
    left = [r for r in g if r["out"] in ("typed", "diverged", "dismissed", "lost")]
    return dict(n=n, **{o: c[o] for o in OUTCOMES},
                saved=sum(max(0, r["acc"] - 1) for r in g),
                match=round(sum(r["match"] for r in left) / len(left), 1) if left else 0.0)


def summary(rows: list[dict]) -> dict:
    """Агрегаты журнала: источник × место, программы, «напечатал сам», расхождения, правки после принятия."""
    rows, lay = _split_layout(rows)
    srcs = []
    for src in ("ngram", "llm", "long"):
        rs = [r for r in rows if r["src"] == src]
        if not rs:
            continue
        pos = [dict(pos=p, name=POS_NAMES[p], **_group(g))
               for p in ("word", "space", "sent", "line", "punct")
               if len(g := [r for r in rs if r["pos"] == p]) >= 3]
        srcs.append(dict(src=src, name=SRC_NAMES[src], all=_group(rs), pos=pos))
    hand = [r for r in rows if r["out"] in ("typed", "diverged") and r["match"] >= 4]
    fast = sum(1 for r in hand if r.get("first", 9999) < 300 and 0 < r.get("gap", 0) < 250)
    late = [r for r in rows if r["out"] in ("accepted", "partial") and r["match"] - r["acc"] >= 4]
    miss = {}
    for src in ("ngram", "llm", "long"):
        c = Counter(r["miss"] for r in rows if r["src"] == src and r.get("miss"))
        tot = sum(c.values())
        if tot:
            miss[src] = [dict(kind=k, name=MISS_NAMES.get(k, k), n=v, share=v / tot) for k, v in c.most_common()]
    fix = {}
    for src in ("ngram", "llm", "long"):
        g = [r for r in rows if r["src"] == src and "fix" in r]
        if g:
            c = Counter(r["fix"] for r in g)
            k = Counter(r["fix_kind"] for r in g if r.get("fix_kind"))
            fix[src] = dict(n=len(g),
                            kinds=[dict(kind=x, name=FIX_NAMES.get(x, x), share=v / len(g)) for x, v in c.most_common()],
                            what=[dict(kind=x, name=MISS_NAMES.get(x, x), n=v) for x, v in k.most_common()])
    apps: dict[str, dict] = {}
    for r in rows:
        a = apps.setdefault(r["app"] or "?", dict(app=r["app"] or "?", n=0, accepted=0, by_src={}))
        s = a["by_src"].setdefault(r["src"], dict(n=0, acc=0))
        ok = r["out"] in ("accepted", "partial")
        a["n"] += 1
        a["accepted"] += ok
        s["n"] += 1
        s["acc"] += ok
    return dict(total=len(rows), sources=srcs, layout=layout_summary(lay),
                hand=dict(n=len(hand), fast=fast, fast_share=fast / len(hand) if hand else 0.0,
                          by_src=dict(Counter(r["src"] for r in hand))),
                late=dict(n=len(late), avg_chars=round(sum(r["match"] - r["acc"] for r in late) / len(late), 1) if late else 0.0),
                miss=miss, fix=fix, apps=sorted(apps.values(), key=lambda a: -a["n"])[:15])
