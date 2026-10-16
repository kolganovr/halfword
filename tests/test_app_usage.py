"""Склейка журнала подсказок с App: события хука → usage.jsonl, без окна и без набора в систему."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from halfword import app as A
from halfword import winhook as W
from halfword.model import BaseModel, Predictor, UserModel
from halfword.stats import Stats
from halfword.usage import Usage, load


def make_app(d: Path):
    me = A.App.__new__(A.App)
    me.cfg = SimpleNamespace(blacklist=[], learn=False, context_read_chars=0, llm_after_enter=True,
                             min_insert_chars=2, fast_typing_ms=0, fast_min_insert=4)  # 0 — без «быстрого набора»
    me.predictor = Predictor(BaseModel.train(["нужно сделать документ сегодня"] * 5), UserModel())
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
