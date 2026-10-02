"""Офлайн-симуляция: «печатаем» отложенные заметки и считаем экономию нажатий при разных порогах.

python -X utf8 -m halfword.evaluate [макс_символов]
"""
from __future__ import annotations

import sys
import time

from . import config as C
from .corpus import read_text_file, text_files
from .model import BaseModel, Predictor, UserModel

SEP = " ,.;:!?)\n"


def split_corpus(holdout_every: int = 10):
    files = [f for _root, f in text_files(C.text_roots(C.Config.load()))]
    train, test = [], []
    for i, f in enumerate(files):
        (test if i % holdout_every == 0 else train).append(f)
    return [read_text_file(f) for f in train], [read_text_file(f) for f in test]


def simulate(pred: Predictor, text: str, learn: bool = True) -> dict:
    shown = accepted = wrong = saved = 0
    last_wrong = None
    buf = ""
    i = 0
    n = len(text)
    while i < n:
        s = pred.suggest(buf) if buf.strip() else None
        if s:
            rest = text[i:]
            tail = rest[len(s.insert):len(s.insert) + 1]
            ok = rest.startswith(s.insert) and (not s.whole_word or tail == "" or tail in SEP)
            if ok:
                accepted += 1
                shown += 1
                add = s.insert + (" " if s.whole_word and tail == " " else "")
                saved += len(add) - 1  # минус нажатие Tab
                for ch in add:
                    if ch in SEP and learn:
                        pred.learn_from(buf)
                    buf += ch
                i += len(add)
                last_wrong = None
                continue
            if s.insert != last_wrong:
                wrong += 1
                shown += 1
                last_wrong = s.insert
        ch = text[i]
        if ch == "\n":
            if learn:
                pred.learn_from(buf)
            buf = ""
        else:
            if ch in SEP and learn:
                pred.learn_from(buf)
            buf = (buf + ch)[-400:]
        i += 1
    return {"chars": n, "shown": shown, "accepted": accepted, "wrong": wrong, "saved": saved}


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000
    t0 = time.time()
    train, test = split_corpus()
    base = BaseModel.train(train)
    text = "\n".join(test)[:limit]
    print(f"обучение {time.time() - t0:.0f} с; тестовый текст {len(text):,} символов")
    grid = [
        ("текущие", dict()),
        ("смелее", dict(min_conf_word=0.25, min_conf_next=0.25, min_support=2, short_penalty=0.05)),
        ("ещё смелее", dict(min_conf_word=0.20, min_conf_next=0.15, min_support=2, short_penalty=0.04)),
        ("максимум", dict(min_conf_word=0.12, min_conf_next=0.10, min_support=1, short_penalty=0.03)),
    ]
    cfg = C.Config.load()
    for name, params in grid:
        p = Predictor(base, UserModel())
        for k in C.PREDICTOR_KEYS:  # «текущие» — пороги из config.json, как в работающем приложении
            setattr(p, k, getattr(cfg, k))
        for k, v in params.items():
            setattr(p, k, v)
        t1 = time.time()
        r = simulate(p, text)
        prec = r["accepted"] / r["shown"] if r["shown"] else 0
        print(f"{name:12} сэкономлено {r['saved'] / r['chars']:6.1%} нажатий | показов {r['shown']:5} "
              f"верных {prec:5.1%} | {time.time() - t1:.0f} с")


if __name__ == "__main__":
    main()
