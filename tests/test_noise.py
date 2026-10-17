"""Генератор синтетических опечаток: зерно, доля, виды ошибок, короткие слова, регистр."""
import random
import unittest

from halfword import noise as N

WORDS = ["привет", "сообщение", "проверить", "отчёт", "работа", "hello", "keyboard", "message", "вечером", "ok",
         "да", "код", "text", "сервер", "написать"]


def sample_text(n: int, seed: int = 5) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(WORDS) for _ in range(n))


class NoiseTest(unittest.TestCase):
    def test_deterministic_by_seed(self):
        text = sample_text(500)
        self.assertEqual(N.plan(text, 0.05, 7), N.plan(text, 0.05, 7))
        self.assertNotEqual(N.plan(text, 0.05, 7), N.plan(text, 0.05, 8))

    def test_zero_rate_is_empty(self):
        self.assertEqual(N.plan(sample_text(100), 0.0), {})

    def test_share_of_words(self):
        text = sample_text(4000)
        eligible = sum(1 for w in text.split() if len(w) >= N.MIN_LEN)
        for rate in (0.01, 0.03, 0.06):
            got = len(N.plan(text, rate))
            self.assertAlmostEqual(got / eligible, rate, delta=rate * 0.4, msg=str(rate))

    def test_lower_rate_is_subset(self):
        text = sample_text(2000)
        small, big = N.plan(text, 0.01), N.plan(text, 0.06)
        self.assertTrue(small)
        for pos, pair in small.items():
            self.assertEqual(big.get(pos), pair)

    def test_plan_points_at_words(self):
        text = sample_text(300)
        for pos, (clean, bad) in N.plan(text, 0.2).items():
            self.assertEqual(text[pos:pos + len(clean)], clean)
            self.assertNotEqual(clean, bad)
            self.assertTrue(text[pos + len(clean):pos + len(clean) + 1] in ("", *N.SEP))

    def test_short_words_untouched(self):
        text = "я ты он да ок мы и"
        self.assertEqual(N.plan(text, 1.0), {})
        for pos, (clean, _bad) in N.plan(sample_text(500), 1.0).items():
            self.assertGreaterEqual(len(clean), N.MIN_LEN)

    def test_error_kinds(self):
        rng = random.Random(3)
        lens, same_sub, same_swap = set(), 0, 0
        for _ in range(600):
            w = rng.choice(["привет", "сообщение", "keyboard", "message"])
            bad = N.corrupt_word(w, rng)
            self.assertNotEqual(bad, w)
            lens.add(len(bad) - len(w))
            if len(bad) == len(w):
                diff = [i for i in range(len(w)) if w[i] != bad[i]]
                if len(diff) == 2 and diff[1] == diff[0] + 1 and w[diff[0]] == bad[diff[1]] and w[diff[1]] == bad[diff[0]]:
                    same_swap += 1
                else:
                    same_sub += 1
        self.assertTrue({-1, 0, 1} <= lens, lens)
        self.assertGreater(same_sub, 0)
        self.assertGreater(same_swap, 0)

    def test_two_errors_happen(self):
        rng = random.Random(11)
        far = sum(1 for _ in range(400) if abs(len(N.corrupt_word("сообщение", rng)) - 9) == 2)
        self.assertGreater(far, 0)

    def test_neighbor_keys(self):
        self.assertIn("ы", N.neighbors("ф", "ru"))
        self.assertIn("в", N.neighbors("ы", "ru"))
        self.assertTrue({"a", "d", "w", "e", "z", "x"} <= set(N.neighbors("s", "en")))
        self.assertEqual(N.neighbors("ё", "ru"), [])

    def test_case_kept(self):
        rng = random.Random(1)
        for _ in range(100):
            title = N.corrupt_word("Привет", rng)
            self.assertTrue(title[0].isupper(), title)
            self.assertTrue(N.corrupt_word("ПРИВЕТ", rng).isupper())
            self.assertTrue(N.corrupt_word("привет", rng).islower())


if __name__ == "__main__":
    unittest.main()
