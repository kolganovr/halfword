"""Личная n-граммная модель (1–3 граммы, stupid backoff) с дообучением на лету.

Базовая модель обучается один раз на корпусе и замораживается (pickle).
Пользовательская модель — маленькие счётчики того, что набрано руками; при подсказке
её счёты складываются с базовыми с весом USER_WEIGHT.
"""
from __future__ import annotations

import bisect
import os
import json
import pickle
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Iterable

WORD_CH = r"0-9A-Za-zÀ-ÿА-Яа-яЁё"
WORD_RE = re.compile(rf"[{WORD_CH}]+(?:[-'’][{WORD_CH}]+)*")
TRAILING_WORD_RE = re.compile(rf"[{WORD_CH}]+(?:[-'’][{WORD_CH}]*)*$")
SENT_SPLIT_RE = re.compile(r"[.!?…\n;:]+")
BOS = "<s>"
NUM = "<num>"

BI_TOP = 100      # сколько продолжений хранить на контекст биграммы
TRI_TOP = 50
TRI_MIN = 2       # триграммы с меньшим счётом выкидываем
MAX_WORD_LEN = 30
USER_WEIGHT = 3   # во сколько раз набранное руками весомее корпуса
BACKOFF = 0.4
DOC_WEIGHT = 3    # слово/пара из текущего поля весит столько вхождений корпуса за раз
USER_HALF_LIFE_DAYS = 365  # затухание набранного руками

# не та раскладка: клавиши ЙЦУКЕН ↔ QWERTY
_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,.~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>"
_RU = "ёйцукенгшщзхъфывапролджэячсмитьбюЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ"
EN2RU = str.maketrans(_EN, _RU)
RU2EN = str.maketrans(_RU, _EN)
LATIN_TAIL_RE = re.compile(r"[A-Za-z`\[\];',.~{}:\"<>]+$")
CYR_TAIL_RE = re.compile(r"[А-Яа-яЁё]+$")


def norm(tok: str) -> str:
    return NUM if tok.isdigit() else tok.lower()


def sentences(text: str) -> Iterable[list[str]]:
    for sent in SENT_SPLIT_RE.split(text):
        toks = [t for t in WORD_RE.findall(sent) if len(t) <= MAX_WORD_LEN]
        if toks:
            yield toks


def match_case(word: str, prefix: str, capitalize: bool) -> str:
    if prefix and len(prefix) >= 2 and prefix.isupper() and word.lower() == word:
        return word.upper()
    if (prefix[:1].isupper() or (capitalize and not prefix)) and word[:1].islower():
        return word[:1].upper() + word[1:]
    return word


@dataclass
class Suggestion:
    insert: str        # что допечатать после курсора
    word: str          # дописанное слово целиком
    confidence: float
    level: str         # tri / uni / stem / layout / snippet
    whole_word: bool = True  # после принятия ставить пробел
    replace: int = 0   # стереть столько символов перед курсором, потом вставить insert (раскладка, сниппет)


