"""Сравнение n-грамм, LLM и их связки на отложенных заметках.

python -X utf8 -m halfword.evaluate_llm [позиций] [модель.gguf]
Берёт случайные места в каждой 10-й заметке (после пробела и внутри слова), спрашивает обе модели
и считает: сколько раз подсказка была верной и сколько символов она сэкономила бы.
"""
from __future__ import annotations

import random
import re
import sys
import time

from . import llm as L
from .evaluate import split_corpus
from .model import WORD_CH, BaseModel, Predictor, Suggestion, UserModel, parse_context

THRESHOLDS = (0.15, 0.2, 0.25, 0.3, 0.35, 0.45)
WORD_START = re.compile(rf"(?<= )[{WORD_CH}]")


def correct(insert: str, truth: str, whole_word: bool) -> bool:
    if not insert or not truth.startswith(insert):
        return False
    nxt = truth[len(insert):len(insert) + 1]
    return not whole_word or nxt == "" or not re.match(rf"[{WORD_CH}]", nxt)


def sample_positions(texts: list[str], n: int, seed: int = 7):
    rnd = random.Random(seed)
    lines = [ln for t in texts for ln in t.split("\n") if len(ln) > 60 and len(ln.split()) > 8]
    out = []
    while len(out) < n:
        ln = rnd.choice(lines)
        starts = [m.start() for m in WORD_START.finditer(ln) if m.start() > 20]
        if not starts:
            continue
        s = rnd.choice(starts)
        mid = len(out) % 2 == 1
        pos = s + rnd.randint(1, 3) if mid else s
        if pos >= len(ln):
            continue
        out.append((ln[:pos], ln[pos:]))
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    model = sys.argv[2] if len(sys.argv) > 2 else "gemma-3-1b-pt-q4_k_m.gguf"
    train, test = split_corpus()
    pred = Predictor(BaseModel.train(train), UserModel())
    positions = sample_positions(test, n)

    captured = {}
    orig = L.build_insert

    def capture(tokens, lead, *a, **kw):
        captured["tokens"], captured["lead"], captured["ended"] = list(tokens), lead, a[-1] if a else False
        return orig(tokens, lead, *a, **kw)

    L.build_insert = capture
    eng = L.LLM(model, port=8795)
    eng.start()
    while not eng.ready:
        time.sleep(0.2)
    rows = []
    t0 = time.time()
    for text, truth in positions:
        prefix = parse_context(text)[2]
        captured.clear()
        eng._complete(L.Job("quick", 0, text, prefix, None, "", min(THRESHOLDS), 4))
        ng = pred.suggest(text)
        rows.append((text, truth, prefix, ng, dict(captured)))
    eng.stop()
    print(f"{len(rows)} позиций, LLM {time.time() - t0:.0f} с")

    def ng_score():
        ok = saved = shown = 0
        for _t, truth, _p, ng, _c in rows:
            if ng:
                shown += 1
                if correct(ng.insert, truth, ng.whole_word):
                    ok += 1
                    saved += len(ng.insert)
        return shown, ok, saved

    sh, ok, sv = ng_score()
    print(f"{'n-граммы':18} показов {sh:3}  верных {ok:3} ({ok / max(sh, 1):4.0%})  символов {sv:4}")
    for th in THRESHOLDS:
        lsh = lok = lsv = msh = mok = msv = 0
        for _t, truth, _p, ng, cap in rows:
            ls = None
            if cap.get("tokens") is not None:
                b = orig(cap["tokens"], cap["lead"], th, 4, cap["ended"])
                if b:
                    ls = Suggestion(insert=b[0], word="", confidence=b[1], level="llm", whole_word=True)
            if ls:
                lsh += 1
                if correct(ls.insert, truth, True):
                    lok += 1
                    lsv += len(ls.insert)
            m = ng if not ls else ls if not ng else (
                ls if ls.insert.startswith(ng.insert) else ng if ng.insert.startswith(ls.insert)
                else ls if ls.confidence > ng.confidence else ng)
            if m:
                msh += 1
                if correct(m.insert, truth, m.whole_word):
                    mok += 1
                    msv += len(m.insert)
        print(f"LLM ≥{th:<4}          показов {lsh:3}  верных {lok:3} ({lok / max(lsh, 1):4.0%})  символов {lsv:4}"
              f"   | связка: показов {msh:3} верных {mok:3} ({mok / max(msh, 1):4.0%}) символов {msv:4}")


if __name__ == "__main__":
    main()
