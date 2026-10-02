"""Веб-панель: защита, API, применение настроек, словарь, песочница — на фейковом App и синтетике."""
import http.client
import json
import queue
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from halfword import config as C
from halfword import panel, usage
from halfword.model import BaseModel, Predictor, UserModel
from halfword.stats import Stats
from halfword.usage import Usage

CORPUS = ["Привет, как дела? Привет, как дела у тебя. Привет, как дела сегодня.",
          "Документ готов. Документы на визу. Документов много. Документа нет."] * 3


def make_app(d: Path):
    cfg = C.Config()
    cfg.panel_port = 0
    cfg.snippets = {}
    cfg.banned_words = []
    me = SimpleNamespace(
        cfg=cfg, predictor=Predictor(BaseModel.train(CORPUS), UserModel()),
        stats=Stats(d / "stats.json"), usage=Usage(d / "usage.jsonl"),
        shared=SimpleNamespace(enabled=True, accept_right=False),
        llm=SimpleNamespace(ready=False, configured={}), cmds=queue.Queue(),
        exe="notepad.exe", blocked=False, key_hint="Tab", calls=[])
    me.llm.configure = lambda **kw: me.llm.configured.update(kw)
    me._llm_status = lambda: "LLM: выключена"
    me._set_enabled = lambda on: (setattr(me.shared, "enabled", on), setattr(me.cfg, "enabled", on))
    me._toggle_llm = lambda: setattr(me.cfg, "llm_enabled", not me.cfg.llm_enabled)
    me._manage_llm = lambda refresh_power=False: me.calls.append("manage_llm")
    return me


class PanelBase(unittest.TestCase):
    run_loop = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(C, "DATA_DIR", d), mock.patch.object(C, "CONFIG_FILE", d / "config.json"),
                        mock.patch.object(C, "USAGE_FILE", d / "usage.jsonl")]
        for p in self.patches:
            p.start()
        self.app = make_app(d)
        self.stop = threading.Event()
        if self.run_loop:  # «главный цикл»: выполняет команды из app.cmds
            def loop():
                while not self.stop.is_set():
                    try:
                        self.app.cmds.get(timeout=0.01)()
                    except queue.Empty:
                        pass
            threading.Thread(target=loop, daemon=True).start()
        self.panel = panel.start(self.app)
        self.port = self.panel.port

    def tearDown(self):
        self.panel.stop()
        self.stop.set()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, token=True):
        h = dict(headers or {})
        if token:
            h.setdefault("X-Token", self.panel.token)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
            try:
                js = json.loads(raw)
            except ValueError:
                js = None
            return r.status, js, r, raw
        finally:
            c.close()


