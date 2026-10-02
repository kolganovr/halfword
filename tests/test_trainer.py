"""Переобучение в отдельном процессе: синтетический корпус во временной папке, настоящее хранилище не читается."""
import json
import os
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from halfword import config as C
from halfword import trainer as T

TERMINAL = ("done", "error", "cancelled")


class TrainerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.data = root / "data"
        self.vault = root / "vault"
        self.data.mkdir()
        self.vault.mkdir()
        for i in range(30):
            (self.vault / f"n{i:02}.md").write_text(
                "Нужно сделать документ сегодня. Потом отправить документ в офис.\n"
                f"Заметка номер {i} про отпуск и документы.\n" * 4, encoding="utf-8")
        patches = {"DATA_DIR": self.data, "BASE_MODEL": self.data / "base_model.pkl",
                   "CONFIG_FILE": self.data / "config.json", "TEXTS_DIR": self.vault,
                   "TELEGRAM_DIR": self.data / "telegram",
                   "CLAUDE_DIR": root / "claude"}
        for k, v in patches.items():
            p = mock.patch.object(C, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.base, self.new, self.prev = (self.data / n for n in
                                          ("base_model.pkl", "base_model.new.pkl", "base_model.prev.pkl"))
        self.events, self.finished = [], threading.Event()
        self.trainer = T.Trainer(self._on_event)
        self.trainer.holdout_every = 3

    def _on_event(self, ev):
        self.events.append(ev)
        if ev["type"] in TERMINAL:
            self.finished.set()

    def run_and_wait(self, reason="manual") -> dict:
        self.finished.clear()
        self.assertTrue(self.trainer.start(reason))
        self.assertTrue(self.finished.wait(120), "обучение не закончилось")
        return self.events[-1]

    def seed_run(self, **kw):
        rec = {"ts": 1, "holdout": "md/3", "result": "accepted", "metric": 5.0}
        rec.update(kw)
        with open(self.data / "train_runs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


class RunTest(TrainerCase):
    def test_run_to_done(self):
        ev = self.run_and_wait()
        self.assertEqual(ev["type"], "done")
        self.assertTrue(ev["accepted"])
        self.assertGreater(ev["words"], 5)
        self.assertIsNotNone(ev["metric"])
        self.assertTrue(self.base.exists())
        self.assertFalse(self.new.exists())
        stages = [e["stage"] for e in self.events if e["type"] == "stage"]
        for s in ("notes", "count", "prune", "write", "check"):
            self.assertIn(s, stages)
        pcts = [e["pct"] for e in self.events if e["type"] == "stage"]
        self.assertEqual(pcts, sorted(pcts))
        self.assertFalse(self.trainer.state["running"])
        h = T.history()
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["result"], "accepted")
        self.assertEqual(h[0]["sources"]["notes"]["files"] + h[0]["sources"]["notes"]["holdout"], 30)
        self.assertEqual(self.trainer.state["last"]["result"], "accepted")

    def test_second_start_refused_while_running(self):
        self.assertTrue(self.trainer.start())
        self.assertFalse(self.trainer.start())
        self.finished.wait(120)

    def test_holdout_excluded_from_training(self):
        files = T.text_files([self.vault])
        hold = [f for root, f in files if T.is_holdout(f, root, 3)]
        self.assertTrue(0 < len(hold) < len(files))


class GateTest(TrainerCase):
    def test_worse_metric_rejected_and_pending_applies(self):
        self.base.write_bytes(b"old-model")
        self.seed_run(metric=99.0)
        ev = self.run_and_wait()
        self.assertEqual(ev["type"], "done")
        self.assertFalse(ev["accepted"])
        self.assertEqual(ev["prev_metric"], 99.0)
        self.assertEqual(self.base.read_bytes(), b"old-model")
        self.assertTrue(self.new.exists())
        self.assertTrue(self.trainer.state["pending"])
        self.assertEqual(T.history(1)[0]["result"], "rejected")
        self.assertTrue(self.trainer.apply_pending())
        self.assertFalse(self.new.exists())
        self.assertEqual(self.prev.read_bytes(), b"old-model")
        self.assertGreater(self.base.stat().st_size, 100)
        self.assertFalse(self.trainer.state["pending"])
        self.assertFalse(self.trainer.apply_pending())

    def test_better_metric_accepted(self):
        self.seed_run(metric=-100.0)  # прежняя метрика ниже любой новой
        self.assertTrue(self.run_and_wait()["accepted"])

    def test_other_holdout_not_compared(self):
        self.seed_run(metric=99.0, holdout="md/40")
        self.assertTrue(self.run_and_wait()["accepted"])


class FilesTest(TrainerCase):
    def test_rollback_swaps_files(self):
        self.base.write_bytes(b"current")
        self.prev.write_bytes(b"previous")
        self.assertTrue(self.trainer.rollback())
        self.assertEqual(self.base.read_bytes(), b"previous")
        self.assertEqual(self.prev.read_bytes(), b"current")
        self.assertTrue(self.trainer.rollback())  # повтор — обратно
        self.assertEqual(self.base.read_bytes(), b"current")
        self.assertEqual(T.history(1)[0]["result"], "rollback")

    def test_rollback_without_prev(self):
        self.base.write_bytes(b"current")
        self.assertFalse(self.trainer.rollback())
        self.assertEqual(self.base.read_bytes(), b"current")

    def test_rollback_restores_previous_metric_for_gate(self):
        self.seed_run(ts=1, metric=5.0)
        self.seed_run(ts=2, metric=9.0)
        self.assertEqual(T._last_metric("md/3"), 9.0)
        self.base.write_bytes(b"a")
        self.prev.write_bytes(b"b")
        self.trainer.rollback()
        self.assertEqual(T._last_metric("md/3"), 5.0)

    def test_cancel_keeps_model(self):
        self.base.write_bytes(b"current")
        self.finished.clear()
        self.assertTrue(self.trainer.start())
        self.assertTrue(self.trainer.cancel())
        self.assertTrue(self.finished.wait(60))
        self.assertEqual(self.events[-1]["type"], "cancelled")
        self.assertEqual(self.base.read_bytes(), b"current")
        self.assertFalse(self.new.exists())
        self.assertFalse(self.trainer.state["running"])
        self.assertEqual(T.history(1)[0]["result"], "cancelled")
        self.assertFalse(self.trainer.cancel())  # ничего не идёт


class HistoryTest(TrainerCase):
    def test_history_newest_first_and_limit(self):
        for i in range(5):
            self.seed_run(ts=i, metric=float(i))
        with open(self.data / "train_runs.jsonl", "a", encoding="utf-8") as f:
            f.write("битая строка\n")
        h = T.history(3)
        self.assertEqual([r["metric"] for r in h], [4.0, 3.0, 2.0])
        self.assertEqual(T.history(), T.history(20))
        self.assertEqual(len(T.history(100)), 5)

    def test_history_empty(self):
        self.assertEqual(T.history(), [])


class SourcesTest(TrainerCase):
    def test_sources_info(self):
        info = {s["key"]: s for s in T.sources_info(C.Config())}
        self.assertEqual(info["notes"]["count"], 30)
        self.assertFalse(info["telegram"]["enabled"])
        self.assertEqual(set(info), {"notes", "telegram", "claude"})

    def test_corpus_changed(self):
        self.assertTrue(self.trainer.corpus_changed())  # запусков не было
        self.run_and_wait()
        self.assertFalse(self.trainer.corpus_changed())
        future = time.time() + 3600
        os.utime(self.vault / "n00.md", (future, future))
        self.assertTrue(self.trainer.corpus_changed())



class AppTrayTest(unittest.TestCase):
    """Склейка трея с Trainer без окна: уведомления, пауза, чёрный список, иконки, пункты меню."""

    def make(self):
        import queue
        from halfword import app as A
        me = A.App.__new__(A.App)
        me.cfg = SimpleNamespace(blacklist=["code.exe"], enabled=True, save=lambda: None, train_auto=True,
                             llm_enabled=True, llm_only_on_ac=True)
        me.shared = SimpleNamespace(enabled=True)
        me.cmds = queue.Queue()
        me.tray = SimpleNamespace(notify=mock.Mock(), update_menu=lambda: None)
        me.llm = SimpleNamespace(running=False, ready=False, fails=0, available=True)
        me.llm_installing = None
        me.trainer = SimpleNamespace(state={"running": False, "stage": "", "label": "", "pct": 0, "pending": False},
                                     last_run_ts=time.time)
        me.buf, me.buf_off, me.history, me.anchor = "", 0, {}, None
        me.overlay = SimpleNamespace(hide=lambda: None)
        me.suggestion = None
        me.pause_until = 0.0
        me.typed_exe, me.exe, me.blocked = "notepad.exe", "notepad.exe", False
        me._tick_at = me._train_check = 0.0
        me._llm_warned = False
        me._icons, me._icon_state = {}, None
        me.usage = SimpleNamespace(reset=lambda *a: None)
        me.on_ac, me.last_key = True, 0.0
        return me

    def test_notifications(self):
        me = self.make()
        me._reload_base = mock.Mock()
        me._on_train({"type": "stage", "stage": "start", "label": "", "pct": 0})
        me._on_train({"type": "done", "accepted": True, "metric": 9.6, "prev_metric": 9.4, "words": 56143, "secs": 22.4})
        me._on_train({"type": "done", "accepted": False, "metric": 9.1, "prev_metric": 9.6, "words": 1, "secs": 1})
        me._on_train({"type": "error", "error": "boom"})
        me._on_train({"type": "cancelled"})
        texts = [c.args[0] for c in me.tray.notify.call_args_list]
        self.assertEqual(texts[1], "готово за 22 с: 56 143 слов, экономия 9.4 → 9.6%")
        self.assertEqual(texts[2], "новая модель хуже: 9.1 против 9.6% — оставил прежнюю")
        self.assertIn("boom", texts[3])
        me._reload_base.assert_called_once()
        self.assertTrue(all(c.args[1] == "Halfword" for c in me.tray.notify.call_args_list))

    def test_pause_does_not_touch_config(self):
        me = self.make()
        me._toggle_pause()
        self.assertFalse(me.shared.enabled)
        self.assertTrue(me.cfg.enabled)
        self.assertGreater(me.pause_until, time.time() + 3000)
        me.pause_until = time.time() - 1
        me._tick()
        self.assertTrue(me.shared.enabled)
        self.assertEqual(me.pause_until, 0.0)
        me._toggle_pause()
        me._toggle_pause()  # повторный клик снимает паузу
        self.assertTrue(me.shared.enabled)

    def test_toggle_exe(self):
        me = self.make()
        me._toggle_exe()
        self.assertIn("notepad.exe", me.cfg.blacklist)
        self.assertEqual(me._tray_state()[0], "blocked")
        me._toggle_exe()
        self.assertNotIn("notepad.exe", me.cfg.blacklist)
        self.assertEqual(me._tray_state(), ("on", None))

    def test_icon_changes_only_on_state_change(self):
        me = self.make()
        me._make_icons()
        self.assertEqual(len(me._icons), 9)
        icon = SimpleNamespace()
        icon.update_menu = mock.Mock()
        me.tray = icon
        me._refresh_tray()
        first = icon.icon
        icon.update_menu.reset_mock()
        me._refresh_tray()
        icon.update_menu.assert_not_called()
        me.trainer.state["running"] = True
        me._refresh_tray()
        self.assertIsNot(icon.icon, first)
        self.assertIn("переобучение", icon.title)

    def test_llm_failure_notified_once(self):
        me = self.make()
        me.llm.fails = 1
        me._tick()
        me._tick_at = 0
        me._tick()
        self.assertEqual(me.tray.notify.call_count, 1)

    def test_menu_items(self):
        import pystray
        me = self.make()
        me.trainer.state.update(running=True, stage="telegram", pct=45)
        with mock.patch.object(pystray, "Icon") as icon_cls:
            me._start_tray()
        menu = icon_cls.call_args.args[3]
        labels = [i.text for i in menu.items]
        self.assertIn("Переобучение: Telegram, 45%…", labels)
        self.assertIn("Отменить переобучение", labels)
        self.assertIn("Выключить в notepad.exe", labels)
        self.assertIn("Пауза на 1 час", labels)
        retrain = next(i for i in menu.items if i.text.startswith("Переобучить"))
        self.assertFalse(retrain.enabled)


if __name__ == "__main__":
    unittest.main()
