"""
Design tokens for FD Studio.

Single source of truth. No hex value and no spacing number appears anywhere else
in the application — if something needs a colour or a gap, it comes from here.

Dark is the primary theme; light is derived from it. Both are complete.
"""

from __future__ import annotations

# ── spacing: 4pt grid, these values only ──────────────────────────────────────
S1, S2, S3, S4, S6, S8, S12 = 4, 8, 12, 16, 24, 32, 48

# ── radii ─────────────────────────────────────────────────────────────────────
R_CTRL, R_CARD, R_PANEL, R_PILL = 6, 10, 14, 999

# ── type ──────────────────────────────────────────────────────────────────────
# Inter is not bundled (no binary to ship), so this resolves to Segoe UI Variable
# on Windows, SF on macOS, and a sane fallback elsewhere.
FONT_UI = '"Segoe UI Variable Text", "SF Pro Text", "Segoe UI", Inter, sans-serif'
FONT_MONO = '"Cascadia Code", "SF Mono", "JetBrains Mono", Consolas, monospace'

T_DISPLAY = (28, 600, -0.02)
T_TITLE = (20, 600, -0.01)
T_HEADING = (15, 590, 0.0)
T_BODY = (13, 400, 0.0)
T_CAPTION = (11, 500, 0.01)
T_MICRO = (10, 600, 0.06)

DARK = {
    "surface_0": "#131316",
    "surface_1": "#1A1A1F",
    "surface_2": "#212127",
    "surface_3": "#2A2A32",
    "hairline": "rgba(255,255,255,0.08)",
    "hairline_st": "rgba(255,255,255,0.14)",
    "text_1": "#F5F5F7",
    "text_2": "rgba(245,245,247,0.62)",
    "text_3": "rgba(245,245,247,0.38)",
    "accent": "#0A84FF",
    "success": "#30D158",
    "warning": "#FF9F0A",
    "danger": "#FF453A",
}

LIGHT = {
    "surface_0": "#FFFFFF",
    "surface_1": "#F5F5F7",
    "surface_2": "#FFFFFF",
    "surface_3": "#ECECEF",
    "hairline": "rgba(0,0,0,0.10)",
    "hairline_st": "rgba(0,0,0,0.18)",
    "text_1": "#1D1D1F",
    "text_2": "rgba(29,29,31,0.60)",
    "text_3": "rgba(29,29,31,0.38)",
    "accent": "#007AFF",
    "success": "#34C759",
    "warning": "#FF9500",
    "danger": "#FF3B30",
}

MOTION_ENABLED = True

_active = dict(DARK)


def use(theme: str) -> None:
    global _active
    _active = dict(DARK if theme == "dark" else LIGHT)


def c(name: str) -> str:
    """Token lookup. Raises rather than silently returning a wrong colour."""
    return _active[name]


def rgba_to_qcolor(value: str):
    """Accept both '#RRGGBB' and 'rgba(r,g,b,a)' so tokens stay one format."""
    from PySide6.QtGui import QColor

    if value.startswith("rgba"):
        parts = value[value.index("(") + 1: value.rindex(")")].split(",")
        r, g, b = (int(p) for p in parts[:3])
        a = float(parts[3])
        col = QColor(r, g, b)
        col.setAlphaF(a)
        return col
    return QColor(value)


