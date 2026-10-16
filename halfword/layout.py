"""Набрано не в той раскладке: «ghbdtn» → «привет», «ащк» → «for».

Без WinAPI: таблица клавиш, частотные словари, символьная модель языка и решение по цепочке слов.
Слово оценивается в обеих раскладках — как слово своего языка (частота в словаре или в текстах
человека) или, если его нет в словаре, по буквенным тройкам. Перевод должен быть заметно правдоподобнее
набранного. Короткие слова (я = z, не = yt) исправляются только вместе с уверенным соседом.
"""
from __future__ import annotations

import bisect
import gzip
import math
import re
from array import array
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

LEXICON_DIR = Path(__file__).resolve().parent / "lexicon"

# US QWERTY ↔ русская ЙЦУКЕН по клавишам: без Shift, с Shift, Shift-цифры
EN_KEYS = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./" + '~QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>?' + "@#$^&|"
RU_KEYS = "ёйцукенгшщзхъфывапролджэячсмитьбю." + "ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ," + '"№;:?/'

CYR = "А-Яа-яЁё"
URLISH_RE = re.compile(r"://|^www\.|@.*\.|[\\_]|\w/\w|\.(?:com|org|net|ru|io|py|js|json|md|txt|exe|html)\b", re.I)


class KeyMap:
    """Пары символов по клавишам двух раскладок (en[i] и ru[i] — одна клавиша)."""

    def __init__(self, en: str = EN_KEYS, ru: str = RU_KEYS):
        if len(en) != len(ru):
            raise ValueError("раскладки разной длины")
        self.en, self.ru = en, ru
        self.en2ru = str.maketrans(en, ru)
        self.ru2en = str.maketrans(ru, en)
        # латинские символы, которые в русской раскладке — буквы: , . ; ' [ ] ` и их Shift-пары
        self.lat_extra = "".join(e for e, r in zip(en, ru) if r.isalpha() and not e.isalpha())
        self.lat_core = re.compile(r"^(?:[A-Za-z%s]+(?:-[A-Za-z%s]+)*)$" % ((re.escape(self.lat_extra),) * 2))
        self.cyr_core = re.compile(rf"^[{CYR}]+(?:-[{CYR}]+)*$")
        self.lat_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") | set(self.lat_extra)

    def convert(self, s: str, to_cyr: bool) -> str:
        return s.translate(self.en2ru if to_cyr else self.ru2en)


class CharModel:
    """Буквенные тройки языка со сглаживанием — оценка слов, которых нет в словаре."""

    K = 4.0

    def __init__(self, words: Iterable[tuple[str, float]]):
        tri, ctx2, bi, ctx1, uni = Counter(), Counter(), Counter(), Counter(), Counter()
        for w, wt in words:
            s = "^^" + w + "$"
            for i in range(2, len(s)):
                tri[s[i - 2:i + 1]] += wt
                ctx2[s[i - 2:i]] += wt
                bi[s[i - 1:i + 1]] += wt
                ctx1[s[i - 1]] += wt
                uni[s[i]] += wt
        self.tri, self.ctx2, self.bi, self.ctx1 = tri, ctx2, bi, ctx1
        total = sum(uni.values())
        self.uni = {c: n / total for c, n in uni.items()}
        self.floor = 0.5 / total

    def mean_log2(self, w: str) -> float:
        """Средний log2 вероятности буквы (с концом слова). Слово языка ≈ −3…−4, мусор ≈ −7 и ниже."""
        s = "^^" + w + "$"
        k, total = self.K, 0.0
        for i in range(2, len(s)):
            c = s[i]
            p1 = self.uni.get(c, self.floor)
            p2 = (self.bi.get(s[i - 1:i + 1], 0) + k * p1) / (self.ctx1.get(s[i - 1], 0) + k)
            p3 = (self.tri.get(s[i - 2:i + 1], 0) + k * p2) / (self.ctx2.get(s[i - 2:i], 0) + k)
            total += math.log2(p3)
        return total / (len(s) - 2)


