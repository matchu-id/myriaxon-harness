import sqlite3
import time

from .constants import MEMORY_DB_FILE

SEARCH_MEMORY_TOOL_DEF = {
    "name": "search_memory",
    "description": (
        "Full-text search over past session transcripts across all Myriaxon projects on this "
        "machine (cross-session memory) -- use this to recall what was done in a previous "
        "session, in this project or another one, before asking the user to re-explain context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for."},
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
        "required": ["query"],
    },
}


def _connect() -> sqlite3.Connection:
    MEMORY_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(MEMORY_DB_FILE))
    # A standalone (non external-content) FTS5 table -- the table itself is the store, no
    # separate rowid-synced base table to keep consistent. One row per project_path: each
    # record_session call replaces that project's row rather than appending, so this stays
    # bounded (one row per distinct project ever used) instead of growing one row per turn.
    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            project_path UNINDEXED, provider UNINDEXED, model UNINDEXED, ts UNINDEXED, transcript
        )
        """
    )
    return con


def record_session(project_path: str, provider: str, model: str, transcript: str) -> None:
    """Best-effort upsert of a project's latest session transcript into the searchable memory
    store. Never raises -- a memory-store failure must never block saving/using the session
    itself."""
    if not transcript.strip():
        return
    try:
        con = _connect()
        with con:
            con.execute("DELETE FROM sessions_fts WHERE project_path = ?", (project_path,))
            con.execute(
                "INSERT INTO sessions_fts (project_path, provider, model, ts, transcript) VALUES (?,?,?,?,?)",
                (project_path, provider, model, time.time(), transcript),
            )
        con.close()
    except sqlite3.Error:
        pass


def search_memory(query: str, limit: int = 10) -> list[dict]:
    """Full-text search across all past session transcripts (any project). Never raises --
    returns an empty list on any failure, including malformed FTS5 query syntax."""
    try:
        con = _connect()
        rows = con.execute(
            """
            SELECT project_path, provider, model, ts, snippet(sessions_fts, 4, '>>>', '<<<', '...', 24)
            FROM sessions_fts
            WHERE sessions_fts MATCH ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        con.close()
        return [
            {"project_path": r[0], "provider": r[1], "model": r[2], "ts": r[3], "snippet": r[4]} for r in rows
        ]
    except sqlite3.Error:
        return []


def format_memory_results(results: list[dict]) -> str:
    if not results:
        return "No matching past sessions found."
    lines = []
    for r in results:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        lines.append(f"[{when}] {r['project_path']} ({r['provider']}/{r['model']}):\n  {r['snippet']}")
    return "\n\n".join(lines)


def execute_search_memory(tool_input: dict) -> tuple[str, bool]:
    results = search_memory(tool_input["query"], tool_input.get("limit") or 10)
    return format_memory_results(results), False
