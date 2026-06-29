"""Reusable, spec-agnostic agent runtime activities.

All activities are generic and parameterized — zero domain-specific values
are hardcoded here. Spec-specific values arrive as parameters from S8-generated
workflow code.
"""
from backend.runtime.activities.classify_documents import classify_documents
from backend.runtime.activities.extract_email_facts import extract_email_facts
from backend.runtime.activities.fetch_email import fetch_email_and_attachments
from backend.runtime.activities.load_document import load_document
from backend.runtime.activities.validate_fields import validate_required_fields
from backend.runtime.activities.match_documents import match_by_key
from backend.runtime.activities.tally import tally
from backend.runtime.activities.report import emit_report, send_report
from backend.runtime.activities.email_dedup import is_email_processed, mark_email_processed

__all__ = [
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
]
