# Meridian Agent Runtime Contract

**Version:** S7  
**Target:** S8 codegen — this document is the single source of truth that generated workflows compile against.

---

## Overview

The runtime translates a frozen spec (from `frozen_specs`) into a running Temporal workflow.
S7 delivers the **substrate** (generic activities + scaffolding).
S8 delivers **codegen** that reads a spec and emits a spec-specific workflow that calls these activities.

All activities are **generic and parameterized**. Spec-specific values (field names, document identifiers, recipients, etc.) arrive as parameters from the generated workflow code, never hardcoded in the library.

---

## Node Kind → Runtime Mapping

| Spec node kind   | Runtime element                       | Notes |
|------------------|---------------------------------------|-------|
| `trigger`        | Workflow entry params                 | Describes what starts the workflow. Not an activity. S11 wires live Gmail trigger. |
| `expected_document` | `load_document()`                  | Locates and extracts text from one document in the attachment list. |
| `extract_validate`  | `validate_required_fields()`       | Presence check for declared fields; honors `appears_as` and `applies_to`. |
| `match_documents`   | `match_by_key()`                   | Cross-references two item collections by a shared key. |
| `count`             | `tally()`                          | Deduped counts per collection grain with tracked states. |
| `summarize`         | `summarize()` (stub — S9)          | LLM summary; S7 registers a stub; implement in S9. |
| `tool_action`       | Dispatched by `action_type`:       | See sub-table below. |
| `report`            | `emit_report()` + `send_report()`  | Build formatted report, then send (send is stub until S11). |
| `scope` (for_each)  | Temporal `for` loop in workflow    | NOT an activity. S8 generates a loop; nesting maps to nested loops. |
| `decision`          | Temporal `if/elif` branch          | NOT an activity. S8 generates a branch. |
| `assumption`        | Spec metadata only                 | Not executed; appears in frozen spec for human review. |
| `custom`            | `custom_step()` stub               | Clearly-marked TODO; user fills in or codegen emits a placeholder. |

### tool_action dispatch by action_type

| action_type | Activity                        | Notes |
|-------------|---------------------------------|-------|
| `fetch`     | `fetch_email_and_attachments()` | S7: fixture mode. S11: Composio Gmail. |
| `send`      | `send_report()`                 | S7: stub logs intent. S11: Composio Gmail send. |
| `call_api`  | `call_api_stub()`               | TODO stub — implement per spec in generated code. |
| `store`     | `store_stub()`                  | TODO stub. |
| `transform` | `transform_stub()`              | TODO stub. |
| `other`     | `custom_step()`                 | TODO stub. |

---

## Activity Signatures

### `fetch_email_and_attachments`

```python
Input:  FetchEmailInput(
    message_id: str          # Gmail RFC-822 message-id (used for dedup)
    use_fixture: bool        # True = fixture mode (S7); False = live Gmail (S11)
    fixture_subject: str     # fixture mode only
    fixture_sender: str      # fixture mode only
    fixture_body: str        # fixture mode only
    fixture_attachments: list[dict]  # list of Attachment-dicts
)
Output: FetchEmailResult(
    message_id: str
    subject: str
    sender: str
    body_text: str
    attachments: list[dict]  # list of Attachment-dicts {name, mime, data_b64}
)
```

Attachment-dict shape: `{name: str, mime: str, data_b64: str}`  
`data_b64` is base64-encoded file content. Empty string = content unavailable.

S8 usage: emit one call per `tool_action(fetch)` node, passing `use_fixture=False` and the dedup `message_id`.

---

### `load_document`

```python
Input:  LoadDocumentInput(
    attachments: list[dict]  # Attachment-dicts from fetch_email result
    identified_by: str       # "filename" | "header_text" | "content"
    identifier: str          # substring / phrase / keyword to match
)
Output: LoadDocumentResult(
    name: str       # matched attachment filename
    mime: str
    text: str       # extracted text (pdfplumber for PDF; utf-8 decode for text/csv)
    found: bool     # False if no attachment matched the rule
)
```

