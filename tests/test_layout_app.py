"""Исправление раскладки в App: auto и отмена Backspace, плашка после пробела, ручная клавиша, переключение,
журнал и статистика. Окно, SendInput и хук — моки; поле ввода — строка, которую меняют подмены send_key/send_text."""
import json
import queue
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from halfword import app as A
from halfword import config as C
from halfword import usage as U
from halfword import winhook as W
from halfword.layout import KeyMap, LayoutDetector
from halfword.model import BaseModel, Predictor, UserModel
from halfword.winhook import LayoutPair
from tests.test_app_usage import make_app

RU, EN, KA = 0x4190419, 0x4090409, 0x4370437  # русская, английская, грузинская
CORPUS = ["привет как дела у тебя"] * 10 + ["привет мир"] * 3 + ["сегодня хорошая погода"] * 6 + \
         ["for example this works"] * 6 + ["документ готов к отправке"] * 4


class Field:
    """Текст поля перед курсором: Backspace от приложения стирает, send_text дописывает."""

    def __init__(self):
        self.text = ""
        self.ops = []

    def key(self, vk, scan=0, extended=False, extra=0):
        self.ops.append(("key", vk, extra))
        if vk == W.VK_BACK:
            self.text = self.text[:-1]

    def send(self, text, gap_s=0.0, extra=0):
        self.ops.append(("text", text))
        self.text += text


class LayoutAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det = LayoutDetector()   # словари грузятся ~1.4 с — один раз на класс

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.field = Field()
        self.hkl = EN                # раскладка окна с фокусом
        self.switch_ok = {False: True, True: True}  # сработает ли просьба: {в окно с фокусом, в окно верхнего уровня}
        self.switched = []
        self.later = []
        self.exe_name = "notepad.exe"
        self.saved = []
        det = self.det
        det.never, det.known = set(), (lambda w: 0)
        det._cache.clear()

        def switch(hkl, fg=None, top=False):
            self.switched.append((hkl, top))
            if self.switch_ok[top]:
                self.hkl = hkl
            return True

        patches = [
            mock.patch.object(A, "user32", SimpleNamespace(GetForegroundWindow=lambda: 1234)),
            mock.patch.object(A, "process_name", lambda h: self.exe_name),
            mock.patch.object(A, "send_key", self.field.key),
            mock.patch.object(A, "send_text", self.field.send),
            mock.patch.object(A, "focus_hkl", lambda fg=None: self.hkl),
            mock.patch.object(A, "switch_layout", switch),
            mock.patch.object(C, "LAYOUT_STATE_FILE", Path(self.tmp.name) / "layout_state.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.me = self.make("auto")

    def make(self, mode="auto", detector=True):
        me = make_app(Path(self.tmp.name))
        me.cfg = SimpleNamespace(blacklist=[], learn=False, context_read_chars=0, llm_after_enter=True,
                                 min_insert_chars=2, fast_typing_ms=0, fast_min_insert=4, layout_mode=mode,
                                 layout_hotkey="pause", layout_never=[], save=lambda: self.saved.append(1),
                                 layout_auto_off=["code.exe", "windowsterminal.exe"])
        me.predictor = Predictor(BaseModel.train(CORPUS), UserModel())
        me.predictor.layout_fix = mode != "off"
        me.root = SimpleNamespace(after=lambda ms, fn, *a: self.later.append((ms, fn, a)))
        me.overlay = SimpleNamespace(show=lambda *a, **k: None, hide=lambda: None)
        me.pair = LayoutPair(ru=RU, en=EN, keymap=KeyMap(), keymap_from="windows")
        me.layout_state = {}
        if detector:
            self.det.known = me.predictor.word_count
            me.layout = me.predictor.layout = self.det
            me.miss = U.MissWatch(self.det.convert_text, lambda t: self.det.detect(t) is None)
        me._handle("reset", "init")  # закрепляет окно и его программу
        return me

    # --- помощники ---
    def type_(self, text):
        for ch in text:
            self.field.text += ch
            self.me._handle("char", ch)

    def backspace(self, n=1):
        for _ in range(n):
            self.field.text = self.field.text[:-1]
            self.me._handle("bs")

    def run_later(self):
        calls, self.later = self.later, []
        for _ms, fn, a in calls:
            fn(*a)

    def show(self):
        self.me.anchor = (100, 100, 20, self.me._end())
        self.me._show_now()
        return self.me.suggestion

    def rows(self, out=None):
        return [r for r in self.me.usage.rows if r.get("src") == "layout" and (out is None or r["out"] == out)]

    # --- auto ---
    def test_auto_fixes_on_space_and_switches(self):
        self.type_("ghbdtn ")
        self.assertEqual(self.field.text, "привет ")
        backspaces = [o for o in self.field.ops if o[0] == "key" and o[1] == W.VK_BACK]
        self.assertEqual(len(backspaces), 7)
        self.assertEqual([o for o in self.field.ops if o[0] == "text"], [("text", "привет ")])
        self.assertEqual(self.me.buf, "привет ")
        self.assertEqual(self.switched, [(RU, False)])
        self.assertTrue(self.me.shared.undo_armed)
        r = self.rows("accepted")[0]
        self.assertEqual((r["dir"], r["len"], r["chain"], r["mode"], r["how"]), ("en2ru", "5+", 1, "auto", "auto"))
        self.assertNotIn("ghbdtn", json.dumps(r))
        day = self.me.stats.days[date.today().isoformat()]
        self.assertEqual((day["layout_fixes"], day["layout_saved"]), (1, 14))
        self.assertIn("исправлено раскладок: 1", self.me.stats.report())

    def test_auto_ru_to_en(self):
        self.hkl = RU
        self.type_("ащк ")
        self.assertEqual(self.field.text, "for ")
        self.assertEqual(self.switched, [(EN, False)])

    def test_backspace_right_after_restores_text_and_layout(self):
        self.type_("ghbdtn ")
        self.me._handle("layout_undo")
        self.assertEqual(self.field.text, "ghbdtn ")
        self.assertEqual(self.me.buf, "ghbdtn ")
        self.assertEqual(self.switched, [(RU, False), (EN, False)])  # прежняя раскладка
        self.assertFalse(self.me.shared.undo_armed)
        self.assertEqual(self.me.layout_floor, self.me._end())
        self.assertEqual(len(self.rows("undone")), 1)
        day = self.me.stats.days[date.today().isoformat()]
        self.assertEqual((day["layout_fixes"], day["layout_saved"]), (0, 0))
        self.me._handle("layout_undo")      # окно отмены закрыто: это обычный Backspace
        self.assertEqual(self.field.text, "ghbdtn")
        self.assertEqual(self.me.buf, "ghbdtn")

    def test_other_event_closes_undo_window(self):
        self.type_("ghbdtn ")
        self.type_("a")
        self.assertFalse(self.me.shared.undo_armed)
        self.assertIsNone(self.me.undo)
        self.me._handle("layout_undo")      # хук успел проглотить Backspace — вернуть его окну
        self.assertEqual(self.field.text, "привет ")
        self.assertEqual(self.me.buf, "привет ")

    def test_second_undo_of_same_word_goes_to_never(self):
        self.type_("ghbdtn ")
        self.me._handle("layout_undo")
        self.assertEqual(self.me.cfg.layout_never, [])
        self.type_("ghbdtn ")                # то же слово ещё раз, уже после «нижней границы»
        self.assertEqual(self.field.text, "ghbdtn привет ")
        self.me._handle("layout_undo")
        self.assertEqual(self.me.cfg.layout_never, ["ghbdtn"])
        self.assertIn("ghbdtn", self.det.never)
        self.assertTrue(self.saved)
        self.assertEqual(len(self.rows("never")), 1)
        state = json.loads((Path(self.tmp.name) / "layout_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["undo"], {})
        self.type_("ghbdtn ")                # исключение: больше не исправляется
        self.assertEqual(self.field.text, "ghbdtn ghbdtn ghbdtn ")

    def test_detector_skips_text_before_floor(self):
        self.type_("ghbdtn ")
        self.me._handle("layout_undo")
        self.type_("rfr ")
        self.assertEqual(self.field.text, "ghbdtn как ")  # отменённое слово в цепочку не вернулось

    def test_auto_silent_in_listed_process(self):
        self.exe_name = "code.exe"
        self.me = self.make("auto")
        self.type_("ghbdtn ")
        self.assertEqual(self.field.text, "ghbdtn ")
        self.assertEqual(self.switched, [])
        self.assertIsNone(self.me.undo)
        s = self.show()                      # а подсказка работает
        self.assertEqual((s.level, s.insert), ("layout", "привет "))

    def test_auto_silent_in_suggest_mode_and_password(self):
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        self.assertEqual(self.field.text, "ghbdtn ")
        self.me = self.make("auto")
        self.me.password = True
        self.me._handle("char", "x")
        self.assertEqual(self.field.ops, [])

    def test_auto_checks_field_agrees_with_buffer(self):
        self.me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "совсем другое")
        self.type_("ghbdtn ")
        self.assertEqual(self.field.text, "ghbdtn ")

    def test_auto_not_on_enter(self):
        self.type_("ghbdtn")
        self.me._handle("enter")
        self.assertEqual(self.field.ops, [])

    def test_third_layout_detector_silent(self):
        self.hkl = KA
        self.type_("ghbdtn ")
        self.assertEqual(self.field.text, "ghbdtn ")
        self.assertIsNone(self.show())

    # --- suggest ---
    def test_suggest_after_space_then_next_char_hides(self):
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        s = self.show()
        self.assertEqual((s.level, s.insert, s.replace), ("layout", "привет ", 7))
        self.assertTrue(s.fix.closed)
        self.assertFalse(s.whole_word)
        self.type_("j")
        self.assertIsNone(self.show())
        r = self.rows("ignored")
        self.assertEqual((len(r), r[0]["how"], r[0]["mode"]), (1, "delim", "suggest"))

    def test_tab_on_closed_fix(self):
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        self.show()
        self.me._handle("accept")
        self.assertEqual(self.field.text, "привет ")   # без второго пробела
        self.assertEqual(self.me.buf, "привет ")
        self.assertEqual(self.switched, [(RU, False)])
        self.assertFalse(self.me.shared.undo_armed)
        r = self.rows("accepted")[0]
        self.assertEqual((r["how"], r["mode"]), ("delim", "suggest"))

    def test_inword_suggestion_keeps_old_behaviour(self):
        self.me = self.make("suggest")
        self.type_("ghbdt")
        s = self.show()
        self.assertEqual((s.level, s.insert, s.replace, s.fix.closed), ("layout", "привет", 5, False))
        self.me._handle("accept")
        self.assertEqual(self.field.text, "привет ")

    def test_inword_chars_do_not_split_journal_row(self):
        self.me = self.make("suggest")
        for ch in "ghbdtn":
            self.type_(ch)
            self.show()
        self.me._handle("accept")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["out"], "accepted")

    def test_escape_hides_and_detector_does_not_retake(self):
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        self.assertIsNotNone(self.show())
        self.me._handle("dismiss")
        self.assertIsNone(self.show())
        self.assertEqual(self.me.layout_floor, self.me._end())
        self.type_("gjxtve ")
        s = self.show()
        self.assertEqual(s.fix.original, "gjxtve ")   # «ghbdtn » в исправление не вернулось
        self.assertEqual(s.replace, 7)

    # --- пока детектор грузится ---
    def test_detector_not_loaded_yet(self):
        self.me = self.make("auto", detector=False)
        self.assertIsNone(self.me._layout_text())
        self.type_("ghbdtn ")               # ничего не падает и не исправляется
        self.assertEqual(self.field.text, "ghbdtn ")
        self.me._handle("layout_key", W.VK_PAUSE)   # клавишу возвращаем окну
        self.assertEqual(self.field.ops[-1], ("key", W.VK_PAUSE, W.MAGIC))
        self.type_("ghbdt")
        s = self.show()                     # прежнее правило посреди слова
        self.assertEqual((s.level, s.insert, s.fix), ("layout", "привет", None))
        self.me._handle("accept")
        self.assertEqual(self.field.text, "ghbdtn привет ")

    # --- ручная клавиша ---
    def test_hotkey_without_selection_converts_last_word_and_repeat_rolls_back(self):
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertEqual(self.field.text, "привет ")
        self.assertEqual(self.switched, [(RU, False)])
        self.assertEqual(self.rows("accepted")[0]["how"], "hotkey")
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertEqual(self.field.text, "ghbdtn ")
        self.assertEqual(self.switched[-1], (EN, False))
        self.assertEqual(self.rows("undone")[0]["how"], "hotkey")
        self.me._handle("layout_key", W.VK_PAUSE)   # отменённое ниже «нижней границы»: перевода нет
        self.assertEqual(self.field.text, "ghbdtn ")

    def test_hotkey_repeat_after_other_key_or_timeout_is_not_rollback(self):
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertIsNotNone(self.me.manual)
        self.type_("a")
        self.assertIsNone(self.me.manual)
        self.field.text = ""
        self.me = self.make("suggest")
        self.type_("ghbdtn ")
        self.me._handle("layout_key", W.VK_PAUSE)
        self.me.manual["t"] -= 5
        self.me._handle("layout_key", W.VK_PAUSE)     # через 5 с — новый перевод последнего слова, а не откат
        self.assertEqual((len(self.rows("accepted")), len(self.rows("undone"))), (2, 0))

    def test_hotkey_with_selection(self):
        self.me = self.make("suggest")
        self.me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "",
                                        selected_text=lambda: "ghbdtn vbh")
        self.field.text = "ghbdtn vbh"
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertEqual(self.field.ops, [("text", "привет мир")])   # поверх выделения, без стирания
        self.assertEqual(self.switched, [(RU, False)])
        self.assertTrue(self.me.pending_reload)
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertEqual(self.field.ops[-1], ("text", "ghbdtn vbh"))
        self.assertEqual(sum(o[0] == "key" for o in self.field.ops), len("привет мир"))

    def test_hotkey_without_uia_does_not_touch_clipboard(self):
        self.me = self.make("suggest")
        self.me.caret = SimpleNamespace(is_password=lambda h: False, text_before_caret=lambda n: "",
                                        selected_text=lambda: None)
        self.type_("ghbdtn ")
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertEqual(self.field.text, "привет ")   # выделения нет — перевели последнее слово

    def test_hotkey_off_in_blacklisted_process_returns_key(self):
        self.exe_name = "keepass.exe"
        self.me = self.make("suggest")
        self.me.cfg.blacklist = ["keepass.exe"]
        self.me._handle("reset", "x")
        self.me.blocked = True
        self.me._handle("layout_key", W.VK_PAUSE)
        self.assertEqual(self.field.ops, [("key", W.VK_PAUSE, W.MAGIC)])
        self.me._handle("layout_key", 0)    # двойной Shift: возвращать нечего
        self.assertEqual(len(self.field.ops), 1)

    # --- переключение ---
    def test_switch_verified_and_logged(self):
        self.type_("ghbdtn ")
        r = self.rows("accepted")[0]
        self.assertNotIn("switch_ok", r)
        self.run_later()
        self.assertTrue(r["switch_ok"])

    def test_switch_retried_in_top_level_window(self):
        self.switch_ok = {False: False, True: True}
        self.type_("ghbdtn ")
        self.run_later()
        self.run_later()
        self.assertEqual(self.switched, [(RU, False), (RU, True)])
        self.assertTrue(self.rows("accepted")[0]["switch_ok"])

    def test_switch_failure_logged(self):
        self.switch_ok = {False: False, True: False}
        self.type_("ghbdtn ")
        with self.assertLogs("halfword", "WARNING"):
            self.run_later()
            self.run_later()
        self.assertFalse(self.rows("accepted")[0]["switch_ok"])
        self.assertEqual(len(self.switched), 2)

    # --- журнал и отчёт ---
    def test_missed_layout_counted_without_text(self):
        self.me = self.make("suggest")
        self.det.never = {"ghbdtn"}          # детектор по этому слову молчит
        self.det._cache.clear()
        self.type_("ghbdtn ")
        self.backspace(7)
        self.type_("привет ")
        r = self.rows("missed")
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["dir"], r[0]["len"]), ("en2ru", "5+"))
        self.assertNotIn("привет", json.dumps(r[0], ensure_ascii=False))

    def test_not_missed_when_typed_something_else_or_detector_spoke(self):
        self.me = self.make("suggest")
        self.det.never = {"ghbdtn"}
        self.det._cache.clear()
        self.type_("ghbdtn ")
        self.backspace(7)
        self.type_("здравствуй ")
        self.assertEqual(self.rows("missed"), [])
        self.det.never = set()
        self.det._cache.clear()
        self.type_("ghbdtn ")                 # детектор показал бы исправление: это не пропуск
        self.backspace(7)
        self.type_("привет ")
        self.assertEqual(self.rows("missed"), [])

    def test_report_has_layout_line(self):
        self.type_("ghbdtn ")
        self.run_later()
        self.me._handle("layout_undo")
        self.type_("rfr ")
        rows = self.me.usage.rows
        text = U.report([dict(r) for r in rows])
        self.assertIn("Раскладка (EN ⇄ RU)", text)
        self.assertIn("отменено 1", text)
        s = U.summary([dict(r) for r in rows])
        self.assertEqual((s["layout"]["auto"], s["layout"]["undone_auto"]), (2, 1))
        self.assertEqual(s["total"], 0)       # строки раскладки не смешиваются с подсказками

    # --- обучение ---
    def test_learn_from_skips_wrong_layout_word(self):
        p = Predictor(BaseModel.train(CORPUS), UserModel())
        p.layout = self.det
        self.assertIsNone(p.learn_from("ghbdtn"))
        self.assertNotIn("ghbdtn", p.user.uni)
        self.assertIsNotNone(p.learn_from("привет"))
        self.assertEqual(p.user.uni["привет"], 1)

    def test_known_counts_base_and_user(self):
        p = Predictor(BaseModel.train(CORPUS), UserModel())
        p.user.learn("<s>", "<s>", "ковчег")
        self.assertEqual(p.word_count("привет"), 13)
        self.assertEqual(p.word_count("ковчег"), 1)


