"""Точность подсказок: шум следующего слова, кэш документа, раскладка, сниппеты, разучивание."""
import time
import unittest

from halfword.model import BaseModel, Predictor, UserModel

CORPUS = ["привет как дела у тебя"] * 10 + ["привет мир"] * 3 + ["сегодня хорошая погода"] * 6 + \
         ["for example this works"] * 6 + ["документ готов к отправке"] * 4


def pred(**kw) -> Predictor:
    p = Predictor(BaseModel.train(CORPUS), UserModel())
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class MinInsertTest(unittest.TestCase):
    def test_short_completion_hidden(self):
        p = pred(min_conf_word=0.0, short_penalty=0.0)
        self.assertIsNotNone(p.suggest("сегодня хорошая пог"))  # «ода» — 3 символа
        p.min_insert = 4
        self.assertIsNone(p.suggest("сегодня хорошая пог"))

    def test_next_word_needs_trigram(self):
        p = pred(min_conf_next=0.0, min_support=1)
        self.assertIsNotNone(p.suggest("хорошая "))  # опора только в биграмме «хорошая → погода»
        p.next_min_tri = 50
        self.assertIsNone(p.suggest("хорошая "))


class DocCacheTest(unittest.TestCase):
    def test_word_from_field_wins(self):
        p = pred(min_conf_word=0.3, short_penalty=0.0)
        text = "Квазистабильность важна. Квазистабильность системы. Про кв"
        self.assertIsNone(p.suggest(text))
        p.doc_weight = 3
        s = p.suggest(text)
        self.assertIsNotNone(s)
        self.assertEqual(s.word.lower(), "квазистабильность")

    def test_prefix_word_not_counted(self):
        p = pred(doc_weight=3)
        p.suggest("абвгд")
        self.assertNotIn("абвгд", p._doc_uni)


class LayoutTest(unittest.TestCase):
    def test_latin_to_cyrillic(self):
        p = pred(layout_fix=True)
        s = p.suggest("ghbdtn")
        self.assertIsNotNone(s)
        self.assertEqual((s.insert, s.replace, s.level), ("привет", 6, "layout"))

    def test_cyrillic_to_latin(self):
        s = pred(layout_fix=True).suggest("ащк")
        self.assertEqual((s.insert, s.replace), ("for", 3))

    def test_real_prefix_untouched(self):
        p = pred(layout_fix=True)
        s = p.suggest("приве")
        self.assertTrue(s is None or s.level != "layout")
        s = p.suggest("exa")
        self.assertTrue(s is None or s.level != "layout")

    def test_off_by_default(self):
        s = pred().suggest("ghbdtn")
        self.assertTrue(s is None or s.level != "layout")


class SnippetTest(unittest.TestCase):
    def test_snippet_replaces_word(self):
        p = pred(snippets={"спс": "спасибо большое"})
        s = p.suggest("ну спс")
        self.assertEqual((s.insert, s.replace, s.level), ("спасибо большое", 3, "snippet"))
        s = p.suggest("ну сп")
        self.assertTrue(s is None or s.level != "snippet")


class UnlearnDecayTest(unittest.TestCase):
    def test_unlearn_reverts_learn(self):
        p = pred()
        got = p.learn_from("сегодня превет")
        self.assertEqual(got[2], "превет")
        self.assertEqual(p.user.uni["превет"], 1)
        p.user.unlearn(*got)
        self.assertNotIn("превет", p.user.uni)
        self.assertFalse(p.user.bi.get(got[1]))

    def test_decay_halves_after_half_life(self):
        u = UserModel()
        u.learn("<s>", "<s>", "слово")
        u.uni["слово"] = 10
        now = time.time()
        u.decay(now)              # первый раз только ставит отметку
        self.assertEqual(u.uni["слово"], 10)
        u.decay(now + 365 * 86400)
        self.assertAlmostEqual(u.uni["слово"], 5, places=2)
        self.assertAlmostEqual(u.bi["<s>"]["слово"], 0.5, places=2)  # 1 × 0.5 ≥ 0.3 — остаётся
        u.decay(now + 2 * 365 * 86400)
        self.assertNotIn("<s>", u.bi)  # 0.25 < 0.3 — выброшено


class AppGlueTest(unittest.TestCase):
    """Склейка в App: замена слова по Tab, разучивание стёртого, быстрый набор."""

    def make(self, d):
        from pathlib import Path
        from tests.test_app_usage import make_app
        me = make_app(Path(d))
        me.predictor = pred(layout_fix=True)
        me._handle("char", "x")
        me.buf = ""
        return me

    def test_layout_accept_erases_and_switches(self):
        import tempfile
        from unittest import mock
        from halfword import app as A
        with tempfile.TemporaryDirectory() as d, mock.patch.object(A, "send_text") as st, \
                mock.patch.object(A, "send_key") as sk, mock.patch.object(A, "switch_layout") as sw:
            me = self.make(d)
            for ch in "ghbdtn":
                me._handle("char", ch)
            me.anchor = (100, 100, 20, me._end())
            me._show_now()
            self.assertEqual(me.suggestion.level, "layout")
            me._handle("accept")
            self.assertEqual(sk.call_count, 6)          # 6 × Backspace
            st.assert_called_once_with("привет ")
            self.assertEqual(me.buf, "привет ")
            sw.assert_called_once()
            self.assertTrue(sw.call_args[0][1])          # на русскую

    def test_erased_word_unlearned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            me = self.make(d)
            me.cfg.learn = True
            for ch in "превет ":
                me._handle("char", ch)
            self.assertEqual(me.predictor.user.uni["превет"], 1)
            me._handle("bs")                              # стёр пробел — ещё не исправление
            self.assertEqual(me.predictor.user.uni["превет"], 1)
            me._handle("bs")                              # залез в слово
            self.assertNotIn("превет", me.predictor.user.uni)

    def test_fast_typing_hides_short_and_next_word(self):
        import tempfile
        from halfword.model import Suggestion
        with tempfile.TemporaryDirectory() as d:
            me = self.make(d)
            me.cfg.fast_typing_ms = 180
            me.gaps, me.last_key = [90, 100, 110], time.time()
            me.buf = "сегодня хорошая пог"
            self.assertFalse(me._worth_showing(Suggestion("ода", "погода", 0.9, "tri")))     # 3 < 4
            self.assertTrue(me._worth_showing(Suggestion("ода хорошая", "погода", 0.9, "tri")))
            me.buf = "сегодня "
            self.assertFalse(me._worth_showing(Suggestion("хорошая", "хорошая", 0.9, "tri")))  # следующее слово
            me.gaps = [300, 320, 310]
            self.assertTrue(me._worth_showing(Suggestion("хорошая", "хорошая", 0.9, "tri")))
