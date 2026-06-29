"""Boards API — canvas persistence via Supabase."""

import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/v1/boards", tags=["boards"])
log = logging.getLogger(__name__)


def _sb():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    return create_client(url, key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BoardCreate(BaseModel):
    name: str


class GraphPayload(BaseModel):
    nodes: list[Any]
    edges: list[Any]


class BoardMeta(BaseModel):
    subject_name: str = ""
    key_field: str = ""


@router.post("")
async def create_board(body: BoardCreate) -> dict:
    try:
        sb = _sb()
        res = sb.table("boards").insert({"name": body.name}).execute()
        if not res.data:
            raise HTTPException(
                status_code=500,
                detail="board insert returned no rows — check the boards table / RLS",
            )
        board = res.data[0]
        sb.table("board_graphs").insert({
            "board_id": board["id"],
            "nodes": [],
            "edges": [],
        }).execute()
        return {"id": board["id"], "name": board["name"], "meta": {}}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("create_board failed")
        raise HTTPException(status_code=500, detail=f"create_board failed: {exc}")


@router.get("")
async def list_boards() -> list:
    try:
        sb = _sb()
        return (
            sb.table("boards")
            .select("id, name, meta, created_at, updated_at")
            .order("created_at", desc=True)
            .execute()
            .data
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("list_boards failed")
        raise HTTPException(status_code=500, detail=f"list_boards failed: {exc}")


@router.get("/{board_id}")
async def get_board(board_id: str) -> dict:
    try:
        sb = _sb()
        board = (
            sb.table("boards").select("*").eq("id", board_id).maybe_single().execute()
        )
        if not board.data:
            raise HTTPException(status_code=404, detail="Board not found")
        graph = (
            sb.table("board_graphs")
            .select("nodes, edges, updated_at")
            .eq("board_id", board_id)
            .maybe_single()
            .execute()
        )
        return {
            **board.data,
            "nodes": graph.data["nodes"] if graph.data else [],
            "edges": graph.data["edges"] if graph.data else [],
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("get_board failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"get_board failed: {exc}")


@router.put("/{board_id}/graph")
async def save_graph(board_id: str, body: GraphPayload) -> dict:
    try:
        sb = _sb()
        now = _now()
        sb.table("board_graphs").upsert({
            "board_id": board_id,
            "nodes": body.nodes,
            "edges": body.edges,
            "updated_at": now,
        }).execute()
        sb.table("boards").update({"updated_at": now}).eq("id", board_id).execute()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("save_graph failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"save_graph failed: {exc}")


@router.put("/{board_id}/meta")
async def save_meta(board_id: str, body: BoardMeta) -> dict:
    try:
        sb = _sb()
        sb.table("boards").update({"meta": body.model_dump()}).eq("id", board_id).execute()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("save_meta failed (board_id=%s)", board_id)
        raise HTTPException(status_code=500, detail=f"save_meta failed: {exc}")
