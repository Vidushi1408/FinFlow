"""Helpers for the PDF exports (reportlab)."""


def pdf_inr(amount):
    """'Rs. 1,234.00'. ReportLab's built-in PDF fonts have no rupee glyph, so a literal ₹ prints as a
    black square; shipping a TTF just for one symbol would add a font file and a license to carry."""
    return f"Rs. {float(amount):,.2f}"
