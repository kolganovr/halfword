"""Сохранение настроек не затирает правки файла, сделанные, пока программа работала."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from halfword import config as C
from halfword import trainer as T


class ConfigSaveCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        self.file = d / "config.json"
        for k, v in {"DATA_DIR": d, "CONFIG_FILE": self.file}.items():
            p = mock.patch.object(C, k, v)
            p.start()
            self.addCleanup(p.stop)

    def _disk(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def _edit_disk(self, **kv):
        d = self._disk()
        d.update(kv)
        self.file.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def test_external_edit_survives_save(self):
        cfg = C.Config.load()  # файла нет — создаётся с умолчаниями
        self._edit_disk(text_dirs=["D:/Заметки"], min_conf_word=0.5)
        cfg.save()  # например, выход из программы
        self.assertEqual(self._disk()["text_dirs"], ["D:/Заметки"])
        self.assertEqual(self._disk()["min_conf_word"], 0.5)
        self.assertEqual(cfg.text_dirs, ["D:/Заметки"])  # и процесс видит правку

    def test_own_change_wins_and_other_keys_kept(self):
        cfg = C.Config.load()
        self._edit_disk(text_dirs=["D:/Заметки"], future_key=1)
        cfg.min_conf_word = 0.3  # поменяли в панели
        cfg.save()
        disk = self._disk()
        self.assertEqual(disk["min_conf_word"], 0.3)
        self.assertEqual(disk["text_dirs"], ["D:/Заметки"])
        self.assertEqual(disk["future_key"], 1)

    def test_same_key_both_sides_process_wins(self):
        cfg = C.Config.load()
        self._edit_disk(min_conf_word=0.5)
        cfg.min_conf_word = 0.3
        cfg.save()
        self.assertEqual(self._disk()["min_conf_word"], 0.3)

    def test_second_save_after_own_change(self):
        cfg = C.Config.load()
        cfg.tg_weight = 2.0
        cfg.save()
        self._edit_disk(tg_weight=6.0)  # руками после сохранения
        cfg.save()
        self.assertEqual(self._disk()["tg_weight"], 6.0)


class GateUnseenNotesCase(unittest.TestCase):
    def test_model_without_notes_counts_any_md_holdout_as_unseen(self):
        runs = [{"result": "accepted", "holdout": "msg/40", "sources": {"notes": {"files": 0}}}]
        with mock.patch.object(T, "history", return_value=runs):
            self.assertTrue(T._current_unseen_md())
        runs = [{"result": "accepted", "holdout": "md/40", "sources": {"notes": {"files": 12}}}]
        with mock.patch.object(T, "history", return_value=runs):
            self.assertFalse(T._current_unseen_md())


if __name__ == "__main__":
    unittest.main()
