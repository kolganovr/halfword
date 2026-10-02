"""Плашка подсказки: разбор hint, выбор палитры по теме, логика «перерисовывать ли» (без Tk-окна)."""
import unittest
from unittest import mock

from halfword import overlay as O


class ParseHintTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(O.parse_hint("Tab"), ("Tab", "", ""))
        self.assertEqual(O.parse_hint("Tab ✦"), ("Tab", "✦", ""))
        self.assertEqual(O.parse_hint("Tab ✦✦ 2/3"), ("Tab", "✦✦", "2/3"))
        self.assertEqual(O.parse_hint("Tab 1/2"), ("Tab", "", "1/2"))

    def test_garbage_falls_back_to_key(self):
        self.assertEqual(O.parse_hint(""), ("", "", ""))
        self.assertEqual(O.parse_hint("Ctrl + Space"), ("Ctrl + Space", "", ""))
        self.assertEqual(O.parse_hint("  Tab  "), ("Tab", "", ""))


class ThemeTest(unittest.TestCase):
    def test_explicit_theme_ignores_registry(self):
        boom = mock.Mock(side_effect=AssertionError("реестр не должен читаться"))
        self.assertIs(O.palette_for("dark", boom), O.DARK)
        self.assertIs(O.palette_for("light", boom), O.LIGHT)

    def test_auto_follows_registry(self):
        self.assertEqual(O.resolve_theme("auto", lambda: 0), "dark")
        self.assertEqual(O.resolve_theme("auto", lambda: 1), "light")
        self.assertEqual(O.resolve_theme("auto", lambda: None), "light")

    def test_auto_default_reader_is_patchable(self):
        with mock.patch.object(O, "read_apps_use_light", return_value=0):
            self.assertIs(O.palette_for("auto"), O.DARK)

    def test_unknown_theme_is_auto(self):
        self.assertEqual(O.resolve_theme("weird", lambda: 0), "dark")

    def test_palettes_complete_and_distinct(self):
        self.assertEqual(set(O.LIGHT), set(O.DARK))
        self.assertNotEqual(O.LIGHT["bg"], O.DARK["bg"])


class NeedsUpdateTest(unittest.TestCase):
    S = ("текст", "Tab", False, 16, 100, 200, "light")

    def test_hidden_always_updates(self):
        self.assertTrue(O.needs_update(self.S, self.S, visible=False))
        self.assertTrue(O.needs_update(None, self.S, visible=True))

    def test_same_state_visible_noop(self):
        self.assertFalse(O.needs_update(self.S, tuple(self.S), visible=True))

    def test_any_field_change_updates(self):
        for i, v in enumerate(["другой", "Tab ✦", True, 20, 101, 201, "dark"]):
            new = self.S[:i] + (v,) + self.S[i + 1:]
            self.assertTrue(O.needs_update(self.S, new, visible=True), i)


if __name__ == "__main__":
    unittest.main()
