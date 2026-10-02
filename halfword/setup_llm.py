"""Установка локальной ИИ-модели: llama-server из релиза llama.cpp и Gemma 3 (GGUF) с Hugging Face.

Версии закреплены, каждый файл сверяется по sha256. Прерванная загрузка продолжается с места обрыва.
Модель Gemma распространяется по Gemma Terms of Use: https://ai.google.dev/gemma/terms
"""
from __future__ import annotations

import hashlib
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import llm as L

GEMMA_TERMS = "https://ai.google.dev/gemma/terms"

# (адрес, sha256, размер в байтах)
LLAMA_ZIP = ("https://github.com/ggml-org/llama.cpp/releases/download/b11321/llama-b11321-bin-win-cpu-x64.zip",
             "8f8c0c6501b075f52deff59537c05acd57d8621a0a7935f29b7d7c4812892569", 19_275_154)
MODELS = {
    "gemma-3-1b-pt-q4_k_m.gguf": (
        "https://huggingface.co/mradermacher/gemma-3-1b-pt-GGUF/resolve/main/gemma-3-1b-pt.Q4_K_M.gguf",
        "caf1c278f8a8ba1e4605af68b6c17c91a18bf315b38bd52efc542d009d19dd57", 806_056_864),
    "gemma-3-270m-q8_0.gguf": (
        "https://huggingface.co/ggml-org/gemma-3-270m-GGUF/resolve/main/gemma-3-270m-Q8_0.gguf",
        "e00cf79514204dfe2f4d6943f277ffea7fd8a4c8e955b4b7a869cfc157694881", 291_543_744),
}

Progress = Callable[[str, int, int], None]  # (что качаем, скачано байт, всего байт)


def installed(model_file: str) -> bool:
    return L.SERVER_EXE.exists() and (L.MODELS_DIR / model_file).exists()


def download_size(model_file: str) -> int:
    """Сколько байт осталось скачать."""
    n = 0 if L.SERVER_EXE.exists() else LLAMA_ZIP[2]
    if not (L.MODELS_DIR / model_file).exists():
        n += MODELS[model_file][2]
    return n


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, sha: str, size: int, dest: Path, label: str, progress: Progress | None,
              cancel: Callable[[], bool] | None = None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have < size:
        req = urllib.request.Request(url, headers={"User-Agent": "halfword-setup"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        with urllib.request.urlopen(req, timeout=60) as r:
            if have and r.status != 206:  # сервер не умеет продолжать — качаем заново
                have = 0
            with open(part, "ab" if have else "wb") as f:
                while True:
                    if cancel and cancel():
                        raise InterruptedError("загрузка отменена")
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    have += len(chunk)
                    if progress:
                        progress(label, have, size)
    if part.stat().st_size != size or _sha256(part) != sha:
        part.unlink()
        raise IOError(f"{dest.name}: файл повреждён при загрузке, попробуйте ещё раз")
    part.replace(dest)


def install(model_file: str = "gemma-3-1b-pt-q4_k_m.gguf", progress: Progress | None = None,
            cancel: Callable[[], bool] | None = None):
    """Скачать то, чего не хватает. Повторный вызов после обрыва продолжает загрузку."""
    if model_file not in MODELS:
        raise ValueError(f"неизвестная модель {model_file}; доступны: {', '.join(MODELS)}")
    if not L.SERVER_EXE.exists():
        url, sha, size = LLAMA_ZIP
        zpath = L.LLM_DIR / "llama.cpp.zip"
        _download(url, sha, size, zpath, "llama.cpp", progress, cancel)
        tmp = L.LLM_DIR / "bin.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        found = next(tmp.rglob("llama-server.exe"), None)
        if not found:
            raise IOError("в архиве llama.cpp нет llama-server.exe")
        shutil.rmtree(L.SERVER_EXE.parent, ignore_errors=True)
        found.parent.replace(L.SERVER_EXE.parent)
        shutil.rmtree(tmp, ignore_errors=True)
        zpath.unlink()
    dest = L.MODELS_DIR / model_file
    if not dest.exists():
        url, sha, size = MODELS[model_file]
        _download(url, sha, size, dest, model_file, progress, cancel)
