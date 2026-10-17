"""Синтетические опечатки для стенда: какие слова испортить и во что.

Только для проверки: шум выдуманный (соседняя клавиша, перестановка, пропуск, удвоение, лишняя буква, иногда две
ошибки в слове), с зерном — один и тот же текст при одном зерне портится одинаково. Слова портятся независимо:
набор испорченных слов при доле 1% входит в набор при 3%, и форма опечатки у них та же.
"""
from __future__ import annotations

import random
import re

SEED = 1
MIN_LEN = 3                  # короче слова не портим
TWO_ERRORS = 0.15            # доля испорченных слов с двумя ошибками
SEP = " ,.;:!?)\n"
WORD_RE = re.compile(r"[^\W\d_]+")

ROWS = {
    "ru": ["йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"],
    "en": ["qwertyuiop", "asdfghjkl", "zxcvbnm"],
}
OPS = ("near", "swap", "omit", "double", "insert")
OP_WEIGHTS = (4, 2, 2, 1.5, 1.5)


def lang_of(word: str) -> str:
    return "ru" if re.search("[а-яё]", word.lower()) else "en"


def neighbors(ch: str, lang: str) -> list[str]:
    """Клавиши рядом с буквой: слева и справа в ряду, сверху и снизу с учётом сдвига рядов."""
    rows = ROWS[lang]
    low = ch.lower()
    for r, row in enumerate(rows):
        c = row.find(low)
        if c < 0:
            continue
        out = [row[j] for j in (c - 1, c + 1) if 0 <= j < len(row)]
        if r > 0:
            out += [rows[r - 1][j] for j in (c, c + 1) if j < len(rows[r - 1])]
        if r + 1 < len(rows):
            out += [rows[r + 1][j] for j in (c - 1, c) if 0 <= j < len(rows[r + 1])]
        return out
    return []


def _recase(orig: str, new: str) -> str:
    """Регистр исходного слова: ВЕРХНИЙ остаётся верхним, Заглавная — заглавной."""
    if not new:
        return new
    if len(orig) > 1 and orig.isupper():
        return new.upper()
    if orig[0].isupper():
        return new[0].upper() + new[1:]
    return new


def _one_error(word: str, rng: random.Random) -> str | None:
    lang = lang_of(word)
    low = word.lower()
    op = rng.choices(OPS, weights=OP_WEIGHTS)[0]
    i = rng.randrange(len(low))
    if op == "near":
        near = neighbors(low[i], lang)
        return low[:i] + rng.choice(near) + low[i + 1:] if near else None
    if op == "swap":
        i = rng.randrange(len(low) - 1)
        if low[i] == low[i + 1]:
            return None
        return low[:i] + low[i + 1] + low[i] + low[i + 2:]
    if op == "omit":
        return low[:i] + low[i + 1:]
    if op == "double":
        return low[:i + 1] + low[i:]
    letters = "".join(ROWS[lang])
    return low[:i] + rng.choice(letters) + low[i:]


def corrupt_word(word: str, rng: random.Random) -> str:
    """Слово с опечаткой (одной, реже двумя); результат всегда отличается от исходного."""
    for _ in range(20):
        low = word.lower()
        for _e in range(2 if rng.random() < TWO_ERRORS else 1):
            got = _one_error(low, rng)
            if got and len(got) >= 2:
                low = got
        if low != word.lower():
            return _recase(word, low)
    return word[1:] if len(word) > 2 else word  # недостижимо на практике: запасной пропуск буквы


def plan(text: str, rate: float, seed: int = SEED) -> dict[int, tuple[str, str]]:
    """{позиция начала слова: (слово, слово с опечаткой)} для доли rate слов от MIN_LEN букв.

    Портятся слова, после которых разделитель или конец текста (в начале слова не цифра и не подчёркивание)."""
    out: dict[int, tuple[str, str]] = {}
    if rate <= 0:
        return out
    rng = random.Random(f"gate-{seed}")
    for m in WORD_RE.finditer(text):
        w = m.group()
        if len(w) < MIN_LEN:
            continue
        u = rng.random()  # бросок на каждое подходящее слово, чтобы наборы разных долей вкладывались друг в друга
        s, e = m.span()
        if u >= rate or (s > 0 and (text[s - 1].isdigit() or text[s - 1] == "_")):
            continue
        if e < len(text) and text[e] not in SEP:
            continue
        bad = corrupt_word(w, random.Random(f"typo-{seed}-{s}"))
        if bad != w:
            out[s] = (w, bad)
    return out
