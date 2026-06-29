"""match_by_key — Temporal activity.

Cross-references two item collections by a shared key field.
Supports exact, normalized (whitespace/case-insensitive), and fuzzy matching.
"""
import difflib
import logging
import unicodedata

from temporalio import activity

from backend.runtime.activities._types import MatchInput, MatchResult

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.85


def _normalize(value: str) -> str:
    """Lowercase, strip accents, collapse internal whitespace."""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _fuzzy_match(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= FUZZY_THRESHOLD


def _get_key(item: dict, key_field: str) -> str:
    return str(item.get(key_field, ""))


def _lookup_key(raw: str, match_type: str) -> str:
    return _normalize(raw) if match_type in ("normalized", "fuzzy") else raw


@activity.defn
def match_by_key(inp: MatchInput) -> MatchResult:
    """Cross-reference two item collections by a shared key field.

    Inputs:
        inp.source_items: list of record dicts (each must contain key_field)
        inp.target_items: list of record dicts (each must contain key_field)
        inp.key_field: the dict key to match on
        inp.on_missing: behavior when a source item has no target match:
            "fail"   → has_failures=True; item added to unmatched_source
            "flag"   → item added to unmatched_source (non-fatal)
            "ignore" → item silently dropped from unmatched_source
        inp.match_type: "exact" | "normalized" | "fuzzy"

    Outputs:
        MatchResult(matched, unmatched_source, unmatched_target, has_failures)
        matched: list of {source: dict, target: dict} pairs
    """
    log.info(
        "match_by_key key_field=%s match_type=%s on_missing=%s source=%d target=%d",
        inp.key_field, inp.match_type, inp.on_missing,
        len(inp.source_items), len(inp.target_items),
    )
    try:
        # Build target lookup
        target_by_key: dict[str, dict] = {}
        for tgt in inp.target_items:
            raw = _get_key(tgt, inp.key_field)
            target_by_key[_lookup_key(raw, inp.match_type)] = tgt

        matched: list[dict] = []
        unmatched_source: list[dict] = []
        used_target_keys: set[str] = set()
        has_failures = False

        for src in inp.source_items:
            src_raw = _get_key(src, inp.key_field)
            src_lk = _lookup_key(src_raw, inp.match_type)

            tgt = None
            matched_key: str | None = None

            if inp.match_type == "fuzzy":
                for tgt_key, tgt_item in target_by_key.items():
                    if tgt_key not in used_target_keys and _fuzzy_match(src_lk, tgt_key):
                        tgt = tgt_item
                        matched_key = tgt_key
                        break
            elif src_lk in target_by_key and src_lk not in used_target_keys:
                tgt = target_by_key[src_lk]
                matched_key = src_lk

            if tgt is not None:
                matched.append({"source": src, "target": tgt})
                used_target_keys.add(matched_key)  # type: ignore[arg-type]
            else:
                log.warning("match_by_key: no match for source key=%r", src_raw)
                if inp.on_missing == "fail":
                    has_failures = True
                if inp.on_missing != "ignore":
                    unmatched_source.append(src)

        unmatched_target = [
            v for k, v in target_by_key.items() if k not in used_target_keys
        ]

        log.info(
            "match_by_key matched=%d unmatched_src=%d unmatched_tgt=%d failures=%s",
            len(matched), len(unmatched_source), len(unmatched_target), has_failures,
        )
        return MatchResult(
            matched=matched,
            unmatched_source=unmatched_source,
            unmatched_target=unmatched_target,
            has_failures=has_failures,
        )
    except Exception as exc:
        log.warning(
            "match_by_key: unexpected error (key_field=%s source=%d target=%d) — returning empty match: %s",
            inp.key_field, len(inp.source_items), len(inp.target_items), exc, exc_info=True,
        )
        return MatchResult(
            matched=[],
            unmatched_source=list(inp.source_items),
            unmatched_target=list(inp.target_items),
            has_failures=True,
        )


# ── Smoke test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    source = [
        {"lot_id": "LOT-001", "qty": 10},
        {"lot_id": "LOT-002", "qty": 5},
        {"lot_id": "LOT-003", "qty": 3},  # no target match
    ]
    target = [
        {"lot_id": "LOT-001", "cert": "CERT-A"},
        {"lot_id": "LOT-002", "cert": "CERT-B"},
        {"lot_id": "LOT-999", "cert": "CERT-UNUSED"},
    ]
    result = match_by_key(MatchInput(
        source_items=source, target_items=target,
        key_field="lot_id", on_missing="flag", match_type="exact",
    ))
    print("match_by_key OK")
    print(f"  matched={len(result.matched)} unmatched_src={len(result.unmatched_source)}")
    print(f"  unmatched_tgt={len(result.unmatched_target)} failures={result.has_failures}")
    assert len(result.matched) == 2
    assert len(result.unmatched_source) == 1
    assert len(result.unmatched_target) == 1
    assert not result.has_failures   # on_missing=flag is non-fatal

    result2 = match_by_key(MatchInput(
        source_items=source, target_items=target,
        key_field="lot_id", on_missing="fail", match_type="exact",
    ))
    assert result2.has_failures   # LOT-003 has no target → failure
    print("  on_missing=fail case OK")
