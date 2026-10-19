import unittest

from halfword import morph
from halfword.model import BaseModel, Predictor


@unittest.skipUnless(morph.available(), "нужен pymorphy3")
class MorphTest(unittest.TestCase):
    def test_preposition_case(self):
        self.assertEqual(morph.compat("к", "дому"), 1)
        self.assertEqual(morph.compat("к", "домом"), -1)
        self.assertEqual(morph.compat("без", "книги"), 1)
        self.assertEqual(morph.compat("без", "книге"), -1)

    def test_adjective_agreement(self):
        self.assertEqual(morph.compat("новый", "дом"), 1)
        self.assertEqual(morph.compat("новая", "дом"), -1)
        self.assertEqual(morph.compat("новая", "книга"), 1)
        self.assertEqual(morph.compat("новые", "книги"), 1)

    def test_unknown_context(self):
        self.assertEqual(morph.compat("вчера", "дом"), 0)

    def test_same_lemma(self):
        self.assertTrue(morph.same_lemma("дому", "дом"))
        self.assertFalse(morph.same_lemma("дом", "дом"))
        self.assertFalse(morph.same_lemma("дом", "сад"))

    def test_rerank_prefers_agreeing_form(self):
        dist = [("дом", 0.5), ("дому", 0.45), ("сад", 0.3)]
        self.assertEqual(morph.rerank(dist, "к", 0.5)[0][0], "дому")
        self.assertEqual(morph.rerank(dist, "к", 0.0), dist)  # бонус 0 — выключено
        self.assertEqual(morph.rerank(dist, "", 0.5), dist)   # нет левого слова
        self.assertEqual(morph.rerank(dist, "к", 0.5)[1:], [("дом", 0.5), ("сад", 0.3)])

    def test_rerank_keeps_other_lemmas(self):
        dist = [("сад", 0.5), ("дому", 0.45), ("дом", 0.4)]
        # лидер «сад» из другой леммы: «дому» и «дом» между собой, «сад» остаётся на своём месте
        out = morph.rerank(dist, "к", 0.5, "lemma")
        self.assertEqual(out[0][0], "сад")


class FlagOffTest(unittest.TestCase):
    def test_default_off(self):
        base = BaseModel.train([("к дому к дому к дому к дому", 1.0)])
        p = Predictor(base)
        self.assertEqual(p.morph_bonus, 0.0)


if __name__ == "__main__":
    unittest.main()