class BaseModel:
    """Замороженная модель на корпусе."""

    def __init__(self):
        self.uni: dict[str, int] = {}
        self.bi: dict[str, tuple[int, list[tuple[str, int]]]] = {}
        self.tri: dict[str, tuple[int, list[tuple[str, int]]]] = {}
        self.case: dict[str, str] = {}
        self.vocab: list[str] = []
        self.cum: list[int] = []
        self.tokens = 0

    # --- обучение ---
    @classmethod
    def train(cls, texts: Iterable[str | tuple[str, float]], progress=None) -> "BaseModel":
        """texts — строки (вес 1) или (текст, вес): счётчики становятся «эффективным числом вхождений».

        progress(stage, n) — необязательный отчёт: ("count", текстов прочитано) каждые 2000 текстов,
        ("prune", None) перед отсевом редких триграмм.
        """
        uni: Counter = Counter()
        bi: dict[str, Counter] = defaultdict(Counter)
        tri: dict[str, Counter] = defaultdict(Counter)
        surface: dict[str, Counter] = defaultdict(Counter)
        for n, item in enumerate(texts, 1):
            if progress and n % 2000 == 0:
                progress("count", n)
            text, wt = (item, 1) if isinstance(item, str) else item
            for toks in sentences(text):
                for i, t in enumerate(toks):
                    if i > 0 and not t.isdigit():
                        surface[t.lower()][t] += wt
                seq = [BOS, BOS] + [norm(t) for t in toks]
                for i in range(2, len(seq)):
                    w = seq[i]
                    uni[w] += wt
                    bi[seq[i - 1]][w] += wt
                    tri[seq[i - 2] + " " + seq[i - 1]][w] += wt
        if progress:
            progress("prune", None)
        m = cls()
        m.uni = dict(uni)
        m.tokens = sum(uni.values())
        m.bi = {k: (sum(c.values()), c.most_common(BI_TOP)) for k, c in bi.items()}
        m.tri = {}
        for k, c in tri.items():
            top = [(w, n) for w, n in c.most_common(TRI_TOP) if n >= TRI_MIN]
            if top:
                m.tri[k] = (sum(c.values()), top)
        for low, forms in surface.items():
            best, _ = forms.most_common(1)[0]
            if best != low:
                m.case[low] = best
        m._index()
        return m

    def _index(self):
        self.vocab = sorted(w for w in self.uni if not w.startswith("<"))
        self.cum = [0] + list(accumulate(self.uni[w] for w in self.vocab))

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")  # обрыв посреди записи не должен оставить битый файл
        with open(tmp, "wb") as f:
            pickle.dump({"uni": self.uni, "bi": self.bi, "tri": self.tri,
                         "case": self.case, "tokens": self.tokens}, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "BaseModel":
        with open(path, "rb") as f:
            d = pickle.load(f)
        m = cls()
        m.uni, m.bi, m.tri, m.case, m.tokens = d["uni"], d["bi"], d["tri"], d["case"], d["tokens"]
        m._index()
        return m

    # --- запросы ---
    def prefix_range(self, prefix: str) -> tuple[int, int]:
        lo = bisect.bisect_left(self.vocab, prefix)
        hi = bisect.bisect_left(self.vocab, prefix + "￿")
        return lo, hi


class UserModel:
    """Счётчики набранного руками. Хранятся только счёты, не сырой текст."""

    def __init__(self):
        self.uni: Counter = Counter()
        self.bi: dict[str, Counter] = defaultdict(Counter)
        self.tri: dict[str, Counter] = defaultdict(Counter)
        self.case: dict[str, str] = {}
        self.decayed_at = 0.0
        self.dirty = False

    def learn(self, c2: str, c1: str, word: str):
        if len(word) > MAX_WORD_LEN:
            return
        w = norm(word)
        self.uni[w] += 1
        self.bi[c1][w] += 1
        self.tri[c2 + " " + c1][w] += 1
        if word != w and not word.isdigit() and c1 != BOS:
            self.case[w] = word
        self.dirty = True

    def unlearn(self, c2: str, c1: str, word: str):
        """Откатить одно learn: слово стёрли или переписали сразу после пробела."""
        w = norm(word)
        for table, key in ((self.uni, None), (self.bi, c1), (self.tri, c2 + " " + c1)):
            c = table if key is None else table.get(key)
            if c is None or w not in c:
                continue
            c[w] -= 1
            if c[w] <= 0:
                del c[w]
            if key is not None and not c:
                del table[key]
        self.dirty = True

    def decay(self, now: float):
        """Забывание: счёты тают вдвое за USER_HALF_LIFE_DAYS, крохи выбрасываются."""
        if not self.decayed_at:
            self.decayed_at = now
            return
        days = (now - self.decayed_at) / 86400
        if days < 7:
            return
        f = 0.5 ** (days / USER_HALF_LIFE_DAYS)

        def shrink(c: Counter) -> Counter:
            return Counter({w: round(n * f, 3) for w, n in c.items() if n * f >= 0.3})

        self.uni = shrink(self.uni)
        for table in (self.bi, self.tri):
            for k in list(table):
                table[k] = shrink(table[k])
                if not table[k]:
                    del table[k]
        self.decayed_at = now
        self.dirty = True

    def forget(self, word: str) -> int:
        """Забыть слово: убрать из всех счётчиков (и как продолжение, и как контекст). → сколько записей убрано."""
        w = norm(word)
        n = int(self.uni.pop(w, 0) > 0) + int(self.case.pop(w, None) is not None)
        for table in (self.bi, self.tri):
            for k in list(table):
                if w in table[k]:
                    del table[k][w]
                    n += 1
                if not table[k] or w in k.split(" "):
                    del table[k]
                    n += 1
        self.dirty = self.dirty or n > 0
        return n

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"uni": self.uni, "bi": {k: dict(v) for k, v in self.bi.items()},
                "tri": {k: dict(v) for k, v in self.tri.items()}, "case": self.case,
                "decayed_at": self.decayed_at}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        self.dirty = False

    @classmethod
    def load(cls, path: Path) -> "UserModel":
        m = cls()
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            m.uni = Counter(d.get("uni", {}))
            for k, v in d.get("bi", {}).items():
                m.bi[k] = Counter(v)
            for k, v in d.get("tri", {}).items():
                m.tri[k] = Counter(v)
            m.case = d.get("case", {})
            m.decayed_at = d.get("decayed_at", 0.0)
        return m


