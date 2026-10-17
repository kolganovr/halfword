"""Храповик стенда на выдуманном корпусе: экономия и доля верных не хуже базы tests/fixtures/bench_baseline.json.

База снимается так: python -X utf8 -m halfword.bench --synthetic --save-baseline
"""
import json
import re
import unittest
from unittest import mock

from halfword import bench
from halfword import evaluate
from halfword.model import Suggestion


class BenchRatchetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.loads(bench.FIXTURE.read_text(encoding="utf-8"))
        cfg = bench._load_cfg(synthetic=True)
        cls.res = bench.run(cfg, cls.base["split"], synthetic=True)

    def test_same_corpus(self):
        for name, m in self.res["corpora"].items():
            b = self.base["corpora"][name]
            self.assertEqual((m["crc"], m["train"]), (b["crc"], b["train"]), name)

    def test_not_worse_than_baseline(self):
        for name, m in self.res["corpora"].items():
            b = self.base["corpora"][name]
            self.assertGreaterEqual(m["saved"], b["saved"] - bench.TOL_SAVED, f"{name}: экономия нажатий")
            self.assertGreaterEqual(m["prec"], b["prec"] - bench.TOL_PREC, f"{name}: доля верных")

    def test_breakdown_adds_up(self):
        for m in self.res["corpora"].values():
            self.assertEqual(m["word_shown"] + m["next_shown"], m["shown"])

    def test_noise_zero_is_same_run(self):
        cfg = bench._load_cfg(synthetic=True)
        again = bench.run(cfg, self.base["split"], synthetic=True, noise=0.0)
        for name, m in again["corpora"].items():
            for k in ("saved", "prec", "shown", "accepted", "word_shown", "next_shown", "word_saved", "next_saved"):
                self.assertEqual(m[k], self.res["corpora"][name][k], f"{name}.{k}")
            self.assertNotIn("typo_words", m)

    def test_noise_run_has_typo_columns(self):
        cfg = bench._load_cfg(synthetic=True)
        noisy = bench.run(cfg, self.base["split"], synthetic=True, noise=0.06)
        for name, m in noisy["corpora"].items():
            self.assertGreater(m["typo_words"], 0, name)
            self.assertEqual(m["typo_words"], m["typo_avoided"] + m["fixed_ok"] + m["fixed_bad"] + m["typo_manual"], name)
            self.assertEqual(m["false_fix"], 0)  # автозамены у предиктора нет
            self.assertLess(m["saved"], self.res["corpora"][name]["saved"], name)  # опечатки стоят нажатий


class BenchUnitsTest(unittest.TestCase):
    def test_split_by_time_is_stable(self):
        msgs = bench.synthetic_messages("chat", 100)
        tr, te, sp = bench._split(msgs, None)
        self.assertEqual((len(tr), len(te)), (90, 10))
        # новые сообщения после конца выборки не сдвигают её
        more = msgs + bench.synthetic_messages("chat", 120)[100:]
        more.sort(key=lambda m: m[0])
        tr2, te2, _ = bench._split(more, sp)
        self.assertEqual((tr2, te2), (tr, te))

    def test_compare_flags_regression(self):
        m = {"saved": 10.0, "prec": 30.0, "p95_ms": 1.0, "crc": 1, "train": 5, "chars": 100}
        base = {"corpora": {"x": m}}
        self.assertFalse(bench.compare({"corpora": {"x": dict(m)}}, base)[1])
        self.assertTrue(bench.compare({"corpora": {"x": dict(m, saved=9.7)}}, base)[1])
        self.assertTrue(bench.compare({"corpora": {"x": dict(m, prec=28.5)}}, base)[1])
        self.assertTrue(bench.compare({"corpora": {"x": dict(m, p95_ms=1.5)}}, base)[1])
        self.assertFalse(bench.compare({"corpora": {"x": dict(m, p95_ms=1.1)}}, base)[1])  # шум таймера
        self.assertTrue(bench.compare({"corpora": {"x": dict(m, saved=10.3)}}, base)[2])

    def test_compare_noise_columns_only_if_in_base(self):
        m = {"saved": 10.0, "prec": 30.0, "p95_ms": 1.0, "crc": 1, "train": 5, "chars": 100}
        noisy = dict(m, fixed_ok_pct=30.0, false_fix_1k=0.2)
        # в базе колонок шума нет — ухудшение по ним не проверяется
        self.assertFalse(bench.compare({"corpora": {"x": dict(noisy, fixed_ok_pct=0.0, false_fix_1k=9.0)}},
                                       {"corpora": {"x": m}})[1])
        base = {"corpora": {"x": noisy}}
        self.assertFalse(bench.compare({"corpora": {"x": dict(noisy)}}, base)[1])
        self.assertTrue(bench.compare({"corpora": {"x": dict(noisy, fixed_ok_pct=28.0)}}, base)[1])
        self.assertTrue(bench.compare({"corpora": {"x": dict(noisy, false_fix_1k=1.0)}}, base)[1])

    def test_noise_without_base_does_not_touch_baseline(self):
        import contextlib
        import io
        buf = io.StringIO()
        before = bench.FIXTURE.read_text(encoding="utf-8")
        with contextlib.redirect_stdout(buf):
            code = bench.main(["--synthetic", "--noise", "0.03"])
        self.assertEqual(code, 0)
        self.assertIn("базы для шума", buf.getvalue())
        self.assertEqual(bench.FIXTURE.read_text(encoding="utf-8"), before)

    def test_synthetic_is_deterministic(self):
        self.assertEqual(bench.synthetic_messages("work", 50), bench.synthetic_messages("work", 50))


