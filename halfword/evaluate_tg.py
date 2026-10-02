"""Как взвешивать Telegram: «печатаем» свои самые свежие сообщения и считаем экономию нажатий.

python -X utf8 -m halfword.evaluate_tg [доля_отложенных] [макс_символов]

Последние 10% своих сообщений (по времени) откладываются как тест — это «как пишут сейчас». Модель учится на заметках
и более старых сообщениях с разным весом: без Telegram, без затухания, с полураспадом 1/2/4 года, с усилением.
Печатает только числа — текст сообщений не выводится (он попал бы в лог сессии).
"""
from __future__ import annotations

import sys
import time

from . import config as C
from .corpus import iter_telegram, iter_texts, telegram_messages
from .evaluate import simulate
from .model import BaseModel, Predictor, UserModel

GRID = [  # (название, полураспад лет, множитель веса; None — без Telegram)
    ("только заметки", None, None),
    ("TG без затухания", 0, 1.0),
    ("без затухания ×2", 0, 2.0),
    ("без затухания ×4", 0, 4.0),
    ("полураспад 2 года ×4", 2, 4.0),
    ("полураспад 1 год ×8", 1, 8.0),
    ("полураспад 2 года ×8", 2, 8.0),
    ("полураспад 4 года ×8", 4, 8.0),
    ("без затухания ×8", 0, 8.0),
]


def main():
    share = float(sys.argv[1]) if len(sys.argv) > 1 else 0.1
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 30_000
    msgs = telegram_messages(C.TELEGRAM_DIR)
    if not msgs:
        raise SystemExit(f"нет сообщений в {C.TELEGRAM_DIR}")
    cut = int(len(msgs) * (1 - share))
    train_msgs, test_msgs = msgs[:cut], msgs[cut:]
    text = "\n".join(t for _w, t in test_msgs)[:limit]
    notes = list(iter_texts(C.text_roots(C.Config.load())))
    print(f"сообщений: обучение {len(train_msgs):,} (до {train_msgs[-1][0]:%Y-%m}), "
          f"тест {len(test_msgs):,} (с {test_msgs[0][0]:%Y-%m}), тестовый текст {len(text):,} символов")
    for name, hl, mult in GRID:
        t0 = time.time()
        tg = [] if hl is None else list(iter_telegram(C.TELEGRAM_DIR, hl, mult, messages=train_msgs))
        eff = sum(w for _t, w in tg)
        base = BaseModel.train(notes + tg)
        p = Predictor(base, UserModel())
        for k in C.PREDICTOR_KEYS:
            setattr(p, k, getattr(C.Config(), k))
        r = simulate(p, text)
        prec = r["accepted"] / r["shown"] if r["shown"] else 0
        print(f"{name:20} сэкономлено {r['saved'] / r['chars']:6.1%} | показов {r['shown']:5} верных {prec:5.1%}"
              f" | эфф. сообщений TG {eff:7.0f} | {time.time() - t0:.0f} с", flush=True)


if __name__ == "__main__":
    main()
