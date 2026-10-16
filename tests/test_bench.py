"""Храповик стенда на выдуманном корпусе: экономия и доля верных не хуже базы tests/fixtures/bench_baseline.json.

База снимается так: python -X utf8 -m halfword.bench --synthetic --save-baseline
"""
import json
import unittest

from halfword import bench


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

    def test_synthetic_is_deterministic(self):
        self.assertEqual(bench.synthetic_messages("work", 50), bench.synthetic_messages("work", 50))


if __name__ == "__main__":
    unittest.main()