class ConfigCompatTest(unittest.TestCase):
    def load(self, d: dict) -> C.Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(d), encoding="utf-8")
            with mock.patch.object(C, "CONFIG_FILE", path):
                return C.Config.load()

    def test_old_layout_fix_false_means_off(self):
        self.assertEqual(self.load({"layout_fix": False}).layout_mode, "off")
        self.assertEqual(self.load({"layout_fix": True}).layout_mode, "suggest")
        self.assertEqual(self.load({}).layout_mode, "suggest")
        self.assertEqual(self.load({"layout_fix": False, "layout_mode": "auto"}).layout_mode, "auto")

    def test_defaults_and_bad_values(self):
        cfg = self.load({"layout_mode": "всегда", "layout_hotkey": "F13", "layout_auto_off": ["Code.EXE"]})
        self.assertEqual((cfg.layout_mode, cfg.layout_hotkey), ("suggest", "pause"))
        self.assertEqual(cfg.layout_auto_off, ["code.exe"])
        d = C.Config()
        for exe in ("windowsterminal.exe", "wt.exe", "code.exe", "cmd.exe", "cursor.exe"):
            self.assertIn(exe, d.layout_auto_off)
        self.assertEqual((d.layout_hotkey, d.layout_never), ("pause", []))


class WindowsLayoutsTest(unittest.TestCase):
    """Раскладки из настоящей Windows; на машине без русской или английской раскладки тесты пропускаются."""

    def test_pair_and_keymap_from_toUnicodeEx(self):
        pair = W.layout_pair()
        if not pair.ok:
            self.skipTest(pair.reason)
        self.assertEqual((W.primary_lang(pair.ru), W.primary_lang(pair.en)), (W.LANG_RUSSIAN, W.LANG_ENGLISH))
        km = pair.keymap
        self.assertEqual(len(km.en), len(km.ru))
        self.assertEqual(len(set(km.en)), len(km.en))
        self.assertEqual(len(set(km.ru)), len(km.ru))
        if pair.keymap_from == "windows" and pair.en & 0xFFFF == 0x0409 and pair.ru & 0xFFFFFFFF == 0x04190419:
            from halfword.layout import EN_KEYS, RU_KEYS
            self.assertEqual(sorted(zip(km.en, km.ru)), sorted(zip(EN_KEYS, RU_KEYS)))  # стандартные раскладки

    def test_no_russian_layout_disables_detector(self):
        with mock.patch.object(W, "installed_layouts", lambda: [EN, KA]):
            pair = W.layout_pair()
        self.assertFalse(pair.ok)
        self.assertIn("русской", pair.reason)
        me = A.App.__new__(A.App)
        me.pair = pair
        self.assertIn("русской", me.layout_status())
        self.assertFalse(me._layout_blocked())


