"""tally — Temporal activity.

Produces deduplicated counts from a list of result dicts.
State detection: result["passed"] bool → succeeded/failed; all counted items → processed.
"""
import logging

from temporalio import activity

from backend.runtime.activities._types import CountKey, TallyInput, TallyResult

log = logging.getLogger(__name__)


@activity.defn
def tally(inp: TallyInput) -> TallyResult:
    """Produce deduped counts per collection grain with tracked states.

    Inputs:
        inp.results: list of result dicts from prior activities.
            Each dict should contain the keys referenced by count_keys.
        inp.count_keys: list[CountKey-dict]:
            - collection: key in result dicts that identifies the collection
                          (a result is counted for this CountKey if the key exists)
            - dedup_key:  key to deduplicate on; same value = same item, counted once
            - label:      output key in TallyResult.counts
            - track:      list of states to count; supported: "processed", "succeeded", "failed"

        State detection from result dicts:
            passed=True   → "succeeded"
            passed=False  → "failed"
            key present   → "processed"

    Outputs:
        TallyResult(counts): {label: {state: int, ...}, ...}
    """
    count_key_objs = [
        CountKey(
            collection=ck["collection"],
            dedup_key=ck["dedup_key"],
            label=ck["label"],
            track=ck["track"],
        )
        for ck in inp.count_keys
    ]

    counts: dict[str, dict[str, int]] = {}

    try:
        for ck in count_key_objs:
            seen: set[str] = set()
            state_counts: dict[str, int] = {state: 0 for state in ck.track}

            for result in inp.results:
                # Only count this result if the collection key exists
                if ck.collection not in result:
                    continue

                dedup_val = str(result.get(ck.dedup_key, ""))
                if dedup_val in seen:
                    continue
                seen.add(dedup_val)

                if "processed" in ck.track:
                    state_counts["processed"] += 1

                passed = result.get("passed")
                if passed is True and "succeeded" in ck.track:
                    state_counts["succeeded"] += 1
                elif passed is False and "failed" in ck.track:
                    state_counts["failed"] += 1

            counts[ck.label] = state_counts
            log.info("tally label=%s counts=%s", ck.label, state_counts)

        return TallyResult(counts=counts)
    except Exception as exc:
        log.warning(
            "tally: unexpected error (results=%d count_keys=%d) — returning empty counts: %s",
            len(inp.results), len(inp.count_keys), exc, exc_info=True,
        )
        return TallyResult(counts={})


# ── Smoke test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    results = [
        {"item": "A", "passed": True},
        {"item": "B", "passed": True},
        {"item": "C", "passed": False},
        {"item": "A", "passed": True},  # duplicate of A — should dedup
    ]
    count_keys = [
        {
            "collection": "item",
            "dedup_key": "item",
            "label": "item_check",
            "track": ["processed", "succeeded", "failed"],
        }
    ]
    result = tally(TallyInput(results=results, count_keys=count_keys))
    print("tally OK")
    counts = result.counts["item_check"]
    print(f"  processed={counts['processed']} succeeded={counts['succeeded']} failed={counts['failed']}")
    assert counts["processed"] == 3   # A deduped
    assert counts["succeeded"] == 2
    assert counts["failed"] == 1
    print("  dedup + state counts correct")
