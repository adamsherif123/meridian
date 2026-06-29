"""AI-check gate — gap detection via Claude (text + vision)."""

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/v1/boards", tags=["gate"])
log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
ADVISORY_CAP      = 4     # max advisory questions returned per run
FILE_TEXT_CAP     = 4_000 # chars per sample file inlined in the text serialization
DEDUP_PREFIX_LEN  = 60    # chars used for open-question dedup in run_gate
VERIFY_MAX_TOKENS = 2048  # verify pass needs fewer tokens than a full run

# ── Vision / document-block budget ───────────────────────────────────────────
# PDFs and images are sent as base64 document/image blocks alongside the text.
# These caps prevent blowing the context window or the API request-size limit.
VISION_SUPPORTED_PDF = {"application/pdf"}
VISION_SUPPORTED_IMG = {"image/jpeg", "image/png", "image/gif", "image/webp"}
VISION_FILE_CAP   = 4                    # at most 4 vision blocks per run
VISION_BYTE_CAP   = 3 * 1024 * 1024     # skip any single file over 3 MB (fall back to text)
VISION_TOTAL_CAP  = 10 * 1024 * 1024    # stop adding vision blocks after 10 MB total


class CommentPatch(BaseModel):
    answer: str | None = None
    status: str | None = None
    followup: str | None = None


def _sb():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    return create_client(url, key)


def _llm() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=api_key)


SYSTEM_PROMPT = """\
You are a process automation reviewer helping a non-technical business owner set up an AI agent \
for their document checking process. Given a process map, identify gaps that would prevent or \
weaken generating a correct, reliable AI agent from it.

A process map uses typed primitives: triggers, document checks (extract_validate), document matching \
(match_documents), decisions, loops (scope), actions (tool_action), report outputs, count nodes, \
summarize nodes, and user-defined steps (custom). Nodes may have typed config (required fields, \
match keys, fail conditions, scope variables, etc.) and extracted sample-document text.

Sample documents (and their extracted text/images) are ILLUSTRATIVE EXAMPLES showing what the real \
documents look like. They are provided ONLY so you can judge whether the board's typed fields and \
identifiers match the documents' actual structure. They are NOT live data to be processed, and the \
running agent will NOT receive these sample files. Therefore:
- Use them for SCHEMA-LEVEL questions: does a field the board requires actually appear in the document, \
is it named differently, is there a field in the document the board is missing, does an identifier/\
match key map to a real field on the document.
- Do NOT ask about the specific VALUES in a sample (e.g. a particular batch number, a specific quantity) \
as if the agent must handle that exact record. The sample is one example among many.
- Do NOT treat a sample document as a step in the process flow or as data the agent reads at runtime.

If a document node lists "fields present in this document", treat that as the author's declaration of \
what the document contains and how its fields appear — do NOT ask what fields the document has or what \
to look for in it. You may still flag a real MISMATCH (e.g. a downstream step requires a field the \
document's field list does not mention, or names it differently).

Return ONLY a JSON array. Each item must have exactly these three keys:
  "node_id": the exact node id string from the input this question is about (null for board-level issues)
  "severity": "blocking" or "advisory"
  "question": a plain-language question in the user's business domain (see language rules below)

Classification:
- "blocking": the agent cannot be generated correctly without this answer
- "advisory": valuable clarification; generation can proceed without it (cap at 4 advisory items max)

LANGUAGE RULES (critical — every question MUST follow these):
1. Write in plain, conversational language that a non-technical process owner immediately understands.
2. FORBIDDEN words and phrases — never use any of these:
   node, node_id, scope, for_each, iterate, collection, line_item, loop, outer loop, inner loop,
   parentId, edgeKind, scope_kind, scope_label, iterate_over, item_name, dedup_key, block, primitive,
   config, extract_validate, match_documents, tool_action, tally, emit_report, send_report, workflow,
   activity, Temporal, implementation, schema, JSON, array, key, field_result, count_key, match_key.
3. Translate every technical concept into the user's domain:
   - "scope iterates over invoices" → "for each invoice in the shipment"
   - "line_item scope" → "for each product line on the invoice"
   - "collection" → "list of [invoices/batches/certificates/etc.]"
   - "match_by_key" → "match [document A] to [document B] using [the field name]"
   - "extract_validate" → "check that [document] contains [field name]"
   - "node" → the step's plain title (e.g. "the batch check step", "the certificate matching step")
4. State the REAL DECISION being clarified and WHY it matters to the outcome. Keep it to 1–3 short \
sentences. End with a question.
5. Reference actual field names, document names, and business actions from this specific board — \
not generic placeholders.

EXAMPLES of good vs bad questions:
BAD:  "The inner scope iterates over 'batch' in 'batches on the invoice', but the 'Validate Goods' \
node (id=f522c507-...) operates at the line_item scope within the same outer invoice loop. Should \
the 'batches on the invoice' collection be sourced from the batch numbers listed on the Commercial \
Invoice line items?"
GOOD: "When a shipment has several batches of the same product, should I check for a separate \
certificate of analysis for each individual batch number, or one certificate covering all batches \
together? This determines how many certificate checks the agent will run per invoice."

BAD:  "The match_documents node uses dedup_key 'batch_no' but the target collection's key field is \
not declared in the extract_validate node config."
GOOD: "The batch matching step needs to pair each batch number from the invoice with its \
certificate of analysis. What field on the certificate should I use to find the matching batch — \
is it the 'Batch No.' label, or does it appear under a different name like 'Lot No.' or 'Reference'?"

Rules:
1. Anchor each question to the specific node_id where the gap lives (in the JSON, not in the text).
2. Make every question specific to THIS board — reference actual field names, document names, \
and business logic from this process map and its sample files.
3. Use sample file content to ask content-aware questions (e.g. a field visible in a sample document \
that the process map doesn't check for — this is often a blocking gap).
4. Do NOT ask generic or hypothetical questions — only flag real gaps from this specific board.
5. Cap advisories at 4. No cap on blockers, but flag only genuine blockers.
6. Return ONLY the JSON array — no prose, no markdown fences, no explanation before or after.\
"""


