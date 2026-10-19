"""Согласование формы слова с левым контекстом (pymorphy3): мягкая подсказка при выборе между формами одной леммы.

Импорт мягкий: нет pymorphy3 — available() даёт False, остальное возвращает нейтральные значения.
Разборы кэшируются (lru_cache): в горячем пути suggest словарь не трогается, если слово уже встречалось.
"""
from __future__ import annotations

from functools import lru_cache

import importlib.util

_MA = None
_OK: bool | None = None
_CASE_FIX = {"gen2": "gent", "acc2": "accs", "loc2": "loct"}
_TOP = 3  # сколько лучших разборов слова учитывать

# управление предлогов (малая таблица: у pymorphy3 своей нет)
PREP_CASES = {
    "в": {"loct", "accs"}, "во": {"loct", "accs"}, "на": {"loct", "accs"},
    "к": {"datv"}, "ко": {"datv"}, "по": {"datv", "loct"}, "благодаря": {"datv"},
    "о": {"loct"}, "об": {"loct"}, "обо": {"loct"}, "при": {"loct"},
    "без": {"gent"}, "для": {"gent"}, "до": {"gent"}, "из": {"gent"}, "от": {"gent"}, "у": {"gent"},
    "около": {"gent"}, "после": {"gent"}, "кроме": {"gent"}, "вместо": {"gent"}, "против": {"gent"},
    "ради": {"gent"}, "среди": {"gent"}, "возле": {"gent"}, "из-за": {"gent"},
    "с": {"gent", "ablt"}, "со": {"gent", "ablt"},
    "над": {"ablt"}, "перед": {"ablt"}, "между": {"ablt", "gent"},
    "за": {"ablt", "accs"}, "под": {"ablt", "accs"},
    "через": {"accs"}, "про": {"accs"}, "сквозь": {"accs"},
}


def available() -> bool:
    """pymorphy3 установлен (сам модуль не импортируется, пока не нужен разбор)."""
    global _OK
    if _OK is None:
        _OK = importlib.util.find_spec("pymorphy3") is not None
    return _OK


def _ma():
    global _MA
    if _MA is None:
        import pymorphy3
        _MA = pymorphy3.MorphAnalyzer()
    return _MA


@lru_cache(maxsize=50_000)
def parses(word: str) -> tuple:
    """→ ((лемма, POS, падеж, род, число), ...) для лучших разборов; пусто без pymorphy3."""
    if not available():
        return ()
    out = []
    for p in _ma().parse(word)[:_TOP]:
        t = p.tag
        case = _CASE_FIX.get(t.case, t.case)
        out.append((p.normal_form, t.POS, case, t.gender, t.number))
    return tuple(out)


def lemmas(word: str) -> frozenset:
    return frozenset(p[0] for p in parses(word))


def same_lemma(a: str, b: str) -> bool:
    """Разные слова с общей леммой (форма вместо формы)."""
    return a != b and bool(lemmas(a) & lemmas(b))


_AGREE_POS = {"ADJF", "PRTF", "NPRO", "NUMR"}


@lru_cache(maxsize=100_000)
def compat(prev: str, cand: str) -> int:
    """+1 — слово cand хорошо стоит после prev, -1 — не согласуется, 0 — не знаем."""
    cp = parses(cand)
    if not cp:
        return 0
    req = PREP_CASES.get(prev)
    if req is not None:
        cases = [p[2] for p in cp if p[1] in ("NOUN", "ADJF", "NPRO", "PRTF") and p[2]]
        if not cases:
            return 0
        return 1 if any(c in req for c in cases) else -1
    pp = parses(prev)
    ctx = [p for p in pp if p[1] in _AGREE_POS and p[2]]
    nouns = [p for p in cp if p[1] == "NOUN" and p[2]]
    if not ctx or not nouns:
        return 0
    for a in ctx:
        for n in nouns:
            if a[2] != n[2]:
                continue
            if a[4] != n[4]:
                continue
            if a[4] == "sing" and a[3] and n[3] and a[3] != n[3]:
                continue
            return 1
    return -1


def rerank(dist: list, c1: str, bonus: float, scope: str = "lemma") -> list:
    """Переставляет кандидатов: p·(1+bonus·compat). Вероятности в результате прежние, меняется только порядок.

    scope "lemma" — только формы, у которых лемма общая с лидером; "all" — все кандидаты."""
    if bonus <= 0 or len(dist) < 2 or not c1 or not available():
        return dist
    lead = dist[0][0]
    if scope == "lemma":
        idx = [i for i, (w, _p) in enumerate(dist) if i == 0 or same_lemma(lead, w)]
    else:
        idx = list(range(len(dist)))
    if len(idx) < 2:
        return dist
    scored = sorted(idx, key=lambda i: (-dist[i][1] * (1 + bonus * compat(c1, dist[i][0])), i))
    if scored == idx:
        return dist
    out = list(dist)
    for slot, i in zip(idx, scored):
        out[slot] = dist[i]
    return out
