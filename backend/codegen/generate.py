"""Codegen endpoint: POST /api/v1/boards/{board_id}/codegen

Reads a frozen spec + CONTRACT.md → Claude generates a Temporal workflow →
validates (syntax + activity-signature check) → writes to disk → persists record.
General: works for any frozen spec; pharma-specifics come only from the spec.
"""
import ast
import json
import logging
import os
import pathlib
import py_compile
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

import anthropic
from fastapi import APIRouter, HTTPException
from supabase import create_client

router = APIRouter(prefix="/api/v1/boards", tags=["codegen"])
log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
MAX_REPAIR = 3

_CONTRACT_PATH = pathlib.Path(__file__).parent.parent / "runtime" / "CONTRACT.md"
_AGENTS_DIR = pathlib.Path(__file__).parent.parent / "agents" / "generated"

# Every activity registered in S7 — the only legal activity names in generated code
S7_ACTIVITIES: frozenset[str] = frozenset({
    "classify_documents",
    "extract_email_facts",
    "fetch_email_and_attachments",
    "load_document",
    "validate_required_fields",
    "match_by_key",
    "tally",
    "emit_report",
    "send_report",
    "is_email_processed",
    "mark_email_processed",
})

# ── Supabase / LLM helpers ────────────────────────────────────────────────────

def _sb():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    return create_client(url, key)


def _llm() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=key)


def _load_contract() -> str:
    try:
        return _CONTRACT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"CONTRACT.md not found at {_CONTRACT_PATH}")


# ── Spec analysis: convert nodes+edges into a structured execution plan ────────

def _safe_var(text: str, fallback: str = "node") -> str:
    """Convert a free-text title/name into a safe Python identifier."""
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s or fallback)[:30]


