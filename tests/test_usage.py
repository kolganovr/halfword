"""Журнал подсказок: исходы, расхождения, правки после принятия."""
import tempfile
import unittest
from pathlib import Path

from halfword import usage as U


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 0.1
        return self.t


def tracker():
    return U.Usage(None, clock=Clock())


def type_(u, text):
    for ch in text:
        u.char(ch)


class PlaceTest(unittest.TestCase):
    def test_places(self):
        self.assertEqual(U.place("Нужно сделать докум"), "word")
        self.assertEqual(U.place("Нужно сделать "), "space")
        self.assertEqual(U.place("Готово. "), "sent")
        self.assertEqual(U.place("Готово.\n"), "line")
        self.assertEqual(U.place(""), "line")
        self.assertEqual(U.place("Итак,"), "punct")


class DiffKindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(U.diff_kind("пошли домой", "пошёл "), "form")
        self.assertEqual(U.diff_kind("пошли домой", "сидим "), "word")
        self.assertEqual(U.diff_kind("мы пошли", "мы, "), "punct")
        self.assertEqual(U.diff_kind("мы пошли", "мы."), "short")
        self.assertEqual(U.diff_kind("мы пошли", "мы\n"), "short")
        self.assertEqual(U.diff_kind("Мы пошли", "мы пошли"), "case")


class EpisodeTest(unittest.TestCase):
    def test_accept_then_kept(self):
        u = tracker()
        u.shown("Нужно сделать докум", "ент", "ngram", "notepad.exe")
        u.accept("ент ")
        self.assertEqual(u.rows, [])  # ждём, поправит ли
        type_(u, "и отправить его сегодня")
        r = u.rows[-1]
        self.assertEqual((r["out"], r["fix"], r["src"], r["pos"], r["acc"]), ("accepted", "kept", "ngram", "word", 4))

    def test_typed_by_hand(self):
        u = tracker()
        u.shown("Нужно сделать докум", "ент", "ngram", "x.exe")
        type_(u, "е")
        u.shown("Нужно сделать докуме", "нт", "ngram", "x.exe")  # та же подсказка, короче
        type_(u, "нт")
        self.assertEqual(len(u.rows), 1)
        self.assertEqual((u.rows[0]["out"], u.rows[0]["match"]), ("typed", 3))

    def test_diverged_form_after_matching_word(self):
        u = tracker()
        u.shown("Ну всё. ", "Мы пошли домой.", "long", "telegram.exe", variants=3)
        type_(u, "Мы пошёл ")
        r = u.rows[0]
        self.assertEqual((r["out"], r["miss"], r["match"], r["pos"], r["var"]), ("diverged", "form", 6, "sent", 3))

    def test_typo_recovered(self):
        u = tracker()
        u.shown("Ну ", "пошли", "llm", "x.exe")
        type_(u, "пл")
        u.bs()
        type_(u, "ошли")
        self.assertEqual(u.rows[0]["out"], "typed")

    def test_partial_by_words(self):
        u = tracker()
        u.shown("Ну всё. ", "Мы пошли домой вместе.", "long", "x.exe")
        u.accept("Мы")
        u.shown("Ну всё. Мы", " пошли домой вместе.", "long", "x.exe")
        u.accept(" пошли")
        type_(u, " домой,")
        r = u.rows[0]
        self.assertEqual((r["out"], r["miss"], r["acc"]), ("partial", "punct", 8))

    def test_growth_and_extension_same_episode(self):
        u = tracker()
        u.shown("Ну ", "мы", "ngram", "x.exe")
        u.shown("Ну ", "мы пошли", "long", "x.exe")
        u.shown("Ну ", "мы пошли домой", "long", "x.exe")
        u.accept("мы пошли домой ")
        u.reset("mouse")
        r = u.rows[0]
        self.assertEqual((len(u.rows), r["src"], r.get("src0"), r["words"]), (1, "long", "ngram", 1))

    def test_variant_cycle(self):
        u = tracker()
        u.shown("Ну ", "мы пошли", "long", "x.exe", variants=3)
        u.cycle()
        u.shown("Ну ", "давай уже", "long", "x.exe", variants=3)
        u.accept("давай уже ")
        u.reset("window")
        r = u.rows[0]
        self.assertEqual((len(u.rows), r["out"], r["cyc"]), (1, "accepted", 1))

    def test_dismiss_reset_erase(self):
        u = tracker()
        u.shown("Ну ", "мы", "ngram", "x.exe")
        u.dismiss()
        u.shown("Ну ", "мы", "ngram", "x.exe")
        u.reset("nav")
        u.shown("Ну ", "мы", "ngram", "x.exe")
        u.bs()
        self.assertEqual([r["out"] for r in u.rows], ["dismissed", "lost", "erased"])

    def test_fix_cut_tail(self):
        u = tracker()
        u.shown("Ну всё. ", "Мы пошли домой вместе.", "long", "x.exe")
        u.accept("Мы пошли домой вместе. ")
        for _ in range(9):
            u.bs()  # → «Мы пошли домой»
        type_(u, ". Потом мы пошли ")
        r = u.rows[-1]
        self.assertEqual((r["fix"], r["fix_kind"], r["fix_del"]), ("cut", "short", 9))

    def test_fix_edit_form(self):
        u = tracker()
        u.shown("Ну ", "пошли домой", "long", "x.exe")
        u.accept("пошли домой ")
        for _ in range(3):
            u.bs()  # «пошли до»
        type_(u, "ма и там поужинали ")
        r = u.rows[-1]
        self.assertEqual((r["fix"], r["fix_kind"], r["fix_del"]), ("edit", "form", 3))

    def test_fix_ctrl_backspace(self):
        u = tracker()
        u.shown("Ну ", "пошли домой", "long", "x.exe")
        u.accept("пошли домой ")
        u.reset("ctrl-bs")
        self.assertEqual(u.rows[-1]["fix"], "ctrlbs")

    def test_flush_load_report(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.jsonl"
            u = U.Usage(path, clock=Clock())
            for src in ("ngram", "llm", "long"):
                for _ in range(3):
                    u.shown("Ну ", "мы пошли", src, "x.exe")
                    type_(u, "мы пошёл ")
                    u.shown("Ну ", "мы", src, "x.exe")
                    u.accept("мы ")
                    u.reset("window")
            u.flush()
            rows = U.load(path)
            self.assertEqual(len(rows), 18)
            text = U.report(rows)
            self.assertIn("пауза ✦✦", text)
            self.assertIn("форма слова", text)


if __name__ == "__main__":
    unittest.main()
