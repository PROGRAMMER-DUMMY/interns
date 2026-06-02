"""Propose-only document content classifier (Phase 2).

Reads the structured JSON sidecar produced by scan_document() and emits
candidate records, each tagged with provenance {source, page, bbox}.

Routing table (from docs/design/pdf_ingestion.md, Section 6):

  Detected shape                   -> Candidate type
  -------------------------------------------------------
  Table with KPI/metric/cuts cols  -> kpi_registry_candidate
  Term | definition table          -> lexicon_candidate
  ERD boxes + FK text              -> data_model_candidate
  Prose rules / SLAs               -> open_question_candidate
  Anything else                    -> raw evidence (no route)

IMPORTANT: This module is PROPOSE-ONLY. It emits candidate records; it does
NOT auto-promote anything into any contract, and it does NOT touch any
existing workspace contract (kpi_registry.json, workspace_lexicon.json, etc.).

Every candidate is tagged:
  source: "document_pdf"
  authoritative: False
  review_required: True
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Candidate type constants
# ---------------------------------------------------------------------------

CANDIDATE_KPI_REGISTRY = "kpi_registry_candidate"
CANDIDATE_LEXICON = "lexicon_candidate"
CANDIDATE_DATA_MODEL = "data_model_candidate"
CANDIDATE_OPEN_QUESTION = "open_question_candidate"
CANDIDATE_RAW_EVIDENCE = "raw_evidence"

# Column heading signals for each route
_KPI_HEADER_TOKENS: frozenset[str] = frozenset({
    "kpi", "metric", "measure", "indicator", "numerator", "denominator",
    "cut", "cuts", "dimension", "target", "threshold", "frequency",
    "formula", "definition", "owner",
})
_LEXICON_HEADER_TOKENS: frozenset[str] = frozenset({
    "term", "definition", "description", "abbreviation", "glossary",
    "concept", "meaning", "explanation",
})
_ERD_TOKENS: frozenset[str] = frozenset({
    "fk", "pk", "foreign key", "primary key", "foreign_key", "primary_key",
    "entity", "relationship", "table", "column", "join", "references", "ref",
})
_PROSE_RULE_TOKENS: frozenset[str] = frozenset({
    "must", "shall", "should", "required", "sla", "rule", "policy",
    "constraint", "business rule", "standard", "requirement",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_document(doc_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Map structured PDF sidecar content to propose-only candidate records.

    Parameters
    ----------
    doc_json:
        The ``extracted_content`` dict from a document sidecar produced by
        scan_document(), or any dict following the opendataloader-pdf JSON
        schema (pages -> blocks -> type/text/table/bbox).

    Returns
    -------
    List of candidate dicts. Each has:
        candidate_type: str
        source: "document_pdf"
        page: int | None
        bbox: dict | None
        content: dict  (type-specific payload)
        authoritative: False
        review_required: True
        confidence: float (0-1)
        reason: str
    """
    candidates: list[dict[str, Any]] = []

    if not isinstance(doc_json, dict):
        return candidates

    # Real opendataloader-pdf shape is a recursive tree:
    #   {"file name": ..., "number of pages": N, "kids": [ {type, content,
    #    "page number", "bounding box", kids: [...]}, ... ]}
    # Walk the tree and classify every typed node. (The "pages"/"blocks" and
    # flat-dict shapes below are kept for synthetic test fixtures.)
    tree_nodes = _iter_tree_nodes(doc_json)
    if tree_nodes:
        for node in tree_nodes:
            page_num = node.get("page number") or node.get("page_number") or node.get("page")
            candidates.extend(_classify_block(node, page_num))
        return _deduplicate(candidates)

    # opendataloader-pdf (assumed) JSON shape: {"pages": [{..., "blocks": [...]}]}
    pages = doc_json.get("pages") or []
    if not pages:
        # Flat dict (e.g. test fixtures or wrapped sidecar)
        pages = [doc_json]

    for page_obj in pages:
        if not isinstance(page_obj, dict):
            continue
        page_num = page_obj.get("page_number") or page_obj.get("page") or None
        blocks = page_obj.get("blocks") or page_obj.get("elements") or []
        if not isinstance(blocks, list):
            blocks = [page_obj]

        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_candidates = _classify_block(block, page_num)
            candidates.extend(block_candidates)

    # Deduplicate by (candidate_type, page, repr(content))
    return _deduplicate(candidates)