S8 usage: emit one call per `expected_document` node. `identified_by` and `identifier` come from the node's spec config.

---

### `validate_required_fields`

```python
Input:  ValidateFieldsInput(
    document_text: str
    fields: list[dict]    # FieldSpec-dicts: {name, appears_as?, scope?, required?}
    fail_if: str          # "any_missing" | "all_missing" | "custom"
    applies_to: str       # "per_document" | "per_line_item"
)
Output: ValidateFieldsResult(
    passed: bool
    field_results: list[dict]  # [{name, found, scope}, ...]
    fail_reason: str           # empty when passed
)
```

FieldSpec-dict shape: `{name: str, appears_as?: str, scope?: str, required?: bool}`

Presence check: searches for `appears_as` (if set) or `name` in the document text (case-insensitive).  
`per_line_item`: checked against each non-empty line independently.  
`fail_if="custom"`: always returns `passed=True`; the calling workflow applies custom logic.

S8 usage: emit one call per `extract_validate` node. `fields` comes from the node's `required_field` blocks. `fail_if` and `applies_to` come from node config.

---

### `match_by_key`

```python
Input:  MatchInput(
    source_items: list[dict]  # each must contain key_field
    target_items: list[dict]  # each must contain key_field
    key_field: str
    on_missing: str           # "fail" | "flag" | "ignore"
    match_type: str           # "exact" | "normalized" | "fuzzy"
)
Output: MatchResult(
    matched: list[dict]           # [{source: dict, target: dict}, ...]
    unmatched_source: list[dict]
    unmatched_target: list[dict]
    has_failures: bool            # True only when on_missing="fail" and items unmatched
)
```

`normalized`: lowercases, strips accents and extra whitespace before comparing.  
`fuzzy`: SequenceMatcher ratio ≥ 0.85.

S8 usage: emit one call per `match_documents` node. Parameters come from `match_key` blocks and node config.

---

### `tally`

```python
Input:  TallyInput(
    results: list[dict]     # result dicts from prior activities
    count_keys: list[dict]  # CountKey-dicts
)
Output: TallyResult(
    counts: dict  # {label: {state: int, ...}, ...}
)
```

CountKey-dict shape: `{collection: str, dedup_key: str, label: str, track: list[str]}`

`collection`: the key in result dicts that must be present for that result to be counted.  
`dedup_key`: results with the same value for this key are counted only once.  
State detection: `passed=True → succeeded`, `passed=False → failed`, key-present → processed.

S8 usage: emit one call per `count` node. `count_keys` come from `count_key` blocks.

---

### `emit_report`

```python
Input:  EmitReportInput(
    format: str        # "csv" | "json"
    columns: list[str]
    rows: list[dict]   # each dict is one row; extra keys are ignored
)
Output: EmitReportResult(
    content: str    # full CSV or JSON string
    format: str
    row_count: int
)
```

S8 usage: emit one call per `report` node. Column list from report node config.

---

### `send_report`

```python
Input:  SendReportInput(
    report_content: str
    format: str
    recipient: str
    subject: str
    body: str
)
Output: SendReportResult(
    sent: bool    # S7: always False (stub). S11: True on success.
    detail: str
)
```

S8 usage: emit after `emit_report` for any `tool_action(send)` node that follows a report.

---

### `extract_email_facts`

