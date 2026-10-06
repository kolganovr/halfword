"""Демо-стенд веб-панели на синтетике: временная папка данных, выдуманный журнал и статистика.

    python -X utf8 tests/demo_panel.py [порт] [--empty] [--no-layout | --no-pair]

Печатает ссылку с токеном. Нужен для ревью и скриншотов панели без запуска Halfword и без личных данных.
--empty — первый запуск: пустая статистика и журнал.
--no-layout — словари раскладки «ещё грузятся» (детектора нет), --no-pair — в Windows нет пары раскладок:
так видна строка о том, что исправление раскладки не работает.
"""
import json
import os
import queue
import random
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DATA = Path(tempfile.mkdtemp(prefix="halfword-demo-"))
os.environ["HALFWORD_DATA"] = str(DATA)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from types import SimpleNamespace  # noqa: E402

from halfword import config as C  # noqa: E402
from halfword import panel  # noqa: E402
from halfword.layout import KeyMap, LayoutDetector  # noqa: E402
from halfword.model import BaseModel, Predictor, UserModel  # noqa: E402
from halfword.stats import Stats  # noqa: E402
from halfword.usage import Usage  # noqa: E402
from halfword.winhook import LayoutPair  # noqa: E402

CORPUS = ["Привет, как дела? Привет, как дела у тебя. Привет, как дела сегодня. Как дела на работе?",
          "Документ готов. Документы на визу. Документов много. Документа нет. Документ отправлен вчера.",
          "Спасибо за помощь. Спасибо большое, всё получилось. Спасибо, посмотрю завтра утром.",
          "Встреча завтра в десять. Встреча перенесена на пятницу. Встретимся после обеда."] * 4
APPS = ["Telegram.exe", "chrome.exe", "Obsidian.exe", "Code.exe", "notepad.exe"]


