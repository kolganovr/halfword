"""Иконка трея: тот же знак, что favicon панели — «h» и янтарная каретка на тёмной плитке.

Состояние читается по знаку: включено — светлая «h» и янтарная каретка; выключено/пауза — всё
приглушено; программа в чёрном списке — приглушено и перечёркнуто. Бейдж в углу — фоновая работа:
переобучение или загрузка LLM. Рисуем крупно и уменьшаем — у ImageDraw нет сглаживания.
"""
from PIL import Image, ImageDraw

SIZE = 64
_SS = 4                      # суперсэмплинг
_K = SIZE * _SS / 32         # координаты — в сетке 32×32, как у favicon

TILE = (23, 21, 18, 255)            # --surface
EDGE = (255, 240, 220, 46)          # --line-2: плитку видно на тёмной панели задач
FG = (237, 230, 219, 255)           # --fg
ACCENT = (232, 162, 58, 255)        # --accent
MUTED = (122, 114, 103, 255)        # между --faint и --muted
BAD = (224, 123, 108, 255)          # --bad
DOTS = {"train": (143, 191, 152, 255), "llm": (122, 178, 230, 255)}  # --ok; голубой — LLM
STROKE = 3.2                         # толще, чем в favicon: в трее иконка 16–24 px


def _bezier(p0, p1, p2, p3, n=24):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


# «h» из favicon: M9 8v16 · M9 16.5c1-2.6 3-4 5.2-4 2.6 0 3.8 1.7 3.8 4.6V24
_STEM = [(9, 8), (9, 24)]
_ARCH = ([(9, 16.5)] + _bezier((9, 16.5), (10, 13.9), (12, 12.5), (14.2, 12.5))
         + _bezier((14.2, 12.5), (16.8, 12.5), (18, 14.2), (18, 17.1)) + [(18, 24)])


def _line(d, pts, col, w):
    """Ломаная с круглыми концами и стыками."""
    pts = [(x * _K, y * _K) for x, y in pts]
    r = w * _K / 2
    d.line(pts, fill=col, width=round(w * _K))
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=col)


def _box(x0, y0, x1, y1):
    return (x0 * _K, y0 * _K, x1 * _K, y1 * _K)


def draw(base: str, dot: str | None = None) -> Image.Image:
    """base: on / off / blocked; dot: None / train / llm."""
    img = Image.new("RGBA", (SIZE * _SS, SIZE * _SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(_box(0.5, 0.5, 31.5, 31.5), 7.5 * _K, fill=TILE, outline=EDGE, width=round(_K))
    on = base == "on"
    glyph = FG if on else MUTED
    _line(d, _STEM, glyph, STROKE)
    _line(d, _ARCH, glyph, STROKE)
    d.rounded_rectangle(_box(21.6, 8.4, 24.8, 24.6), 1.6 * _K, fill=ACCENT if on else MUTED)
    if base == "blocked":
        _line(d, [(6, 26), (26, 6)], TILE, STROKE + 2.4)  # зазор вокруг черты
        _line(d, [(6, 26), (26, 6)], BAD, STROKE)
    if dot:
        d.ellipse(_box(19.5, 19.5, 31.5, 31.5), fill=TILE)
        d.ellipse(_box(21.5, 21.5, 29.5, 29.5), fill=DOTS[dot])
    return img.resize((SIZE, SIZE), Image.LANCZOS)


def all_states() -> dict:
    """{(base, dot): Image} — все 9 вариантов, рисуются один раз при запуске."""
    return {(b, dot): draw(b, dot) for b in ("on", "off", "blocked") for dot in (None, *DOTS)}