```python
Input:  ExtractEmailFactsInput(
    subject:          str   # email subject line
    body:             str   # email body text
    shipment_id_hint: str   # describes the shipment ID format (from spec key_field / resolved_assumptions)
                            # e.g. "MAWB number, format NNN-NNNNNNNN" or "container number"
                            # use "" if not known / not applicable
    invoice_hint:     str   # describes how invoice refs appear in this sender's emails
                            # e.g. "invoice numbers after 'INVOICE NO' label, comma-separated,
                            #       may be abbreviated (e.g. '235, 238' expands two invoices)"
                            # use "" if invoice numbers are not expected in the email header
)
Output: ExtractEmailFactsResult(
    shipment_id:     str        # canonical identifier (MAWB / container / BL), or ""
    invoice_numbers: list[str]  # trailing numeric portion of each invoice reference,
                                # expanded from abbreviated comma-separated forms
                                # e.g. "100/26-27/235, 238" → ["235", "238"]
                                # [] when no invoice list is present
)
```

Extracts structured shipment facts from the email using Claude LLM, guided by spec-derived hints.
Falls back to regex on any LLM failure — never raises.

S8 usage: call **once** right after `fetch_email_and_attachments`; use the results for shipment ID
and invoice reconciliation with `classify_documents` output.

```python
email_facts = await workflow.execute_activity(
    extract_email_facts,
    ExtractEmailFactsInput(
        subject=fetch_result.subject,
        body=fetch_result.body_text,
        shipment_id_hint="<from spec key_field, e.g. 'MAWB number, format NNN-NNNNNNNN'>",
        invoice_hint="<from spec resolved_assumptions, or '' if not in email header>",
    ),
    start_to_close_timeout=_CLASSIFY_TIMEOUT,
    retry_policy=RetryPolicy(maximum_attempts=1),
)
shipment_number  = email_facts.shipment_id or fetch_result.subject.strip()
invoice_numbers  = email_facts.invoice_numbers

# Reconcile with classify_documents output by CONTENT (not filename):
# classify_documents already extracted each invoice's identifier from its content
# (e.g. identifier='C04/26-27/578' for a doc named 'C04 26-27 578 DAP Shipment.pdf').
# Match by: (a) classifier identifier contains inv_num, (b) doc text contains inv_num,
# (c) filename contains inv_num (last resort). Never match by filename-ending alone.
invoices_in_shipment: list[dict] = []
if invoice_numbers:
    remaining = list(invoice_docs)  # avoid matching the same doc twice
    for inv_num in invoice_numbers:
        matched = next(
            (d for d in remaining
             if inv_num in d.get("identifier", "")
             or inv_num in d.get("text", "")
             or inv_num in d.get("name", "")),
            None,
        )
        if matched:
            invoices_in_shipment.append(matched)
            remaining.remove(matched)
        # Do NOT append empty stubs — they produce false validation failures.
    if not invoices_in_shipment:
        invoices_in_shipment = invoice_docs  # fallback: all classified invoices
else:
    # No invoice list in email — trust strict content classification
    invoices_in_shipment = invoice_docs
```

---

### `classify_documents`

```python
Input:  ClassifyDocumentsInput(
    attachments: list[dict]   # Attachment-dicts from fetch_email result
                              # (storage_path / original_mime keys enable vision for scanned PDFs)
    doc_types: list[dict]     # [{type_name: str, description: str}, ...]
                              # Derived from the spec's expected_document nodes.
                              # Always include {"type_name": "other", "description": "..."}
)
Output: ClassifyDocumentsResult(
    documents: list[dict]   # One entry per attachment, same order as input:
                            #   name: str        — attachment filename
                            #   doc_type: str    — matched type_name, or "other"
                            #   identifier: str  — primary key extracted from content
                            #                      (invoice number, batch number, etc.); "" if none
                            #   text: str        — full extracted text, vision-enriched for scanned PDFs
)
```

Classifies every attachment by document **content** (not filename) using Claude LLM.  
Falls back to filename-keyword heuristics on LLM failure — never raises.

Vision extraction fires automatically for scanned PDFs:
- **Fixture mode** (`mime="text/plain"`, `storage_path` set): downloads raw bytes from Supabase.
- **Live mode** (`mime="application/pdf"`, no `storage_path`): uses `data_b64` bytes directly.

