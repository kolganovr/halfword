"""Правки слов на расстоянии 1 (Дамерау-Левенштейн) и вероятные опечатки в словаре.

Правки: замена, вставка, удаление, перестановка соседних букв. Соседние клавиши заданы своей таблицей рядов
(со сдвигом рядов), а не моделью шума стенда: модель исправления не должна повторять модель порчи.
Вероятная опечатка — редкое слово, у которого есть заметно более частый сосед по правке, и которого нет ни в
одном частотном словаре языка: такое слово лучше не подсказывать.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache

from .layout import Lexicon

RU = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
EN = "abcdefghijklmnopqrstuvwxyz"
ALPHABET = RU + EN
MIN_LEN = 4        # короче слово не помечаем: у коротких слишком много соседей по правке
USER_MIN = 3       # слово, набранное руками столько раз и чаще, не опечатка

# ряды клавиш и сдвиг рядов в долях клавиши (как на физической клавиатуре)
ROWS = {
    "ru": ("йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"),
    "en": ("qwertyuiop", "asdfghjkl", "zxcvbnm"),
}
ROW_SHIFT = (0.0, 0.25, 0.75)
REACH = 1.2        # клавиши ближе этого (в ширинах клавиши) считаются соседними


def _script(word: str) -> str:
    return RU if any("а" <= ch <= "я" or ch == "ё" for ch in word) else EN


def edits1(word: str, alphabet: str | None = None) -> set[str]:
    """Все строки на расстоянии 1 от слова (без самого слова).

    alphabet — буквы для замен и вставок; по умолчанию алфавит письма слова (русский или латинский)."""
    letters = alphabet if alphabet is not None else _script(word)
    n = len(word)
    out = set()
    for i in range(n):
        out.add(word[:i] + word[i + 1:])  # удаление
        if i + 1 < n and word[i] != word[i + 1]:
            out.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])  # перестановка соседних
        for ch in letters:
            out.add(word[:i] + ch + word[i + 1:])  # замена
    for i in range(n + 1):
        for ch in letters:
            out.add(word[:i] + ch + word[i:])  # вставка
    out.discard(word)
    return out


def is_edit1(a: str, b: str) -> bool:
    """Расстояние Дамерау-Левенштейна между a и b ровно 1."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    while i < min(la, lb) and a[i] == b[i]:
        i += 1
    if la == lb:
        if a[i + 1:] == b[i + 1:]:
            return True  # замена
        return i + 1 < la and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]  # перестановка
    if la < lb:
        a, b = b, a
    return a[:i] + a[i + 1:] == b  # a длиннее на букву


@lru_cache(maxsize=None)
def _key_pos() -> dict[str, tuple[float, float]]:
    pos = {}
    for rows in ROWS.values():
        for r, row in enumerate(rows):
            for c, ch in enumerate(row):
                pos[ch] = (c + ROW_SHIFT[r], float(r))
    return pos


@lru_cache(maxsize=None)
def neighbors(ch: str) -> str:
    """Буквы на соседних клавишах (того же языка), ближние первыми. Для неизвестного символа — пусто."""
    low = ch.lower()
    pos = _key_pos()
    if low not in pos:
        return ""
    x, y = pos[low]
    lang = "ru" if low in "".join(ROWS["ru"]) else "en"
    near = []
    for row in ROWS[lang]:
        for other in row:
            if other == low:
                continue
            ox, oy = pos[other]
            d = ((ox - x) ** 2 + (oy - y) ** 2) ** 0.5
            if d <= REACH:
                near.append((d, other))
    return "".join(c for _d, c in sorted(near))


def near_edits1(word: str) -> set[str]:
    """Правки на расстоянии 1, какие бывают при наборе: соседняя клавиша, перестановка, пропуск, удвоение."""
    out = set()
    for i, ch in enumerate(word):
        out.add(word[:i] + word[i + 1:])
        out.add(word[:i] + ch + word[i:])
        if i + 1 < len(word) and ch != word[i + 1]:
            out.add(word[:i] + word[i + 1] + ch + word[i + 2:])
        for nb in neighbors(ch):
            out.add(word[:i] + nb + word[i + 1:])
    out.discard(word)
    return out


@lru_cache(maxsize=None)
def default_lexicons() -> tuple[Lexicon, ...]:
    """Словари русского и английского из lexicon/ (читаются один раз на процесс)."""
    return (Lexicon.load("ru"), Lexicon.load("en"))


def _is_real(word: str, lexicons: Iterable) -> bool:
    return any(lex.zipf(word) > 0 for lex in lexicons)


def is_probable_typo(word: str, uni: Mapping[str, float], lexicons: Iterable, ratio: float,
                     user_uni: Mapping[str, float] | None = None, user_min: int = USER_MIN,
                     min_len: int = MIN_LEN) -> bool:
    """Слово — вероятная опечатка: есть сосед по правке не реже чем в ratio раз чаще, а в словарях языка слова нет.

    uni — частоты базы (слово в нижнем регистре), lexicons — частотные словари языков (у каждого zipf(w))."""
    if ratio <= 0 or len(word) < min_len or word.startswith("<"):
        return False
    f = uni.get(word, 0)
    if f <= 0 or (user_uni is not None and user_uni.get(word, 0) >= user_min):
        return False
    if not set(word) <= set(ALPHABET) or _is_real(word, tuple(lexicons)):
        return False
    need = ratio * f
    return any(uni.get(v, 0) >= need for v in edits1(word))


def probable_typos(uni: Mapping[str, float], lexicons: Iterable, ratio: float,
                   user_uni: Mapping[str, float] | None = None, user_min: int = USER_MIN,
                   min_len: int = MIN_LEN) -> set[str]:
    """Все вероятные опечатки словаря uni (условия как у is_probable_typo). ratio <= 0 — пусто."""
    if ratio <= 0 or not uni:
        return set()
    lexicons = tuple(lexicons)
    letters = set(ALPHABET)
    top = max(uni.values())
    out = set()
    for w, f in uni.items():
        # от дешёвых проверок к дорогим; сосед должен быть в ratio раз частее самого слова
        if len(w) < min_len or f <= 0 or f * ratio > top or w.startswith("<"):
            continue
        if user_uni is not None and user_uni.get(w, 0) >= user_min:
            continue
        if not letters.issuperset(w) or _is_real(w, lexicons):
            continue
        need = ratio * f
        get = uni.get
        if any(get(v, 0) >= need for v in edits1(w)):
            out.add(w)
    return out