TAIL = re.compile(r"[^\W\d_]+$")


class FakePred:
    """Знает три опечатки: подсказка со стиранием для одной, автозамена для другой, и «портит» верное слово."""
    SUGGEST = {"првиет": "привет"}
    AUTO = {"привте": "привет", "мир": "мор", "кдо": "дом"}

    def __init__(self):
        self.learned = []

    def _tail(self, buf):
        m = TAIL.search(buf)
        return m.group() if m else ""

    def suggest(self, buf):
        w = self.SUGGEST.get(self._tail(buf))
        return Suggestion(insert=w, word=w, confidence=0.9, level="fix", replace=len(self._tail(buf))) if w else None

    def autocorrect(self, buf):
        w = self.AUTO.get(self._tail(buf))
        return Suggestion(insert=w, word=w, confidence=0.9, level="fix", replace=len(self._tail(buf))) if w else None

    def learn_from(self, buf):
        self.learned.append(buf)


class NoisySimulateTest(unittest.TestCase):
    def sim(self, text, plan, pred=None):
        with mock.patch.object(evaluate.N, "plan", return_value=plan):
            return evaluate.simulate(pred or FakePred(), text, noise=0.5)

    def test_counters_and_keystrokes(self):
        # привет мир привет мир; в 1-м «привет» опечатка исправляется подсказкой со стиранием, во 2-м автозаменой
        text = "привет мир привет мир"
        r = self.sim(text, {0: ("привет", "првиет"), 11: ("привет", "привте")})
        self.assertEqual((r["typo_words"], r["fixed_ok"], r["fixed_bad"], r["typo_manual"], r["false_fix"]),
                         (2, 2, 0, 0, 1))
        # 6 букв + Tab, «мир» 3 + пробел + возврат ложной замены (мор→мир: 2 стереть, 2 допечатать), 6 + пробел + 3
        self.assertEqual(r["saved"], len(text) - (7 + 3 + 4 + 1 + 6 + 1 + 3))
        self.assertEqual(r["words"], 4)

    def test_unfixed_typo_costs_manual_fix(self):
        r = self.sim("мир да", {0: ("мир", "мри")}, _Silent())
        self.assertEqual((r["typo_words"], r["typo_manual"], r["fixed_ok"]), (1, 1, 0))
        # «мри» 3 буквы + стереть 2 и допечатать 2 + « да» 3 нажатия
        self.assertEqual(r["saved"], 6 - (3 + 4 + 3))

    def test_autocorrect_result(self):
        r = self.sim("дом да", {0: ("дом", "кдо")})
        self.assertEqual((r["typo_words"], r["fixed_ok"], r["fixed_bad"]), (1, 1, 0))
        # цель «мир», автозамена дала «дом»: исправлено в другое слово, ручная правка с нуля
        r = self.sim("мир да", {0: ("мир", "кдо")})
        self.assertEqual((r["fixed_ok"], r["fixed_bad"]), (0, 1))
        self.assertEqual(r["saved"], 6 - (3 + 6 + 3))

    def test_clean_path_matches_old_without_noise(self):
        text = "привет друг. привет мир" + chr(10) + "привет"
        old = evaluate.simulate(_Prefix(), text)
        with mock.patch.object(evaluate.N, "plan", return_value={}):
            new = evaluate.simulate(_Prefix(), text, noise=0.5)
        for k in ("chars", "shown", "accepted", "wrong", "saved"):
            self.assertEqual(new[k], old[k], k)


class _Silent:
    def suggest(self, buf):
        return None

    def learn_from(self, buf):
        pass


class _Prefix:
    """Дописывает «привет» после трёх букв; замены нет."""

    def suggest(self, buf):
        m = TAIL.search(buf)
        w = m.group() if m else ""
        if len(w) >= 3 and "привет".startswith(w) and w != "привет":
            return Suggestion(insert="привет"[len(w):], word="привет", confidence=0.9, level="uni")
        return None

    def learn_from(self, buf):
        pass


if __name__ == "__main__":
    unittest.main()
