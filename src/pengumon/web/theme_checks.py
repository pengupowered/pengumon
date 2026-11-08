"""What must hold in every theme (docs/themes.md, "Checks"): text contrast, status colors that stay apart, and series
colors that can be told apart, also by people with red-green color blindness. tests/test_themes.py runs every theme
folder through `problems`; the numbers are the ones the default theme was designed against.

Contrast is WCAG 2's (relative luminance); a translucent color (an -bg token, the scrim) is laid over the surface it
sits on first. Distances are OKLab's, x100 (a just noticeable difference is about 2). Color blindness is simulated
with Machado, Oliveira and Fernandes (2009) at full severity, in linear RGB.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from pengumon.web.themes import Theme, parse_color

RGB = Tuple[float, float, float]  # 0..1, gamma-encoded sRGB

# ---------- the rules ----------

TEXT = 4.5      # body text (WCAG AA)
UI = 3.0        # large text, icons, focus rings, status dots (WCAG AA for UI components)
SERIES_DISTANCE = 8.0       # adjacent series colors, normal vision
SERIES_CVD_DISTANCE = 6.0   # the same under deuteranopia and protanopia
STATUS_DISTANCE = 12.0      # good, warn, crit and busy from one another (normal vision; they also carry an icon and a word)

SURFACES = ("--bg", "--surface-1", "--surface-2")
# (text token, the surfaces it is read on, minimum)
CONTRAST: Tuple[Tuple[str, Sequence[str], float], ...] = (
    ("--ink-1", SURFACES + ("--surface-3",), TEXT),
    ("--ink-2", SURFACES + ("--surface-3",), TEXT),
    ("--ink-3", SURFACES, TEXT),
    ("--ink-4", ("--surface-1", "--surface-2"), UI),  # >= 12 px non-essential text only (tokens.css)
    ("--accent", ("--surface-1", "--surface-2"), TEXT),  # links
    ("--focus", SURFACES, UI),
    ("--good", ("--surface-1",), UI),
    ("--warn", ("--surface-1",), UI),
    ("--serious", ("--surface-1",), UI),
    ("--crit", ("--surface-1",), UI),
    ("--busy", ("--surface-1",), UI),
    ("--unknown", ("--surface-1",), UI),
)
# a status chip: its word in the status color on its own -bg, over a panel
CHIPS = ("good", "warn", "crit", "busy")
STATUS = ("--good", "--warn", "--crit", "--busy")
SERIES = tuple(f"--s{i}" for i in range(1, 9))


# ---------- color arithmetic ----------

def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _enc(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def rgb(text: str, under: RGB = (0.0, 0.0, 0.0)) -> RGB:
    """A token's color as 0..1 sRGB; a translucent one laid over `under`."""
    parsed = parse_color(text)
    if parsed is None:
        raise ValueError(f"not a color: {text!r}")
    r, g, b, a = parsed
    return tuple(a * c / 255 + (1 - a) * u for c, u in zip((r, g, b), under))  # type: ignore[return-value]


def luminance(c: RGB) -> float:
    r, g, b = (_lin(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def oklab(c: RGB) -> Tuple[float, float, float]:
    r, g, b = (_lin(x) for x in c)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def distance(a: RGB, b: RGB) -> float:
    """OKLab distance x100."""
    return 100 * sum((x - y) ** 2 for x, y in zip(oklab(a), oklab(b))) ** 0.5


_CVD = {
    "deuteranopia": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "protanopia": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
}


def simulate(c: RGB, kind: str) -> RGB:
    lin = [_lin(x) for x in c]
    return tuple(_enc(sum(m * v for m, v in zip(row, lin))) for row in _CVD[kind])  # type: ignore[return-value]


# ---------- the checks ----------

def mode_problems(tokens: Dict[str, str], where: str = "") -> List[str]:
    """What fails in one mode's resolved tokens, in words ("dark: --ink-3 on --bg is 3.9:1, needs 4.5")."""
    out: List[str] = []
    black = (0.0, 0.0, 0.0)
    surfaces = {n: rgb(tokens[n], black) for n in (*SURFACES, "--surface-3")}
    for text, on, need in CONTRAST:
        for s in on:
            ratio = contrast(rgb(tokens[text], surfaces[s]), surfaces[s])
            if ratio < need:
                out.append(f"{where}{text} on {s} is {ratio:.2f}:1, needs {need:g}")
    accent = rgb(tokens["--accent"], surfaces["--surface-1"])
    ratio = contrast(rgb(tokens["--on-accent"], accent), accent)
    if ratio < TEXT:
        out.append(f"{where}--on-accent on --accent is {ratio:.2f}:1, needs {TEXT:g}")
    for name in CHIPS:
        chip = rgb(tokens[f"--{name}-bg"], surfaces["--surface-1"])
        ratio = contrast(rgb(tokens[f"--{name}"], chip), chip)
        if ratio < TEXT:
            out.append(f"{where}--{name} on --{name}-bg is {ratio:.2f}:1, needs {TEXT:g} (a status chip's word)")
    status = [(n, rgb(tokens[n], surfaces["--surface-1"])) for n in STATUS]
    for i, (a, ca) in enumerate(status):
        for b, cb in status[i + 1:]:
            d = distance(ca, cb)
            if d < STATUS_DISTANCE:
                out.append(f"{where}{a} and {b} are {d:.1f} apart, need {STATUS_DISTANCE:g}")
    series = [rgb(tokens[n], surfaces["--surface-1"]) for n in SERIES]
    for i in range(len(series) - 1):
        a, b = series[i], series[i + 1]
        d = distance(a, b)
        if d < SERIES_DISTANCE:
            out.append(f"{where}{SERIES[i]} and {SERIES[i + 1]} are {d:.1f} apart, need {SERIES_DISTANCE:g}")
        for kind in _CVD:
            d = distance(simulate(a, kind), simulate(b, kind))
            if d < SERIES_CVD_DISTANCE:
                out.append(f"{where}{SERIES[i]} and {SERIES[i + 1]} are {d:.1f} apart under {kind}, need "
                           f"{SERIES_CVD_DISTANCE:g}")
    return out


def problems(theme: Theme, modes: Iterable[str] = ()) -> List[str]:
    """Every rule that `theme` breaks, in each of its modes."""
    out: List[str] = []
    for mode in modes or theme.modes:
        out.extend(mode_problems(theme.resolved(mode), f"{theme.id} {mode}: "))
    return out
