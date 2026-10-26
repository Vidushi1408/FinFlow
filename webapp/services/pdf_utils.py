"""Helpers for the PDF exports (reportlab)."""
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "vendor" / "fonts"
FONT, FONT_BOLD = "DejaVuSans", "DejaVuSans-Bold"
_registered = {"done": False, "glyphs": frozenset()}


def register_fonts():
    """Register DejaVu Sans (Bitstream Vera licence, vendored) once. ReportLab's built-in fonts have no rupee
    sign, so a literal ₹ printed as a black square; DejaVu has it, plus Greek/Cyrillic/most Latin."""
    if _registered["done"]:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    regular = TTFont(FONT, str(FONT_DIR / "DejaVuSans.ttf"))
    pdfmetrics.registerFont(regular)
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT, boldItalic=FONT_BOLD)
    _registered["glyphs"] = frozenset(regular.face.charToGlyph)
    _registered["done"] = True


def pdf_styles():
    """ReportLab's sample stylesheet with every style switched to the registered Unicode font."""
    from reportlab.lib.styles import getSampleStyleSheet
    register_fonts()
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        if not hasattr(style, "fontName"):       # e.g. ListStyle has no font
            continue
        style.fontName = FONT_BOLD if "bold" in style.fontName.lower() or style.name in ("Title", "Heading1", "Heading2", "Heading3") else FONT
    return styles


def pdf_safe(value, limit=None):
    """Text safe for the PDF font: characters it can't draw (e.g. Devanagari) become '?' instead of boxes."""
    register_fonts()
    text = "" if value is None else str(value)
    text = "".join(ch if ord(ch) in _registered["glyphs"] else "?" for ch in text)
    return text[:limit] if limit else text


def pdf_inr(amount):
    """'₹1,234.00'."""
    return f"₹{float(amount):,.2f}"
