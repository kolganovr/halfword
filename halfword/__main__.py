"""python -X utf8 -m halfword [run|setup [--light]|train|stats|usage [дней]|suggest "текст"]"""
from __future__ import annotations

import argparse
import sys
import time

from . import config as C
from .model import BaseModel, Predictor, UserModel


def cmd_train(_):
    t0 = time.time()
    texts = C.train_texts(C.Config.load())
    m = BaseModel.train(texts)
    m.save(C.BASE_MODEL)
    size = C.BASE_MODEL.stat().st_size / 1e6
    print(f"токенов: {m.tokens:,.0f}  слов: {len(m.uni):,}  биграмм-контекстов: {len(m.bi):,}  "
          f"триграмм-контекстов: {len(m.tri):,}  файл: {size:.1f} МБ  за {time.time() - t0:.1f} с")


def load_predictor() -> Predictor:
    if not C.BASE_MODEL.exists():
        print("Модели нет — обучаю на ваших текстах…")
        cmd_train(None)
    return Predictor(BaseModel.load(C.BASE_MODEL), UserModel.load(C.USER_MODEL))


def cmd_suggest(args):
    p = load_predictor()
    text = " ".join(args.text)
    t0 = time.perf_counter()
    s = p.suggest(text)
    dt = (time.perf_counter() - t0) * 1000
    print(f"{text!r} → {s.insert!r} ({s.level}, {s.confidence:.2f})" if s else f"{text!r} → —", f"[{dt:.1f} мс]")


def cmd_stats(_):
    from .stats import Stats
    print(Stats(C.STATS_FILE).report())


def cmd_usage(args):
    from . import usage
    print(usage.report(usage.load(C.USAGE_FILE, args.days)))


def cmd_setup(args):
    from . import setup_llm as S
    model = "gemma-3-270m-q8_0.gguf" if args.light else C.Config.load().llm_model
    if S.installed(model):
        print("ИИ-модель уже установлена.")
        return
    print(f"Скачиваю ИИ-модель ({S.download_size(model) / 1e6:.0f} МБ). Gemma распространяется по {S.GEMMA_TERMS}")
    last = [None, -1]

    def progress(label, done, total):
        pct = done * 100 // total
        if [label, pct] != last:
            last[:] = [label, pct]
            print(f"\r  {label}: {pct}% ({done / 1e6:.0f} из {total / 1e6:.0f} МБ)   ", end="", flush=True)

    S.install(model, progress)
    if args.light:
        cfg = C.Config.load()
        cfg.llm_model = model
        cfg.save()
    print("\nГотово.")


def cmd_run(_):
    from .app import main
    main()


def main():
    ap = argparse.ArgumentParser(prog="halfword")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    st = sub.add_parser("setup", help="скачать локальную ИИ-модель")
    st.add_argument("--light", action="store_true", help="лёгкая модель 270M (~0.3 ГБ) вместо 1B")
    sub.add_parser("train")
    sub.add_parser("stats")
    u = sub.add_parser("usage")
    u.add_argument("days", nargs="?", type=int, default=None)
    s = sub.add_parser("suggest")
    s.add_argument("text", nargs="+")
    args = ap.parse_args()
    {"setup": cmd_setup, "train": cmd_train, "suggest": cmd_suggest, "stats": cmd_stats, "usage": cmd_usage, "run": cmd_run, None: cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
