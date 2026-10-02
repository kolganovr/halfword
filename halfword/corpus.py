"""Сбор обучающего текста: .md и .txt из папок-источников, экспорт Telegram (только свои сообщения), сообщения Claude Code."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

SKIP_DIRS = {".obsidian", ".git", "node_modules", ".agents", ".claude", ".venv", "venv",
             "__pycache__", ".trash", "data"}

_FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+|www\.\S+")
_WIKILINK = re.compile(r"!?\[\[([^\]|]*\|)?([^\]]*)\]\]")
_MDLINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<[^>]+>")
_PATHLIKE = re.compile(r"\S*[\\/]\S*")


def clean_markdown(text: str) -> str:
    text = _FRONTMATTER.sub("", text)
    text = _CODE_BLOCK.sub("\n", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _WIKILINK.sub(lambda m: m.group(2), text)
    text = _MDLINK.sub(lambda m: m.group(1), text)
    text = _URL.sub(" ", text)
    text = _HTML.sub(" ", text)
    text = _PATHLIKE.sub(" ", text)
    # таблицы и разметка: | # * > превращаем в разрыв фразы
    text = re.sub(r"[|#*>_=~]+", "\n", text)
    return text


TEXT_EXT = (".md", ".txt")


def text_files(roots) -> list[tuple[Path, Path]]:
    """(папка-источник, файл) для всех .md и .txt, в стабильном порядке."""
    out = []
    for root in roots:
        root = Path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            out += [(root, Path(dirpath) / f) for f in sorted(filenames) if f.lower().endswith(TEXT_EXT)]
    return out


def read_text_file(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return clean_markdown(raw) if path.suffix.lower() == ".md" else raw


def iter_texts(roots) -> Iterator[str]:
    for _root, path in text_files(roots):
        try:
            yield read_text_file(path)
        except OSError:
            continue


def _tg_text(msg: dict) -> str:
    t = msg.get("text", "")
    if isinstance(t, list):
        return "".join(x if isinstance(x, str) else x.get("text", "") for x in t)
    return t or ""


def telegram_messages(folder: Path) -> list[tuple[datetime, str]]:
    """Telegram Desktop → Экспорт данных → JSON: сообщения владельца экспорта (дата, текст), старые первыми.

    Только сообщения владельца аккаунта, из текущих и покинутых чатов; служебные, медиа без подписи,
    аватарки и «о себе» пропускаются.
    """
    out = [(when, txt) for _chat, when, txt in telegram_chat_messages(folder)]
    out.sort(key=lambda x: x[0])
    return out


def telegram_chat_messages(folder: Path) -> list[tuple[str, datetime, str]]:
    """То же с названием чата: (чат, дата, текст), внутри чата по порядку."""
    out = []
    if not folder.is_dir():
        return out
    for p in folder.rglob("result.json"):
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        me = data.get("personal_information", {}).get("user_id")
        if not me:
            # без «Информации об аккаунте» не понять, где сообщения владельца, — чужие не берём
            log.warning("Telegram: в %s нет personal_information.user_id — пропускаю", p.parent.name)
            continue
        me_id = f"user{me}"
        chats = data.get("chats", {}).get("list", []) + data.get("left_chats", {}).get("list", [])
        for i, chat in enumerate(chats):
            name = chat.get("name") or f"чат {i}"
            for msg in chat.get("messages", []):
                if msg.get("type") != "message" or msg.get("from_id") != me_id:
                    continue
                txt = _URL.sub(" ", _tg_text(msg)).strip()
                if not txt:
                    continue
                try:
                    when = datetime.fromisoformat(msg.get("date", ""))
                except ValueError:
                    continue
                out.append((name, when, txt))
    return out


def recency_weight(when: datetime, newest: datetime, half_life_years: float) -> float:
    """Вес сообщения: каждые half_life_years лет в прошлое — вдвое меньше (0 — без затухания)."""
    if half_life_years <= 0:
        return 1.0
    age = max(0.0, (newest - when).days / 365.25)
    return 0.5 ** (age / half_life_years)


def iter_telegram(folder: Path, half_life_years: float = 0.0, weight: float = 1.0,
                  messages: list[tuple[datetime, str]] | None = None) -> Iterator[tuple[str, float]]:
    """→ (текст, вес): как я пишу сейчас важнее, чем как писал несколько лет назад.

    Возраст считается от самого свежего сообщения в экспорте, а не от сегодняшнего дня.
    """
    msgs = telegram_messages(folder) if messages is None else messages
    if not msgs:
        return
    newest = msgs[-1][0]
    for when, txt in msgs:
        yield txt, weight * recency_weight(when, newest, half_life_years)


# служебные вставки в сообщениях Claude Code: вставленный текст, напоминания, вывод команд
_CLAUDE_TAGS = re.compile(r"<(pasted_content|system-reminder|command-[a-z]+|local-command-[a-z]+|ide_[a-z_]+"
                          r"|task-notification|ci-monitor-event)[^>]*>.*?</\1>", re.S)


def _claude_text(d: dict) -> str:
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return ""  # isSidechain — промпты субагентам, их пишет Claude, а не пользователь
    c = d.get("message", {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return ""
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str):
        return ""
    t = _CLAUDE_TAGS.sub(" ", c).strip()
    if not t or t.startswith("<") or "autonomous-loop" in t or t.startswith("This session is being continued"):
        return ""
    return clean_markdown(t)


def claude_messages(root: Path) -> list[tuple[str, str]]:
    """Сообщения пользователя в Claude Code (~/.claude/projects/*/*.jsonl): (время ISO, текст), старые первыми, без повторов."""
    out, seen = [], set()
    if not root.is_dir():
        return out
    for p in root.rglob("*.jsonl"):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = _claude_text(d) if isinstance(d, dict) else ""
            if t.strip() and t not in seen:
                seen.add(t)
                out.append((d.get("timestamp", ""), t))
    out.sort(key=lambda x: x[0])
    return out


def iter_claude(root: Path, weight: float = 1.0) -> Iterator[tuple[str, float]]:
    """→ (текст, вес): сообщения пользователя в Claude Code. weight 0 — не брать."""
    if weight <= 0:
        return
    for _when, txt in claude_messages(root):
        yield txt, weight