class _Words(Sequence):
    """Отсортированные слова одной строкой со смещениями: в 6 раз меньше памяти, чем список строк."""

    def __init__(self, words: list[str]):
        self.text = "\n".join(words) + "\n"
        self.off = array("I", [0])
        for w in words:
            self.off.append(self.off[-1] + len(w) + 1)

    def __len__(self):
        return len(self.off) - 1

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self[k] for k in range(*i.indices(len(self)))]
        if i < 0:
            i += len(self)
        return self.text[self.off[i]:self.off[i + 1] - 1]


class Lexicon:
    """Частотный словарь языка: слово → Zipf (log10 частоты на миллиард слов)."""

    CHAR_WORDS = 40_000

    def __init__(self, freq: dict[str, float]):
        words = sorted(freq)
        self.sorted = _Words(words)
        self.z = array("H", (round(freq[w] * 100) for w in words))
        top = sorted(freq.items(), key=lambda kv: -kv[1])[: self.CHAR_WORDS]
        self.chars = CharModel(top)

    @classmethod
    def load(cls, lang: str, path: Path | None = None) -> "Lexicon":
        path = path or LEXICON_DIR / f"{lang}.txt.gz"
        freq: dict[str, float] = {}
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                w, _, z = line.rstrip("\n").partition("\t")
                if w:
                    freq[w] = int(z) / 100
        return cls(freq)

    @property
    def freq(self) -> dict[str, float]:
        """Словарь целиком (для тестов и стенда; в работе не нужен)."""
        return {self.sorted[i]: self.z[i] / 100 for i in range(len(self.sorted))}

    def zipf(self, w: str) -> float:
        i = bisect.bisect_left(self.sorted, w)
        return self.z[i] / 100 if i < len(self.sorted) and self.sorted[i] == w else 0.0

    def has_prefix(self, p: str) -> bool:
        i = bisect.bisect_left(self.sorted, p)
        return i < len(self.sorted) and self.sorted[i].startswith(p)

    def best_with_prefix(self, p: str, limit: int = 3000) -> tuple[str, float] | None:
        """Самое частое слово, начинающееся с p."""
        i = bisect.bisect_left(self.sorted, p)
        best = None
        for k in range(i, min(len(self.sorted), i + limit)):
            w = self.sorted[k]
            if not w.startswith(p):
                break
            if best is None or self.z[k] > best[1]:
                best = (w, self.z[k])
        return (best[0], best[1] / 100) if best else None


@dataclass
class Params:
    """Пороги; подобраны стендом `halfword.evaluate_layout`."""
    corp_base: float = 1.5      # слово из текстов человека, встреченное c раз: zipf = base + log10(c)
    corp_min: int = 2
    corp_cap: float = 5.0
    unk_mid: float = -3.6       # средний log2 буквы, при котором незнакомое слово оценивается в unk_at
    unk_at: float = 1.0
    unk_slope: float = 0.8
    unk_cap: float = 2.0        # незнакомое слово не правдоподобнее редкого словарного
    short_unknown: float = 0.0  # оценка незнакомого слова из 1–2 букв: буквенные тройки на нём врут
    wrong_margin: float = 1.6   # слово ≥3 букв точно не в той раскладке
    conv_min: float = 0.8       # и перевод хоть сколько-то похож на слово
    own_known: float = 3.0      # набранное — частое слово своего языка: нужен перевес strong_margin
    strong_margin: float = 3.0
    short_margin: float = 1.0   # короткое слово идёт в цепочку с таким перевесом
    suggest_total: float = 1.6  # сумма по цепочке для подсказки
    auto_total: float = 2.5     # и для автозамены
    auto_conv_known: float = 2.0  # в цепочке есть слово ≥3 букв, перевод которого в словаре с такой частотой
    auto_long_letters: int = 5  # длинное слово, которого нет в словаре, — автозамена по буквам
    auto_long_margin: float = 3.5
    long_margin: float = 3.0    # слово ≥5 букв: перевод может быть и незнакомым (опечатка, сленг), если перевес большой
    strip_penalty: float = 1.5  # за каждый , . ; ' [ ] вокруг «английского» слова, кроме точки/запятой в конце
    ru2en_short_letters: int = 4 # и короткое слово — только в частое английское с большим перевесом
    ru2en_short_z: float = 3.5
    ru2en_short_margin: float = 5.0
    ru2en_extra: float = 1.0  # русский текст основной: в английский переводить с бо́льшим перевесом
    ctx_bonus: float = 0.5      # перед цепочкой слова языка перевода
    max_chunks: int = 8


