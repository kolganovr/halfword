"""Золотой тест: ускорения suggest не меняют подсказки. Эталон снят до ускорений на выдуманном корпусе.

Переснять эталон (только если подсказки меняются намеренно): python -X utf8 -m tests.test_golden --write
"""
import json
import random
import sys
import unittest
from pathlib import Path

from halfword import bench
from halfword.model import BaseModel, Predictor, UserModel

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "golden_suggest.json"
SYL = ["ка", "ро", "ми", "ту", "ле", "ша", "до", "ви", "на", "пе", "зо", "лу", "ти", "го", "ры"]


def _made_up_words(rng: random.Random, n: int) -> list[str]:
    return ["".join(rng.choice(SYL) for _ in range(rng.randint(2, 4))) for _ in range(n)]


def cases() -> list:
    """2000+ позиций: дописывание, следующее слово, основа; личный словарь растёт по ходу, есть запрет и кэш поля."""
    cfg = bench._load_cfg(synthetic=True)
    train, tests, _meta = bench.load_corpora(cfg, None, synthetic=True)
    base = BaseModel.train(train)
    p = bench.make_predictor(base, cfg)
    rng = random.Random(7)
    words = _made_up_words(rng, 300)
    # личный словарь: выдуманные слова и фразы из корпуса
    for i in range(1500):
        ctx = " ".join(rng.choice(words) for _ in range(rng.randint(0, 3)))
        p.learn_from((ctx + " " + rng.choice(words) + " ").strip() + " ")
        if i % 3 == 0:
            p.learn_from(rng.choice(tests["claude"].split("\n")) + " ")
    out = []
    text = tests["claude"][:1400] + "\n" + tests["telegram"][:600]
    mixed = " ".join(rng.choice(words) for _ in range(120))
    for phase, src in (("plain", text), ("made_up", mixed)):
        buf = ""
        for ch in src:
            if ch == "\n":
                buf = ""
                continue
            if not ch.isalpha() and buf:
                p.learn_from(buf)  # обучение по ходу: индекс личного словаря должен успевать
            buf += ch
            s = p.suggest(buf)
            out.append([phase, None] if s is None else
                       [phase, s.insert, s.word, round(s.confidence, 9), s.level, s.whole_word])
    p.banned = {"отчёт", "сервер"}
    p.doc_weight = 3.0
    for i in range(1, 300):
        s = p.suggest(text[:i])
        out.append(["doc", None] if s is None else
                   ["doc", s.insert, s.word, round(s.confidence, 9), s.level, s.whole_word])
    p.user.forget(words[0])
    p.user.unlearn("<s>", "<s>", words[1])
    for w in words[:40]:
        s = p.suggest("ну " + w[:3])
        out.append(["forget", None] if s is None else
                   ["forget", s.insert, s.word, round(s.confidence, 9), s.level, s.whole_word])
    return out


class GoldenTest(unittest.TestCase):
    def test_suggestions_unchanged(self):
        want = json.loads(FIXTURE.read_text(encoding="utf-8"))
        got = json.loads(json.dumps(cases(), ensure_ascii=False))
        self.assertGreater(len(want), 2000)
        self.assertEqual(len(got), len(want))
        diff = [i for i, (a, b) in enumerate(zip(got, want)) if a != b]
        self.assertEqual(diff[:10], [], f"{len(diff)} подсказок отличаются от эталона")


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.write_text(json.dumps(cases(), ensure_ascii=False), encoding="utf-8")
        print("эталон записан")
    else:
        unittest.main()