VERIFY_SYSTEM_PROMPT = """\
You are a process automation reviewer helping a non-technical business owner set up an AI agent \
for their document checking process. You are given a process map and a set of gap questions that \
the board author has answered. For each question-answer pair, judge whether the answer is sufficient \
to fully resolve the gap, or whether further clarification is still needed.

"resolved": the answer clearly addresses the gap, names the relevant field/behavior/document, \
and no material ambiguity remains for building a correct automated agent.
"insufficient": the answer is vague, incomplete, contradictory, or raises a new material question \
that must be answered before the agent can be built correctly.

When writing a follow-up question, use the same plain-language rules as the original question:
- Write in plain, conversational language — no technical jargon.
- FORBIDDEN: node, node_id, scope, iterate, collection, loop, line_item, workflow, activity, \
  Temporal, schema, JSON, config, block, primitive, key, dedup_key, match_key.
- Speak in the user's domain: shipments, invoices, batches, certificates, documents, fields.
- State what's still unclear and why it matters. 1–3 short sentences. End with a question.

Return ONLY a JSON array. Each item must have exactly these keys:
  "comment_id": the exact comment id string from the input
  "verdict": "resolved" or "insufficient"
  "followup": (only when verdict is "insufficient") a plain-language follow-up question \
that tells the author exactly what remaining clarity is needed, in their business domain

Return ONLY the JSON array — no prose, no markdown fences, no explanation before or after.\
"""


