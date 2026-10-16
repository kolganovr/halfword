import unittest

from halfword.model import BaseModel, Predictor, UserModel, parse_context

CORPUS = ["Привет, как дела? Привет, как дела у тебя. Привет, как дела сегодня.",
          "Документ готов. Документы на визу. Документов много. Документа нет.",
          "Поездка в Казань. Поездка в Казань скоро. Поездка в Казань весной."] * 3


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = Predictor(BaseModel.train(CORPUS), UserModel())

    def test_parse_context(self):
        self.assertEqual(parse_context("Привет, как де"), ("привет", "как", "де", False, False))
        self.assertEqual(parse_context("Конец. Нов")[:3], ("<s>", "<s>", "Нов"))

    def test_complete_word_in_context(self):
        s = self.p.suggest("Привет, как де")
        self.assertIsNotNone(s)
        self.assertTrue(s.insert.startswith("ла"))

    def test_next_word_after_space(self):
        s = self.p.suggest("Поездка в ")
        self.assertIsNotNone(s)
        self.assertTrue(s.insert.startswith("Казань"))

    def test_no_next_word_without_space(self):
        self.assertIsNone(self.p.suggest("Поездка в,"))

    def test_stem(self):
        s = self.p.suggest("докум")
        self.assertEqual((s.insert, s.whole_word), ("ент", False))

    def test_case_follows_prefix(self):
        s = self.p.suggest("ПРИВ")
        self.assertEqual(s.word, "ПРИВЕТ")

    def test_user_learning_wins(self):
        p = Predictor(BaseModel.train(CORPUS), UserModel())
        for _ in range(5):
            p.learn_from("Привет, как делишки ")
        self.assertTrue(p.suggest("Привет, как де").insert.startswith("лишки"))

    def test_incremental_caches_match_fresh(self):
        """Кэши личного словаря догоняются по журналу правок — подсказки как у построенных заново."""
        import random
        rng = random.Random(3)
        words = ["дела", "делишки", "делать", "документ", "домой", "дом", "казань", "кот", "котик", "как"]
        p = Predictor(BaseModel.train(CORPUS), UserModel())
        fresh = Predictor(p.base, p.user)
        for i in range(400):
            w = rng.choice(words)
            if rng.random() < 0.8:
                p.learn_from(f"{rng.choice(words)} {w} ")
            else:
                p.user.unlearn("<s>", rng.choice(words), w)
            text = rng.choice(["Привет, как ", "Поездка в ", "", "ну "]) + rng.choice(words)[:rng.randint(0, 3)]
            if not text.strip():
                continue
            fresh._ckey = None  # заставить строить кэши заново
            a, b = p.suggest(text), fresh.suggest(text)
            self.assertEqual(a and (a.insert, a.confidence), b and (b.insert, b.confidence), (i, text))

    def test_roundtrip(self):
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        self.p.base.save(d / "b.pkl")
        u = UserModel(); u.learn("<s>", "<s>", "Тест"); u.save(d / "u.json")
        p2 = Predictor(BaseModel.load(d / "b.pkl"), UserModel.load(d / "u.json"))
        self.assertEqual(p2.suggest("докум").insert, "ент")
        self.assertEqual(p2.user.uni["тест"], 1)


if __name__ == "__main__":
    unittest.main()


class StatsTest(unittest.TestCase):
    def test_counts_and_report(self):
        import tempfile, pathlib
        from halfword.stats import Stats
        path = pathlib.Path(tempfile.mkdtemp()) / "s.json"
        st = Stats(path)
        for _ in range(10):
            st.typed()
        st.shown("ент")
        st.shown("нт")        # та же подсказка, укоротилась при наборе — не новый показ
        st.shown("ачем")
        st.accepted("ачем ")
        st.save()
        d = Stats(path)._today()
        self.assertEqual((d["typed"], d["shown"], d["accepted"], d["saved"]), (10, 2, 1, 4))
        self.assertIn("сэкономлено нажатий: 4", Stats(path).report())
