"""pdf_parse smoke test with a synthetic PDF (skipped if reportlab is unavailable)."""
from __future__ import annotations

import io

import pytest

from backend.tools.pdf_parse import PdfParseError, parse_pdf_bytes


def _try_make_pdf(text: str) -> bytes:
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed; skipping pdf_parse smoke test")
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 800, text)
    c.save()
    return buf.getvalue()


def test_parse_pdf_bytes_reads_text():
    pdf = _try_make_pdf("Aloo Paratha")
    parsed = parse_pdf_bytes(pdf)
    assert parsed.page_count == 1
    assert "aloo" in parsed.text.lower()


def test_parse_pdf_bytes_rejects_empty():
    with pytest.raises(PdfParseError):
        parse_pdf_bytes(b"")


def test_parse_pdf_bytes_rejects_garbage():
    with pytest.raises(PdfParseError):
        parse_pdf_bytes(b"not a pdf")
