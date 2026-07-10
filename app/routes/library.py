"""书库 API:列表/详情/封面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import db

router = APIRouter(prefix="/api/library", tags=["library"])

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"


@router.get("")
def list_books():
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT b.id, b.title, b.author, b.format, b.ingest_status,
                      b.total_chars, b.cover_path,
                      COALESCE(p.percent, 0) AS progress
               FROM books b
               LEFT JOIN reading_progress p ON p.book_id = b.id
               ORDER BY b.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{book_id}")
def get_book(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT b.*, COALESCE(p.percent, 0) AS progress,
                      COALESCE(p.spine_index, 0) AS spine_index,
                      p.cfi
               FROM books b
               LEFT JOIN reading_progress p ON p.book_id = b.id
               WHERE b.id = ?""",
            (book_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "book not found")
    return dict(row)


@router.get("/{book_id}/cover")
def get_cover(book_id: int):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT cover_path FROM books WHERE id=?", (book_id,)
        ).fetchone()
    if not row or not row["cover_path"]:
        raise HTTPException(404, "no cover")
    cover_file = DATA_ROOT.parent / row["cover_path"]
    if not cover_file.is_file():
        raise HTTPException(404, "cover file missing")
    return FileResponse(str(cover_file))
