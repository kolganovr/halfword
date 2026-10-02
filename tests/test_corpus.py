import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from halfword.corpus import claude_messages, iter_telegram, recency_weight, telegram_chat_messages
from halfword.model import BaseModel


def _export(folder: Path, with_me: bool):
    msgs = [{"type": "message", "date": "2022-05-01T10:00:00", "from_id": "user1", "text": "старое моё"},
            {"type": "message", "date": "2024-05-01T10:00:00", "from_id": "user2", "text": "сообщение друга"},
            {"type": "service", "date": "2024-05-01T10:00:00", "from_id": "user1", "action": "join"},
            {"type": "message", "date": "2024-05-01T10:00:00", "from_id": "user1", "text": "моё сообщение"}]
    left = [{"type": "message", "date": "2024-04-01T10:00:00", "from_id": "user1", "text": "из покинутого чата"}]
    data = {"chats": {"list": [{"messages": msgs}]}, "left_chats": {"list": [{"messages": left}]}}
    if with_me:
        data["personal_information"] = {"user_id": 1}
    (folder / "result.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TelegramTest(unittest.TestCase):
    def test_chat_names(self):
        with tempfile.TemporaryDirectory() as d:
            _export(Path(d), with_me=True)
            self.assertEqual({c for c, _w, _t in telegram_chat_messages(Path(d))}, {"чат 0", "чат 1"})

    def test_only_my_messages_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            _export(Path(d), with_me=True)
            self.assertEqual([t for t, _w in iter_telegram(Path(d))],
                             ["старое моё", "из покинутого чата", "моё сообщение"])

    def test_export_without_account_info_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            _export(Path(d), with_me=False)
            self.assertEqual(list(iter_telegram(Path(d))), [])  # раньше сюда попадали чужие сообщения

    def test_recency_weight_from_newest_message(self):
        with tempfile.TemporaryDirectory() as d:
            _export(Path(d), with_me=True)
            w = dict(iter_telegram(Path(d), half_life_years=2, weight=4))
            self.assertAlmostEqual(w["моё сообщение"], 4.0)          # самое свежее — полный вес
            self.assertAlmostEqual(w["старое моё"], 2.0, places=2)   # на 2 года старше — вдвое меньше
        self.assertEqual(recency_weight(datetime(2020, 1, 1), datetime(2024, 1, 1), 0), 1.0)


class WeightedTrainTest(unittest.TestCase):
    def test_weights_scale_counts(self):
        m = BaseModel.train(["кот спит", ("кот ест", 0.25)])
        self.assertEqual(m.uni["кот"], 1.25)
        self.assertEqual(m.uni["ест"], 0.25)


class ClaudeTest(unittest.TestCase):
    def test_only_my_typed_messages(self):
        rows = [
            {"type": "user", "timestamp": "2026-09-02", "message": {"content": "второе"}},
            {"type": "user", "timestamp": "2026-09-01", "message": {"content": [{"type": "text", "text": "первое"}]}},
            {"type": "user", "timestamp": "2026-09-03", "message": {"content": "первое"}},  # повтор
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "вывод"}]}},
            {"type": "user", "isSidechain": True, "message": {"content": "промпт субагенту"}},
            {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
            {"type": "assistant", "message": {"content": "ответ"}},
            {"type": "user", "timestamp": "2026-09-04",
             "message": {"content": "смотри <pasted_content id=1>чужой текст</pasted_content> вот"}},
        ]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p").mkdir()
            (Path(d) / "p" / "s.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                                                   encoding="utf-8")
            self.assertEqual([t for _w, t in claude_messages(Path(d))], ["первое", "второе", "смотри   вот"])


if __name__ == "__main__":
    unittest.main()
