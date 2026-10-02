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
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from .model import WORD_CH

WORD = re.compile(rf"[{WORD_CH}\-'’]")
SENT_END = ".!?…"
MAX_OPEN = 4            # сколько подсказок отслеживать одновременно (n-граммы меняются на каждом нажатии)
DIVERGE_WAIT = 30       # сколько символов ждать конца слова, в котором разошлись
FIX_SETTLE = 12         # столько символов после самой глубокой точки правки — правка закончена
FIX_KEPT = 20           # столько символов дальше без стирания — принятое оставлено как есть
FIX_TIMEOUT_S = 30
UNDO_S = 5
MAX_BYTES = 20_000_000  # дальше usage.jsonl → usage.1.jsonl


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


class Episode:
    def __init__(self, full: str, src: str, pos: str, app: str, now: float, gap: int, idle: int, variants: int):
        self.full = full            # подсказка целиком от точки показа
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
        return r


class Usage:
    def __init__(self, path: Path | None, clock=time.time):
        self.path = path
        self.clock = clock
        self.open: list[Episode] = []
        self.cur: Episode | None = None     # та, что сейчас на плашке
        self.fix: Episode | None = None     # принятая, ждём правок
        self.rows: list[dict] = []
        self.last_key = 0.0
        self.gaps: list[float] = []
        self.swap = False

    # --- события от приложения ---
    def shown(self, buf: str, insert: str, level: str, app: str, variants: int = 0):
        now = self.clock()
        src = {"llm": "llm", "long": "long"}.get(level, "ngram")
        for ep in self.open:
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
        ep = Episode(insert, src, place(buf), app, now, gap, idle, variants)
        self.open.append(ep)
        self.cur = ep
        if len(self.open) > MAX_OPEN:
            self._close(self.open[0], "lost")

    def char(self, ch: str):
        self._key()
        for ep in list(self.open):
            self._step(ep)
            ep.typed += ch
            self._check(ep)
        if self.fix:
            self.fix.tail += ch
            self._fix_check(self.fix, ch)

    def bs(self):
        self._key()
        for ep in list(self.open):
            self._step(ep)
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
        if self.cur in self.open:
            self._close(self.cur, "dismissed")
        self.cur = None

    def reset(self, reason: str = ""):
        """Клик, стрелки, сочетания клавиш, смена окна: текст перед курсором уже не тот."""
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


def report(rows: list[dict]) -> str:
    if not rows:
        return "Журнал подсказок пока пуст (data/usage.jsonl)."
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
    return dict(total=len(rows), sources=srcs,
                hand=dict(n=len(hand), fast=fast, fast_share=fast / len(hand) if hand else 0.0,
                          by_src=dict(Counter(r["src"] for r in hand))),
                late=dict(n=len(late), avg_chars=round(sum(r["match"] - r["acc"] for r in late) / len(late), 1) if late else 0.0),
                miss=miss, fix=fix, apps=sorted(apps.values(), key=lambda a: -a["n"])[:15])
