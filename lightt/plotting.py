from __future__ import annotations

import matplotlib
from matplotlib import font_manager


def configure_plot_font() -> bool:
    """Use an installed Korean font when available; otherwise use portable English labels."""
    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in (
        "Malgun Gothic",
        "Noto Sans CJK KR",
        "Noto Sans KR",
        "AppleGothic",
        "NanumGothic",
        "Arial Unicode MS",
    ):
        if candidate in available:
            matplotlib.rcParams["font.family"] = candidate
            matplotlib.rcParams["axes.unicode_minus"] = False
            return True
    matplotlib.rcParams["font.family"] = "DejaVu Sans"
    matplotlib.rcParams["axes.unicode_minus"] = False
    return False


KOREAN_PLOT_TEXT = configure_plot_font()


def plot_text(korean: str, english: str) -> str:
    return korean if KOREAN_PLOT_TEXT else english
