"""Склейка журнала подсказок с App: события хука → usage.jsonl, без окна и без набора в систему."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from halfword import app as A
from halfword import overlay as O
from halfword import winhook as W
from halfword.model import BaseModel, Predictor, UserModel
from halfword.stats import Stats
from halfword.usage import Usage, load


def make_app(d: Path):
    me = A.App.__new__(A.App)
    me.cfg = SimpleNamespace(blacklist=[], learn=False, context_read_chars=0, llm_after_enter=True,
                             min_insert_chars=2, fast_typing_ms=0, fast_min_insert=4)  # 0 — без «быстрого набора»
    me.predictor = Predictor(BaseModel.train(["нужно сделать документ сегодня"] * 5), UserModel())
    me.predictor.max_extra_words = 2  # подсказка из нескольких слов: журнал различает принятое и набранное
    me.stats = Stats(d / "stats.json")
    me.usage = Usage(d / "usage.jsonl")
    me.shared = W.Shared(enabled=True, shown_seq=0)
    me.overlay = SimpleNamespace(show=lambda *a, **k: None, hide=lambda: None)
    me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "",
                               locate=lambda h: (100, 100, 20, "test"))
    me.llm = SimpleNamespace(running=True)
    me.hook = SimpleNamespace(release_held=lambda force=False: None)
    me.key_hint = "Tab"
    me.buf, me.buf_off, me.fg, me.exe = "", 0, None, ""
    me.history, me.shown_id = {}, 0
    me.pending_reload = me.sync_due = False
    me.sync_ok, me.sync_miss = True, 0
    me.blocked = me.password = me.dismissed = me.partial = False
    me.need_check = False
    me.suggestion, me.last_seq, me.anchor = None, 0, (100, 100, 20, 0)
    me.cw_ratio, me.llm_res, me.variants, me.vi = 0.5, None, [], 0
    me.last_key = 0.0
    return me


class AppUsageTest(unittest.TestCase):
    def _type(self, me, text):
        for ch in text:
            me._handle("char", ch)
            me.anchor = (100, 100, 20, me._end())
            me._show_now()

    def test_accept_and_typed_by_hand(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(A, "send_text"):
            me = make_app(Path(d))
            me._handle("char", "x")  # фиксирует активное окно, дальше оно не меняется
            me.buf = ""
            self._type(me, "нужно сделать докум")
            self.assertEqual(me.suggestion.insert, "ент сегодня")
            me._handle("accept")
            self._type(me, "и завтра, а потом ещё раз.")
            self._type(me, " Нужно сделать док")
            self._type(me, "умент сегодня")
            me._handle("reset", "mouse")
            me.usage.flush()
            rows = load(Path(d) / "usage.jsonl")
            # подсказка, которую n-граммы продлевали по ходу набора, — одна строка с места первого показа
            outs = [(r["out"], r.get("fix"), r["acc"], r["pos"]) for r in rows]
            self.assertEqual(outs, [("accepted", "kept", len("ент сегодня "), "word"), ("typed", None, 0, "sent")])
            self.assertGreater(rows[0]["match"] - rows[0]["acc"], len(" сделать докум"))  # до Tab печатал руками


class FixSuggestionTest(unittest.TestCase):
    """Исправление опечатки в начале слова (level fix): плашка, принятие, журнал, фильтр быстрого набора."""
    CORPUS = ["Кортавель готов. Кортавели на визу. Кортавелей много. Кортавеля нет.",
              "Поездка в Казань. Поездка в Казань скоро. Поездка в Казань весной."] * 6

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.me = make_app(Path(self.tmp.name))
        p = self.me.predictor = Predictor(BaseModel.train(self.CORPUS), UserModel())
        p.typo_penalty, p.typo_min_count = 0.9, 5  # корпус теста мал
        self.shown = []
        self.me.overlay = SimpleNamespace(show=lambda *a, **k: self.shown.append(a), hide=lambda: None)
        self.me._handle("char", "x")  # фиксирует активное окно
        self.me._reset_buf()
        self.me.buf_off = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _type(self, text):
        for ch in text:
            self.me._handle("char", ch)
            self.me.anchor = (100, 100, 20, self.me._end())
            self.me._show_now()

    def test_overlay_shows_whole_word_with_sign(self):
        self._type("Скоро кротавель")
        s = self.me.suggestion
        self.assertEqual((s.level, s.replace, s.insert), ("fix", len("кротавель"), "кортавель"))
        text, *_rest, hint = self.shown[-1][0], self.shown[-1][-1]
        self.assertEqual(text, "кортавель")
        self.assertEqual(O.parse_hint(hint), ("Tab", "✎", ""))

    def test_accept_erases_prefix_and_adds_space(self):
        calls = []
        with mock.patch.object(A, "send_text", side_effect=lambda t: calls.append(("text", t))),                 mock.patch.object(A, "send_key", side_effect=lambda vk, **k: calls.append(("key", vk))):
            self._type("Скоро кротавель")
            self.me._handle("accept")
        self.assertEqual(calls, [("key", A.VK_BACK)] * len("кротавель") + [("text", "кортавель ")])
        self.assertEqual(self.me.buf, "Скоро кортавель ")
        self.assertIsNone(self.me.suggestion)

    def test_case_follows_typed(self):
        with mock.patch.object(A, "send_text") as st, mock.patch.object(A, "send_key"):
            self._type("Скоро Кротавель")
            self.me._handle("accept")
        st.assert_called_once_with("Кортавель ")

    def test_usage_logs_fix_level(self):
        with mock.patch.object(A, "send_text"), mock.patch.object(A, "send_key"):
            self._type("Скоро кротавел")  # на каждой букве исправление пересчитывается: один показ
            self._type("ь")
            self.me._handle("accept")
        self.me._handle("reset", "mouse")
        self.me.usage.flush()
        rows = load(Path(self.tmp.name) / "usage.jsonl")
        self.assertEqual([(r["out"], r["lvl"]) for r in rows if r["lvl"] == "fix"], [("accepted", "fix")])

    def test_unaccepted_fix_does_not_become_diverged_noise(self):
        self._type("Скоро кротавель")
        self._type(" и")  # исправление не приняли, слово закончили
        self.me._handle("reset", "mouse")
        self.me.usage.flush()
        rows = load(Path(self.tmp.name) / "usage.jsonl")
        self.assertEqual([r["out"] for r in rows if r.get("lvl") == "fix"], ["lost"])

    def test_fast_typing_filter_uses_whole_word(self):
        me = self.me
        me.cfg.fast_typing_ms = 10_000  # весь набор в тесте — «быстрый»
        me.cfg.fast_min_insert = 8
        me.gaps = [50, 50, 50]
        me.last_key = time.time()
        self._type("Скоро кротавель")
        self.assertIsNotNone(me.suggestion)  # слово из 9 букв не короче порога, хотя «хвоста» нет
        me.cfg.fast_min_insert = 12
        me.last_key = time.time()
        self.assertFalse(me._worth_showing(me.suggestion))


class BannedLLMTest(unittest.TestCase):
    """Запрещённые слова не подсказывает и LLM: и короткая подсказка, и вариант длинной."""

    def test_banned_in_llm(self):
        from halfword.llm import LLMResult
        with tempfile.TemporaryDirectory() as d:
            me = make_app(Path(d))
            me.predictor.banned = {"блин"}
            me.cmds = None
            me._refresh = lambda seq: None
            me.buf = "ну бл"
            self.assertTrue(me._banned_in(LLMResult(seq=0, text="ну бл", insert="ин какой", prob=0.9)))  # дописывает слово
            self.assertFalse(me._banned_in(LLMResult(seq=0, text="ну бл", insert="агодарю", prob=0.9)))
            self.assertFalse(me._banned_in(LLMResult(seq=0, text="блин, ну ", insert="ладно", prob=0.9)))  # набрал сам — не наше дело
            me._on_llm(LLMResult(seq=0, text="ну бл", insert="ин какой", prob=0.9))
            self.assertIsNone(me.llm_res)
            me.variants = [LLMResult(seq=0, text="ну ", insert="и ладно", prob=0.5, kind="long", variant=0)]
            me._on_long(LLMResult(seq=0, text="ну ", insert="и ладно, блин", prob=0.5, kind="long", variant=0))
            self.assertEqual(me.variants, [])
            # убрали вариант перед показанным — на плашке остаётся тот же вариант
            v = [LLMResult(seq=0, text="ну ", insert=t, prob=0.5, kind="long", variant=i)
                 for i, t in enumerate(("и ладно", "и всё", "и хватит"))]
            me.variants, me.vi = list(v), 1
            me._on_long(LLMResult(seq=0, text="ну ", insert="и ладно, блин", prob=0.5, kind="long", variant=0))
            self.assertEqual((me.variants, me.variants[me.vi]), ([v[1], v[2]], v[1]))


if __name__ == "__main__":
    unittest.main()
