"""Deterministic PDF parsing for menu ingestion (§6.1 step 1).

Wraps pdfplumber. Returns:
- `text`: concatenated per-page text (page separator: form-feed U+000C)
- `tables`: list of {page_index, rows: list[list[str|None]]}

No LLM, no normalization here. The Menu Intelligence Agent decides what to do
with the parsed content next. Errors surface as `PdfParseError` so the caller
can HITL or degrade rather than silently returning empty text.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import pdfplumber


class PdfParseError(RuntimeError):
    """Raised when pdfplumber cannot open or read the supplied bytes."""


@dataclass(frozen=True)
class PdfTable:
    page_index: int
    rows: list[list[str | None]]


@dataclass(frozen=True)
class ParsedPdf:
    text: str
    tables: list[PdfTable] = field(default_factory=list)
    page_count: int = 0


def parse_pdf_bytes(data: bytes) -> ParsedPdf:
    """Deterministic extract of text + tables. Empty pages become empty strings.

    We keep tables even when text extraction hits them so the LLM step gets both
    shapes — mess menu PDFs often lay dishes out as a weekday×meal grid.
    """
    if not data:
        raise PdfParseError("pdf payload is empty")
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # pdfplumber raises varied exception types
        raise PdfParseError(f"pdfplumber failed to open document: {exc}") from exc

    page_texts: list[str] = []
    tables: list[PdfTable] = []
    try:
        for i, page in enumerate(pdf.pages):
            page_texts.append(page.extract_text() or "")
            for raw in page.extract_tables() or []:
                rows = [[(cell if cell is None else str(cell).strip()) for cell in row] for row in raw]
                tables.append(PdfTable(page_index=i, rows=rows))
        return ParsedPdf(text="\f".join(page_texts), tables=tables, page_count=len(pdf.pages))
    finally:
        pdf.close()


def parsed_pdf_to_llm_snippet(parsed: ParsedPdf, *, max_chars: int = 12_000) -> str:
    """Compact, deterministic representation for the LLM extractor.

    Full text first (truncated at max_chars), then a summary of the first tables.
    Truncation is deterministic so idempotency-key stability is preserved when
    the same PDF is re-uploaded.
    """
    parts: list[str] = []
    text = parsed.text or ""
    parts.append(text[:max_chars])
    if parsed.tables:
        parts.append("\n\n---TABLES---")
        for tbl in parsed.tables[:8]:
            parts.append(f"[page {tbl.page_index + 1}]")
            for row in tbl.rows[:32]:
                parts.append(" | ".join("" if c is None else c for c in row))
    return "\n".join(parts)


def summarize_parsed_pdf(parsed: ParsedPdf) -> dict[str, Any]:
    """Small dict for observability / decision_traces without dumping the whole text."""
    return {
        "page_count": parsed.page_count,
        "text_chars": len(parsed.text),
        "table_count": len(parsed.tables),
    }