def _build_board_description(board: dict[str, Any], file_texts: dict[str, str]) -> str:
    """Serialize the full board into a structured text description for the LLM."""
    lines: list[str] = []
    meta = board.get("meta") or {}
    lines.append(f'PROCESS MAP: "{board.get("name", "Untitled")}"')
    if meta.get("subject_name"):
        lines.append(f'Subject entity: {meta["subject_name"]}')
    if meta.get("key_field"):
        lines.append(f'Subject keyed by field: {meta["key_field"]}')

    nodes: list[dict] = board.get("nodes", [])
    edges: list[dict] = board.get("edges", [])

    node_by_id: dict[str, dict] = {n["id"]: n for n in nodes}
    children: dict[str, list[str]] = {}
    parent_of: dict[str, str] = {}
    for n in nodes:
        pid = n.get("parentId")
        if pid:
            children.setdefault(pid, []).append(n["id"])
            parent_of[n["id"]] = pid

    lines.append("\n=== NODES ===")
    for node in nodes:
        ndata = node.get("data") or {}
        kind = ndata.get("kind", "unknown")
        title = ndata.get("title", "Untitled")
        nid = node["id"]
        config = ndata.get("config") or {}

        lines.append(f'\n[{kind}] "{title}"  id={nid}')

        if config.get("description"):
            lines.append(f'  Description: {config["description"]}')

        # Parent scope context
        pid = parent_of.get(nid)
        if pid and pid in node_by_id:
            pdata = node_by_id[pid].get("data") or {}
            pconf = pdata.get("config") or {}
            if pconf.get("scope_kind") == "custom":
                lines.append(
                    f'  Inside group: "{pdata.get("title","")}" '
                    f'(id={pid}, label: "{pconf.get("scope_label","?")}")'
                )
            else:
                lines.append(
                    f'  Inside scope: "{pdata.get("title","")}" '
                    f'(id={pid}, iterates \'{pconf.get("item_name","item")}\' '
                    f'in \'{pconf.get("iterate_over","collection")}\')'
                )

        # Kind-specific config
        if kind == "scope":
            sk = config.get("scope_kind", "for_each")
            if sk == "custom":
                lines.append(f'  Type: custom group — label: "{config.get("scope_label","(none)")}"')
            else:
                lines.append(
                    f'  Iterates: \'{config.get("item_name","item")}\' '
                    f'in \'{config.get("iterate_over","collection")}\''
                )
            cids = children.get(nid, [])
            if cids:
                ctitles = [
                    f'"{node_by_id[c]["data"]["title"]}" (id={c})'
                    for c in cids if c in node_by_id
                ]
                lines.append(f'  Contains: {", ".join(ctitles)}')

        elif kind == "expected_document":
            if config.get("identified_by"):
                lines.append(f'  Identified by: {config["identified_by"]} = "{config.get("identifier","")}"')

        elif kind == "extract_validate":
            if config.get("applies_to"):
                lines.append(f'  Applies to: {config["applies_to"]}')
            if config.get("fail_if"):
                fc = config["fail_if"]
                if config.get("custom_expr"):
                    fc += f' ({config["custom_expr"]})'
                lines.append(f'  Fail condition: {fc}')

        elif kind == "match_documents":
            if config.get("match_type"):
                lines.append(f'  Match type: {config["match_type"]}')

        elif kind == "summarize":
            if config.get("summarize_source"):
                lines.append(f'  Summarize source: {config["summarize_source"]}')
            if config.get("summarize_instructions"):
                lines.append(f'  Instructions: {config["summarize_instructions"]}')

        elif kind == "assumption":
            lines.append(f'  Assumption text: "{title}"')

        # Blocks
        blocks: list[dict] = config.get("blocks") or []
        req_fields = [b for b in blocks if b.get("kind") == "required_field"]
        match_keys = [b for b in blocks if b.get("kind") == "match_key"]
        count_keys = [b for b in blocks if b.get("kind") == "count_key"]
        branch_conds = [b for b in blocks if b.get("kind") == "branch_condition"]
        notes = [b for b in blocks if b.get("kind") == "note"]
        sample_files_blocks = [b for b in blocks if b.get("kind") == "sample_file" and b.get("file_id")]

        if req_fields:
            lines.append("  Required fields:")
            for f in req_fields:
                req_str = "required" if f.get("required") else "optional"
                lines.append(f'    · "{f.get("name","")}" (scope: {f.get("scope","document")}, {req_str})')

        if match_keys:
            lines.append("  Match keys:")
            for mk in match_keys:
                lines.append(
                    f'    · {mk.get("source_collection","?")}::{mk.get("key_field","?")} '
                    f'→ {mk.get("target_collection","?")}::{mk.get("key_field","?")} '
                    f'(on_missing: {mk.get("on_missing","fail")})'
                )

        if count_keys:
            lines.append("  Count keys:")
            for ck in count_keys:
                track_str = ", ".join(ck.get("track") or []) or "none"
                lines.append(
                    f'    · collection={ck.get("collection","?")}, '
                    f'dedup_key={ck.get("dedup_key","?")}, '
                    f'label={ck.get("label","?")}, tracks=[{track_str}]'
                )

        if branch_conds:
            lines.append("  Branch conditions:")
            for bc in branch_conds:
                lines.append(f'    · "{bc.get("condition","")}" → "{bc.get("outcome","")}"')

        for nb in notes:
            if nb.get("text"):
                lines.append(f'  Note: {nb["text"]}')

        doc_fields = [b for b in blocks if b.get("kind") == "doc_field" and b.get("name")]
        if doc_fields:
            lines.append("  Fields present in this document:")
            for df in doc_fields:
                scope_str = "  [per line item]" if df.get("scope") == "line_item" else ""
                appears_str = f"  (appears as: {df['appears_as']})" if df.get("appears_as") else ""
                lines.append(f'    · {df["name"]}{appears_str}{scope_str}')

        if sample_files_blocks:
            for sf in sample_files_blocks:
                fid = sf["file_id"]
                fname = sf.get("filename", fid)
                text = file_texts.get(fid, "[no extracted text — see attached document block if present]")
                lines.append(f'  Sample file: {fname}')
                lines.append("  --- file content (extracted text) ---")
                lines.append(text[:FILE_TEXT_CAP])
                if len(text) > FILE_TEXT_CAP:
                    lines.append(f"  [... truncated at {FILE_TEXT_CAP} chars ...]")
                lines.append("  --- end file ---")

    lines.append("\n=== EDGES ===")
    title_map = {n["id"]: (n.get("data") or {}).get("title", n["id"]) for n in nodes}
    for edge in edges:
        src = edge.get("source", "?")
        tgt = edge.get("target", "?")
        ek = (edge.get("data") or {}).get("edgeKind", "default")
        lines.append(
            f'  "{title_map.get(src, src)}" (id={src}) '
            f'→ "{title_map.get(tgt, tgt)}" (id={tgt})  [{ek}]'
        )

    return "\n".join(lines)