class HookLayoutKeysTest(unittest.TestCase):
    def setUp(self):
        self.q = queue.Queue()
        self.sh = W.Shared(enabled=True, layout_hotkey="pause")
        self.hook = W.Hook(self.q, self.sh)
        p = mock.patch.object(W, "_down", lambda vk: False)
        p.start()
        self.addCleanup(p.stop)

    def key(self, vk, up=False):
        kb = W.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0)
        return self.hook._handle_key(kb, W.WM_KEYUP if up else W.WM_KEYDOWN)

    def events(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get()[1:])
        return out

    def test_pause_swallowed_and_reported(self):
        self.assertTrue(self.key(W.VK_PAUSE))
        self.assertTrue(self.key(W.VK_PAUSE, up=True))
        self.assertEqual(self.events(), [("layout_key", W.VK_PAUSE)])

    def test_scroll_lock_hotkey_and_other_key_untouched(self):
        self.sh.layout_hotkey = "scroll_lock"
        self.assertTrue(self.key(W.VK_SCROLL))
        self.assertFalse(self.key(W.VK_PAUSE))
        self.assertEqual(self.events(), [("layout_key", W.VK_SCROLL)])

    def test_none_and_disabled(self):
        self.sh.layout_hotkey = "none"
        self.assertFalse(self.key(W.VK_PAUSE))
        self.sh.layout_hotkey, self.sh.enabled = "pause", False
        self.assertFalse(self.key(W.VK_PAUSE))

    def test_armed_backspace_is_swallowed_once(self):
        self.sh.undo_armed = True
        self.assertTrue(self.key(W.VK_BACK))
        self.assertEqual(self.events(), [("layout_undo",)])
        self.assertFalse(self.sh.undo_armed)
        self.assertFalse(self.key(W.VK_BACK))     # дальше обычный Backspace
        self.assertEqual(self.events(), [("bs",)])

    def test_other_key_disarms_undo(self):
        self.sh.undo_armed = True
        self.key(W.VK_RETURN)
        self.assertFalse(self.sh.undo_armed)
        self.assertFalse(self.key(W.VK_BACK))

    def test_double_shift(self):
        self.sh.layout_hotkey = "double_shift"
        clock = SimpleNamespace(t=10.0)
        fake_time = SimpleNamespace(perf_counter=lambda: clock.t, sleep=lambda s: None)
        with mock.patch.object(W, "time", fake_time):
            for dt in (0.0, 0.2):
                clock.t = 10.0 + dt
                self.assertFalse(self.key(0xA0))          # Shift окну не мешаем
                self.assertFalse(self.key(0xA0, up=True))
            self.assertEqual(self.events(), [("layout_key", 0)])
            clock.t = 20.0                                  # медленные тапы — не двойной
            self.key(0xA0), self.key(0xA0, up=True)
            clock.t = 20.6
            self.key(0xA0), self.key(0xA0, up=True)
            self.assertEqual(self.events(), [])
            clock.t = 30.0                                  # Shift+буква между тапами — не тап
            self.key(0xA0), self.key(W.VK_RETURN), self.key(0xA0, up=True)
            clock.t = 30.1
            self.key(0xA0), self.key(0xA0, up=True)
            self.assertEqual([e for e in self.events() if e[0] == "layout_key"], [])


if __name__ == "__main__":
    unittest.main()
