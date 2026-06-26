"""Boards API — canvas persistence via Supabase."""

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/v1/boards", tags=["boards"])


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


@router.post("")
async def create_board(body: BoardCreate) -> dict:
    sb = _sb()
    board = sb.table("boards").insert({"name": body.name}).execute().data[0]
    sb.table("board_graphs").insert({
        "board_id": board["id"],
        "nodes": [],
        "edges": [],
    }).execute()
    return {"id": board["id"], "name": board["name"]}


@router.get("")
async def list_boards() -> list:
    sb = _sb()
    return (
        sb.table("boards")
        .select("id, name, created_at, updated_at")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@router.get("/{board_id}")
async def get_board(board_id: str) -> dict:
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


@router.put("/{board_id}/graph")
async def save_graph(board_id: str, body: GraphPayload) -> dict:
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
