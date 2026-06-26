"""Activity: run_skeleton_checks — all I/O lives here, never in the workflow."""

import os
from temporalio import activity


def _check_composio() -> dict:
    api_key = os.environ.get("COMPOSIO_API_KEY")
    if not api_key:
        return {"status": "not_configured", "detail": "COMPOSIO_API_KEY not set"}
    try:
        from composio import Composio  # type: ignore
        client = Composio(api_key=api_key)
        # Documented 0.16.0 call — fetches tool schemas, which validates the key.
        tools = client.tools.get(user_id="default", toolkits=["gmail"])
        count = len(tools) if hasattr(tools, "__len__") else "?"
        return {"status": "ok", "detail": f"SDK authenticated; {count} Gmail tools available"}
    except ImportError:
        return {"status": "error", "detail": "composio package not installed"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


def _check_supabase(source: str) -> dict:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return {
            "status": "not_configured",
            "detail": "SUPABASE_URL or SUPABASE_SERVICE_KEY not set",
        }
    try:
        from supabase import create_client  # type: ignore

        client = create_client(url, key)

        insert_result = (
            client.table("skeleton_pings")
            .insert({"source": source, "note": "walking skeleton"})
            .execute()
        )
        row_id = insert_result.data[0]["id"]

        read_result = (
            client.table("skeleton_pings").select("*").eq("id", row_id).execute()
        )
        row = read_result.data[0]
        return {
            "status": "ok",
            "detail": {"id": row["id"], "created_at": row["created_at"]},
        }
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@activity.defn
def run_skeleton_checks(source: str) -> dict:
    """Single activity that exercises the Composio and Supabase legs."""
    return {
        "composio": _check_composio(),
        "supabase": _check_supabase(source),
    }
