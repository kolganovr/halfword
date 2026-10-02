"""Режим паузы: стоп-правила, ветки, окно промпта, шапка, приём по словам, варианты."""
import unittest
from types import SimpleNamespace

from halfword import app as A
from halfword.llm import LLMResult, branch_alternatives, lead_grammar, long_cut, prompt_window
from halfword.winhook import clean_title


class LongCutTest(unittest.TestCase):
    def test_stops_at_sentence_end(self):
        toks = [(" Но", .1), (" есть", .5), (" проблема", .4), (".", .6), (" Я", .3), (" живу", .5)]
        ins, stop, _lp, k = long_cut(toks, " ")
        self.assertEqual(ins, "Но есть проблема.")
        self.assertTrue(stop)
        self.assertEqual(k, 4)

    def test_only_finished_words_while_streaming(self):
        ins, stop, _lp, _k = long_cut([(" Но", .1), (" ес", .5)], " ")
        self.assertEqual((ins, stop), ("Но", False))

    def test_ended_takes_last_word(self):
        self.assertEqual(long_cut([(" Но", .1), (" есть", .5)], " ", ended=True)[0], "Но есть")

    def test_mid_word_lead(self):
        toks = [(" дела", .9), (",", .7), (" как", .5), (" ты", .4), ("?", .8), (" Я", .5)]
        self.assertEqual(long_cut(toks, " де")[0], "ла, как ты?")

    def test_max_words(self):
        toks = [(f" w{i}", .9) for i in range(10)]
        ins, stop, _lp, _k = long_cut(toks, " ", max_words=3)
        self.assertEqual((ins, stop), ("w0 w1 w2", True))

    def test_low_confidence_cut_from_third_word(self):
        toks = [(" а", .01), (" б", .5), (" в", .01), (" г", .5)]
        self.assertEqual(long_cut(toks, " ", min_tok_p=.05)[0], "а б")  # первое слово может быть маловероятным
        self.assertEqual(long_cut(toks, " ", min_tok_p=0, ended=True)[0], "а б в г")

    def test_repeat_trimmed(self):
        toks = [(f" {w}", .5) for w in "я не могу сказать что я не могу сказать".split()] + [(" x", .5)]
        ins, stop, _lp, _k = long_cut(toks, " ")
        self.assertEqual((ins, stop), ("я не могу сказать что", True))

    def test_line_start_grammar(self):
        self.assertEqual(lead_grammar(""), r"root ::= [^\n ] [^\n]*")


class BranchTest(unittest.TestCase):
    def test_alternatives(self):
        tops = [(" Но", .3), (" Я", .2), (" Н", .1), (" ,", .1), (" я", .05), (" В", .05)]
        alts = branch_alternatives(tops, " Но", " ", 2)
        self.assertEqual([a for a, _p in alts], [" Я", " В"])  # без жадного, обрубка, пунктуации и дублей

    def test_alternatives_respect_lead(self):
        tops = [(" дела", .5), (" день", .2), (" как", .2), (" делать", .1)]
        self.assertEqual([a for a, _ in branch_alternatives(tops, " дела", " де", 3)], [" день", " делать"])


class PromptWindowTest(unittest.TestCase):
    def test_quick_keeps_short_window_and_pause_grows_it(self):
        t = "слово " * 300
        w1, st = prompt_window(t, None, keep=350)
        self.assertLessEqual(len(w1), 350)
        w2, st2 = prompt_window(t + "ещё", st, keep=1000, grow=True)
        self.assertGreater(len(w2), 900)
        w3, _ = prompt_window(t + "ещё два", st2, keep=350)  # быстрый режим не сужает выросшее окно
        self.assertTrue(w3.startswith(w2[:40]))

    def test_rebases_past_max(self):
        t = "слово " * 500
        _w, st = prompt_window(t[:1000], None, keep=1000)
        w, _ = prompt_window(t, st, max_chars=2000, keep=350)
        self.assertLessEqual(len(w), 350)


class TitleTest(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(clean_title("msedge.exe", "Почта — Личный — Microsoft​ Edge"), "Почта — Личный")
        self.assertEqual(clean_title("telegram.exe", "Анна"), "Telegram — Анна")
        self.assertEqual(clean_title("telegram.exe", "Telegram"), "Telegram")
        self.assertEqual(clean_title("notepad.exe", "заметка.txt - Блокнот"), "Блокнот — заметка.txt")
        self.assertEqual(clean_title("obsidian.exe", "00_HOME - Notes - Obsidian v1.8.9"),
                         "Obsidian — 00_HOME - Notes")


class AppLogicTest(unittest.TestCase):
    def _app(self, buf, variants, vi=0):
        return SimpleNamespace(buf=buf, buf_off=0, variants=variants, vi=vi, shared=SimpleNamespace(variants=0))

    def test_accept_chunk(self):
        self.assertEqual(A.accept_chunk(" Но есть"), " Но")
        self.assertEqual(A.accept_chunk("ла, как ты?"), "ла,")
        self.assertEqual(A.accept_chunk("слово"), "слово")

    def test_long_suggestion_follows_typing_and_drops_mismatches(self):
        v0 = LLMResult(0, "Привет. ", "Но есть проблема.", -1.0, "long", 0)
        v1 = LLMResult(0, "Привет. ", "Я живу в Казани.", -1.2, "long", 1)
        me = self._app("Привет. Н", [v0, v1], vi=1)
        s = A.App._long_suggestion(me)
        self.assertEqual(s.insert, "о есть проблема.")
        self.assertEqual((len(me.variants), me.vi, me.shared.variants), (1, 0, 1))

    def test_long_suggestion_none_when_typed_through(self):
        v0 = LLMResult(0, "a ", "bc", -1.0, "long", 0)
        self.assertIsNone(A.App._long_suggestion(self._app("a bc", [v0])))

    def test_on_long_replaces_progress_and_resets_on_new_text(self):
        me = SimpleNamespace(buf="Привет. ", buf_off=0, variants=[], vi=0, cfg=SimpleNamespace(llm_rank=False),
                             last_seq=5, _refresh=lambda seq: None)
        A.App._on_long(me, LLMResult(5, "Привет. ", "Но", -1, "long", 0, False))
        A.App._on_long(me, LLMResult(5, "Привет. ", "Но есть", -1, "long", 0, True))
        A.App._on_long(me, LLMResult(5, "Привет. ", "Я тут", -2, "long", 1, True))
        self.assertEqual([v.insert for v in me.variants], ["Но есть", "Я тут"])
        me.buf = "Привет. Как "
        A.App._on_long(me, LLMResult(6, "Привет. Как ", "дела", -1, "long", 0, False))
        self.assertEqual([v.insert for v in me.variants], ["дела"])

    def test_rank_sorts_by_score(self):
        me = SimpleNamespace(buf="x ", buf_off=0, variants=[], vi=0, cfg=SimpleNamespace(llm_rank=True),
                             last_seq=1, _refresh=lambda seq: None)
        A.App._on_long(me, LLMResult(1, "x ", "a", -2, "long", 0))
        A.App._on_long(me, LLMResult(1, "x ", "b", -1, "long", 1))
        self.assertEqual([v.insert for v in me.variants], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