S8 usage: emit **one call** near the top of the workflow (after `fetch_email_and_attachments`),  
before any document loop. Use the returned `doc_type` to select invoices, COAs, etc.;  
use `text` directly for `validate_required_fields` and field extraction — no separate  
`load_document` call needed for documents that were classified here.

```python
classify_result = await workflow.execute_activity(
    classify_documents,
    ClassifyDocumentsInput(
        attachments=fetch_result.attachments,
        doc_types=[
            {"type_name": "commercial_invoice", "description": "<from spec>"},
            {"type_name": "certificate_of_analysis", "description": "<from spec>"},
            {"type_name": "packing_list", "description": "packing list — not an invoice"},
            {"type_name": "other", "description": "any other document"},
        ],
    ),
    start_to_close_timeout=_VISION_TIMEOUT,  # may vision-read multiple scanned PDFs
)
invoice_docs = [d for d in classify_result.documents if d["doc_type"] == "commercial_invoice"]
coa_docs     = [d for d in classify_result.documents if d["doc_type"] == "certificate_of_analysis"]
```

---

### `is_email_processed` / `mark_email_processed`

```python
Input:  EmailDedupInput(
    message_id: str   # Gmail RFC-822 message-id
    board_id: str     # optional; scopes dedup to one board agent
)
Output: EmailDedupResult(
    already_processed: bool
    marked_at: str          # ISO timestamp; empty if not yet processed
)
```

S8 usage: generated workflows call `is_email_processed` at the top (before any work) and `mark_email_processed` at the end. Together they ensure each Gmail message is processed exactly once.

---

## Control-Flow Patterns (NOT activities — generated workflow code)

### scope / for_each loop

```python
# S8 generates this pattern for each scope node:
# iterate_over = spec node config → inp.<collection_name>
# item_name    = spec node config → local variable name

for <item_name> in inp.<iterate_over>:
    <activities inside the scope node>
    results.append(...)

# Nested scopes = nested loops:
for batch in inp.batches:
    for item in batch["line_items"]:
        ...
```

`parentId` on scope nodes maps to nesting. The example workflow (`example_workflow.py`) demonstrates the single-loop case.

### decision branch

```python
# S8 generates this pattern for each decision node:
# branch_conditions from the node's branch_condition blocks

if <condition_A>:
    result = await workflow.execute_activity(path_a_activity, ...)
elif <condition_B>:
    result = await workflow.execute_activity(path_b_activity, ...)
else:
    result = await workflow.execute_activity(default_activity, ...)
```

---

## Email Dedup Guard

All email-triggered agent workflows must call the dedup guard:

```python
# At workflow start:
dedup = await workflow.execute_activity(
    is_email_processed,
    EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
    start_to_close_timeout=timedelta(seconds=30),
)
if dedup.already_processed:
    return EarlyExitResult(reason="already_processed", marked_at=dedup.marked_at)

# ... do all work ...

# At workflow end (on success):
await workflow.execute_activity(
    mark_email_processed,
    EmailDedupInput(message_id=inp.message_id, board_id=inp.board_id),
    start_to_close_timeout=timedelta(seconds=30),
)
```

Requires the `processed_emails` table in Supabase (see schema.sql, S7 section).

---

## Task Queue

All activities and generated workflows run on task queue: **`meridian-agent`**

The S2 skeleton uses a separate `meridian-skeleton` queue and is unaffected.

---

## What S8 Will Do

1. Read the `frozen_specs` row for a board.
2. Walk `spec.nodes` in topological order (edges define the DAG).
3. For each node, emit the corresponding activity call or control-flow pattern above.
4. For `scope` nodes: emit a `for` loop wrapping the inner node calls.
5. For `decision` nodes: emit an `if/elif` branch.
6. Wire outputs of one activity as inputs to the next (via local variables).
7. Collect results → `tally` → `emit_report` → `send_report`.
8. Wrap with email dedup guard.
9. Write the generated file to `backend/generated/<board_id>_workflow.py`.