def fake_stats(path: Path, rnd: random.Random):
    days = {}
    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        if i and rnd.random() < 0.15:
            continue
        typed = rnd.randint(1500, 9000)
        shown = typed // rnd.randint(4, 7)
        acc = int(shown * rnd.uniform(0.12, 0.3))
        llm_s, long_s = shown // 6, shown // 25
        llm_a, long_a = int(llm_s * rnd.uniform(0.1, 0.3)), int(long_s * rnd.uniform(0.05, 0.2))
        days[d] = dict(typed=typed, shown=shown, accepted=acc, inserted=acc * 5, saved=acc * 4,
                       type_ms=typed * 180, type_n=typed, llm_shown=llm_s, llm_accepted=llm_a, llm_saved=llm_a * 7,
                       long_shown=long_s, long_accepted=long_a, long_saved=long_a * 14, word_accepts=acc // 2,
                       layout_fixes=rnd.randint(0, 14), layout_saved=rnd.randint(0, 14) * 9,
                       no_caret={"Discord.exe": rnd.randint(0, 40)} if i < 3 else {})
    path.write_text(json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8")


def fake_usage(path: Path, rnd: random.Random):
    outs = ["accepted"] * 5 + ["partial", "typed", "typed"] + ["diverged"] * 6 + ["erased", "dismissed", "lost", "lost"]
    rows, now = [], time.time()
    for _ in range(1800):
        src = rnd.choice(["ngram"] * 6 + ["llm"] * 2 + ["long"])
        out = rnd.choice(outs)
        chars = rnd.randint(2, 30 if src == "long" else 12)
        r = dict(t=int(now - rnd.uniform(0, 7 * 86400)), app=rnd.choice(APPS), src=src,
                 pos=rnd.choice(["word"] * 5 + ["space", "sent", "line", "punct"]), words=1 + chars // 8,
                 chars=chars, out=out, match=rnd.randint(0, chars), acc=chars if out == "accepted" else 0,
                 keys=rnd.randint(0, 9), vis=rnd.randint(150, 4000), gap=rnd.randint(60, 400), idle=rnd.randint(0, 2000))
        if out in ("diverged", "partial"):
            r["miss"] = rnd.choice(["form", "word", "word", "punct", "case", "short"])
        if out == "accepted":
            r["fix"] = rnd.choice(["kept"] * 6 + ["cut", "edit", "ctrlbs", "undo"])
        if src == "long":
            r["var"], r["cyc"] = 3, rnd.randint(0, 2)
        rows.append(r)
    rows += fake_layout_rows(rnd, now)
    rows.sort(key=lambda r: r["t"])
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def fake_layout_rows(rnd: random.Random, now: float) -> list[dict]:
    """Исправления раскладки без текстов: только форма события, как пишет usage.Usage."""
    rows = []
    for _ in range(220):
        how = rnd.choice(["auto"] * 4 + ["inword"] * 3 + ["delim"] * 2 + ["hotkey"])
        out = rnd.choice(["accepted"] * 6 + ["ignored"] * 3 + ["undone"] * 2 + ["never", "missed"])
        if how in ("inword", "delim") and out in ("undone", "never"):
            out = "ignored"
        if how == "auto" and out == "ignored":
            out = "accepted"
        r = dict(t=int(now - rnd.uniform(0, 7 * 86400)), app=rnd.choice(APPS), src="layout", out=out,
                 dir=rnd.choice(["en2ru"] * 4 + ["ru2en"]), len=rnd.choice(["1-2", "3-4", "5+", "5+"]))
        if out != "missed":
            r.update(chain=rnd.choice([1, 1, 1, 2, 3]), mode="auto" if how == "auto" else "suggest", how=how)
        if out in ("accepted", "ignored"):
            r["vis"] = rnd.randint(150, 4000)
        if out == "accepted":
            r["switch_ok"] = rnd.random() < (0.5 if r["app"] == "Telegram.exe" else 0.95)
        rows.append(r)
    return rows


def fake_runs(path: Path):
    t = time.time() - 3 * 86400
    recs = []
    for i, (res, m) in enumerate([("accepted", 24.1), ("rejected", 23.2), ("accepted", 24.6)]):
        ts = t + i * 86400
        recs.append(dict(ts=ts, when=datetime.fromtimestamp(ts).isoformat(timespec="seconds"), secs=312.4 + i * 9,
                         reason="auto" if i else "manual", holdout="md/20", sources={"notes": 412, "telegram": 1},
                         words=812_000 + i * 4000, metric=m, prev_metric=24.1 if i else None, result=res, error=None))
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


def make_app(empty: bool):
    rnd = random.Random(7)
    if not empty:
        fake_stats(C.STATS_FILE, rnd)
        fake_usage(C.USAGE_FILE, rnd)
        fake_runs(DATA / "train_runs.jsonl")
    cfg = C.Config()
    cfg.panel_port = 0
    cfg.snippets = {} if empty else {"спс": "спасибо", "мск": "Москва", "адр": "ул. Примерная, 1"}
    cfg.banned_words = []
    cfg.layout_mode = "auto"
    cfg.layout_never = [] if empty else ["ghbv", "cdtn"]
    cfg.blacklist = list(cfg.blacklist) + ([] if empty else ["keepass.exe"])
    user = UserModel()
    if not empty:
        for c2, c1, w in [("", "", "привет"), ("", "привет", "как"), ("как", "дела", "сегодня"),
                          ("", "", "документ"), ("встреча", "завтра", "утром"), ("", "", "получилось")] * 3:
            user.learn(c2, c1, w)
    me = SimpleNamespace(
        cfg=cfg, predictor=Predictor(BaseModel.train(CORPUS), user),
        stats=Stats(C.STATS_FILE), usage=Usage(C.USAGE_FILE),
        shared=SimpleNamespace(enabled=True, accept_right=False),
        llm=SimpleNamespace(ready=not empty, configured={}, last_ms=340), cmds=queue.Queue(),
        exe="Telegram.exe", typed_exe="Telegram.exe", blocked=False, key_hint="Tab", on_ac=True, pause_until=0.0)
    me.llm.configure = lambda **kw: me.llm.configured.update(kw)
    add_layout(me, cfg)
    me._llm_status = lambda: "LLM: готова" if me.llm.ready else "LLM: не установлена"
    me._set_enabled = lambda on: (setattr(me.shared, "enabled", on), setattr(me.cfg, "enabled", on))
    me._toggle_llm = lambda: setattr(me.cfg, "llm_enabled", not me.cfg.llm_enabled)
    me._manage_llm = lambda refresh_power=False: None
    me.llm.available, me.llm_installing = not empty, None

    def install():  # демо: «качаем» 10 секунд, без сети
        def work():
            for p in range(0, 100, 10):
                me.llm_installing = f"gemma-3-1b {p}%"
                time.sleep(1)
            me.llm_installing, me.llm.available, me.llm.ready = None, True, True
        if not me.llm_installing and not me.llm.available:
            me.llm_installing = "0%"
            __import__("threading").Thread(target=work, daemon=True).start()
    me._install_llm = install
    try:
        from halfword.trainer import Trainer
        me.trainer = Trainer(lambda ev: None)
        me.trainer.start = lambda reason="manual": False  # демо: обучение не запускаем
    except Exception:  # noqa: BLE001
        pass
    return me


def add_layout(me, cfg):
    """Детектор раскладки, пара раскладок и статус — как у App; флаги --no-layout и --no-pair ломают их напоказ."""
    if "--no-pair" in sys.argv:
        me.pair = LayoutPair(reason="в Windows нет русской и английской раскладок")
    else:
        me.pair = LayoutPair(ru=0x4190419, en=0x4090409, keymap=KeyMap(), keymap_from="windows")
    me.layout = None
    me.predictor.layout_fix = True
    if "--no-layout" not in sys.argv and me.pair.ok:
        me.layout = me.predictor.layout = LayoutDetector(known=me.predictor.word_count, never=cfg.layout_never)
    try:
        from halfword.app import App
        me.layout_status = lambda: App.layout_status(me)
    except Exception:  # noqa: BLE001 — без tkinter статус собираем тем же текстом
        me.layout_status = lambda: ("выключено: " + me.pair.reason if not me.pair.ok else
                                    "словари загружаются" if me.layout is None else "работает")


def main():
    port = int(next((a for a in sys.argv[1:] if a.isdigit()), 8790))
    app = make_app("--empty" in sys.argv)
    app.cfg.panel_port = port
    srv = panel.start(app)
    print(srv.url, flush=True)
    print("данные:", DATA, flush=True)
    try:
        while True:
            try:
                app.cmds.get(timeout=0.05)()
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