class SecurityTest(PanelBase):
    def test_no_token_forbidden(self):
        self.assertEqual(self.req("GET", "/", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/status", token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/status", headers={"X-Token": "wrong"}, token=False)[0], 403)

    def test_foreign_host_forbidden(self):
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": "127.0.0.1:1"})[0], 403)
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_post_origin_and_token(self):
        body = {"on": False}
        self.assertEqual(self.req("POST", "/api/enabled", body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/enabled", body, {"Origin": "http://127.0.0.1:1"})[0], 403)
        self.assertTrue(self.app.shared.enabled)
        # cookie без X-Token для POST не годится
        cookie = {"Cookie": f"st_token={self.panel.token}"}
        self.assertEqual(self.req("POST", "/api/enabled", body, cookie, token=False)[0], 403)
        ok = {"Origin": f"http://127.0.0.1:{self.port}"}
        st, js, *_ = self.req("POST", "/api/enabled", body, ok)
        self.assertEqual(st, 200)
        self.assertFalse(js["enabled"])
        self.assertFalse(self.app.shared.enabled)

    def test_login_cookie_redirect(self):
        st, _, r, _ = self.req("GET", "/?t=" + self.panel.token, token=False)
        self.assertEqual(st, 302)
        self.assertEqual(r.getheader("Location"), "/")
        sc = r.getheader("Set-Cookie")
        self.assertIn(f"st_token={self.panel.token}", sc)
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)
        self.assertEqual(self.req("GET", "/?t=wrong", token=False)[0], 403)
        st, _, r, raw = self.req("GET", "/", headers={"Cookie": f"st_token={self.panel.token}"}, token=False)
        self.assertEqual(st, 200)
        self.assertIn(self.panel.token.encode(), raw)  # токен для X-Token встроен в страницу
        self.assertEqual(self.req("GET", "/app.js", headers={"Cookie": f"st_token={self.panel.token}"}, token=False)[0], 200)

    def test_url_has_token_and_loopback(self):
        self.assertTrue(self.panel.url.startswith(f"http://127.0.0.1:{self.port}/?t="))
        self.assertEqual(self.panel.httpd.server_address[0], "127.0.0.1")

    def test_body_limits(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/sandbox", body=b"x" * 70000, headers={"X-Token": self.panel.token})
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        self.assertEqual(self.req("POST", "/api/sandbox", [1])[0], 400)


class PortTest(unittest.TestCase):
    def test_busy_port_next(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        d = tempfile.TemporaryDirectory()
        try:
            app = make_app(Path(d.name))
            app.cfg.panel_port = busy.getsockname()[1]
            p = panel.PanelServer(app)
            try:
                self.assertGreater(p.port, app.cfg.panel_port)
                self.assertLessEqual(p.port, app.cfg.panel_port + 7)
            finally:
                p.httpd.server_close()
        finally:
            busy.close()
            d.cleanup()


class CallMainTest(unittest.TestCase):
    def test_result_error_timeout(self):
        app = SimpleNamespace(cmds=queue.Queue())
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    app.cmds.get(timeout=0.01)()
                except queue.Empty:
                    pass
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        self.assertEqual(panel.call_main(app, lambda: 42), 42)
        with self.assertRaises(ZeroDivisionError):
            panel.call_main(app, lambda: 1 / 0)
        stop.set()
        t.join()
        ran = []
        with self.assertRaises(TimeoutError):
            panel.call_main(app, lambda: ran.append(1), timeout=0.05)
        app.cmds.get_nowait()()  # цикл «ожил» поздно: опоздавшая команда уже отменена
        self.assertEqual(ran, [])


class NoMainLoopTest(PanelBase):
    run_loop = False

    def test_503_when_main_is_stuck(self):
        self.assertEqual(self.req("GET", "/api/status")[0], 503)  # ждёт CALL_TIMEOUT (2 с)


class ApiTest(PanelBase):
    def test_status(self):
        st, js, *_ = self.req("GET", "/api/status")
        self.assertEqual(st, 200)
        self.assertTrue(js["enabled"])
        self.assertEqual(js["llm_status"], "LLM: выключена")
        self.assertNotIn("train", js)  # у фейкового App нет trainer
        st, js, *_ = self.req("GET", "/api/overview")
        self.assertEqual((st, len(js["daily"])), (200, 30))

    def test_sse_status(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events", headers={"X-Token": self.panel.token})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("Content-Type"))
        got = ""
        t0 = time.time()
        while "data:" not in got and time.time() - t0 < 4:
            got += r.fp.readline().decode()
        c.close()
        self.assertIn("event: status", got)
        data = json.loads(got.split("data: ", 1)[1])
        self.assertIn("enabled", data)

    def test_train_without_trainer(self):
        st, js, *_ = self.req("GET", "/api/train")
        self.assertEqual(st, 200)
        self.assertFalse(js["available"])
        self.assertEqual(self.req("POST", "/api/train/cancel", {})[0], 501)

    def test_train_with_trainer(self):
        tr = SimpleNamespace(state={"running": False, "pending": None}, calls=[])
        tr.start = lambda reason="manual": tr.calls.append(("start", reason)) or True
        tr.apply_pending = lambda: tr.calls.append("apply") or True
        tr.rollback = lambda: tr.calls.append("rollback") or True
        tr.cancel = lambda: tr.calls.append("cancel") or True
        self.app.trainer = tr
        self.assertEqual(self.req("POST", "/api/train/start", {})[1], {"ok": True})
        self.assertEqual(self.req("POST", "/api/train/apply", {})[1], {"ok": True})
        self.assertEqual(self.req("POST", "/api/train/cancel", {})[1], {"ok": True})
        self.app._train_rollback = lambda: tr.calls.append("app_rollback") or True  # метод App важнее trainer
        self.req("POST", "/api/train/rollback", {})
        self.assertEqual(tr.calls, [("start", "manual"), "apply", "cancel", "app_rollback"])
        self.assertIn("train", self.req("GET", "/api/status")[1])

    def test_llm_toggle(self):
        self.assertTrue(self.app.cfg.llm_enabled)
        self.req("POST", "/api/llm", {"on": False})
        self.assertFalse(self.app.cfg.llm_enabled)
        self.req("POST", "/api/llm", {"on": False})  # уже выключена: не переключаем обратно
        self.assertFalse(self.app.cfg.llm_enabled)

    def test_settings_roundtrip(self):
        st, js, *_ = self.req("GET", "/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual(js["values"]["min_conf_word"], self.app.cfg.min_conf_word)
        st, js, *_ = self.req("POST", "/api/settings", {"changes": {"min_conf_word": 0.5, "min_support": 99}})
        self.assertEqual(st, 200)
        self.assertEqual(js["applied"], {"min_conf_word": 0.5})
        self.assertIn("min_support", js["errors"])
        self.assertEqual(self.req("POST", "/api/settings", {"changes": 5})[0], 400)

    def test_blacklist(self):
        st, js, *_ = self.req("POST", "/api/blacklist", {"blacklist": [" Notepad.EXE ", "notepad.exe", "a.exe"]})
        self.assertEqual(js["blacklist"], ["notepad.exe", "a.exe"])
        self.assertTrue(self.app.blocked)  # текущая программа теперь в чёрном списке
        saved = json.loads((Path(self.tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["blacklist"], ["notepad.exe", "a.exe"])
        self.assertEqual(self.req("POST", "/api/blacklist", {"blacklist": ["..\\x"]})[0], 400)
        self.req("POST", "/api/blacklist", {"blacklist": []})
        self.assertFalse(self.app.blocked)
        rows = [{"t": time.time(), "app": "Word.exe", "src": "ngram", "pos": "word", "out": "typed",
                 "match": 0, "acc": 0}]
        with mock.patch.object(usage, "load", lambda p, d=None: rows):
            js = self.req("GET", "/api/blacklist")[1]
        self.assertEqual([r["exe"] for r in js["recent"]], ["word.exe"])

    def test_dictionary_forget_ban(self):
        u = self.app.predictor.user
        for _ in range(3):
            u.learn("<s>", "<s>", "Тестослово")
        u.learn("<s>", "<s>", "Другое")
        js = self.req("GET", "/api/dict?prefix=%D1%82%D0%B5%D1%81%D1%82")[1]
        self.assertEqual(js["words"], [{"word": "тестослово", "n": 3}])
        self.assertEqual((js["found"], js["total"]), (1, 2))
        self.assertGreater(self.req("POST", "/api/dict/forget", {"word": "Тестослово"})[1]["removed"], 0)
        self.assertNotIn("тестослово", u.uni)
        self.req("POST", "/api/dict/ban", {"word": "Привет"})
        self.assertIn("привет", self.app.cfg.banned_words)
        self.assertEqual(self.app.predictor.banned, {"привет"})
        self.assertEqual(self.req("GET", "/api/dict")[1]["banned"], ["привет"])
        self.req("POST", "/api/dict/unban", {"word": "привет"})
        self.assertEqual(self.app.predictor.banned, set())
        self.assertEqual(self.req("POST", "/api/dict/ban", {"word": "два слова"})[0], 400)
        self.assertEqual(self.req("POST", "/api/dict/ban", {})[0], 400)

    def test_snippets(self):
        self.assertEqual(self.req("POST", "/api/snippets", {"abbr": "спс", "text": "спасибо"})[1]["snippets"], {"спс": "спасибо"})
        self.assertEqual(self.req("POST", "/api/snippets", {"abbr": "с п", "text": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/snippets", {"delete": "спс"})[1]["snippets"], {})
        self.assertEqual(self.app.cfg.snippets, {})

    def test_sandbox(self):
        st, js, *_ = self.req("POST", "/api/sandbox", {"text": "Привет, как де"})
        self.assertEqual(st, 200)
        self.assertTrue(js["suggestion"]["insert"].startswith("ла"))
        self.assertEqual(js["context"]["prefix"], "де")
        self.assertTrue(0 < len(js["top"]) <= 8)
        self.assertEqual(js["top"][0]["word"], "дела")
        self.assertGreater(js["top"][0]["support"], 0)
        self.assertIsNone(self.req("POST", "/api/sandbox", {"text": "Привет,"})[1]["suggestion"])

    def test_usage_summary_endpoint(self):
        now = time.time()
        rows = [dict(t=now, app="a.exe", src="ngram", pos="word", out="accepted", match=3, acc=5, keys=1),
                dict(t=now, app="a.exe", src="ngram", pos="word", out="typed", match=5, acc=0, keys=3, first=100, gap=100),
                dict(t=now, app="a.exe", src="ngram", pos="word", out="diverged", match=2, acc=0, keys=3, miss="form"),
                dict(t=now, app="b.exe", src="llm", pos="line", out="dismissed", match=0, acc=0, keys=0)]
        with mock.patch.object(usage, "load", lambda p, d=None: rows):
            st, js, *_ = self.req("GET", "/api/usage?days=30")
        self.assertEqual((st, js["days"], js["total"]), (200, 30, 4))
        ng = js["sources"][0]
        self.assertEqual((ng["src"], ng["all"]["n"], ng["all"]["accepted"]), ("ngram", 3, 1))
        self.assertEqual(ng["pos"][0]["pos"], "word")
        self.assertEqual(js["hand"]["n"], 1)
        self.assertEqual(js["miss"]["ngram"][0]["kind"], "form")
        self.assertEqual(js["apps"][0]["app"], "a.exe")


class ApplyConfigTest(PanelBase):
    def test_validation(self):
        r = panel.apply_config(self.app, {"min_conf_word": "0.5", "min_support": 1.5, "learn": 1,
                                          "accept_key": "left", "nope": 1, "llm_min_prob": float("nan"),
                                          "fast_typing_ms": True, "llm_delay_ms": 5000})
        self.assertEqual(r["applied"], {})
        self.assertEqual(set(r["errors"]), {"min_conf_word", "min_support", "learn", "accept_key", "nope",
                                            "llm_min_prob", "fast_typing_ms", "llm_delay_ms"})
        self.assertFalse((Path(self.tmp.name) / "config.json").exists())  # нечего сохранять

    def test_apply_everywhere(self):
        app = self.app
        r = panel.apply_config(app, {"min_conf_word": 0.6, "min_support": 5, "llm_variants": 2, "llm_long_words": 12,
                                     "accept_key": "right", "llm_enabled": False, "overlay_theme": "dark",
                                     "train_gate_pp": 1})
        self.assertEqual(r["errors"], {})
        self.assertEqual(r["restart"], [])  # тема плашки меняется на лету (overlay.set_theme)
        self.assertEqual((app.cfg.min_conf_word, app.cfg.min_support, app.cfg.train_gate_pp), (0.6, 5, 1.0))
        self.assertIsInstance(app.cfg.train_gate_pp, float)
        self.assertEqual((app.predictor.min_conf_word, app.predictor.min_support), (0.6, 5))
        self.assertEqual(app.llm.configured, {"variants": 2, "long_words": 12})
        self.assertTrue(app.shared.accept_right)
        self.assertEqual(app.key_hint, "→")
        self.assertIn("manage_llm", app.calls)
        saved = json.loads((Path(self.tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["min_support"], saved["accept_key"], saved["llm_enabled"]), (5, "right", False))
        panel.apply_config(app, {"accept_key": "tab"})
        self.assertFalse(app.shared.accept_right)
        self.assertEqual(app.key_hint, "Tab")

    def test_schema_matches_config(self):
        names = {f.name for f in fields(C.Config)}
        for s in panel.SETTINGS:
            self.assertIn(s["key"], names)
            v = getattr(C.Config(), s["key"])
            panel._coerce(s, v)  # значение по умолчанию укладывается в свой диапазон


class AggregatesTest(unittest.TestCase):
    def test_stats_summary_daily(self):
        with tempfile.TemporaryDirectory() as d:
            s = Stats(Path(d) / "stats.json")
            s.shown("abc")
            s.accepted("abcdef")
            s.typed()
            s.no_caret("x.exe")
            t = s.summary(1)
            self.assertEqual((t["shown"], t["accepted"], t["saved"]), (1, 1, 5))
            self.assertAlmostEqual(t["accept_rate"], 1.0)
            self.assertEqual(len(s.daily(7)), 7)
            self.assertEqual(s.daily(7)[-1]["saved"], 5)
            self.assertEqual(s.no_caret_apps(7), {"x.exe": 1})
            self.assertEqual(s.summary(None)["saved"], 5)

    def test_usage_summary_empty(self):
        r = usage.summary([])
        self.assertEqual((r["total"], r["sources"], r["apps"]), (0, [], []))


if __name__ == "__main__":
    unittest.main()