def _topo_order_subset(
    node_ids: set[str],
    all_edges: list[dict],
    nodes_map: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """Kahn's topological sort over a subset of node IDs.

    Considers only edges whose both endpoints are in node_ids.
    Returns (ordered, cyclic) — cyclic are nodes remaining after sort (in a cycle).
    Tie-breaks by canvas y then x position so visual order is preserved.
    """
    in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
    out_adj: dict[str, list[str]] = {nid: [] for nid in node_ids}

    for e in all_edges:
        src, tgt = e.get("source", ""), e.get("target", "")
        if src in node_ids and tgt in node_ids:
            out_adj[src].append(tgt)
            in_degree[tgt] += 1

    def _pos_key(nid: str) -> tuple[float, float]:
        pos = nodes_map.get(nid, {}).get("position", {})
        return (pos.get("y", 0), pos.get("x", 0))

    queue = sorted([nid for nid, d in in_degree.items() if d == 0], key=_pos_key)
    result: list[str] = []
    while queue:
        nid = queue.pop(0)
        result.append(nid)
        for nxt in sorted(out_adj[nid], key=_pos_key):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    cyclic = [nid for nid, d in in_degree.items() if d > 0]
    return result, cyclic


def _incoming_nodes(node_id: str, all_edges: list[dict]) -> list[dict]:
    return [
        {"from_node_id": e["source"], "edge_kind": e.get("data", {}).get("edgeKind", "default")}
        for e in all_edges
        if e.get("target") == node_id
    ]


def _node_to_step(
    node_id: str,
    nodes_map: dict[str, dict],
    scope_members: dict[str, set[str]],
    all_edges: list[dict],
) -> dict[str, Any]:
    """Convert one spec node into a structured plan step."""
    node = nodes_map[node_id]
    data = node.get("data", {})
    kind = data.get("kind", node.get("type", "custom"))
    config = data.get("config", {})
    blocks = config.get("blocks", [])
    title = data.get("title", "")

    step: dict[str, Any] = {
        "node_id": node_id,
        "kind": kind,
        "title": title,
        "var_name": _safe_var(title, kind),
        "config": {
            "fail_if":    config.get("fail_if", "any_missing"),
            "applies_to": config.get("applies_to", "per_document"),
            "match_type": config.get("match_type", "exact"),
            "identified_by": config.get("identified_by", "filename"),
            "identifier":    config.get("identifier", ""),
            "iterate_over":  config.get("iterate_over", "items"),
            "item_name":     config.get("item_name", "item"),
            "scope_kind":    config.get("scope_kind", "for_each"),
            "action_type":   config.get("action_type", ""),
            "action_target": config.get("action_target", ""),
            "description":   config.get("description", ""),
        },
        "required_fields": [
            {"name": b.get("name", ""), "appears_as": b.get("appears_as", ""),
             "scope": b.get("scope", "document"), "required": b.get("required", True)}
            for b in blocks if b.get("kind") == "required_field"
        ],
        "match_keys": [
            {"source_collection": b.get("source_collection", ""),
             "target_collection": b.get("target_collection", ""),
             "key_field": b.get("key_field", ""), "on_missing": b.get("on_missing", "fail")}
            for b in blocks if b.get("kind") == "match_key"
        ],
        "count_keys": [
            {"collection": b.get("collection", ""), "dedup_key": b.get("dedup_key", ""),
             "label": b.get("label", ""), "track": b.get("track", ["processed", "succeeded", "failed"])}
            for b in blocks if b.get("kind") == "count_key"
        ],
        "branch_conditions": [
            {"condition": b.get("condition", ""), "outcome": b.get("outcome", "")}
            for b in blocks if b.get("kind") == "branch_condition"
        ],
        "doc_fields": [
            {"name": b.get("name", ""), "appears_as": b.get("appears_as", ""),
             "scope": b.get("scope", "document")}
            for b in blocks if b.get("kind") == "doc_field"
        ],
        "incoming": _incoming_nodes(node_id, all_edges),
    }

    if kind == "scope":
        member_ids = scope_members.get(node_id, set())
        ordered, cyclic = _topo_order_subset(member_ids, all_edges, nodes_map)
        step["children"] = [
            _node_to_step(mid, nodes_map, scope_members, all_edges)
            for mid in ordered if mid in nodes_map
        ]
        step["cyclic_children"] = cyclic

    return step


def _analyze_spec(spec: dict) -> dict:
    """Convert frozen spec nodes+edges into a structured execution plan.

    Returns a dict with:
      - steps: top-level steps in execution order
      - has_cycles: whether any cycles exist (rare; requires max-iter guard)
      - resolved_assumptions: Q&A from the gate review
    """
    nodes_list: list[dict] = spec.get("nodes", [])
    edges: list[dict] = spec.get("edges", [])
    nodes_map = {n["id"]: n for n in nodes_list}

    # Scope hierarchy: parentId indicates containment
    scope_members: dict[str, set[str]] = {}
    member_of_scope: dict[str, str] = {}
    for n in nodes_list:
        pid = n.get("parentId")
        if pid:
            scope_members.setdefault(pid, set()).add(n["id"])
            member_of_scope[n["id"]] = pid

    # Top-level nodes: not inside any scope
    top_ids = {n["id"] for n in nodes_list if not n.get("parentId")}
    ordered_top, cyclic_top = _topo_order_subset(top_ids, edges, nodes_map)

    steps = [
        _node_to_step(nid, nodes_map, scope_members, edges)
        for nid in ordered_top if nid in nodes_map
    ]

    return {
        "steps": steps,
        "cyclic_node_ids": cyclic_top,
        "has_cycles": bool(cyclic_top),
    }


# ── Prompt builder ────────────────────────────────────────────────────────────

_CODEGEN_SYSTEM = """\
You are a Temporal workflow code generator for the Meridian agent runtime (Python 3.11+).

TASK: Generate a single, complete, runnable Python source file implementing a Temporal workflow \
that automates the process described in the spec below.

GENERALIZATION PRINCIPLES (these override any specific pattern below):

1. GENERALIZE FROM THE EXAMPLE — DO NOT TRANSCRIBE IT.
   The worked example demonstrates ONE sender's formatting. Other senders in the same document
   family use different field labels, identifier formats, separators, casing, and filenames.
   NEVER bake a literal string that you only saw in the example into the generated agent as a
   hard requirement. Encode the CONCEPT the literal is an instance of. Only treat a literal as
   invariant if the spec explicitly states it is fixed for all senders.

2. IDENTIFY DOCUMENTS BY MEANING, NOT BY ONE LABEL.
   Describe each document type by its PURPOSE and the KINDS of information it carries, then let
   classify_documents reason. When a distinguishing label exists, give it as ONE EXAMPLE among
   likely variants ("e.g. 'Alias Batch No.', 'Batch No.', 'Lot No.'"), never as a required
   exact phrase. A document still qualifies if it clearly serves that purpose even when the
   exact example label is absent.

3. MATCH IDENTIFIERS BY EQUIVALENCE, NOT BYTE-EQUALITY.
   The same batch/invoice/lot is often written differently on two independently-authored
   documents: different case, whitespace, separators, zero-padding, or a trailing/leading
   qualifier letter or suffix. Treat two identifiers as the same when they refer to the same
   real-world thing. Normalize before comparing, prefer match_by_key with
   match_type="normalized" or "fuzzy", and when the spec's items legitimately differ in format,
   lean on the LLM-backed match rather than ==. NEVER require a raw exact-string match across
   two separate documents.

4. USE ALL AVAILABLE EVIDENCE, INCLUDING FILENAMES AND STATUS MARKERS.
   "Prefer content" does NOT mean "ignore filenames." Filenames and document markers (e.g.
   QUARANTINE, HOLD, REJECTED, EMBARGOED, VOID, DRAFT) often carry the only signal of a
   document's status, and that status can decide pass/fail. A reasoning agent reads content
   FIRST but also consults the filename and any status stamp, and treats a
   clearly-marked quarantine/hold/rejected item as a non-passing match. Do not discard filename
   evidence as a matter of principle.

5. DELEGATE JUDGMENT TO THE LLM-BACKED ACTIVITIES; REGEX IS A FALLBACK, NOT A GATE.
   classify_documents, extract_email_facts, and match_by_key exist to make semantic decisions
   for any sender. Use them as the PRIMARY mechanism. A hand-written regex may appear only as a
   tolerant fallback for when those activities return empty — never as the thing that decides
   whether a document or identifier qualifies.

OUTPUT RULES (critical — do not violate):
- Return ONLY raw Python code.  No markdown fences (no ```python), no prose before or after.
- First line of output must be a triple-quoted module docstring or an import statement.
- The file must be importable and pass py_compile with zero errors.

REQUIRED FILE STRUCTURE — follow this template exactly:
```
\"\"\"<one-line description>

Generated from spec: <board_name>
Frozen at: <frozen_at>
DO NOT EDIT MANUALLY — regenerate via POST /api/v1/boards/<board_id>/codegen
\"\"\"
import asyncio
import re as _re
from dataclasses import dataclass, field
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from backend.runtime.activities.classify_documents import classify_documents
    from backend.runtime.activities.extract_email_facts import extract_email_facts
    from backend.runtime.activities.fetch_email import fetch_email_and_attachments
    from backend.runtime.activities.load_document import load_document
    from backend.runtime.activities.validate_fields import validate_required_fields
    from backend.runtime.activities.match_documents import match_by_key
    from backend.runtime.activities.tally import tally
    from backend.runtime.activities.report import emit_report, send_report
    from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed
    from backend.runtime.activities._types import (
        ClassifyDocumentsInput,
        ExtractEmailFactsInput,
        FetchEmailInput, LoadDocumentInput, ValidateFieldsInput,
        MatchInput, TallyInput, EmitReportInput, SendReportInput,
        EmailDedupInput,
    )

_TIMEOUT = timedelta(seconds=60)
_VISION_TIMEOUT = timedelta(seconds=180)   # load_document calls that may invoke Claude vision
_CLASSIFY_TIMEOUT = timedelta(minutes=10)  # classify_documents: parallel vision across all attachments


@dataclass
class <Name>AgentInput:
    message_id: str = ""
    use_fixture: bool = True
    fixture_body: str = ""
    fixture_attachments: list[dict] = field(default_factory=list)
    board_id: str = ""
    fixture_subject: str = ""   # eval/fixture runs set the shipment key via this field
    fixture_sender: str = ""


@dataclass
class <Name>AgentResult:
    message_id: str
    <...relevant result fields...>


@workflow.defn
class <Name>AgentWorkflow:
    @workflow.run
    async def run(self, inp: <Name>AgentInput) -> <Name>AgentResult:
        # ── Dedup guard ────────────────────────────────────────────────────
        dedup = await workflow.execute_activity(
            is_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        if dedup.already_processed:
            return <Name>AgentResult(message_id=inp.message_id, <...defaults...>)

        # ── [implement process steps here] ─────────────────────────────────

        # ── Mark processed ─────────────────────────────────────────────────
        await workflow.execute_activity(
            mark_email_processed,
            EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
            start_to_close_timeout=_TIMEOUT,
        )
        return <Name>AgentResult(message_id=inp.message_id, <...results...>)
```

ACTIVITY CALL RULES:
- ONLY call activities from this list: classify_documents, extract_email_facts, \
fetch_email_and_attachments, load_document, validate_required_fields, match_by_key, tally, \
emit_report, send_report, is_email_processed, mark_email_processed.
- NEVER invent new activity names or helper functions (except dataclasses for I/O).
- ALWAYS pass the input as a positional arg to workflow.execute_activity, like:
    result = await workflow.execute_activity(activity_fn, ActivityInput(...), start_to_close_timeout=_TIMEOUT)
- NEVER call workflow.execute_activity with keyword arg for the input (Temporal requires positional).
- When loading a specific document with load_document(identified_by="filename"), use the FULL \
attachment filename (including file extension, e.g. "INVOICE-001.pdf") as the identifier — never \
strip the extension. Use att.get("name", "") directly from the attachment dict.

DATA FLOW RULES:
- Each activity result is stored in a local variable (named after its node title).
- Pass previous results as parameters to subsequent activities where they are needed.
- For load_document: pass the attachments list from the fetch_email result.
- For validate_required_fields: pass the .text field from a load_document result.
- For match_by_key: source_items and target_items come from prior activity outputs (use .field_results, \
match results, or raw lists as appropriate based on what the spec expects to cross-reference).

SCOPE / FOR-EACH LOOP RULES:
- A scope step in the EXECUTION PLAN means a Python `for` loop.
- Nested scopes mean nested `for` loops.
- Use `inp.<iterate_over>` for the iterable if the collection comes from workflow input.
- If the items come from a prior activity result, use that result's relevant field.
- Collect loop results in a list, pass to tally at the end.

DECISION BRANCH RULES:
- A decision step means `if / elif / else`.
- Base conditions on prior activity result fields (e.g., `if validation.passed:`).

DOCUMENT PARSING RULES — always apply ALL of these patterns when writing extraction / matching logic:

  P1 — WHITESPACE-TOLERANT FIELD MATCHING:
    Document text often wraps field labels across lines or collapses/drops spaces. An exact
    substring search against raw text will miss labels that span a line break or have extra spaces.
    ALWAYS pass a whitespace-normalised copy of the document text to validate_required_fields:
        normalised_text = " ".join(doc_text.split())   # collapses \\n, \\t, double-spaces → single space
        validate_required_fields(ValidateFieldsInput(document_text=normalised_text, ...))
    Keep the ORIGINAL doc_text for any line-based parsing (collection extraction, etc.).
    Apply to EVERY validate_required_fields call — it is always safe because the activity only
    does substring search, not line-sensitive parsing.

  P2 — LABELED-LIST VALUES: ROBUST TOKEN EXTRACTION:
    A labeled collection field (e.g. a match_key or count_key collection marker like "BATCH NOS:",
    "SERIAL NOS:", "ITEM CODES:", etc.) may have its values in any of these layouts:
      (a) same line after the colon:  "LABEL: A001, A002, A003"
      (b) immediately following line: "LABEL:\\nA001, A002, A003"
      (c) description on same line, values on NEXT line: "LABEL: <description text>\\nA001"
    Layout (c) is common in PDF tables where the identifier is a sub-row below a description.
    A simple comma-split of the same line OR a simple "if empty, take next line" will fail
    on (c) because the line is NOT empty — it contains description text, not identifier tokens.
    Robust approach — always scan BOTH the text after the colon AND the next line, then filter:
        _TOKEN_RE = _re.compile(r'\\b([A-Za-z0-9]{{7,}})\\b')  # 7+ chars, letters and/or digits
        lines = doc_text.splitlines()
        for i, line in enumerate(lines):
            if "LABEL" in line.upper():          # derive "LABEL" from spec's appears_as / key
                after = ""
                colon_idx = line.find(":")
                if colon_idx != -1:
                    after = line[colon_idx + 1:]
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                search = after + " " + next_line
                for m in _TOKEN_RE.finditer(search):
                    tok = m.group(1)
                    if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
                        collected.append(tok)   # mixed alphanumeric: rejects pure-digit and pure-word tokens
    The mixed-alphanumeric filter is general: real batch/serial codes always contain both letters
    and digits; English description words are pure-alpha and quantity/AWB digits are pure-numeric.
    Derive the label string from the spec's match_key / count_key / doc_field appears_as values.

  P3 — CONTENT-BASED DOCUMENT IDENTIFICATION (classify_documents):
    ALWAYS use classify_documents to identify document types by CONTENT — never by filename
    regex alone. Filename patterns over-fit to a single sender's naming convention and break
    when different suppliers name the same document differently.

    Call classify_documents ONCE, immediately after fetch_email_and_attachments,
    before any document-processing loop:

        classify_result = await workflow.execute_activity(
            classify_documents,
            ClassifyDocumentsInput(
                attachments=fetch_result.attachments,
                doc_types=[
                    # Descriptions MUST be CONTENT-ONLY: state which field labels or
                    # validation codes MUST appear in the document text to qualify for
                    # that type.  Do NOT reference filenames or filename patterns
                    # (e.g. "does not have PL in the name", "ends in -PL.pdf") —
                    # the classifier ignores filenames and assigns types strictly by
                    # what the text contains.  A type is assigned only when its
                    # required markers are clearly present in the text; ambiguous or
                    # short documents must fall through to "other".
                    {{
                        "type_name": "commercial_invoice",
                        "description": (
                            # Required text markers: list the regulatory/validation codes
                            # from the spec that MUST appear in the text — e.g.:
                            # "Text must contain at least one of: HTS No, ANDA No, FDA No,
                            #  Reg.No, or NDC No.  These codes are absent from packing lists
                            #  that cover the same goods — their presence is the decisive test."
                            "<content-only: required field codes from spec that MUST appear in the text>"
                        ),
                    }},
                    {{
                        "type_name": "certificate_of_analysis",
                        "description": (
                            # MEANING-FIRST (per Generalization Principle 2): describe PURPOSE
                            # and field KINDS with example label variants — not one exact label.
                            # Example description:
                            #   "A certificate reporting laboratory test results / specification
                            #    values for a specific manufactured batch or lot of product.
                            #    It states a batch or lot identifier — labelled in ways that
                            #    VARY by sender, e.g. 'Alias Batch No.', 'Batch No.', 'Lot No.'
                            #    — alongside test parameters and their pass/fail results.
                            #    Qualify any document that clearly serves this purpose even if
                            #    the exact example label is not present."
                            # Never write: "Text MUST contain '<exact phrase>'".
                            # Never write filename rules like "ends in -COA.pdf".
                            "<content-meaning: purpose + batch/lot identifier kinds with label variants>"
                        ),
                    }},
                    {{
                        "type_name": "packing_list",
                        "description": (
                            "Packing or carton list: shows item counts, quantities, and weights "
                            "for the same shipment goods but does NOT contain regulatory "
                            "validation codes (HTS No, ANDA No, FDA No, Reg.No, NDC No)."
                        ),
                    }},
                    {{
                        "type_name": "other",
                        "description": "logistics, transport, or any uncategorised document",
                    }},
                ],
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    Select document sets by classified type:
        invoice_docs = [d for d in classify_result.documents if d["doc_type"] == "commercial_invoice"]
        coa_docs     = [d for d in classify_result.documents if d["doc_type"] == "certificate_of_analysis"]

    Result dict fields per document:
        d["name"]       — attachment filename
        d["doc_type"]   — matched type_name
        d["identifier"] — primary key extracted by LLM (invoice number, batch number, etc.)
        d["text"]       — full extracted text, vision-enriched for scanned PDFs

    USE d["text"] DIRECTLY in validate_required_fields and field parsers — do NOT call
    load_document separately for documents already returned by classify_documents.
    The classify_documents activity handles scanned-PDF vision extraction automatically.

    For the outer invoice loop, iterate over invoice_docs (not over raw attachments):
        for inv in invoice_docs:
            normalised = " ".join(inv["text"].split())
            val = await workflow.execute_activity(
                validate_required_fields,
                ValidateFieldsInput(document_text=normalised, ...),
                start_to_close_timeout=_TIMEOUT,
            )
            batch_nos = _extract_batch_numbers(inv["text"])  # P2 pattern on inv["text"]
            ...

    DOCUMENT-TYPE DESCRIPTIONS — apply to every type, not just COA:
      Write each "description" as a statement of PURPOSE plus the KINDS of fields it carries,
      with example label variants.  Never write "text MUST contain '<exact phrase>'".
      Never write filename rules like "ends in -PL.pdf" — the classifier ignores filenames and
      assigns types by content only; status detection from filenames happens in matching, below.

    IDENTIFIER EXTRACTION from COA docs (tolerant):
      Prefer the identifier classify_documents already extracted into d["identifier"].
      Fall back to a tolerant scan of d["text"] only when it is empty — accept the value
      following ANY likely label variant, tolerating separators (':', '|', whitespace, newline).
      Example fallback (adapt LABEL to the spec's batch/lot field name and likely variants):
        _IDENT_RE = _re.compile(
            r'(?:Alias Batch No|Batch No|Lot No)\\.?[\\s|:.]*([A-Za-z0-9]+)',
            _re.IGNORECASE,
        )
        for c in coa_docs:
            if not c["identifier"]:
                m = _IDENT_RE.search(c["text"])
                if m:
                    c = dict(c, identifier=m.group(1).strip())

    BATCH → COA MATCHING (equivalence + status-aware):
      Build the COA pool from EVERY document whose doc_type is the certificate type.
      Match each invoice batch number to a COA by EQUIVALENCE, not byte-equality:
        - Normalize both sides (strip, uppercase) before comparing;
        - Treat identifiers that differ only by a trailing/leading qualifier letter or suffix as
          the same batch — the invoice may carry a packaging/finish suffix the COA omits, or
          vice versa (e.g. "1CV3U2601A" on the invoice matches "1CV3U2601" on the COA);
        - Use match_by_key with match_type="normalized" as the primary mechanism; use "fuzzy"
          only when the spec notes identifiers are noisy, and keep the threshold high enough not
          to collapse two genuinely adjacent but different batch numbers.
      A batch PASSES only if it matches a COA that is NOT marked quarantine/hold/rejected.
      Detect that status from the COA's content AND its filename (per Principle 4): a matched
      COA whose text or filename contains QUARANTINE, HOLD, REJECTED, EMBARGOED, VOID, or
      DRAFT is treated as a FAILED match, not a passing one.
      Example pattern:
        for batch_no in all_batch_nos:
            # Primary: use identifier already extracted by classify_documents
            norm_batch = batch_no.upper().strip()
            coa = next(
                (c for c in coa_docs
                 if (c.get("identifier") or "").upper().strip().rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                    == norm_batch.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
                None,
            )
            if coa is None:
                # Fallback: tolerant text scan for COAs whose LLM identifier was empty (P5)
                for c in coa_docs:
                    if not c.get("identifier") and norm_batch in c.get("text", "").upper():
                        coa = c
                        break
            if coa is not None:
                _status_text = (coa.get("text", "") + " " + coa.get("name", "")).upper()
                _quarantined = any(
                    w in _status_text
                    for w in ("QUARANTINE", "HOLD", "REJECTED", "EMBARGOED", "VOID", "DRAFT")
                )
            ...  # passed = coa is not None and not _quarantined

    Retain P2 for BATCH NOS extraction from invoice text — it remains the same.

  P4 — SHIPMENT ID + INVOICE LIST: USE extract_email_facts (LLM READS THE EMAIL):
    Call extract_email_facts ONCE, immediately after fetch_email_and_attachments, to let the LLM
    extract the shipment identifier and invoice number list from the email subject + body.
    Do NOT write _extract_invoice_numbers_from_subject or _extract_shipment_number helper
    functions — extract_email_facts replaces them for all senders and abbreviation styles.

        email_facts = await workflow.execute_activity(
            extract_email_facts,
            ExtractEmailFactsInput(
                subject=fetch_result.subject,
                body=fetch_result.body_text,
                # Derive hints from the spec's key_field config and resolved_assumptions:
                shipment_id_hint="<format of the shipment identifier, e.g. 'MAWB number, format NNN-NNNNNNNN'>",
                invoice_hint="<how invoice numbers appear in the email, derived from resolved_assumptions, or '' if not in email header>",
            ),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        shipment_number = email_facts.shipment_id or fetch_result.subject.strip()
        invoice_numbers = email_facts.invoice_numbers

    Reconcile invoice_numbers with classify_documents output to build invoices_in_shipment.
    Match PRECISELY — segment equality on the classifier identifier first, then bounded regex
    on full text/filename.  NEVER use a bare substring test (_n in text / _n in ident): short
    invoice numbers such as '235' appear as substrings of compound identifiers like the shipment
    number '235-36716875', causing false matches that drop invoices from the count.
    classify_documents already extracted each invoice's identifier from its content; use that
    first with a LAST-SEGMENT equality check:
        invoices_in_shipment: list[dict] = []
        if invoice_numbers:
            remaining = list(invoice_docs)   # avoid matching the same doc to two invoice numbers
            for inv_num in invoice_numbers:
                def _content_match(d, _n=inv_num):
                    ident = (d.get("identifier") or "").strip()
                    text  = d.get("text", "")
                    name  = d.get("name", "")
                    # (a) Precise identifier match: split the classifier-extracted identifier
                    #     on path separators (/ - space) and compare the LAST segment for
                    #     equality.  '235' matches '100/26-27/235' (last segment '235') but
                    #     NOT '235-36716875' (last segment '36716875'), and NOT '238'-ending
                    #     identifiers.
                    if ident:
                        segs = _re.split(r'[/\-\s]+', ident)
                        if segs[-1] == _n:
                            return True
                    # (b) Bounded text match: invoice number as a distinct numeric token,
                    #     NOT followed by a hyphen or digit so '235' does not match inside
                    #     the compound shipment id '235-36716875'.
                    if text and _re.search(r'(?<!\d)' + _re.escape(_n) + r'(?![\-\d])', text):
                        return True
                    # (c) Same bounded check on the filename.
                    if name and _re.search(r'(?<!\d)' + _re.escape(_n) + r'(?![\-\d])', name):
                        return True
                    return False
                matched = next((d for d in remaining if _content_match(d)), None)
                if matched:
                    invoices_in_shipment.append(matched)
                    remaining.remove(matched)
                # Do NOT append empty stubs for unmatched inv_nums — an empty-text stub
                # fails all field checks and produces false negatives.  If reconciliation
                # fails for all invoice numbers, the fallback below catches it.
            if not invoices_in_shipment:
                # None of the email's invoice numbers matched any classified doc by content —
                # fall back to processing all classified commercial invoices.
                invoices_in_shipment = invoice_docs
        else:
            # No invoice list in email — trust strict content classification
            invoices_in_shipment = invoice_docs

    Always pass inp.fixture_subject and inp.fixture_sender to FetchEmailInput for fixture runs.

  P5 — MATCH-KEY EXTRACTION FROM SUPPORTING DOCUMENTS: SEPARATOR TOLERANCE + LITERAL FALLBACK:
    When extracting the match key from a supporting document (e.g. extracting a "Lot No.",
    "Alias Batch No.", "Serial No.", or similar label+value from a COA, lookup table, or
    scanned doc), the vision model may transcribe the separator between label and value as
    any of: "|", ":", plain whitespace, newline, or nothing at all. The label may also have
    optional punctuation (trailing period, varied capitalisation, extra spaces).
    Use a compiled regex that tolerates all separator forms:
        _KEY_RE = _re.compile(
            r'<LABEL FROM SPEC>\\.?'    # label with optional trailing period
            r'[\\s|:.]* '               # any separators or none (pipe, colon, dot, whitespace, newline)
            r'([A-Za-z]{{2,}}\\d+[A-Za-z])',  # key token: 2+ letters, digits, trailing letter
            _re.IGNORECASE,
        )
        _m = _KEY_RE.search(doc_text)
        key_value = _m.group(1).strip() if _m else ""
    Replace <LABEL FROM SPEC> with the spec's match_key label (from resolved_assumptions or
    the expected_document / match_documents node config). Keep the pattern general: only the
    label string is spec-specific; the separator class and token pattern are universal.
    ALWAYS add a LITERAL FALLBACK: if label-based extraction yields empty but the SOURCE KEY
    being matched appears literally in the supporting document's text, treat the source key as
    the extracted value — the source key appearing anywhere in its own supporting doc is strong
    evidence the doc belongs to that item (robust to vision label-formatting noise):
        if not key_value and source_key and source_key in doc_text:
            key_value = source_key
    This fallback is general: it works off the spec's match_key field value at runtime, not
    any hardcoded literal.

  P6 — CONCURRENT SUPPORTING-DOC LOADING + PER-RESULT CACHE + VISION TIMEOUT:
    When a loop body loads one supporting document per source item (e.g. load a COA per batch,
    a spec sheet per part number), do NOT load them serially — each vision read of a scanned doc
    takes 40-60 seconds, and N serial calls overflow the workflow timeout for large shipments.
    Instead, parallel-load all uncached documents with asyncio.gather, then process match logic
    sequentially (fast, no I/O):
        # Before the inner loop — collect uncached identifiers:
        _support_cache: dict = {}    # identifier → LoadDocumentResult; declare before outer loop
        uncached = [key for key in source_keys if key not in _support_cache]
        if uncached:
            _tasks = [
                workflow.execute_activity(
                    load_document,
                    LoadDocumentInput(
                        attachments=fetch_result.attachments,
                        identified_by="filename",
                        identifier=key,
                    ),
                    start_to_close_timeout=_VISION_TIMEOUT,    # NOT _TIMEOUT — vision needs headroom
                )
                for key in uncached
            ]
            _results = await asyncio.gather(*_tasks)
            for key, res in zip(uncached, _results):
                _support_cache[key] = res
        # Inner loop — read from cache, no activity calls:
        for key in source_keys:
            support_doc = _support_cache[key]
            ...
    The cache persists across outer-loop iterations — if the same supporting doc is referenced
    by multiple source items, it is vision-read only once per workflow run.
    Always use _VISION_TIMEOUT (not _TIMEOUT) for load_document calls that may trigger the
    vision fallback (i.e. any supporting doc that may be a scanned PDF).

  P7 — PER-LINE-ITEM REGULATORY CODE COUNTING:
    When a spec requires counting pass/fail at the product-line (line-item) level — e.g. each
    product line must carry a fixed set of regulatory codes — DO NOT split the document text on
    any one code label to segment lines.  Code ORDER varies across senders; splitting on a label
    discards every code that appears before it in the document.

    CORRECT APPROACH — count by coverage:
        def _count_line_item_results(doc_text: str) -> tuple[int, int]:
            \"\"\"Return (passed, failed) product lines by regulatory code coverage.

            Infers the number of product lines from how many times each code appears;
            never assumes code order or that any one label begins each line.
            \"\"\"
            norm = " ".join(doc_text.split()).upper()
            # Derive labels from the spec's _INVOICE_REQUIRED_FIELDS "appears_as" values
            required_labels = [f["appears_as"].upper() for f in _INVOICE_REQUIRED_FIELDS]
            counts = [norm.count(lbl) for lbl in required_labels]
            n_lines = max(counts) if counts else 0
            if n_lines == 0:
                return (0, 1)   # no regulatory codes at all → one failing block
            # The label with the fewest occurrences limits how many lines have full coverage.
            passed = min(counts)
            failed  = n_lines - passed
            return (passed, failed)

    The formula: n_lines = max occurrence count (the most-repeated code tells us how many lines
    exist); passed = min occurrence count (how many lines have EVERY code); failed = remainder.
    Example — all five codes appear twice → n_lines=2, passed=2, failed=0.
    Example — one code appears once while the rest appear twice → n_lines=2, passed=1, failed=1.

ASSUMPTIONS: The RESOLVED ASSUMPTIONS in the spec contain the board author's answers to AI-identified \
gaps. These answers affect parameter values (identifiers, field names, match keys) and logic. \
Bake them into the code as parameters and inline comments.

CYCLES: If the spec's EXECUTION PLAN notes `has_cycles=True`, wrap the cyclic portion in a bounded \
`for _ in range(MAX_ITERATIONS):` loop (MAX_ITERATIONS = 10) with a break condition.
"""


def _build_user_message(spec: dict, plan: dict, contract: str) -> str:
    board_name = spec.get("board_name", "Board")
    frozen_at = spec.get("frozen_at", "unknown")
    meta = spec.get("meta", {})
    resolved = spec.get("resolved_assumptions", [])

    return (
        f"# CONTRACT (activity signatures you must use)\n\n{contract}\n\n"
        f"---\n\n"
        f"# SPEC\n\n"
        f"board_name: {board_name}\n"
        f"board_id: {spec.get('board_id', '')}\n"
        f"frozen_at: {frozen_at}\n"
        f"meta: {json.dumps(meta)}\n\n"
        f"# EXECUTION PLAN (ordered steps; implement in this order)\n\n"
        f"{json.dumps(plan, indent=2)}\n\n"
        f"---\n\n"
        f"# RESOLVED ASSUMPTIONS (bake these into parameters and comments)\n\n"
        + (
            "\n".join(
                f"- [{a.get('severity','').upper()} | {a.get('status','')}] "
                f"Q: {a.get('question','')}  "
                f"A: {a.get('answer','(no answer)')}"
                for a in resolved
                if a.get("status") in ("resolved", "answered")
            ) or "(none)"
        )
        + f"\n\n---\n\n"
        f"Generate the Python workflow file for: **{board_name}**\n"
        f"Class name: {_class_name(board_name)}AgentWorkflow\n"
        f"Input class: {_class_name(board_name)}AgentInput\n"
        f"Result class: {_class_name(board_name)}AgentResult\n"
    )


def _class_name(board_name: str) -> str:
    """Produce a valid PascalCase class prefix from the board name."""
    parts = re.split(r"[^a-zA-Z0-9]+", board_name)
    return "".join(p.capitalize() for p in parts if p) or "Agent"


# ── Code cleaning + validation ────────────────────────────────────────────────

def _clean_code(raw: str) -> str:
    """Strip accidental markdown fences and leading/trailing prose."""
    # Strip ```python ... ``` fences
    raw = re.sub(r"^```(?:python)?\s*\n", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n```\s*$", "", raw, flags=re.MULTILINE)
    # Strip any trailing ``` without a newline
    raw = re.sub(r"```\s*$", "", raw).strip()
    return raw


def _validate_code(code: str, filepath: pathlib.Path) -> list[str]:
    """Validate generated code. Returns a list of error strings (empty = valid).

    Checks:
      1. Syntax (py_compile)
      2. All workflow.execute_activity(fn, ...) calls use S7 activity names
      3. The file defines exactly one @workflow.defn class with an async run() method
    """
    errors: list[str] = []

    # ── 1. Syntax ──────────────────────────────────────────────────────────
    try:
        py_compile.compile(str(filepath), doraise=True)
    except py_compile.PyCompileError as e:
        errors.append(f"SyntaxError: {e}")
        return errors  # can't proceed without valid syntax

    # ── 2. AST analysis ────────────────────────────────────────────────────
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        errors.append(f"ParseError: {e}")
        return errors

    # Find workflow.execute_activity calls and check activity names
    found_activities: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "execute_activity"
            and isinstance(func.value, ast.Name)
            and func.value.id == "workflow"
        ):
            continue
        if not node.args:
            errors.append(
                f"Line {node.lineno}: workflow.execute_activity() called with no positional args"
            )
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Name):
            fn_name = first_arg.id
            found_activities.append(fn_name)
            if fn_name not in S7_ACTIVITIES:
                errors.append(
                    f"Line {node.lineno}: unknown activity '{fn_name}' — not in S7 library. "
                    f"Valid activities: {sorted(S7_ACTIVITIES)}"
                )
        # Attribute ref like module.fn_name is also acceptable (e.g., activities.fetch_email...)
        # but we only validate plain Name refs since imports pull them into scope

    # ── 3. Workflow class structure ────────────────────────────────────────
    workflow_classes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef)
        and any(
            (isinstance(d, ast.Attribute) and d.attr == "defn"
             and isinstance(d.value, ast.Name) and d.value.id == "workflow")
            or (isinstance(d, ast.Name) and d.id == "defn")
            for d in n.decorator_list
        )
    ]
    if not workflow_classes:
        errors.append("No @workflow.defn class found in generated code")
    else:
        wf_cls = workflow_classes[0]
        run_methods = [
            n for n in ast.walk(wf_cls)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
        ]
        if not run_methods:
            errors.append(f"Workflow class '{wf_cls.name}' has no async def run() method")

    if not found_activities:
        errors.append("Generated code contains no workflow.execute_activity() calls")

    return errors


