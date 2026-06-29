"""Seed Board 9 sample_files from the verified live-run storage directory.

Idempotent: deletes all existing sample_files rows for the board, then inserts
fresh rows from live-runs/19f0ca0cfd34972d/ so the deterministic eval has access
to the same document set the live email produced (3 invoices + 8 COAs + ancillary).

Text-native PDFs (invoices) get extracted_text via pdfplumber.
Scanned PDFs (COAs) get extracted_text="" — vision fallback in load_document
fetches the raw bytes from storage_path and reads them with Claude vision.
"""
import io
import os
import pathlib
import sys

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

BOARD_ID     = "44092820-8f55-4fba-8b9e-5cb16f10493a"
BUCKET       = "sample-files"
LIVE_RUN_DIR = "live-runs/19f0ca0cfd34972d"


def main() -> None:
    import pdfplumber
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — check backend/.env")

    sb = create_client(url, key)

    # ── 1. Delete stale sample_files rows for this board ──────────────────
    print(f"Deleting stale sample_files for board {BOARD_ID} ...")
    sb.table("sample_files").delete().eq("board_id", BOARD_ID).execute()
    print("  done.")

    # ── 2. List files in the verified live-run storage directory ──────────
    files = sb.storage.from_(BUCKET).list(LIVE_RUN_DIR) or []
    print(f"\nFound {len(files)} files in {BUCKET}/{LIVE_RUN_DIR}/")

    # ── 3. Download, optionally extract text, build insert rows ───────────
    rows: list[dict] = []
    for f in files:
        name = f.get("name", "")
        if not name:
            continue

        storage_path = f"{LIVE_RUN_DIR}/{name}"
        is_pdf = name.lower().endswith(".pdf")
        mime   = "application/pdf" if is_pdf else "application/octet-stream"

        print(f"  {name!r:45s}", end=" ")
        try:
            data = sb.storage.from_(BUCKET).download(storage_path)
        except Exception as exc:
            print(f"SKIP (download error: {exc})")
            continue

        extracted_text = ""
        if is_pdf:
            try:
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    raw = "\n".join(p.extract_text() or "" for p in pdf.pages)
                extracted_text = raw[:20_000]
                status = f"{len(extracted_text)} chars"
            except Exception as exc:
                status = f"pdfplumber failed ({exc})"
        else:
            status = "non-PDF, no text"
        print(status)

        rows.append({
            "board_id":       BOARD_ID,
            "node_id":        None,
            "filename":       name,
            "mime":           mime,
            "storage_path":   storage_path,
            "extracted_text": extracted_text,
        })

    # ── 4. Add a "sample_email" placeholder so fixture_body detection works
    # The eval runner finds the email body via email_filename_hint="sample_email".
    # The agent doesn't use body text for counting, so a stub is fine.
    rows.append({
        "board_id":       BOARD_ID,
        "node_id":        None,
        "filename":       "sample_email.txt",
        "mime":           "text/plain",
        "storage_path":   "",
        "extracted_text": "Pre-alert email for MAWB 176-63154280 — see attachments.",
    })

    # ── 5. Insert all rows ─────────────────────────────────────────────────
    print(f"\nInserting {len(rows)} rows into sample_files ...")
    sb.table("sample_files").insert(rows).execute()
    print("  done.")

    # ── 6. Verification summary ────────────────────────────────────────────
    res = (
        sb.table("sample_files")
        .select("filename, storage_path, extracted_text")
        .eq("board_id", BOARD_ID)
        .execute()
    )
    seeded = res.data or []
    print(f"\n=== Seeded {len(seeded)} sample_files rows ===")
    for r in sorted(seeded, key=lambda x: x["filename"]):
        text_len = len(r.get("extracted_text") or "")
        sp = (r.get("storage_path") or "")[:45]
        print(f"  {r['filename']!r:45s}  text={text_len:5d}  storage={sp!r}")

    invoices = [r for r in seeded if r["filename"].lower().endswith(".pdf")
                and "u06-" in r["filename"].lower()
                and "-pl" not in r["filename"].lower()
                and "coa" not in r["filename"].lower()]
    coas     = [r for r in seeded if "coa.pdf" in r["filename"].lower()]
    print(f"\n  Invoices (U06-*.pdf): {len(invoices)}")
    for r in sorted(invoices, key=lambda x: x["filename"]):
        print(f"    {r['filename']!r}  text={len(r['extracted_text'] or '')} chars")
    print(f"  COAs (*COA.pdf): {len(coas)}")
    for r in sorted(coas, key=lambda x: x["filename"]):
        text_len = len(r.get("extracted_text") or "")
        print(f"    {r['filename']!r}  text={text_len} chars  (vision={'needed' if not text_len else 'not needed'})")


if __name__ == "__main__":
    main()
