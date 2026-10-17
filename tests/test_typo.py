"""Правки на расстоянии 1 и пометка вероятных опечаток на выдуманном словаре."""
import unittest

from halfword import typo
from halfword.layout import Lexicon

# слова базы: частые настоящие и редкие искажённые
UNI = {
    "привет": 500, "првиет": 4, "приве": 3,           # перестановка и усечение рядом с частым словом
    "сообщение": 300, "сообщениие": 2,                # удвоение
    "кофейня": 40, "кофейна": 30,                     # сосед есть, но не в 5 раз частее
    "ананас": 6, "ананаса": 5,
    "keyboard": 200, "keybaord": 3,
    "борщевик": 3,                                    # рядом никого
    "дом": 900, "дои": 2,                             # короче min_len: не трогаем
    "зюзя": 8, "зюза": 400,                           # слово из словаря языка, хоть и редкое
    "<num>": 9000,
}
RU_LEX = Lexicon({"привет": 5.5, "сообщение": 5.0, "кофейня": 3.5, "ананас": 3.0, "дом": 6.0, "зюзя": 2.0})
EN_LEX = Lexicon({"keyboard": 4.0})
LEXES = (RU_LEX, EN_LEX)


class Edits1Test(unittest.TestCase):
    def test_kinds(self):
        got = typo.edits1("кот")
        self.assertIn("ко", got)       # удаление
        self.assertIn("окт", got)      # перестановка
        self.assertIn("кит", got)      # замена
        self.assertIn("коты", got)     # вставка
        self.assertNotIn("кот", got)
        self.assertNotIn("тко", got)   # две правки

    def test_alphabet_follows_script(self):
        self.assertIn("cat", typo.edits1("cot"))
        self.assertTrue(all("а" <= ch <= "я" or ch == "ё" for w in typo.edits1("кот") for ch in w))
        self.assertTrue(all(ch.isascii() for w in typo.edits1("cot") for ch in w))
        self.assertIn("coя", typo.edits1("cot", typo.ALPHABET))

    def test_count(self):
        got = typo.edits1("кот")
        self.assertEqual(len(got), len(set(got)))
        self.assertTrue(all(typo.is_edit1("кот", w) for w in got))

    def test_is_edit1(self):
        for a, b in [("привет", "првиет"), ("привет", "приве"), ("привет", "привеет"), ("привет", "прибет")]:
            self.assertTrue(typo.is_edit1(a, b), (a, b))
            self.assertTrue(typo.is_edit1(b, a), (b, a))
        for a, b in [("привет", "привет"), ("привет", "првиеь"), ("привет", "прив"), ("ab", "ba" + "c")]:
            self.assertFalse(typo.is_edit1(a, b), (a, b))


class NeighborsTest(unittest.TestCase):
    def test_neighbors(self):
        self.assertIn("к", typo.neighbors("у"))
        self.assertIn("ы", typo.neighbors("в"))
        self.assertIn("s", typo.neighbors("a"))
        self.assertNotIn("a", typo.neighbors("a"))
        self.assertNotIn("п", typo.neighbors("a"))  # другой язык
        self.assertEqual(typo.neighbors("5"), "")
        self.assertEqual(typo.neighbors("К"), typo.neighbors("к"))

    def test_near_edits_are_edits(self):
        for w in ("привет", "keyboard"):
            near = typo.near_edits1(w)
            self.assertTrue(near)
            self.assertTrue(near <= typo.edits1(w))
        self.assertIn("првиет", typo.near_edits1("привет"))


class ProbableTyposTest(unittest.TestCase):
    def mark(self, ratio=5, **kw):
        return typo.probable_typos(UNI, [RU_LEX, EN_LEX], ratio, **kw)

    def test_marks_rare_neighbor_of_frequent(self):
        got = self.mark()
        self.assertTrue({"првиет", "приве", "сообщениие", "keybaord"} <= got)

    def test_real_words_stay(self):
        got = self.mark()
        for w in ("привет", "сообщение", "зюзя", "keyboard", "ананас"):
            self.assertNotIn(w, got)

    def test_ratio_is_respected(self):
        got = self.mark(5)
        self.assertNotIn("кофейна", got)       # 40/30 < 5
        self.assertNotIn("ананаса", got)       # 6/5 < 5
        self.assertIn("кофейна", self.mark(1.2))
        self.assertIn("ананаса", self.mark(1.1))
        self.assertNotIn("кофейна", self.mark(1.5))

    def test_no_neighbor_no_mark(self):
        self.assertNotIn("борщевик", self.mark())

    def test_short_and_service_words(self):
        got = self.mark()
        self.assertNotIn("дои", got)
        self.assertNotIn("<num>", got)
        self.assertIn("дои", self.mark(5, min_len=3))

    def test_off_marks_nothing(self):
        self.assertEqual(self.mark(0), set())
        self.assertEqual(self.mark(-1), set())
        self.assertFalse(typo.is_probable_typo("првиет", UNI, [RU_LEX], 0))

    def test_frequent_personal_word_stays(self):
        self.assertIn("првиет", self.mark(user_uni={"првиет": typo.USER_MIN - 1}))
        got = self.mark(user_uni={"првиет": typo.USER_MIN, "приве": 1})
        self.assertNotIn("првиет", got)
        self.assertIn("приве", got)
        self.assertNotIn("првиет", self.mark(user_uni={"првиет": 2}, user_min=2))

    def test_is_probable_typo_matches_set(self):
        got = self.mark()
        for w in UNI:
            self.assertEqual(typo.is_probable_typo(w, UNI, [RU_LEX, EN_LEX], 5), w in got, w)


if __name__ == "__main__":
    unittest.main()