# ── File I/O ──────────────────────────────────────────────────────────────────

def _agent_filepath(board_id: str) -> pathlib.Path:
    _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = board_id.replace("-", "_")
    return _AGENTS_DIR / f"agent_{safe_id}.py"


def _write_agent_file(board_id: str, code: str) -> pathlib.Path:
    path = _agent_filepath(board_id)
    path.write_text(code, encoding="utf-8")
    log.info("codegen wrote agent file: %s (%d chars)", path, len(code))
    return path


def _persist_record(
    sb,
    board_id: str,
    filepath: pathlib.Path,
    status: str,
    attempts: int,
    errors: list[str],
) -> None:
    try:
        sb.table("generated_agents").upsert(
            {
                "board_id": board_id,
                "file_path": str(filepath),
                "status": status,
                "attempts": attempts,
                "errors": errors or None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="board_id",
        ).execute()
    except Exception as exc:
        log.warning("codegen: failed to persist record: %s", exc)


# ── LLM generation + repair loop ─────────────────────────────────────────────

def _attempt_generation(
    llm: anthropic.Anthropic,
    system: str,
    user: str,
    repair_context: str | None = None,
) -> str:
    """Call Claude and return cleaned code. repair_context prepended on retries."""
    if repair_context:
        user = repair_context + "\n\n---\n\nOriginal task:\n" + user

    response = llm.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = response.content[0].text
    return _clean_code(raw)


# ── FastAPI endpoint ───────────────────────────────────────────────────────────

@router.post("/{board_id}/codegen")
async def codegen(board_id: str) -> dict:
    """Generate a Temporal workflow from a frozen spec.

    Reads the frozen spec + CONTRACT.md → Claude generates a workflow →
    validates (syntax + S7 activity signature check) with up to 3 repair attempts →
    writes to backend/agents/generated/agent_{board_id}.py →
    persists a record in generated_agents → returns status + summary.
    """
    log.info("codegen start board_id=%s", board_id)
    try:
        sb = _sb()
        llm = _llm()

        # ── 1. Load frozen spec ───────────────────────────────────────────
        spec_res = (
            sb.table("frozen_specs")
            .select("*")
            .eq("board_id", board_id)
            .maybe_single()
            .execute()
        )
        if not spec_res.data:
            raise HTTPException(
                status_code=400,
                detail="No frozen spec found. Freeze the spec first via POST /gate/freeze.",
            )
        spec: dict = spec_res.data.get("spec", spec_res.data)

        # ── 2. Load CONTRACT.md ───────────────────────────────────────────
        contract = _load_contract()

        # ── 3. Build execution plan from spec ─────────────────────────────
        plan = _analyze_spec(spec)
        log.info(
            "codegen plan: %d steps, has_cycles=%s board_id=%s",
            len(plan["steps"]), plan["has_cycles"], board_id,
        )

        # ── 4. Build prompt ───────────────────────────────────────────────
        system_prompt = _CODEGEN_SYSTEM
        user_message = _build_user_message(spec, plan, contract)

        # ── 5. Generate + validate (with repair) ──────────────────────────
        filepath = _agent_filepath(board_id)
        code: str = ""
        errors: list[str] = []
        repair_context: str | None = None
        final_attempt = 0

        for attempt in range(1, MAX_REPAIR + 1):
            final_attempt = attempt
            log.info("codegen attempt %d/%d board_id=%s", attempt, MAX_REPAIR, board_id)

            code = _attempt_generation(llm, system_prompt, user_message, repair_context)
            _write_agent_file(board_id, code)
            errors = _validate_code(code, filepath)

            if not errors:
                log.info("codegen validation PASSED on attempt %d board_id=%s", attempt, board_id)
                break

            log.warning(
                "codegen attempt %d validation FAILED (%d errors): %s board_id=%s",
                attempt, len(errors), errors[:2], board_id,
            )
            repair_context = (
                f"The previously generated code failed validation with these errors:\n"
                + "\n".join(f"  - {e}" for e in errors)
                + f"\n\nFailed code:\n```python\n{code}\n```\n\n"
                f"Fix ALL errors. Return ONLY the corrected Python code (no fences, no prose)."
            )

        # ── 6. Persist record ─────────────────────────────────────────────
        status = "valid" if not errors else "invalid"
        _persist_record(sb, board_id, filepath, status, final_attempt, errors)

        # ── 7. Build summary ──────────────────────────────────────────────
        step_summary = _summarize_steps(plan["steps"])
        board_name = spec.get("board_name", board_id)
        class_pfx = _class_name(board_name)

        return {
            "status": status,
            "board_id": board_id,
            "board_name": board_name,
            "file_path": str(filepath),
            "attempts": final_attempt,
            "validation_errors": errors,
            "workflow_class": f"{class_pfx}AgentWorkflow",
            "steps_summary": step_summary,
            "has_cycles": plan["has_cycles"],
            "code_preview": code[:800] + ("…" if len(code) > 800 else ""),
        }

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("codegen failed board_id=%s", board_id)
        raise HTTPException(status_code=500, detail=f"codegen failed: {exc}")


def _summarize_steps(steps: list[dict], depth: int = 0) -> list[str]:
    """Produce a human-readable summary of the execution plan for the API response."""
    lines: list[str] = []
    indent = "  " * depth
    for s in steps:
        kind = s["kind"]
        title = s["title"]
        if kind == "scope":
            lines.append(f"{indent}[LOOP] {title} — for_each({s['config']['iterate_over']})")
            lines.extend(_summarize_steps(s.get("children", []), depth + 1))
        elif kind == "trigger":
            lines.append(f"{indent}[TRIGGER] {title}")
        elif kind == "expected_document":
            cfg = s["config"]
            lines.append(f"{indent}[LOAD DOC] {title} — load_document(identified_by={cfg['identified_by']!r})")
        elif kind == "extract_validate":
            n = len(s.get("required_fields", []))
            lines.append(f"{indent}[VALIDATE] {title} — validate_required_fields({n} fields)")
        elif kind == "match_documents":
            mk = s.get("match_keys", [{}])
            key = mk[0].get("key_field", "?") if mk else "?"
            lines.append(f"{indent}[MATCH] {title} — match_by_key(key_field={key!r})")
        elif kind in ("count", "aggregate"):
            lines.append(f"{indent}[TALLY] {title} — tally()")
        elif kind == "report":
            lines.append(f"{indent}[REPORT] {title} — emit_report()")
        elif kind == "tool_action":
            at = s["config"].get("action_type", "?")
            lines.append(f"{indent}[ACTION:{at.upper()}] {title}")
        elif kind == "decision":
            lines.append(f"{indent}[DECISION] {title}")
        elif kind in ("assumption", "custom"):
            lines.append(f"{indent}[{kind.upper()}] {title} (skipped in execution)")
        else:
            lines.append(f"{indent}[{kind.upper()}] {title}")
    return lines