@dataclass
class LayoutFix:
    erase: int        # стереть столько символов перед курсором
    insert: str       # и вставить это
    original: str     # что стёрто — для отмены
    to_cyr: bool      # перевод в русскую раскладку (False — в английскую)
    words: int        # слов в исправлении
    margin: float     # уверенность: сумма перевесов по словам
    auto: bool        # уверенно настолько, что можно исправить без подтверждения
    closed: bool = True  # слово закончено разделителем; False — подсказка посреди слова


@dataclass
class _Tok:
    start: int        # смещение куска в тексте
    end: int
    to_cyr: bool | None  # None — не слово для перевода (ссылка, код, цифры, смешанные буквы)
    core: str = ""    # слово без окружающей пунктуации
    conv: str = ""    # перевод куска целиком
    conv_core: str = ""
    letters: int = 0
    margin: float = 0.0
    conv_z: float = 0.0
    own_z: float = 0.0
    status: str = "right"  # wrong / unsure / right / stop
    never: bool = False    # слово в исключениях: само не исправляется, но ручная клавиша его переводит


class LayoutDetector:
    """Решение «набрано не в той раскладке» по тексту перед курсором."""

    def __init__(self, ru: Lexicon | None = None, en: Lexicon | None = None, keymap: KeyMap | None = None,
                 known: Callable[[str], int] | None = None, never: Iterable[str] = (),
                 params: Params | None = None):
        self.ru = ru or Lexicon.load("ru")
        self.en = en or Lexicon.load("en")
        self.keys = keymap or KeyMap()
        self.known = known or (lambda w: 0)   # счёт слова в текстах человека (нижний регистр)
        self.never = {w.lower() for w in never}
        self.p = params or Params()
        self._cache: dict[str, _Tok] = {}

    # --- оценка слова -------------------------------------------------------------------------
    def word_score(self, w: str, cyr: bool) -> tuple[float, float]:
        """→ (оценка, частота в словаре/текстах). Оценка ≈ Zipf для знакомых, ниже — для мусора."""
        p, low = self.p, w.lower()
        lex = self.ru if cyr else self.en
        z = lex.zipf(low)
        c = self.known(low)
        if c >= p.corp_min:
            z = max(z, min(p.corp_cap, p.corp_base + math.log10(c)))
        if z > 0:
            return z, z
        if "-" in low.strip("-"):  # «кто-то»: в словаре части по отдельности
            parts = [self.word_score(part, cyr) for part in low.split("-") if part]
            return min(s for s, _ in parts), min(zz for _, zz in parts)
        if len(low) <= 2:
            return p.short_unknown, 0.0
        m = lex.chars.mean_log2(low)
        return min(p.unk_cap, p.unk_at + p.unk_slope * (m - p.unk_mid)), 0.0

    def _tok(self, chunk: str, start: int) -> _Tok:
        t = self._cache.get(chunk)
        if t is None:
            t = self._analyze(chunk)
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[chunk] = t
        return _Tok(start, start + len(chunk), t.to_cyr, t.core, t.conv, t.conv_core, t.letters,
                    t.margin, t.conv_z, t.own_z, t.status, t.never)

    def _analyze(self, chunk: str) -> _Tok:
        t = _Tok(0, len(chunk), None, status="stop")
        has_lat = any(ch in self.keys.lat_chars and ch.isalpha() for ch in chunk)
        has_cyr = any("а" <= ch.lower() <= "я" or ch in "Ёё" for ch in chunk)
        if has_lat == has_cyr or any(ch.isdigit() for ch in chunk) or URLISH_RE.search(chunk):
            return t
        to_cyr = has_lat
        core = self._core(chunk, to_cyr)
        if not core:
            return t
        lead = chunk.index(core)
        trail = chunk[lead + len(core):]
        # заглавные Ж Б Ю Э Х Ъ Ё в «сщььшеЖ» — это Shift+; и т.п. другой раскладки, а не регистр
        cased = [ch for ch in core if ch.isalpha() and self.keys.convert(ch, to_cyr).isalpha()]
        if any(ch.isupper() for ch in cased[1:]) and not all(ch.isupper() for ch in cased):
            return t  # iPhone, GitHub: смешанный регистр — имя, а не ошибка
        conv_core = self.keys.convert(core, to_cyr)
        conv = self.keys.convert(chunk, to_cyr)
        conv_word = conv_core.strip(".,;:?!\"'")
        if to_cyr and core[-1] in ",." and not self._word_known(conv_core, True) \
                and self._word_known(conv_core[:-1], True):
            # «ghbdtn,» — запятая набрана той же клавишей, что в русской: оставить запятую
            conv_word = conv_core[:-1]
            conv = self.keys.convert(chunk[:lead], True) + conv_word + core[-1] + self.keys.convert(trail, True)
        letters = sum(ch.isalpha() for ch in conv_word)
        own, own_z = self._own_score(core, to_cyr)
        cs, conv_z = self.word_score(conv_word, to_cyr) if conv_word else (-3.0, 0.0)
        p = self.p
        t.to_cyr, t.core, t.conv, t.conv_core, t.letters = to_cyr, core, conv, conv_word, letters
        t.margin, t.conv_z, t.own_z = cs - own, conv_z, own_z
        if letters >= 3:
            if own_z >= p.own_known and t.margin < p.strong_margin:
                # частое слово своего языка: само не исправляем, но в цепочку с уверенным соседом идёт
                t.status = "unsure" if t.margin >= p.wrong_margin else "right"
            elif t.margin >= p.wrong_margin + (0 if to_cyr else p.ru2en_extra) and (
                    cs >= p.conv_min or letters >= 5 and t.margin >= p.long_margin):
                # русская опечатка из 3–4 букв легко «переводится» в редкое английское слово
                short_en = not to_cyr and letters <= p.ru2en_short_letters and not (
                    conv_z >= p.ru2en_short_z and t.margin >= p.ru2en_short_margin)
                t.status = "unsure" if short_en else "wrong"
            elif t.margin <= 0:
                t.status = "right"
            else:
                t.status = "unsure"
        else:
            t.status = "unsure" if t.margin >= p.short_margin and conv_z > 0 else "right"
        if core.lower() in self.never:
            t.status, t.never = "right", True  # не трогать само; перевод для ручной клавиши посчитан
        return t

    def _own_score(self, core: str, to_cyr: bool) -> tuple[float, float]:
        """Набранное как слово своего языка. «it.», «bye,» — английское слово с точкой или запятой;
        «nt,z» — запятая внутри: так английские слова не пишут."""
        if not to_cyr:
            return self.word_score(core, True)
        extra = self.keys.lat_extra
        word = core.strip(extra)
        if not word or any(ch in extra and ch != "'" for ch in word):
            return -3.0, 0.0
        s, z = self.word_score(word, False)
        lead, trail = core[:core.index(word)], core[core.index(word) + len(word):]
        # точка и запятая после английского слова обычны, а «'nj», «[jhjij» — буквы э и х
        odd = len(lead) + len(trail.rstrip(",."))
        if lead.startswith("'") and trail.endswith("'"):
            odd -= 2  # 'quoted'
        return s - self.p.strip_penalty * max(0, odd), z

    def _core(self, chunk: str, to_cyr: bool) -> str:
        """Слово без окружающей пунктуации: для латиницы , . ; ' [ ] — тоже буквы (б ю ж э х ъ)."""
        if to_cyr:
            ok = self.keys.lat_chars | {"-"}
        else:
            ok = None
        i, j = 0, len(chunk)
        if ok is not None:
            while i < j and chunk[i] not in ok:
                i += 1
            while j > i and chunk[j - 1] not in ok:
                j -= 1
            core = chunk[i:j].strip("-")
            return core if core and self.keys.lat_core.match(core) else ""
        m = re.search(rf"[{CYR}]+(?:-[{CYR}]+)*", chunk)
        if not m or chunk[:m.start()].strip("([{«\"'") or chunk[m.end():].strip(".,;:!?)]}»\"'…"):
            return ""
        return m.group(0)

    def _word_known(self, w: str, cyr: bool) -> bool:
        return self.word_score(w, cyr)[1] > 0

    # --- решение по тексту ----------------------------------------------------------------------
    def _chunks(self, text: str) -> list[_Tok]:
        line_start = max(text.rfind("\n"), text.rfind("\r")) + 1
        toks = []
        for m in re.finditer(r"\S+", text[line_start:]):
            toks.append((m.group(0), line_start + m.start()))
        return [self._tok(c, s) for c, s in toks[-(self.p.max_chunks + 4):]]

    def detect(self, text: str) -> LayoutFix | None:
        """Исправление для текста перед курсором или None.

        Текст кончается разделителем — решение по закрытой цепочке слов (можно и автозамену);
        иначе — подсказка по недописанному слову под курсором.
        """
        if not text:
            return None
        toks = self._chunks(text)
        if not toks:
            return None
        closed = text[-1].isspace() or self._closed_by_punct(toks[-1], text)
        if not closed:
            return self._open(text, toks)
        return self._closed(text, toks)

    def _closed_by_punct(self, last: _Tok, text: str) -> bool:
        """«ghbdtn?» — после слова пунктуация, которая не буква ни в одной раскладке."""
        if last.to_cyr is None or not last.core or last.end != len(text):
            return False
        chunk = text[last.start:last.end]
        return not chunk.endswith(last.core)

    def _chain(self, toks: list[_Tok], relaxed: bool = False) -> list[_Tok]:
        """Хвост подряд идущих слов одной раскладки, похожих на ошибку."""
        chain: list[_Tok] = []
        direction = None
        for t in reversed(toks[-self.p.max_chunks:]):
            if t.to_cyr is None or (direction is not None and t.to_cyr != direction):
                break
            if t.status == "right" and not (relaxed and t.margin > 0):
                break
            direction = t.to_cyr
            chain.append(t)
        chain.reverse()
        if not relaxed:
            while chain and chain[0].status == "unsure" and chain[0].margin <= 0:
                chain.pop(0)
        return chain

    def _closed(self, text: str, toks: list[_Tok]) -> LayoutFix | None:
        p = self.p
        chain = self._chain(toks)
        if not chain or not any(t.status == "wrong" for t in chain):
            return None
        total = sum(t.margin for t in chain)
        before = toks[: len(toks) - len(chain)]
        if before and any(t.to_cyr is not None and t.to_cyr != chain[0].to_cyr and t.status == "right"
                          and t.own_z > 0 and not t.never for t in before[-3:]):
            total += p.ctx_bonus  # перед цепочкой уже пишут на языке перевода
        if total < p.suggest_total:
            return None
        auto = total >= p.auto_total and any(
            t.letters >= 3 and t.status == "wrong" and t.conv_z >= p.auto_conv_known
            or t.letters >= p.auto_long_letters and t.status == "wrong" and t.margin >= p.auto_long_margin
            for t in chain)
        return self._fix(text, chain, total, auto, closed=True)

    def _open(self, text: str, toks: list[_Tok]) -> LayoutFix | None:
        """Недописанное слово: свой язык так не начинается, а перевод — начало частого слова."""
        t = toks[-1]
        if (t.to_cyr is None or t.never or t.end != len(text) or t.letters < 3
                or not text[t.start:].endswith(t.core)):
            return None
        if text[t.start:t.end] != t.core or t.conv_core != self.keys.convert(t.core, t.to_cyr):
            return None  # «(ghbd», «ghbdtn,»: подсказку посреди слова даём только для чистого слова
        low = t.core.lower()
        own = self.en if t.to_cyr else self.ru
        if not (t.to_cyr and set(low) & set(self.keys.lat_extra)) and (own.has_prefix(low) or self.known(low)):
            return None
        conv = t.conv_core.lower()
        lex = self.ru if t.to_cyr else self.en
        best = lex.best_with_prefix(conv)
        if best is None or best[1] < self.p.auto_conv_known:
            return None
        word = _match_case(best[0], t.conv_core)
        fix = LayoutFix(erase=len(t.core), insert=word, original=t.core, to_cyr=t.to_cyr, words=1,
                        margin=best[1], auto=False, closed=False)
        return fix

    def _fix(self, text: str, chain: list[_Tok], total: float, auto: bool, closed: bool) -> LayoutFix:
        start = chain[0].start
        orig = text[start:]
        parts, pos = [], start
        for t in chain:
            parts.append(text[pos:t.start])
            parts.append(t.conv)
            pos = t.end
        parts.append(text[pos:])
        return LayoutFix(erase=len(orig), insert="".join(parts), original=orig, to_cyr=chain[0].to_cyr,
                         words=len(chain), margin=total, auto=auto, closed=closed)

    # --- ручной перевод -------------------------------------------------------------------------
    def convert_last(self, text: str) -> LayoutFix | None:
        """Ручная клавиша без выделения: последняя цепочка не в той раскладке, иначе последнее слово."""
        toks = self._chunks(text.rstrip())
        if not toks:
            return None
        chain = self._chain(toks, relaxed=True)
        if not chain:
            last = toks[-1]
            if last.to_cyr is None:
                return None
            chain = [last]
        body = text.rstrip()
        fix = self._fix(body, chain, sum(t.margin for t in chain), auto=False, closed=True)
        tail = text[len(body):]
        fix.erase += len(tail)
        fix.original += tail
        fix.insert += tail
        return fix

    def convert_text(self, s: str) -> tuple[str, bool]:
        """Выделенный текст целиком: направление — по большинству букв. → (перевод, в кириллицу?)."""
        lat = sum(ch in self.keys.lat_chars and ch.isalpha() for ch in s)
        cyr = sum(bool(re.match(f"[{CYR}]", ch)) for ch in s)
        to_cyr = lat >= cyr
        return self.keys.convert(s, to_cyr), to_cyr

    def never_key(self, chunk: str) -> str:
        """Как слово записывается в исключения: в латинице , . ; ' [ ] — часть слова («nt,z»)."""
        to_cyr = any(ch in self.keys.lat_chars and ch.isalpha() for ch in chunk)
        return (self._core(chunk, to_cyr) or chunk.strip(".,;:!?()\"'")).lower()

    def is_wrong_layout(self, word: str) -> bool:
        """Законченное слово похоже на набранное не в той раскладке — такое не учить."""
        t = self._tok(word, 0)
        return t.status == "wrong"


def _match_case(word: str, like: str) -> str:
    if like.isupper() and len(like) > 1:
        return word.upper()
    if like[:1].isupper():
        return word[:1].upper() + word[1:]
    return word