# ---------------------------------------------------------------------------
# Block-level classifier
# ---------------------------------------------------------------------------

def _iter_tree_nodes(doc_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an opendataloader-pdf ``kids`` tree into a list of typed nodes.

    Returns [] when the document is not a kids-tree (so the caller falls back to
    the pages/blocks or flat-dict shapes used by synthetic test fixtures)."""
    if not isinstance(doc_json, dict) or not isinstance(
        doc_json.get("kids") or doc_json.get("children"), list
    ):
        return []
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") or node.get("block_type"):
                out.append(node)
            for key in ("kids", "children", "blocks", "elements"):
                child = node.get(key)
                if isinstance(child, list):
                    for c in child:
                        walk(c)
        elif isinstance(node, list):
            for c in node:
                walk(c)

    for key in ("kids", "children"):
        top = doc_json.get(key)
        if isinstance(top, list):
            for c in top:
                walk(c)
    return out


def _classify_block(block: dict[str, Any], page: Any) -> list[dict[str, Any]]:
    bbox = block.get("bbox") or block.get("bounding_box") or block.get("bounding box") or None
    block_type = str(block.get("type") or block.get("block_type") or "").lower()
    candidates: list[dict[str, Any]] = []

    # Tables
    if block_type in {"table", "table_block"} or "table" in block_type:
        candidates.extend(_classify_table_block(block, page, bbox))
        return candidates

    # Heading + text blocks -- check for ERD signals or prose rules
    text = _extract_text(block)
    if not text:
        return candidates

    # ERD / data-model signals
    if _has_erd_signals(text, block):
        candidates.append(_make_candidate(
            CANDIDATE_DATA_MODEL,
            page=page,
            bbox=bbox,
            content={
                "text_snippet": text[:500],
                "detected_signals": _detected_erd_signals(text),
            },
            confidence=0.60,
            reason="Block contains ERD/FK/PK/entity relationship signals.",
        ))
        return candidates

    # Prose rules / SLAs
    if _has_prose_rule_signals(text):
        candidates.append(_make_candidate(
            CANDIDATE_OPEN_QUESTION,
            page=page,
            bbox=bbox,
            content={
                "text_snippet": text[:500],
                "detected_signals": _detected_prose_signals(text),
            },
            confidence=0.55,
            reason="Block contains business rule / SLA / policy language.",
        ))

    return candidates


def _classify_table_block(
    block: dict[str, Any],
    page: Any,
    bbox: Any,
) -> list[dict[str, Any]]:
    """Route a table block to one or more candidate types."""
    headers = _extract_table_headers(block)
    rows = _extract_table_rows(block)
    candidates: list[dict[str, Any]] = []

    if not headers and not rows:
        return candidates

    header_norms = {_norm(h) for h in headers}

    # KPI / metric table
    kpi_score = _score_token_overlap(header_norms, _KPI_HEADER_TOKENS)
    if kpi_score >= 0.15:
        candidates.append(_make_candidate(
            CANDIDATE_KPI_REGISTRY,
            page=page,
            bbox=bbox,
            content={
                "headers": headers,
                "row_count": len(rows),
                "sample_rows": rows[:3],
                "matched_signals": sorted(header_norms & _KPI_HEADER_TOKENS),
            },
            confidence=min(0.40 + kpi_score, 0.90),
            reason=(
                f"Table headers overlap with KPI/metric signals "
                f"({sorted(header_norms & _KPI_HEADER_TOKENS)})."
            ),
        ))

    # Lexicon / term-definition table
    lex_score = _score_token_overlap(header_norms, _LEXICON_HEADER_TOKENS)
    if lex_score >= 0.20:
        candidates.append(_make_candidate(
            CANDIDATE_LEXICON,
            page=page,
            bbox=bbox,
            content={
                "headers": headers,
                "row_count": len(rows),
                "sample_rows": rows[:3],
                "matched_signals": sorted(header_norms & _LEXICON_HEADER_TOKENS),
            },
            confidence=min(0.40 + lex_score, 0.90),
            reason=(
                f"Table headers overlap with term/definition signals "
                f"({sorted(header_norms & _LEXICON_HEADER_TOKENS)})."
            ),
        ))

    # ERD / FK table
    erd_score = _score_token_overlap(header_norms, _ERD_TOKENS)
    full_text = " ".join(str(h) for h in headers) + " ".join(
        str(v) for row in rows for v in (row.values() if isinstance(row, dict) else row)
    )
    if erd_score >= 0.15 or _has_erd_signals(full_text, block):
        candidates.append(_make_candidate(
            CANDIDATE_DATA_MODEL,
            page=page,
            bbox=bbox,
            content={
                "headers": headers,
                "row_count": len(rows),
                "sample_rows": rows[:3],
                "detected_signals": _detected_erd_signals(full_text),
            },
            confidence=min(0.40 + erd_score, 0.85),
            reason="Table contains ERD/FK/entity-relationship signals.",
        ))

    if not candidates:
        # Raw evidence: table detected but no routing signal
        candidates.append(_make_candidate(
            CANDIDATE_RAW_EVIDENCE,
            page=page,
            bbox=bbox,
            content={
                "headers": headers,
                "row_count": len(rows),
            },
            confidence=0.30,
            reason="Table detected but no routing signal matched; stored as raw evidence.",
        ))

    return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    candidate_type: str,
    *,
    page: Any,
    bbox: Any,
    content: dict[str, Any],
    confidence: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "candidate_type": candidate_type,
        "source": "document_pdf",
        "page": page,
        "bbox": bbox,
        "content": content,
        "authoritative": False,
        "review_required": True,
        "confidence": round(confidence, 3),
        "reason": reason,
    }


def _extract_text(block: dict[str, Any]) -> str:
    """Extract plain text from a block dict."""
    for key in ("text", "content", "raw_text", "value"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list):
            return " ".join(str(v) for v in val if v).strip()
    return ""


def _extract_cell_text(cell: dict[str, Any]) -> str:
    """Extract text from a real opendataloader-pdf 2.4.7 table-cell node.

    Real schema:
      {type: "table cell", ..., content: None,
       kids: [{type: "paragraph", ..., content: "<text>"}]}

    Also handles legacy/synthetic fixtures where cells carry a plain
    ``content`` string directly (no kids).
    """
    # Real shape: text lives in the first kid paragraph's content
    kids = cell.get("kids") or cell.get("children") or []
    if isinstance(kids, list) and kids:
        for kid in kids:
            if isinstance(kid, dict):
                c = kid.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()

    # Legacy / synthetic fixture: content is a plain string on the cell itself
    direct = cell.get("content")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    return ""


def _extract_table_headers_real(block: dict[str, Any]) -> list[str]:
    """Extract header row from a REAL opendataloader-pdf 2.4.7 table node.

    Real schema:
      {type: "table",
       "number of rows": N, "number of columns": M,
       rows: [{type: "table row", "row number": 1, cells: [...]}, ...]}

    Row 1 (row_number == 1) is treated as the header row.
    Returns [] when the block does not match the real schema.
    """
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        return []

    first_row = rows[0]
    if not isinstance(first_row, dict) or first_row.get("type") != "table row":
        return []

    cells = first_row.get("cells") or []
    if not isinstance(cells, list) or not cells:
        return []

    # Confirm cells have the real cell shape (type == "table cell")
    if not any(isinstance(c, dict) and c.get("type") == "table cell" for c in cells):
        return []

    return [_extract_cell_text(c) for c in cells if isinstance(c, dict)]


def _extract_table_rows_real(block: dict[str, Any]) -> list[dict[str, str]]:
    """Extract data rows (skipping header row 1) from a real table node.

    Returns a list of dicts keyed by column index (as string) or by header
    text when headers are available.  Each value is the cell text.
    """
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        return []

    headers = _extract_table_headers_real(block)
    result: list[dict[str, str]] = []

    for row in rows[1:]:  # skip header row
        if not isinstance(row, dict) or row.get("type") != "table row":
            continue
        cells = row.get("cells") or []
        if not isinstance(cells, list):
            continue
        row_dict: dict[str, str] = {}
        for i, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            key = headers[i] if i < len(headers) else str(i)
            row_dict[key] = _extract_cell_text(cell)
        result.append(row_dict)

    return result


def _extract_table_headers(block: dict[str, Any]) -> list[str]:
    """Extract column headers from a table block.

    Tries the real opendataloader-pdf 2.4.7 schema first (rows of table-row
    + table-cell nodes), then falls back to the legacy flat-key shapes used
    by synthetic test fixtures (``headers``, ``columns``, etc.).
    """
    # Real opendataloader-pdf 2.4.7 schema
    real_headers = _extract_table_headers_real(block)
    if real_headers:
        return real_headers

    # Legacy / synthetic fixture shapes
    for key in ("headers", "columns", "column_names", "header_row"):
        val = block.get(key)
        if isinstance(val, list) and val:
            return [str(h) for h in val if h is not None]
    # Try rows[0] as headers if rows is a list of lists
    rows = block.get("rows") or block.get("data") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], list):
        return [str(h) for h in rows[0]]
    # rows as list of dicts: use keys of first row
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return list(rows[0].keys())
    return []


def _extract_table_rows(block: dict[str, Any]) -> list[Any]:
    """Extract data rows from a table block.

    Tries the real opendataloader-pdf 2.4.7 schema first, then falls back to
    legacy flat-key shapes used by synthetic test fixtures.
    """
    # Real opendataloader-pdf 2.4.7 schema
    real_rows = _extract_table_rows_real(block)
    if real_rows:
        return real_rows

    # Legacy / synthetic fixture shapes
    for key in ("rows", "data", "body", "cells"):
        val = block.get(key)
        if isinstance(val, list) and val:
            # Skip first row if it was used as headers
            headers = _extract_table_headers(block)
            if headers and isinstance(val[0], list):
                return val[1:]
            return val
    return []


def _has_erd_signals(text: str, block: dict[str, Any]) -> bool:
    text_norm = text.lower()
    # FK/PK abbreviations
    if re.search(r"\b(fk|pk|foreign[_ ]key|primary[_ ]key)\b", text_norm):
        return True
    # Arrow notation common in ERDs
    if re.search(r"\b\w+\s*->\s*\w+|\b\w+\s*\|\|[\-o<>]+", text_norm):
        return True
    # Entity/table/relationship wording with column mentions
    if re.search(r"\b(entity|entities|relationship|references?|joins?)\b", text_norm):
        return True
    return False


def _detected_erd_signals(text: str) -> list[str]:
    signals = []
    text_norm = text.lower()
    for token in sorted(_ERD_TOKENS):
        if token in text_norm:
            signals.append(token)
    return signals[:10]


def _has_prose_rule_signals(text: str) -> bool:
    text_norm = text.lower()
    for token in _PROSE_RULE_TOKENS:
        if re.search(r"\b" + re.escape(token) + r"\b", text_norm):
            return True
    return False


def _detected_prose_signals(text: str) -> list[str]:
    signals = []
    text_norm = text.lower()
    for token in sorted(_PROSE_RULE_TOKENS):
        if re.search(r"\b" + re.escape(token) + r"\b", text_norm):
            signals.append(token)
    return signals[:10]


def _score_token_overlap(header_norms: set[str], signal_tokens: frozenset[str]) -> float:
    """Overlap score: matched_count / max(header_count, 1)."""
    if not header_norms:
        return 0.0
    matched = len(header_norms & signal_tokens)
    return matched / max(len(header_norms), 1)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _deduplicate(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for c in candidates:
        key = f"{c['candidate_type']}|{c['page']}|{repr(c['content'])}"
        if key in seen:
            continue
        seen.add(key)
        result.append(c)
    return result


__all__ = [
    "CANDIDATE_KPI_REGISTRY",
    "CANDIDATE_LEXICON",
    "CANDIDATE_DATA_MODEL",
    "CANDIDATE_OPEN_QUESTION",
    "CANDIDATE_RAW_EVIDENCE",
    "classify_document",
]
