"""скользящий буфер и ответы LLM, сверка с полем (раскладка), Tab после смены окна, дообучение при приёме,
отложенные нажатия во время вставки, перезапуск упавшего сервера, атомарная базовая модель."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from halfword import app as A
from halfword import llm as L
from halfword import winhook as W
from halfword.llm import LLMResult
from halfword.model import BaseModel, Suggestion
from tests.test_app_usage import make_app


class TypedAfterTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(A.typed_after("ab cd", 0, "ab ", 0), "cd")
        self.assertIsNone(A.typed_after("ax cd", 0, "ab ", 0))

    def test_buffer_start_moved(self):
        # буфер обрезан с начала на 2 символа: ответ на старый текст всё ещё подходит
        self.assertEqual(A.typed_after("cdef", 2, "abcde", 0), "f")
        self.assertIsNone(A.typed_after("cdxf", 2, "abcde", 0))

    def test_after_reset(self):
        self.assertIsNone(A.typed_after("ab", 10, "ab", 0))  # тот же текст, но после сброса буфера


class FieldAgreesTest(unittest.TestCase):
    def test_cases(self):
        self.assertTrue(A.field_agrees("for ", "for "))
        self.assertTrue(A.field_agrees("old\nHi", "Hi"))       # Enter не стирает буфер, поле — новое сообщение
        self.assertTrue(A.field_agrees("for i", "for "))       # поле ещё не вставило последний символ
        self.assertFalse(A.field_agrees("ащк ", "for "))       # не та раскладка


class AppFixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.me = make_app(Path(self.tmp.name))
        self.me.cfg.context_read_chars = 1500
        self.me._handle("char", "x")  # фиксирует активное окно
        self.me._reset_buf()
        self.me.pending_reload = False

    def tearDown(self):
        self.tmp.cleanup()

    def test_llm_answer_survives_full_buffer(self):
        me = self.me
        me._append("я" * A.BUF_MAX)
        me._on_llm(LLMResult(1, me.buf, " привет мир", 0.9), me.buf_off)
        me._handle("char", " ")
        me._handle("char", "п")
        self.assertEqual(len(me.buf), A.BUF_MAX)
        s = me._llm_suggestion()
        self.assertIsNotNone(s)
        self.assertEqual(s.insert, "ривет мир")

    def test_wrong_layout_resynced_from_field(self):
        me = self.me
        me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "for ")
        for ch in "ащк ":
            me._handle("char", ch)
        me._sync_field()
        self.assertEqual(me.buf, "for ")

    def test_reload_after_click_takes_field_and_lagging_char(self):
        me = self.me
        me.need_check = True
        me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "Мой клас")
        me._handle("char", "с")
        self.assertTrue(me.pending_reload)
        me._sync_field()
        self.assertEqual(me.buf, "Мой клас")  # символ уже в поле — не дублируем
        me.need_check, me.buf = True, ""
        me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "Привет")
        me._handle("char", ",")
        me._handle("char", " ")
        me._sync_field()
        self.assertEqual(me.buf, "Привет, ")  # поле отстаёт на два символа — дописали их

    def test_tab_after_window_change_goes_to_window(self):
        me = self.me
        me.suggestion = Suggestion("ент", "документ", .9, "tri")
        me.history[7] = me.suggestion
        me.fg = -1  # окно сменилось без клика и без нажатий
        with mock.patch.object(A, "send_text") as st, mock.patch.object(A, "send_key") as sk:
            me._handle("accept", W.VK_TAB, 7)
        st.assert_not_called()
        sk.assert_called_once_with(W.VK_TAB, extended=False)

    def test_accept_inserts_what_was_shown(self):
        me = self.me
        me._append("до")
        shown = Suggestion("кумент", "документ", .9, "tri")
        me.history[3] = shown
        me.suggestion = Suggestion("кумент сегодня", "документ", .9, "llm")  # сменилась после нажатия
        with mock.patch.object(A, "send_text") as st:
            me._handle("accept", W.VK_TAB, 3)
        st.assert_called_once_with("кумент ")

    def test_accept_learns_only_finished_words(self):
        me = self.me
        me.cfg.learn = True
        learned = []
        me.predictor = SimpleNamespace(learn_from=learned.append, suggest=lambda t: None)
        me._append("Сделай доку")
        me.suggestion = Suggestion("мент", "документ", .9, "stem", whole_word=False)
        with mock.patch.object(A, "send_text"):
            me._handle("accept")
        self.assertEqual(learned, [])  # основа «документ» — ещё не слово
        me._handle("char", "ы")
        me._handle("char", " ")
        self.assertEqual(learned, ["Сделай документы"])
        learned.clear()
        me.suggestion = Suggestion("и ещё", "", .9, "long")
        with mock.patch.object(A, "send_text"):
            me._handle("accept_word")  # Ctrl+→: «и» без пробела после
        me._handle("char", " ")
        self.assertEqual(learned, ["Сделай документы и"])  # один раз, когда поставил пробел


class HeldKeysTest(unittest.TestCase):
    def test_keys_during_insert_replayed_after(self):
        sh = W.Shared()
        hook = W.Hook(None, sh)
        sh.inserting = True
        kb = W.KBDLLHOOKSTRUCT(vkCode=W.VK_PACKET, scanCode=ord("ж"))
        self.assertTrue(hook._hold(kb, W.VK_PACKET, False))
        kb2 = W.KBDLLHOOKSTRUCT(vkCode=W.VK_BACK, scanCode=14)
        self.assertTrue(hook._hold(kb2, W.VK_BACK, False))
        sent = []
        with mock.patch.object(W, "send_text", lambda s, gap_s=0, extra=0: sent.append(("t", s, extra))), \
                mock.patch.object(W, "send_key", lambda vk, scan=0, ext=False: sent.append(("k", vk))):
            hook.release_held()
        self.assertEqual(sent, [("t", "ж", W.REPLAY), ("k", W.VK_BACK)])
        self.assertFalse(sh.inserting)
        self.assertFalse(hook._hold(kb, W.VK_PACKET, False))  # вставка кончилась — не откладываем


class LLMBackoffTest(unittest.TestCase):
    def test_no_restart_right_after_crash(self):
        llm = L.LLM("нет.gguf")
        llm._failed("сервер завершился")
        with mock.patch.object(L.subprocess, "Popen", side_effect=AssertionError("перезапуск")):
            llm.start()
        self.assertGreater(llm.retry_at, time.time() + 10)
        self.assertEqual(llm.fails, 1)

    def test_ready_ignored_after_stop(self):
        llm = L.LLM("нет.gguf")
        old = SimpleNamespace(poll=lambda: None)
        llm.proc = None  # выгрузили, пока ждали /health
        llm._wait_ready(old)
        self.assertFalse(llm.ready)


class BaseModelFileTest(unittest.TestCase):
    def test_atomic_save_and_corrupt_file_starts_trainer(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "base_model.pkl"
            m = BaseModel.train(["раз два три"] * 3)
            m.save(path)
            self.assertFalse(path.with_suffix(".tmp").exists())
            self.assertEqual(BaseModel.load(path).uni, m.uni)
            path.write_bytes(b"\x80\x05oborvano")
            me = A.App.__new__(A.App)
            me.trainer = mock.Mock(apply_pending=lambda: False)
            with mock.patch.object(A.C, "BASE_MODEL", path):
                # битая модель: до готовности работает пустая, переобучение идёт в отдельном процессе
                self.assertEqual(me._load_base().uni, {})
            me.trainer.start.assert_called_once_with("first")


if __name__ == "__main__":
    unittest.main()
