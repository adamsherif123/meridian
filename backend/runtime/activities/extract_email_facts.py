"""extract_email_facts — Temporal activity.

Uses the LLM to extract structured shipment facts (shipment identifier + invoice numbers)
from the email subject and body. General and spec-driven: what to extract is guided by
hint strings derived from the spec's resolved_assumptions and key_field config.

Handles sender-specific abbreviation patterns (e.g. "100/26-27/235, 238" → ["235", "238"])
that rigid regex cannot.  Falls back to regex on any LLM failure — never raises.
"""
import json
import logging
import os
import re

from temporalio import activity

from backend.runtime.activities._types import (
    ExtractEmailFactsInput,
    ExtractEmailFactsResult,
)

log = logging.getLogger(__name__)

MODEL     = "claude-sonnet-4-6"
BODY_CAP  = 3_000   # chars of body sent to LLM; headers hold the key facts
MAX_TOKENS = 512    # response is a small JSON object


# ── LLM extraction ─────────────────────────────────────────────────────────────

def _build_prompt(inp: ExtractEmailFactsInput) -> str:
    body_preview = (inp.body or "")[:BODY_CAP]
    parts = []
    if inp.shipment_id_hint:
        parts.append(f"SHIPMENT ID FORMAT: {inp.shipment_id_hint}")
    if inp.invoice_hint:
        parts.append(f"INVOICE REFERENCE FORMAT: {inp.invoice_hint}")
    hints = ("\n" + "\n".join(parts)) if parts else ""
    return f"""Extract shipping facts from the email below. Return JSON only — no prose, no markdown fences.{hints}

EMAIL SUBJECT: {inp.subject}
EMAIL BODY:
{body_preview}

Return exactly:
{{
  "shipment_id": "<MAWB / container / BL number matching the format hint, or empty string>",
  "invoice_numbers": ["<trailing number of invoice ref 1>", "<trailing number 2>", ...]
}}

RULES:
- shipment_id: extract the canonical shipment identifier. Return "" if not present.
- invoice_numbers: extract the trailing numeric portion of each distinct invoice reference.
  Expand abbreviated comma-separated forms — "100/26-27/235, 238" means two invoices: "235" and "238".
  The trailing number is what appears at the end of the corresponding attachment filename.
  Return [] when no invoice list is present."""


def _llm_extract(inp: ExtractEmailFactsInput) -> ExtractEmailFactsResult:
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": _build_prompt(inp)}],
    )
    raw = (resp.content[0].text if resp.content else "{}").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw.strip())
    shipment_id = str(parsed.get("shipment_id") or "").strip()
    invoice_numbers = [
        str(n).strip()
        for n in (parsed.get("invoice_numbers") or [])
        if str(n).strip()
    ]
    return ExtractEmailFactsResult(shipment_id=shipment_id, invoice_numbers=invoice_numbers)


# ── Regex fallback ─────────────────────────────────────────────────────────────

_MAWB_RE         = re.compile(r'\b(\d{3}-\d{8})\b')
_INV_LABEL_RE    = re.compile(r'INVOICE\s*N[O.]?\s*:?\s*([^\n]+)', re.IGNORECASE)
# Matches the terminal 3+ digit number after the last "/" in a reference like "100/26-27/235"
# Lookahead ensures the number is followed by comma, whitespace, or end of string.
_SLASH_TRAIL_RE  = re.compile(r'/(\d{3,})(?=[,\s]|$)')
# Matches 3+ digit numbers that appear after a comma (abbreviated continuations, e.g. ", 238")
_COMMA_TRAIL_RE  = re.compile(r',\s*(\d{3,})')


def _regex_extract(inp: ExtractEmailFactsInput) -> ExtractEmailFactsResult:
    text = f"{inp.subject}\n{(inp.body or '')[:BODY_CAP]}"

    # Shipment ID — try MAWB format first; fall back to full subject
    m = _MAWB_RE.search(text)
    shipment_id = m.group(1) if m else inp.subject.strip()

    # Invoice numbers — look for INVOICE NO block
    invoice_numbers: list[str] = []
    bm = _INV_LABEL_RE.search(text)
    if bm:
        block = bm.group(1)
        slash_nums = _SLASH_TRAIL_RE.findall(block)
        comma_nums = _COMMA_TRAIL_RE.findall(block)
        seen: set[str] = set()
        for n in slash_nums + comma_nums:
            if n not in seen:
                seen.add(n)
                invoice_numbers.append(n)

    return ExtractEmailFactsResult(shipment_id=shipment_id, invoice_numbers=invoice_numbers)


# ── Activity ───────────────────────────────────────────────────────────────────

@activity.defn
def extract_email_facts(inp: ExtractEmailFactsInput) -> ExtractEmailFactsResult:
    """Extract shipment identifier and invoice numbers from the email subject and body.

    Uses Claude LLM guided by spec-derived hints. Falls back to regex on any LLM failure.
    Never raises.
    """
    log.info(
        "extract_email_facts: subject=%r body_len=%d",
        (inp.subject or "")[:80], len(inp.body or ""),
    )
    try:
        result = _llm_extract(inp)
        log.info(
            "extract_email_facts: LLM → shipment_id=%r invoices=%s",
            result.shipment_id, result.invoice_numbers,
        )
        return result
    except Exception as exc:
        log.warning("extract_email_facts: LLM failed (%s) — using regex fallback", exc)

    result = _regex_extract(inp)
    log.info(
        "extract_email_facts: regex → shipment_id=%r invoices=%s",
        result.shipment_id, result.invoice_numbers,
    )
    return result