def parse_context(text: str) -> tuple[str, str, str, bool, bool]:
    """→ (c2, c1, prefix, sentence_start, ends_with_space).

    prefix — недописанное слово перед курсором (может быть пустым).
    """
    tail = text[-300:]
    parts = SENT_SPLIT_RE.split(tail)
    sent = parts[-1]
    m = TRAILING_WORD_RE.search(sent)
    prefix = m.group(0) if m else ""
    before = sent[: len(sent) - len(prefix)] if prefix else sent
    words = [norm(t) for t in WORD_RE.findall(before)]
    seq = [BOS, BOS] + words
    sentence_start = not words
    ends_space = text[-1:].isspace() if text else True
    return seq[-2], seq[-1], prefix, sentence_start, ends_space


class Predictor:
    def __init__(self, base: BaseModel, user: UserModel | None = None):
        self.base = base
        self.user = user or UserModel()
        # пороги показа
        self.min_conf_word = 0.35     # дописать слово целиком
        self.min_conf_next = 0.40     # предложить следующее слово после пробела
        self.min_conf_extra = 0.60    # продлить подсказку ещё словом
        self.min_support = 3          # сколько раз пара/тройка должна встретиться
        self.stem_cover = 0.60        # общая основа для вариантов с такой суммарной вероятностью
        self.max_extra_words = 2
        self.min_insert = 2           # подсказку короче (символов) не показывать
        self.next_min_tri = 0         # следующее слово без префикса: опора в триграмме не меньше
        self.short_penalty = 0.07     # +к порогу за каждый символ префикса короче 4
        self.k = 6.0                  # сглаживание интерполяции (подобрано evaluate.py)
        self.banned: set[str] = set()  # не подсказывать (нижний регистр)
        self.doc_weight = 0.0         # вес слов текущего поля; 0 — выключено
        self.layout_fix = False       # подсказка при не той раскладке
        self.snippets: dict[str, str] = {}  # сокращение (нижний регистр) → текст
        self._doc_key = None
        self._doc_uni: Counter = Counter()
        self._doc_bi: dict[str, Counter] = {}
        self._uni_cache: dict[str, list[str]] = {}
        self._uni_top("")  # прогрев: самый дорогой запрос

    def _uni_top(self, prefix: str) -> list[str]:
        top = self._uni_cache.get(prefix)
        if top is None:
            lo, hi = self.base.prefix_range(prefix)
            words = self.base.vocab[lo:hi]
            top = sorted(words, key=self.base.uni.__getitem__, reverse=True)[:40]
            if len(self._uni_cache) > 5000:
                self._uni_cache.clear()
            self._uni_cache[prefix] = top
        return top

    def _level(self, level: str, key: str, prefix: str) -> tuple[Counter, float]:
        """Счёты слов под префиксом на уровне и полная масса под префиксом."""
        got: Counter = Counter()
        mass = 0.0
        if level == "uni":
            lo, hi = self.base.prefix_range(prefix)
            mass = float(self.base.cum[hi] - self.base.cum[lo])
            for w in self._uni_top(prefix):
                got[w] = self.base.uni[w]
            utab = self.user.uni
            dtab = self._doc_uni
        else:
            table = self.base.tri if level == "tri" else self.base.bi
            if key in table:
                total, lst = table[key]
                for w, n in lst:
                    if w.startswith(prefix):
                        got[w] += n
                stored = sum(n for _, n in lst)
                matched = sum(got.values())
                # хвост, не попавший в топ, распределяем пропорционально
                mass = matched + (total - stored) * (matched / stored if stored else 0)
            utab = (self.user.tri if level == "tri" else self.user.bi).get(key, {})
            dtab = self._doc_bi.get(key, {}) if level == "bi" else {}
        for w, n in utab.items():
            if w.startswith(prefix):
                got[w] += n * USER_WEIGHT
                mass += n * USER_WEIGHT
        if self.doc_weight:
            for w, n in dtab.items():
                if w.startswith(prefix):
                    got[w] += n * self.doc_weight
                    mass += n * self.doc_weight
        for w in [w for w in got if w.startswith("<") or w in self.banned]:
            del got[w]
        return got, mass

    def distribution(self, c2: str, c1: str, prefix: str) -> tuple[list[tuple[str, float]], Counter]:
        """Интерполяция 3→2→1 грамм. → ([(слово, p)] по убыванию, опора в би/триграммах)."""
        dist, support, _g3 = self._dist(c2, c1, prefix)
        return dist, support

    def _dist(self, c2: str, c1: str, prefix: str) -> tuple[list[tuple[str, float]], Counter, Counter]:
        p = prefix.lower()
        g1, m1 = self._level("uni", "", p)
        g2, m2 = self._level("bi", c1, p)
        g3, m3 = self._level("tri", c2 + " " + c1, p)
        l2 = m2 / (m2 + self.k)
        l3 = m3 / (m3 + self.k)
        probs = {}
        for w in set(g1) | set(g2) | set(g3):
            p1 = g1.get(w, 0) / m1 if m1 else 0.0
            p2 = l2 * (g2.get(w, 0) / m2 if m2 else 0.0) + (1 - l2) * p1
            probs[w] = l3 * (g3.get(w, 0) / m3 if m3 else 0.0) + (1 - l3) * p2
        return sorted(probs.items(), key=lambda x: -x[1]), g2 + g3, g3

    def surface(self, w: str) -> str:
        return self.user.case.get(w) or self.base.case.get(w) or w

    def _next_words(self, a: str, b: str) -> list[str]:
        out = []
        for _ in range(self.max_extra_words):
            dist, sup = self.distribution(a, b, "")
            if not dist:
                break
            w, pr = dist[0]
            if pr < self.min_conf_extra or sup[w] < self.min_support or w == NUM:
                break
            out.append(self.surface(w))
            a, b = b, w
        return out

    def _doc_update(self, text: str, prefix: str):
        """Счётчики слов и пар из текста поля перед курсором, без недописанного слова."""
        body = text[:len(text) - len(prefix)] if prefix else text
        key = (len(body), body[-40:])
        if key == self._doc_key:
            return
        self._doc_key = key
        uni: Counter = Counter()
        bi: dict[str, Counter] = defaultdict(Counter)
        for toks in sentences(body[-1500:]):
            prev = BOS
            for t in toks:
                w = norm(t)
                if w != NUM and len(w) >= 3:  # короткие служебные и так есть в корпусе
                    uni[w] += 1
                    bi[prev][w] += 1
                prev = w
        self._doc_uni, self._doc_bi = uni, dict(bi)

    def _snippet(self, text: str, prefix: str) -> Suggestion | None:
        """Своё сокращение целиком перед курсором: «спс» → «спасибо»."""
        full = self.snippets.get(prefix.lower()) if prefix else None
        if not full:
            return None
        return Suggestion(insert=full, word=full, confidence=1.0, level="snippet", whole_word=True,
                          replace=len(prefix))

    def _layout(self, text: str, c2: str, c1: str, sent_start: bool) -> Suggestion | None:
        """Набрано не в той раскладке: «ghbdtn» → «привет», «ащк» → «for»."""
        m = LATIN_TAIL_RE.search(text) or CYR_TAIL_RE.search(text)
        if not m:
            return None
        raw = m.group(0)
        if sum(ch.isalpha() for ch in raw) < 3 or text[:m.start()][-1:] not in ("", " ", "\n", "\t", "(", "«", '"'):
            return None
        low = raw.lower()
        lo, hi = self.base.prefix_range(low)
        if hi > lo or any(w.startswith(low) for w in self.user.uni):
            return None  # так начинается слово и в этой раскладке
        conv = raw.translate(EN2RU if m.re is LATIN_TAIL_RE else RU2EN)
        if not WORD_RE.fullmatch(conv):
            return None
        dist, _sup, _g3 = self._dist(c2, c1, conv)
        if not dist:
            return None
        w, pr = dist[0]
        if self.base.uni.get(w, 0) < 5 or (w != conv.lower() and pr < self.min_conf_word):
            return None
        word = match_case(self.surface(w), conv, sent_start)
        return Suggestion(insert=word, word=word, confidence=pr, level="layout", whole_word=True,
                          replace=len(raw))

    def suggest(self, text: str) -> Suggestion | None:
        c2, c1, prefix, sent_start, ends_space = parse_context(text)
        if prefix and self.snippets:
            s = self._snippet(text, prefix)
            if s:
                return s
        if self.layout_fix and text and not ends_space:
            s = self._layout(text, c2, c1, sent_start)
            if s:
                return s
        if prefix and (prefix[-1] in "-'’" or prefix.isdigit()):
            return None
        if self.doc_weight:
            self._doc_update(text, prefix)
        if not prefix and text and not ends_space:
            return None
        dist, sup, sup3 = self._dist(c2, c1, prefix)
        if not dist:
            return None
        low = prefix.lower()
        w, pr = dist[0]
        if w == low:
            return None
        if not prefix:
            if (pr < self.min_conf_next or sup[w] < self.min_support or sup3[w] < self.next_min_tri
                    or w == NUM):
                return None
        if prefix:
            # короткий префикс — нужна большая уверенность
            need = self.min_conf_word + max(0, 4 - len(prefix)) * self.short_penalty
        else:
            need = self.min_conf_next
        if pr >= need:
            word = match_case(self.surface(w), prefix, sent_start)
            insert = word[len(prefix):]
            extra = self._next_words(c1, w)
            if extra:
                insert += " " + " ".join(extra)
            if len(insert.strip()) < max(1, self.min_insert):
                return None
            return Suggestion(insert=insert, word=word, confidence=pr,
                              level="tri" if sup[w] else "uni", whole_word=True)
        if not prefix:
            return None
        # нет явного лидера — дописываем общую основу (докум → документ)
        group, cover = [], 0.0
        for cand, cp in dist[:8]:
            group.append(cand)
            cover += cp
            if cover >= self.stem_cover:
                break
        if cover < self.stem_cover or low in group:
            return None
        stem = os.path.commonprefix(group)
        if len(stem) - len(low) < max(2, self.min_insert):
            return None
        stem = match_case(stem, prefix, sent_start)
        return Suggestion(insert=stem[len(prefix):], word=stem, confidence=cover,
                          level="stem", whole_word=False)

    def learn_from(self, text_before_sep: str) -> tuple[str, str, str] | None:
        """Вызывать, когда слово закончено (набран разделитель). → (c2, c1, слово) выученного — для unlearn."""
        c2, c1, prefix, _, _ = parse_context(text_before_sep.rstrip())
        if prefix and not prefix.isdigit():
            self.user.learn(c2, c1, prefix)
            return c2, c1, prefix
        return None