def _parse_llm_json(raw: str) -> list[dict]:
    """Strip markdown fences and parse JSON from LLM response."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


# ── Route handlers ────────────────────────────────────────────────────────

@router.post("/{board_id}/gate/run")
async def run_gate(board_id: str) -> list[dict]:
    try:
        sb = _sb()
        llm = _llm()

        # Load board + graph
        board_row = (
            sb.table("boards").select("id, name, meta").eq("id", board_id).maybe_single().execute()
        )
        if not board_row.data:
            raise HTTPException(status_code=404, detail="Board not found")
        graph_row = (
            sb.table("board_graphs")
            .select("nodes, edges")
            .eq("board_id", board_id)
            .maybe_single()
            .execute()
        )
        board = {
            **board_row.data,
            "nodes": (graph_row.data or {}).get("nodes", []),
            "edges": (graph_row.data or {}).get("edges", []),
        }

        # Collect unique file_ids (deduplicated, in encounter order) + file → node mapping
        seen_file_ids: set[str] = set()
        file_ids: list[str] = []
        file_to_nodes: dict[str, list[tuple[str, str]]] = {}  # file_id -> [(node_id, node_title)]

        for node in board["nodes"]:
            ndata = node.get("data") or {}
            node_title = ndata.get("title", "Untitled")
            blocks = (ndata.get("config") or {}).get("blocks") or []
            for b in blocks:
                if b.get("kind") == "sample_file" and b.get("file_id"):
                    fid = b["file_id"]
                    if fid not in seen_file_ids:
                        file_ids.append(fid)
                        seen_file_ids.add(fid)
                    file_to_nodes.setdefault(fid, []).append((node["id"], node_title))

        # Fetch extracted text + storage metadata for all files
        file_texts: dict[str, str] = {}
        file_meta: dict[str, dict] = {}   # file_id -> {storage_path, mime, filename}

        if file_ids:
            res = (
                sb.table("sample_files")
                .select("id, extracted_text, storage_path, mime, filename")
                .in_("id", file_ids)
                .execute()
            )
            for row in (res.data or []):
                fid = row["id"]
                if row.get("extracted_text"):
                    file_texts[fid] = row["extracted_text"]
                file_meta[fid] = {
                    "storage_path": row.get("storage_path") or "",
                    "mime":         row.get("mime") or "",
                    "filename":     row.get("filename") or fid,
                }

        # Build text serialization (always sent — unchanged from prior behavior)
        board_description = _build_board_description(board, file_texts)

        # ── Download vision bytes (PDFs + images), within budget ──────────────
        # Vision blocks are ADDITIVE to the text. A failed/oversized download
        # silently falls back to the extracted text — never 500s the gate.

        # VisionEntry = (file_id, filename, mime, data)
        vision_blocks: list[tuple[str, str, str, bytes]] = []
        total_vision_bytes = 0

        for fid in file_ids:
            if len(vision_blocks) >= VISION_FILE_CAP:
                log.info(
                    "gate/run: vision file cap (%d) reached — remaining files use text only",
                    VISION_FILE_CAP,
                )
                break

            meta = file_meta.get(fid, {})
            mime         = meta.get("mime", "")
            storage_path = meta.get("storage_path", "")
            filename     = meta.get("filename", fid)

            is_pdf = mime in VISION_SUPPORTED_PDF
            is_img = mime in VISION_SUPPORTED_IMG
            if not (is_pdf or is_img):
                log.debug("gate/run: %s (mime=%s) — not vision-capable, text-only", filename, mime)
                continue

            if not storage_path:
                log.debug("gate/run: %s has no storage_path — skipping vision block", filename)
                continue

            if total_vision_bytes >= VISION_TOTAL_CAP:
                log.info(
                    "gate/run: total vision budget (%d bytes) exhausted — %s uses text only",
                    VISION_TOTAL_CAP, filename,
                )
                continue

            try:
                data: bytes = sb.storage.from_("sample-files").download(storage_path)
            except Exception as dl_exc:
                log.warning(
                    "gate/run: download failed for %s (path=%s): %s — text fallback",
                    filename, storage_path, dl_exc,
                )
                continue

            if len(data) > VISION_BYTE_CAP:
                log.warning(
                    "gate/run: %s is %d bytes (> %d cap) — skipping vision block, text fallback active",
                    filename, len(data), VISION_BYTE_CAP,
                )
                continue

            if total_vision_bytes + len(data) > VISION_TOTAL_CAP:
                log.info(
                    "gate/run: adding %s (%d bytes) would exceed total vision budget — skipping",
                    filename, len(data),
                )
                continue

            vision_blocks.append((fid, filename, mime, data))
            total_vision_bytes += len(data)
            log.info(
                "gate/run: vision block queued — %s (%d bytes, mime=%s)",
                filename, len(data), mime,
            )

        # ── Build message content list: text first, then labeled vision blocks ─
        # DocumentBlockParam shape (verified against anthropic==0.112.0):
        #   {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": <b64>}}
        # ImageBlockParam shape:
        #   {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg|png|gif|webp", "data": <b64>}}

        content: list[dict] = [{"type": "text", "text": board_description}]

        for fid, filename, mime, data in vision_blocks:
            nodes_for_file = file_to_nodes.get(fid, [])
            if nodes_for_file:
                node_refs = "; ".join(
                    f'node {nid} ("{ntitle}")' for nid, ntitle in nodes_for_file
                )
                label = f"--- Sample document attached to {node_refs} | filename: {filename} ---"
            else:
                label = f"--- Sample document: {filename} ---"

            content.append({"type": "text", "text": label})
            encoded = base64.b64encode(data).decode()

            if mime in VISION_SUPPORTED_PDF:
                content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": encoded,
                    },
                })
            else:  # image
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": encoded,
                    },
                })

        # Log final message structure so callers can verify vision is attached
        vision_summary = [
            f"{fname} ({mime}, {len(data)} raw bytes)"
            for _, fname, mime, data in vision_blocks
        ]
        log.info(
            "gate/run board_id=%s nodes=%d edges=%d unique_files=%d text_len=%d "
            "vision_blocks=%d total_vision_bytes=%d attached=%s",
            board_id,
            len(board["nodes"]), len(board["edges"]),
            len(file_ids), len(board_description),
            len(vision_blocks), total_vision_bytes,
            vision_summary or "none",
        )

        # Call Claude
        response = llm.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text
        log.debug("gate/run raw LLM response: %s", raw[:500])

        # Parse JSON
        try:
            gaps: list[dict] = _parse_llm_json(raw)
        except Exception as parse_exc:
            log.error("gate/run JSON parse failed. raw=%r error=%s", raw[:500], parse_exc)
            raise HTTPException(
                status_code=502,
                detail=(
                    f"LLM returned non-JSON response: {parse_exc}. "
                    f"Raw (first 200 chars): {raw[:200]}"
                ),
            )

        # Enforce advisory cap
        advisory_count = 0
        filtered: list[dict] = []
        for gap in gaps:
            if gap.get("severity") == "advisory":
                if advisory_count >= ADVISORY_CAP:
                    continue
                advisory_count += 1
            filtered.append(gap)

        # ── Round-aware insert: answered/resolved comments are NEVER deleted ───
        # Find the current max round for this board (0 if none exist yet).
        max_round_res = (
            sb.table("gate_comments")
            .select("round")
            .eq("board_id", board_id)
            .order("round", desc=True)
            .limit(1)
            .execute()
        )
        current_max = max_round_res.data[0]["round"] if max_round_res.data else 0
        new_round = current_max + 1
        log.info("gate/run board_id=%s round=%d", board_id, new_round)

        # Dedup: if a new gap shares the same node_id AND the same first
        # DEDUP_PREFIX_LEN chars of question as an already-open comment, skip it.
        open_res = (
            sb.table("gate_comments")
            .select("node_id, question")
            .eq("board_id", board_id)
            .eq("status", "open")
            .execute()
        )
        existing_open: list[tuple[str | None, str]] = [
            (r.get("node_id"), (r.get("question") or "")[:DEDUP_PREFIX_LEN].lower())
            for r in (open_res.data or [])
        ]

        def _is_dup(gap: dict) -> bool:
            nid = gap.get("node_id")
            q = (gap.get("question") or "")[:DEDUP_PREFIX_LEN].lower()
            return any(e_nid == nid and e_q == q for e_nid, e_q in existing_open)

        rows_to_insert = [
            {
                "board_id": board_id,
                "node_id": gap.get("node_id"),
                "severity": gap.get("severity", "advisory"),
                "status": "open",
                "question": gap.get("question", ""),
                "round": new_round,
            }
            for gap in filtered
            if gap.get("question") and not _is_dup(gap)
        ]

        if rows_to_insert:
            log.info("gate/run inserting %d new gap(s)", len(rows_to_insert))
            sb.table("gate_comments").insert(rows_to_insert).execute()
        else:
            log.info("gate/run no new non-duplicate gaps to insert")

        # Return ALL comments for this board so the frontend refreshes its full
        # view without losing answered/resolved work from prior rounds.
        all_res = (
            sb.table("gate_comments")
            .select("*")
            .eq("board_id", board_id)
            .order("created_at")
            .execute()
        )
        return all_res.data or []

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("run_gate failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"run_gate failed: {exc}")


@router.get("/{board_id}/gate/comments")
async def get_comments(board_id: str) -> list[dict]:
    try:
        sb = _sb()
        res = (
            sb.table("gate_comments")
            .select("*")
            .eq("board_id", board_id)
            .order("created_at")
            .execute()
        )
        return res.data or []
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("get_comments failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"get_comments failed: {exc}")


@router.patch("/{board_id}/gate/comments/{comment_id}")
async def patch_comment(board_id: str, comment_id: str, body: CommentPatch) -> dict:
    try:
        sb = _sb()
        updates: dict[str, Any] = {}
        if body.answer is not None:
            updates["answer"] = body.answer
        if body.status is not None:
            updates["status"] = body.status
        if body.followup is not None:
            updates["followup"] = body.followup
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        res = (
            sb.table("gate_comments")
            .update(updates)
            .eq("id", comment_id)
            .eq("board_id", board_id)
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Comment not found")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("patch_comment failed (board_id=%s, comment_id=%s)", board_id, comment_id)
        raise HTTPException(status_code=500, detail=f"patch_comment failed: {exc}")


@router.post("/{board_id}/gate/verify")
async def verify_gate(board_id: str) -> list[dict]:
    """Re-evaluate answered comments: resolve or append a follow-up question."""
    try:
        sb = _sb()
        llm = _llm()

        # Load all answered (but not yet resolved/rejected) comments
        answered_res = (
            sb.table("gate_comments")
            .select("*")
            .eq("board_id", board_id)
            .eq("status", "answered")
            .execute()
        )
        answered = answered_res.data or []
        if not answered:
            log.info("verify_gate board_id=%s no answered comments to verify", board_id)
            return []

        # Load board context for the model
        board_row = (
            sb.table("boards").select("id, name, meta").eq("id", board_id).maybe_single().execute()
        )
        graph_row = (
            sb.table("board_graphs")
            .select("nodes, edges")
            .eq("board_id", board_id)
            .maybe_single()
            .execute()
        )
        board = {
            **(board_row.data or {}),
            "nodes": (graph_row.data or {}).get("nodes", []),
            "edges": (graph_row.data or {}).get("edges", []),
        }
        board_description = _build_board_description(board, {})

        # Build the verify message
        qa_lines = [
            "The following questions were previously identified as gaps in the process map. "
            "The board author has provided answers. Verify each one.\n"
        ]
        for c in answered:
            qa_lines.append(f"COMMENT_ID: {c['id']}")
            qa_lines.append(f"NODE: {c.get('node_id') or 'board-level'}")
            qa_lines.append(f"SEVERITY: {c['severity']}")
            qa_lines.append(f"QUESTION: {c['question']}")
            qa_lines.append(f"USER_ANSWER: {c.get('answer') or '(no answer provided)'}")
            if c.get("followup"):
                qa_lines.append(f"PRIOR_FOLLOWUP: {c['followup']}")
            qa_lines.append("")

        user_message = f"[Board context]\n{board_description}\n\n---\n\n{''.join(l + chr(10) for l in qa_lines)}"

        log.info(
            "verify_gate board_id=%s verifying %d answered comment(s)",
            board_id, len(answered),
        )

        response = llm.messages.create(
            model=MODEL,
            max_tokens=VERIFY_MAX_TOKENS,
            system=VERIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text
        log.debug("verify_gate raw response: %s", raw[:400])

        try:
            verdicts: list[dict] = _parse_llm_json(raw)
        except Exception as parse_exc:
            log.error("verify_gate JSON parse failed. raw=%r error=%s", raw[:400], parse_exc)
            raise HTTPException(
                status_code=502,
                detail=f"Verify LLM returned non-JSON: {parse_exc}. Raw: {raw[:200]}",
            )

        # Apply verdicts
        now_iso = datetime.now(timezone.utc).isoformat()
        updated_comments: list[dict] = []
        verdict_map = {v.get("comment_id"): v for v in verdicts if v.get("comment_id")}

        for comment in answered:
            cid = comment["id"]
            verdict = verdict_map.get(cid)
            if not verdict:
                log.warning("verify_gate: no verdict returned for comment %s", cid)
                continue

            if verdict.get("verdict") == "resolved":
                updates = {"status": "resolved", "resolved_at": now_iso, "followup": None}
            else:  # insufficient
                followup = (verdict.get("followup") or "").strip() or "Please provide more detail."
                updates = {"followup": followup}

            res = (
                sb.table("gate_comments")
                .update(updates)
                .eq("id", cid)
                .eq("board_id", board_id)
                .execute()
            )
            if res.data:
                updated_comments.append(res.data[0])
                log.info(
                    "verify_gate comment %s → verdict=%s",
                    cid, verdict.get("verdict"),
                )

        return updated_comments

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("verify_gate failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"verify_gate failed: {exc}")


@router.post("/{board_id}/gate/freeze")
async def freeze_gate(board_id: str) -> dict:
    """Freeze the board into an immutable spec snapshot.

    Blocked while any blocking gap is uncleared (status not 'resolved' or 'rejected').
    Re-freezing replaces the prior spec (single-spec model).
    """
    try:
        sb = _sb()

        # PRECONDITION: no uncleared blocking gaps
        blocking_res = (
            sb.table("gate_comments")
            .select("id, question, status")
            .eq("board_id", board_id)
            .eq("severity", "blocking")
            .not_.in_("status", ["resolved", "rejected"])
            .execute()
        )
        uncleared = blocking_res.data or []
        if uncleared:
            count = len(uncleared)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot freeze: {count} blocking gap(s) are still open or unanswered. "
                    f"Resolve or dismiss all blocking questions first. "
                    f"Open blocking: {[c['question'][:60] + '…' for c in uncleared[:3]]}"
                ),
            )

        # Load board
        board_row = (
            sb.table("boards").select("id, name, meta").eq("id", board_id).maybe_single().execute()
        )
        if not board_row.data:
            raise HTTPException(status_code=404, detail="Board not found")
        graph_row = (
            sb.table("board_graphs")
            .select("nodes, edges")
            .eq("board_id", board_id)
            .maybe_single()
            .execute()
        )

        # Load all gate_comments for resolved_assumptions
        all_comments_res = (
            sb.table("gate_comments")
            .select("id, node_id, severity, status, question, answer, followup, round")
            .eq("board_id", board_id)
            .execute()
        )
        all_comments = all_comments_res.data or []

        frozen_at = datetime.now(timezone.utc).isoformat()

        spec: dict[str, Any] = {
            "board_id": board_id,
            "board_name": board_row.data.get("name", ""),
            "frozen_at": frozen_at,
            "meta": board_row.data.get("meta") or {},
            "nodes": (graph_row.data or {}).get("nodes", []),
            "edges": (graph_row.data or {}).get("edges", []),
            "resolved_assumptions": [
                {
                    "comment_id": c["id"],
                    "node_id": c.get("node_id"),
                    "severity": c["severity"],
                    "status": c["status"],
                    "round": c.get("round", 1),
                    "question": c["question"],
                    "answer": c.get("answer"),
                    "followup": c.get("followup"),
                }
                for c in all_comments
                if c["status"] in ("resolved", "answered", "rejected")
            ],
        }

        # Upsert (re-freeze replaces the single spec for this board)
        res = (
            sb.table("frozen_specs")
            .upsert(
                {"board_id": board_id, "spec": spec, "frozen_at": frozen_at},
                on_conflict="board_id",
            )
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=500, detail="frozen_specs upsert returned no rows")

        log.info(
            "freeze_gate board_id=%s nodes=%d edges=%d assumptions=%d",
            board_id,
            len(spec["nodes"]), len(spec["edges"]),
            len(spec["resolved_assumptions"]),
        )
        return res.data[0]

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("freeze_gate failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"freeze_gate failed: {exc}")


@router.get("/{board_id}/spec")
async def get_spec(board_id: str) -> dict:
    """Return the current frozen spec snapshot, or 404 if never frozen."""
    try:
        sb = _sb()
        res = (
            sb.table("frozen_specs")
            .select("*")
            .eq("board_id", board_id)
            .maybe_single()
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="No spec frozen yet for this board")
        return res.data
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("get_spec failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"get_spec failed: {exc}")