def qss() -> str:
    """Application stylesheet, generated from the active token set."""
    t = _active
    return f"""
    QWidget {{
        background: transparent;
        color: {t['text_1']};
        font-family: {FONT_UI};
        font-size: {T_BODY[0]}px;
    }}
    #Root      {{ background: {t['surface_0']}; border-radius: {R_PANEL}px; }}
    #Toolbar   {{ background: {t['surface_1']}; border-bottom: 1px solid {t['hairline']}; }}
    #Sidebar   {{ background: {t['surface_1']}; border-right: 1px solid {t['hairline']}; }}
    #Inspector {{ background: {t['surface_1']}; border-left: 1px solid {t['hairline']}; }}
    #Status    {{ background: {t['surface_1']}; border-top: 1px solid {t['hairline']}; }}

    QLabel#Micro {{
        color: {t['text_3']};
        font-size: {T_MICRO[0]}px;
        font-weight: {T_MICRO[1]};
        letter-spacing: {T_MICRO[2]}em;
    }}
    QLabel#Caption  {{ color: {t['text_2']}; font-size: {T_CAPTION[0]}px; }}
    QLabel#Heading  {{ font-size: {T_HEADING[0]}px; font-weight: {T_HEADING[1]}; }}
    QLabel#Mono     {{ font-family: {FONT_MONO}; color: {t['text_1']}; }}
    QLabel#MonoDim  {{ font-family: {FONT_MONO}; color: {t['text_2']}; font-size: {T_CAPTION[0]}px; }}

    QPushButton {{
        background: {t['surface_2']};
        border: 1px solid {t['hairline']};
        border-radius: {R_CTRL}px;
        padding: 6px 14px;
        color: {t['text_1']};
    }}
    QPushButton:hover   {{ background: {t['surface_3']}; }}
    QPushButton:pressed {{ background: {t['surface_1']}; }}
    QPushButton:disabled{{ color: {t['text_3']}; background: {t['surface_1']}; }}
    QPushButton#Primary {{
        background: {t['accent']};
        border: 1px solid {t['accent']};
        color: #FFFFFF;
    }}
    QPushButton#Primary:hover    {{ background: {t['accent']}; }}
    QPushButton#Primary:disabled {{ background: {t['surface_1']}; border-color: {t['hairline']};
                                    color: {t['text_3']}; }}
    QPushButton#Danger {{
        background: {t['danger']};
        border: 1px solid {t['danger']};
        color: #FFFFFF;
        font-size: {T_HEADING[0]}px;
        font-weight: {T_HEADING[1]};
    }}

    /* Segmented control: the User tab's wear-position picker. Reads as one
       control with a selected segment, not three independent buttons. */
    QPushButton#Segment {{
        background: {t['surface_2']};
        border: 1px solid {t['hairline']};
        color: {t['text_2']};
        font-weight: 600;
    }}
    QPushButton#Segment:hover   {{ background: {t['surface_3']}; }}
    QPushButton#Segment:checked {{
        background: {t['accent']};
        border-color: {t['accent']};
        color: #FFFFFF;
    }}

    QTabWidget#Tabs::pane {{ border: none; background: {t['surface_0']}; }}
    QTabBar {{ background: {t['surface_1']}; }}
    QTabBar::tab {{
        background: transparent;
        color: {t['text_2']};
        padding: 9px 20px;
        margin: 0;
        border: none;
        border-bottom: 2px solid transparent;
        font-size: {T_BODY[0]}px;
        font-weight: 600;
    }}
    QTabBar::tab:hover    {{ color: {t['text_1']}; }}
    QTabBar::tab:selected {{ color: {t['text_1']}; border-bottom-color: {t['accent']}; }}

    QProgressBar {{
        background: {t['surface_3']};
        border: none;
        border-radius: 3px;
    }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}

    QComboBox, QSpinBox, QLineEdit {{
        background: {t['surface_2']};
        border: 1px solid {t['hairline']};
        border-radius: {R_CTRL}px;
        padding: 5px 10px;
        min-height: 20px;
        selection-background-color: {t['accent']};
    }}
    QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {t['hairline_st']}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {t['surface_2']};
        border: 1px solid {t['hairline_st']};
        selection-background-color: {t['surface_3']};
        outline: none;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 14px; border: none;
                                                  background: {t['surface_3']}; }}

    QPlainTextEdit {{
        background: {t['surface_0']};
        border: 1px solid {t['hairline']};
        border-radius: {R_CARD}px;
        font-family: {FONT_MONO};
        font-size: {T_CAPTION[0]}px;
        color: {t['text_2']};
    }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {t['surface_3']}; border-radius: 5px;
                                   min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """
