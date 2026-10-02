from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("HALFWORD_DATA") or TOOL_DIR / "data")  # переопределение — для тестов
BASE_MODEL = DATA_DIR / "base_model.pkl"
USER_MODEL = DATA_DIR / "user_model.json"
CONFIG_FILE = DATA_DIR / "config.json"
STATS_FILE = DATA_DIR / "stats.json"
USAGE_FILE = DATA_DIR / "usage.jsonl"  # журнал подсказок без текста
PREDICTOR_KEYS = ("min_conf_word", "min_conf_next", "min_support", "short_penalty")


TEXTS_DIR = DATA_DIR / "texts"          # сюда можно просто положить свои .txt и .md
TELEGRAM_DIR = DATA_DIR / "telegram"    # сюда класть папку экспорта Telegram (result.json)
CLAUDE_DIR = Path.home() / ".claude" / "projects"  # сообщения пользователя в Claude Code


def text_roots(cfg: "Config") -> list[Path]:
    """Папки с текстами для обучения: data/texts и папки из настройки text_dirs."""
    roots, seen = [], set()
    for d in [TEXTS_DIR, *cfg.text_dirs]:
        p = Path(os.path.expandvars(str(d))).expanduser()
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            roots.append(p)
    return roots


def train_texts(cfg: "Config"):
    """Корпус базовой модели: тексты из папок-источников, сообщения Telegram с весом по давности, сообщения Claude Code."""
    from itertools import chain
    from .corpus import iter_claude, iter_telegram, iter_texts
    return chain(iter_texts(text_roots(cfg)),
                 iter_telegram(TELEGRAM_DIR, cfg.tg_half_life_years, cfg.tg_weight),
                 iter_claude(CLAUDE_DIR, cfg.claude_weight))


DEFAULT_BLACKLIST = [
    # пароли и системные окна ввода
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "lockapp.exe",
    "logonui.exe", "consent.exe", "credentialuibroker.exe",
    # там Tab занят своим автодополнением
    "code.exe", "antigravity.exe", "cursor.exe", "devenv.exe", "idea64.exe", "pycharm64.exe",
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "mintty.exe",
    "openconsole.exe", "excel.exe",
]


@dataclass
class Config:
    enabled: bool = True
    accept_key: str = "tab"                 # tab | right
    learn: bool = True                      # дообучаться на наборе
    font: str = "Segoe UI"
    # пороги предсказателя (подобраны симуляцией, см. halfword/evaluate.py)
    min_conf_word: float = 0.35     # уверенность, чтобы дописать слово
    min_conf_next: float = 0.40     # уверенность, чтобы предложить слово до начала набора (0.30 → 0.40)
    min_support: int = 2            # сколько раз пара/тройка слов должна встретиться
    short_penalty: float = 0.07     # +к порогу за каждый символ префикса короче 4
    # Telegram в базовой модели (подбор — halfword.evaluate_tg): свежие сообщения весомее старых
    tg_half_life_years: float = 2.0  # вес вдвое меньше на каждые 2 года назад (0 — без затухания)
    tg_weight: float = 4.0           # множитель к весу сообщения против строки заметок
    claude_weight: float = 0.0       # сообщения пользователя в Claude Code; 0 — не брать (на замерах лучше всего ×4)
    text_dirs: list[str] = field(default_factory=list)  # папки с вашими текстами (.md, .txt) для обучения
    # LLM (llama.cpp): модель в %USERPROFILE%\.halfword\llm\models
    llm_enabled: bool = True
    llm_model: str = "gemma-3-1b-pt-q4_k_m.gguf"   # лёгкий вариант: gemma-3-270m-q8_0.gguf
    llm_only_on_ac: bool = True     # на батарее выгружать
    llm_min_prob: float = 0.40      # совокупная вероятность вставки (evaluate_llm: 0.35–0.45 оптимум)
    llm_max_words: int = 4
    llm_delay_ms: int = 120         # пауза в наборе перед запросом
    llm_threads: int = 4
    llm_idle_unload_min: int = 15   # выгрузить после простоя
    llm_ctx: int = 2048             # контекст сервера (токены)
    # режим «пауза»: длинное продолжение, когда перестал печатать
    llm_pause_ms: int = 600         # пауза в наборе перед длинной подсказкой
    llm_prompt_chars: int = 1000    # окно текста в промпте в паузе (быстрый режим — 350, пока окно не выросло)
    llm_long_tokens: int = 40       # максимум токенов продолжения
    llm_long_words: int = 20        # максимум слов
    llm_long_min_tok_p: float = 0.10  # обрезать на токене с p ниже (с 3-го слова): короче при той же точности
    llm_variants: int = 3           # жадный вариант + ветки (Alt+↓/↑)
    llm_rank: bool = False          # пересортировать варианты по оценке модели, когда ветки готовы
    llm_after_enter: bool = True    # подсказывать в начале новой строки
    llm_header: bool = True         # приложение и заголовок окна первой строкой промпта
    context_read_chars: int = 1500  # сколько текста перед курсором читать из поля (UIA)
    # точность
    min_insert_chars: int = 2       # подсказку короче не показывать (3 срезает экономию на 2.7 п.п.)
    next_min_tri: int = 2           # следующее слово без префикса: опора в триграмме не меньше
    fast_typing_ms: int = 180       # медиана пауз между нажатиями быстрее — набор «вслепую»
    fast_min_insert: int = 4        # на быстром наборе показывать только подсказки не короче
    doc_cache: bool = False         # слова и пары из текущего поля с повышенным весом (+0.1 п.п.)
    layout_fix: bool = True         # «ghbdtn» → «привет»: подсказка при не той раскладке
    snippets: dict[str, str] = field(default_factory=dict)  # свои сокращения: «спс» → «спасибо»
    banned_words: list[str] = field(default_factory=list)   # не подсказывать эти слова
    # обучение
    train_auto: bool = True         # раз в сутки на зарядке и в простое, если корпус изменился
    train_gate_pp: float = 0.5      # новая модель хуже прежней на столько п.п. экономии — не подменять
    # интерфейс
    overlay_theme: str = "auto"     # auto (как Windows) | light | dark
    panel_port: int = 8768          # веб-панель на 127.0.0.1 (занят — пробуются следующие)
    blacklist: list[str] = field(default_factory=lambda: list(DEFAULT_BLACKLIST))

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in d.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        else:
            cfg.save()
        cfg.blacklist = [x.lower() for x in cfg.blacklist]
        return cfg

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
