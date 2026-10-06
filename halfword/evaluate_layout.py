"""Офлайн-стенд детектора раскладки: искажаем чистые сообщения, «печатаем» их и считаем метрики.

python -X utf8 -m halfword.evaluate_layout [--set public|notes|tg|all] [--detector v2|baseline]
                                           [--mode suggest|auto] [--limit N] [--seed 1] [--grid] [--json]

Метрики и цели — Документация/Спецификации/Раскладка_v2.md (§ Метрики успеха → Офлайн).
Печатаются только агрегаты: ни одной строки текста (личные наборы — заметки и Telegram).

Направление «en→ru» — набрано в английской раскладке то, что надо по-русски (исправление ведёт в кириллицу);
«ru→en» — наоборот. Короткие слова считаются по буквам правильного слова: 1–2, 3–4, 5+.

Модель набора (одно искажение на вариант): слова искажённого отрезка набираются в чужой раскладке, пока детектор
не исправил (suggest — исправление принято, как по Tab; auto — только fix.auto). После исправления раскладка
считается переключённой: остальные слова отрезка набраны уже правильно и в знаменатель не входят.
Детектор v2 спрашиваем сразу после пробела за каждым словом; baseline (Predictor._layout) — перед пробелом.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import re
import sys
import time
import zlib
from collections import Counter, defaultdict, namedtuple
from dataclasses import dataclass, replace
from pathlib import Path

from . import config as C
from .layout import URLISH_RE, KeyMap, LayoutDetector, Lexicon, Params

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "layout"
PUBLIC_FILES = (("clean_ru.txt", "clean"), ("clean_en.txt", "clean"), ("hard_ru.txt", "hard"), ("hard_en.txt", "hard"))
HOLDOUT = 10          # 1 из 10 по crc32 — отложенная часть
PERSONAL_LIMIT = 2000  # сообщений личного набора по умолчанию
BUCKETS = ("1-2", "3-4", "5+")
DIRS = ("en→ru", "ru→en")
KM = KeyMap()
Fix = namedtuple("Fix", "erase insert auto")

# слово: скобки/кавычки спереди, ядро-буквы одного алфавита, знаки препинания сзади (цифр, «_», путей, флагов нет)
TOKEN_RE = re.compile(r"""([(\[{«"'“]*)([A-Za-z]+(?:['-][A-Za-z]+)*|[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*)([.,;:!?)\]}»"'”…]*)""")
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:['-][A-Za-zА-Яа-яЁё]+)*")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bucket(letters: int) -> str:
    return "1-2" if letters <= 2 else "3-4" if letters <= 4 else "5+"


# --- искажения -------------------------------------------------------------------------------------------------
@dataclass
class T:
    correct: str        # слово как задумано (с пунктуацией и регистром)
    typed: str          # как набрано в этом варианте (искажено или как есть)
    dist: bool          # искажено в этом варианте
    script: str | None  # cyr / lat; None — не искажаем (цифры, URL, смешанное, без букв)
    letters: int

    @property
    def dir(self) -> str:
        return "en→ru" if self.script == "cyr" else "ru→en"


@dataclass
class Variant:
    kind: str           # clean / word / seg / all / head
    group: str          # clean / hard / personal
    fold: int           # crc32 % 10 — для baseline с кросс-валидацией
    toks: list
    nspan: int = 0      # сколько слов искажено


def classify(tok: str) -> tuple[str | None, int]:
    m = TOKEN_RE.fullmatch(tok)
    if not m or URLISH_RE.search(tok):
        return None, 0
    core = m.group(2)
    if any(c.isupper() for c in core[1:]) and not core.isupper():
        return None, 0  # iPhone, GitHub: имя, детектор их намеренно не трогает
    return ("cyr" if "А" <= core[0].upper() <= "Я" or core[0] in "Ёё" else "lat"), sum(c.isalpha() for c in core)


def make_variants(msg: str, group: str, seed: int) -> list[Variant]:
    rng = random.Random(f"{seed}:{msg}")
    words = msg.split()
    base = [T(w, w, False, *classify(w)) for w in words]
    fold = zlib.crc32(msg.encode("utf-8")) % HOLDOUT
    out = [Variant("clean", group, fold, base)]
    cand = [i for i, t in enumerate(base) if t.script]
    if not cand:
        return out

    def distorted(idx: set[int], kind: str) -> Variant:
        toks = [replace(t, typed=KM.convert(t.correct, t.script == "lat"), dist=True) if i in idx else t
                for i, t in enumerate(base)]
        return Variant(kind, group, fold, toks, len(idx))

    for i in rng.sample(cand, min(2, len(cand))):                          # (а) одно слово
        out.append(distorted({i}, "word"))
    runs, cur = [], [cand[0]]                                              # (б) отрезок 2–5 слов одного алфавита
    for i in cand[1:]:
        if i == cur[-1] + 1 and base[i].script == base[cur[-1]].script:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    long_runs = [r for r in runs if len(r) >= 2]
    if long_runs:
        r = rng.choice(long_runs)
        size = rng.randint(2, min(5, len(r)))
        s = rng.randint(0, len(r) - size)
        out.append(distorted(set(r[s:s + size]), "seg"))
    ncyr = sum(base[i].script == "cyr" for i in cand)
    dom = "cyr" if ncyr * 2 >= len(cand) else "lat"
    dom_idx = [i for i in cand if base[i].script == dom]
    if len(dom_idx) >= 2:                                                  # (в) всё сообщение
        out.append(distorted(set(dom_idx), "all"))
    if len(words) >= 3:                                                    # (г) начало до случайного места
        k = rng.randint(2, len(words) - 1)
        head = {i for i in dom_idx if i < k}
        if len(head) >= 2:
            out.append(distorted(head, "head"))
    return out


# --- симуляция набора --------------------------------------------------------------------------------------------
class V2Engine:
    delim = True        # решаем сразу после пробела
    has_auto = True

    def __init__(self, det: LayoutDetector):
        self.det = det

    def fix(self, text: str, fold: int) -> Fix | None:
        f = self.det.detect(text)
        return Fix(f.erase, f.insert, f.auto) if f else None


class BaselineEngine:
    delim = False       # старый _layout работает посреди слова: спрашиваем на последнем символе слова
    has_auto = False

    def __init__(self, pick):
        self.pick = pick    # fold → Predictor

    def fix(self, text: str, fold: int) -> Fix | None:
        s = self.pick(fold).suggest(text)
        return Fix(s.replace, s.insert, False) if s is not None and s.level == "layout" else None


class Stats:
    def __init__(self):
        self.rec = defaultdict(lambda: [0, 0, 0])   # (dir, bucket) → всего, покрыто, верно
        self.short_chain = [0, 0]                   # слова 1–2 буквы в отрезках ≥2: всего, верно
        self.chain = [0, 0]                         # отрезки ≥2: всего, исправлены полностью
        self.fp = Counter()                         # группа → ложных исправлений на чистых сообщениях
        self.words = Counter()                      # группа → слов в чистых сообщениях
        self.fp_dist = 0                            # ложные в искажённых сообщениях (мимо искажённых слов)
        self.right_dist = 0                         # верно набранных слов в искажённых сообщениях
        self.collateral = 0                         # исправления, задевшие верно набранные слова
        self.times: list[float] = []                # мс на вызов
        self.variants = Counter()
        self.kind_rec = defaultdict(lambda: [0, 0])  # вид искажения → слова ≥3 букв: всего, верно
        self.on_miss = None                         # отладка: callback(вариант, индекс слова, исход) для не-«ok»


def simulate(v: Variant, engine, mode: str, st: Stats):
    ch, want, tid, cnt = [], [], [], Counter()
    n = len(v.toks)
    typed_wrong, wrong_now, res = [False] * n, [False] * n, [None] * n
    switched, suppress = False, 0
    for i, t in enumerate(v.toks):
        wrong = t.dist and not switched
        typed = t.typed if wrong else t.correct
        typed_wrong[i] = wrong_now[i] = wrong
        ch += typed
        want += t.correct
        tid += [i] * len(typed)
        cnt[i] = len(typed)
        if engine.delim:
            ch.append(" ")
            want.append(" ")
            tid.append(-1)
        t0 = time.perf_counter()
        fix = engine.fix("".join(ch), v.fold)
        st.times.append((time.perf_counter() - t0) * 1000)
        if fix is not None and fix.erase > 0 and not (mode == "auto" and engine.has_auto and not fix.auto):
            a = len(ch) - fix.erase
            if a >= max(suppress, 0):
                cov = {tid[j] for j in range(a, len(ch))} - {-1}
                if not any(wrong_now[x] for x in cov):
                    # исправление трогает только верно набранное — ложное; откатываем и не считаем повторно
                    if v.kind == "clean":
                        st.fp[v.group] += 1
                    else:
                        st.fp_dist += 1
                    suppress = len(ch)
                else:
                    old = ch[a:]
                    new = list(fix.insert)
                    same_len = len(new) == len(old)
                    exact = new == want[a:]
                    ok_pos = [new[k] == want[a + k] for k in range(len(new))] if same_len else None
                    spoiled = False
                    for x in cov:
                        pos = [j for j in range(a, len(tid)) if tid[j] == x]
                        full = len(pos) == cnt[x]
                        if wrong_now[x]:
                            good = full and (all(ok_pos[j - a] for j in pos) if same_len else exact)
                            res[x] = "ok" if good else "bad"
                            wrong_now[x] = False
                        elif (not same_len) or any(new[j - a] != old[j - a] for j in pos):
                            spoiled = True
                    if spoiled:
                        st.collateral += 1
                    if same_len:
                        ch[a:] = new
                    else:
                        first = min(cov)
                        ch[a:], want[a:], tid[a:] = new, new[:], [first] * len(new)
                    switched = True
        if not engine.delim:
            ch.append(" ")
            want.append(" ")
            tid.append(-1)
    # итоги варианта
    st.variants[v.kind] += 1
    if v.kind == "clean":
        st.words[v.group] += sum(any(c.isalpha() for c in t.correct) for t in v.toks)
        return
    st.right_dist += sum(any(c.isalpha() for c in t.correct) for i, t in enumerate(v.toks) if not typed_wrong[i])
    all_ok = True
    for i, t in enumerate(v.toks):
        if not typed_wrong[i]:
            continue
        r = st.rec[(t.dir, bucket(t.letters))]
        r[0] += 1
        r[1] += res[i] is not None
        r[2] += res[i] == "ok"
        if st.on_miss and res[i] != "ok":
            st.on_miss(v, i, res[i])
        if t.letters >= 3:
            st.kind_rec[v.kind][0] += 1
            st.kind_rec[v.kind][1] += res[i] == "ok"
        if v.nspan >= 2 and t.letters <= 2:
            st.short_chain[0] += 1
            st.short_chain[1] += res[i] == "ok"
        all_ok &= res[i] == "ok"
    if v.nspan >= 2:
        st.chain[0] += 1
        st.chain[1] += all_ok


def run(variants: list[Variant], engine, mode: str) -> Stats:
    st = Stats()
    for v in variants:
        simulate(v, engine, mode, st)
    return st


# --- отчёт -------------------------------------------------------------------------------------------------------
def ratio(a: int, b: int):
    return a / b if b else None


def p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.95 * len(xs)))]


def report(st: Stats, meta: dict) -> dict:
    def cell(dirs, buckets):
        tot = sum(st.rec[(d, b)][0] for d in dirs for b in buckets)
        cov = sum(st.rec[(d, b)][1] for d in dirs for b in buckets)
        ok = sum(st.rec[(d, b)][2] for d in dirs for b in buckets)
        return {"n": tot, "recall": ratio(ok, tot), "covered": cov, "ok": ok}

    rec = {d: {b: cell([d], [b]) for b in BUCKETS} | {"all": cell([d], BUCKETS)} for d in DIRS}
    rec["all"] = {b: cell(DIRS, [b]) for b in BUCKETS} | {"all": cell(DIRS, BUCKETS)}
    ge3 = cell(DIRS, ("3-4", "5+"))
    allc = cell(DIRS, BUCKETS)
    fp = {g: {"fires": st.fp[g], "words": st.words[g], "per_10k": ratio(st.fp[g] * 10_000, st.words[g])}
          for g in sorted(st.words)}
    return {
        **meta,
        "variants": dict(st.variants),
        "typed_wrong_words": allc["n"],
        "recall_by_len_dir": rec,
        "recall_ge3": ge3["recall"],
        "recall_short_in_chain": {"n": st.short_chain[0], "recall": ratio(st.short_chain[1], st.short_chain[0])},
        "recall_chain": {"n": st.chain[0], "recall": ratio(st.chain[1], st.chain[0])},
        "false_per_10k": fp,
        "correct_translation": {"covered": allc["covered"], "recall": ratio(allc["ok"], allc["covered"])},
        "false_in_distorted": {"fires": st.fp_dist, "words": st.right_dist,
                               "per_10k": ratio(st.fp_dist * 10_000, st.right_dist)},
        "collateral_fixes": st.collateral,
        "recall_ge3_by_kind": {k: ratio(v[1], v[0]) for k, v in sorted(st.kind_rec.items())},
        "detect_p95_ms": p95(st.times),
        "detect_calls": len(st.times),
    }


def pc(x, d=1) -> str:
    return "  –  " if x is None else f"{100 * x:.{d}f}%"


def num(x) -> str:
    return "–" if x is None else f"{x:.1f}"


def print_report(r: dict):
    auto = r["mode"] == "auto"
    print(f"\n=== {r['set']} | {r['detector']} | {r['mode'] if r['detector'] == 'v2' else 'suggest (у baseline нет auto)'}"
          f" | seed {r['seed']} ===")
    wd = " ".join(f"{g} {v['words']}" for g, v in r["false_per_10k"].items())
    vs = " ".join(f"{k} {v}" for k, v in r["variants"].items())
    print(f"варианты: {vs} | слов в чистых: {wd} | набрано неверно: {r['typed_wrong_words']}")
    print(f"{'recall по длине (букв)':24}{'1-2':>16}{'3-4':>16}{'5+':>16}{'все':>16}")
    for d in (*DIRS, "all"):
        row = r["recall_by_len_dir"][d]
        cells = "".join(f"{pc(row[b]['recall']):>9} ({row[b]['n']:>4})" for b in (*BUCKETS, "all"))
        print(f"{'  ' + (d if d != 'all' else 'оба направления'):24}{cells}")
    tg = ("≥95%", "≥80%", "≥90%", "≤5 / ≤10", "≥99%") if not auto else ("≥95%", "≥80%", "≥90%", "≤1 / ≤2", "≥99.5%")
    fp = r["false_per_10k"]
    fps = " ".join(f"{g} {num(v['per_10k'])} ({v['fires']}/{v['words']})" for g, v in fp.items())
    print(f"recall ≥3 букв            {pc(r['recall_ge3']):>7}   цель {tg[0]}")
    print(f"recall 1–2 в цепочке ≥2   {pc(r['recall_short_in_chain']['recall']):>7}   цель {tg[1]}   (n={r['recall_short_in_chain']['n']})")
    print(f"recall цепочки целиком    {pc(r['recall_chain']['recall']):>7}   цель {tg[2]}   (n={r['recall_chain']['n']})")
    print(f"ложные на 10k слов        {fps}   цель {tg[3]}")
    print(f"верный перевод            {pc(r['correct_translation']['recall'], 2):>7}   цель {tg[4]}   (найдено {r['correct_translation']['covered']})")
    fd = r["false_in_distorted"]
    print(f"в искажённых: ложных/10k  {num(fd['per_10k'])} ({fd['fires']}/{fd['words']}); исправлений, задевших верные слова: {r['collateral_fixes']}")
    print(f"detect p95                {r['detect_p95_ms']:.2f} мс ({r['detect_calls']} вызовов)   цель ≤2 мс")
    kinds = " ".join(f"{k} {pc(v)}" for k, v in r["recall_ge3_by_kind"].items())
    print(f"recall ≥3 по видам        {kinds}")


GRID = {"wrong_margin": (1.2, 1.6, 2.5), "suggest_total": (1.2, 1.6, 3.0), "auto_total": (2.0, 2.5, 4.0),
        "unk_mid": (-3.9, -3.6, -3.3), "conv_min": (0.4, 0.8, 1.2), "short_margin": (0.6, 1.0, 1.4)}


def grid_row(label: str, r: dict) -> str:
    fp = r["false_per_10k"]
    fps = "/".join(num(fp[g]["per_10k"]) for g in fp)
    return (f"{label:34} R≥3 {pc(r['recall_ge3']):>6} | R1-2цеп {pc(r['recall_short_in_chain']['recall']):>6} | "
            f"Rцеп {pc(r['recall_chain']['recall']):>6} | ложн/10k ({'/'.join(fp)}) {fps:>10} | "
            f"верн {pc(r['correct_translation']['recall'], 2):>7} | p95 {r['detect_p95_ms']:.2f}")


# --- наборы ------------------------------------------------------------------------------------------------------
def split_messages(text: str, max_tokens: int = 30) -> list[str]:
    """Личный текст → «сообщения»: строки, длинные режем по концам предложений."""
    out = []
    for line in text.splitlines():
        cur: list[str] = []
        for tok in line.split():
            cur.append(tok)
            if len(cur) >= max_tokens or (len(tok) > 2 and tok[-1] in ".!?…"):
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
    return [" ".join(c) for c in out if any(ch.isalpha() for t in c for ch in t)]


def known_counter(texts) -> Counter:
    c: Counter = Counter()
    for t in texts:
        c.update(w.lower() for w in WORD_RE.findall(t))
    return c


def sample(items: list, limit: int, seed: int) -> list:
    if limit and len(items) > limit:
        idx = sorted(random.Random(seed).sample(range(len(items)), limit))
        return [items[i] for i in idx]
    return items


@dataclass
class DataSet:
    name: str
    items: list         # (сообщение, группа)
    train: list         # обучающие тексты baseline (для public — на каждый fold своё: dict fold → list)
    known: Counter
    per_fold: bool = False


def load_public() -> DataSet:
    items = []
    for fn, group in PUBLIC_FILES:
        for line in (FIXTURES / fn).read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append((line.strip(), group))
    return DataSet("public", items, [m for m, _g in items], Counter(), per_fold=True)


def load_notes(limit: int, seed: int) -> DataSet | None:
    files = [f for _root, f in text_files_safe()]
    if not files:
        return None
    test, train = [], []
    for f in files:
        try:
            text = read_text_file_safe(f)
        except OSError:
            continue
        if zlib.crc32(str(f).encode("utf-8")) % HOLDOUT == 0:
            test += split_messages(text)
        else:
            train.append(text)
    if not test:
        return None
    return DataSet("notes", [(m, "personal") for m in sample(test, limit, seed)], train, known_counter(train))


def load_tg(limit: int, seed: int) -> DataSet | None:
    from .corpus import telegram_messages
    try:
        msgs = [t for _when, t in telegram_messages(C.TELEGRAM_DIR)]
    except (OSError, ValueError):
        return None
    if not msgs:
        return None
    test, train = [], []
    for t in msgs:
        (test if zlib.crc32(t.encode("utf-8")) % HOLDOUT == 0 else train).append(t)
    chunks = [m for t in test for m in split_messages(t)]
    if not chunks:
        return None
    return DataSet("tg", [(m, "personal") for m in sample(chunks, limit, seed)], train, known_counter(train))


def text_files_safe():
    from .corpus import text_files
    return text_files(C.text_roots(C.Config.load()))


def read_text_file_safe(f):
    from .corpus import read_text_file
    return read_text_file(f)


def baseline_engine(ds: DataSet) -> BaselineEngine:
    from .model import BaseModel, Predictor, UserModel

    def make(texts) -> Predictor:
        p = Predictor(BaseModel.train(texts), UserModel())
        p.layout_fix = True
        return p

    t0 = time.time()
    if ds.per_fold:  # публичный набор мал: 10 моделей, каждая не видела своей отложенной десятой части
        by_fold = defaultdict(list)
        for m in ds.train:
            by_fold[zlib.crc32(m.encode("utf-8")) % HOLDOUT].append(m)
        preds = {f: make([m for g, ms in by_fold.items() if g != f for m in ms]) for f in range(HOLDOUT)}
        pick = preds.__getitem__
    else:
        p = make(ds.train)
        pick = lambda _f: p  # noqa: E731
    log(f"baseline: обучение {time.time() - t0:.0f} с")
    return BaselineEngine(pick)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set", default="public", choices=("public", "notes", "tg", "all"))
    ap.add_argument("--detector", default="v2", choices=("v2", "baseline"))
    ap.add_argument("--mode", default="suggest", choices=("suggest", "auto"))
    ap.add_argument("--limit", type=int, default=None, help="сообщений в наборе (public — все, личные — 2000; 0 — без предела)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--grid", action="store_true", help="v2: перебор ключевых порогов Params")
    ap.add_argument("--grid-full", action="store_true", help="с --grid: полное произведение значений (много прогонов)")
    ap.add_argument("--json", action="store_true", help="машинный вывод: JSON в stdout")
    args = ap.parse_args(argv)

    names = ["public", "notes", "tg"] if args.set == "all" else [args.set]
    ru = en = None
    if args.detector == "v2":
        t0 = time.time()
        ru, en = Lexicon.load("ru"), Lexicon.load("en")
        log(f"словари: {time.time() - t0:.1f} с")
    out = []
    for name in names:
        limit = args.limit if args.limit is not None else (0 if name == "public" else PERSONAL_LIMIT)
        if name == "public":
            ds = load_public()
            ds.items = sample(ds.items, limit, args.seed)
        else:
            ds = (load_notes if name == "notes" else load_tg)(limit, args.seed)
        if ds is None:
            msg = f"{name}: нет данных"
            print(msg, file=sys.stderr if args.json else sys.stdout)
            out.append({"set": name, "error": "нет данных"})
            continue
        variants = [v for m, g in ds.items for v in make_variants(m, g, args.seed)]
        log(f"{name}: сообщений {len(ds.items)}, вариантов {len(variants)}")
        meta = {"set": name, "detector": args.detector, "mode": args.mode, "seed": args.seed, "messages": len(ds.items)}
        if args.detector == "baseline":
            r = report(run(variants, baseline_engine(ds), "suggest"), meta | {"mode": "suggest"})
            out.append(r)
            if not args.json:
                print_report(r)
            continue
        known = ds.known
        det = LayoutDetector(ru, en, known=lambda w, k=known: k.get(w, 0)) if known else LayoutDetector(ru, en)
        eng = V2Engine(det)
        if not args.grid:
            r = report(run(variants, eng, args.mode), meta)
            out.append(r)
            if not args.json:
                print_report(r)
            continue
        base = Params()
        combos = [("по умолчанию", {})]
        if args.grid_full:
            for vals in itertools.product(*GRID.values()):
                combos.append((" ".join(f"{k}={v}" for k, v in zip(GRID, vals)), dict(zip(GRID, vals))))
        else:
            for k, vals in GRID.items():
                combos += [(f"{k}={v}", {k: v}) for v in vals if v != getattr(base, k)]
        if not args.json:
            print(f"\n=== grid {name} | v2 | {args.mode} | seed {args.seed} | сообщений {len(ds.items)} ===")
        for label, over in combos:
            det.p = replace(base, **over)
            det._cache.clear()
            r = report(run(variants, eng, args.mode), meta | {"params": over})
            out.append(r)
            if not args.json:
                print(grid_row(label, r), flush=True)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
