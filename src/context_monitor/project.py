"""Unified Project Extraction, Normalization, and DB Backfill Module.

Handles extracting project names from Antigravity worktrees, workspace paths,
<user_information> blocks, and tool arguments, as well as bidirectionally propagating
projects across subagent swarms and backfilling historical database records.
"""

from __future__ import annotations

import collections
import json
import re
import sqlite3
from typing import Any

__all__ = [
    "normalize_project_name",
    "extract_project_from_text",
    "extract_project_from_tool_args",
    "backfill_db_projects",
]

# Pre-compiled regex patterns for project extraction
_USER_INFO_ARROW_RE = re.compile(
    r"(?:/Users/[^\s]+)\s*->\s*[^\s]+/([^\s/\"\'<>`(),;:]+)"
)

_USER_INFO_WORKSPACE_RE = re.compile(
    r"(?:Active Workspaces:\s*(?:\n|\r\n)\s*-\s*|Command Working Directory:\s*)([^\r\n]+)"
)

_WORKTREE_PATH_RE = re.compile(
    r"[/\\]+\.gemini[/\\]+antigravity[/\\]+worktrees[/\\]+([^/\\\"'\s<>`(),;:]+)"
)

_WORKSPACE_PATH_RE = re.compile(
    r"(?:(?:file://)?(?:/Users/[^/\\\"'\s<>`]+|~|[A-Za-z]:[/\\]Users[/\\][^/\\\"'\s<>`]+|/home/[^/\\\"'\s<>`]+|/[a-zA-Z0-9_.-]+)?[/\\]+|\b)[Ww]orkspaces?[/\\]+([^/\\\"'\s<>`(),;:]+)"
)

_GENERIC_ROOT_PATHS = re.compile(
    r"^(?:/Users/[^/]+|/home/[^/]+|~|[A-Za-z]:[/\\]Users[/\\][^/\\]+|/tmp|/var/tmp|/)$"
)

_PUNCT_STRIP_CHARS = " \"'`.,;:()[]{}<>\\/"


def normalize_project_name(raw: str | None) -> str | None:
    """Normalizes a raw project name string.

    Strips surrounding whitespace, quotes, backticks, escaped quotes, and trailing
    punctuation (periods, commas, parens, brackets, colons).
    Returns None if the resulting string is empty, consists only of punctuation,
    or matches '(unassigned)'.
    """
    if raw is None:
        return None

    val = str(raw).strip()
    if not val:
        return None

    # Strip surrounding quotes, backticks, escaped slashes
    val = re.sub(r"^(?:[\"\'`\\]+)", "", val)
    val = re.sub(r"(?:[\"\'`\\]+)$", "", val)

    # Strip leading/trailing punctuation characters
    val = val.strip(_PUNCT_STRIP_CHARS)

    if not val or val.lower() in ("(unassigned)", "unassigned"):
        return None

    # Must contain at least one alphanumeric character
    if not re.search(r"[A-Za-z0-9]", val):
        return None

    return val


def extract_project_from_text(text: str | None) -> str | None:
    """Extracts and normalizes a project name from text content.

    Matches:
    1. <user_information> blocks:
       - Legacy arrow syntax: /Users/... -> /Users/.../<project>
       - Modern Active Workspaces / Command Working Directory: .../<project>
    2. Antigravity worktrees: .../.gemini/antigravity/worktrees/<project>/...
    3. Workspace paths (case-insensitive): .../[Ww]orkspaces?/<project>/...
    """
    if not text:
        return None

    s = str(text)

    # 1. <user_information> blocks
    if "<user_information>" in s:
        m = _USER_INFO_ARROW_RE.search(s)
        if m:
            proj = normalize_project_name(m.group(1))
            if proj:
                return proj

        m_ws = _USER_INFO_WORKSPACE_RE.search(s)
        if m_ws:
            raw_path = m_ws.group(1).strip().strip("\"'`")
            clean_path = raw_path.rstrip("/\\")
            if clean_path and not _GENERIC_ROOT_PATHS.match(clean_path):
                base = clean_path.split("/")[-1].split("\\")[-1]
                proj = normalize_project_name(base)
                if proj:
                    return proj

    # 2. Antigravity worktrees
    m = _WORKTREE_PATH_RE.search(s)
    if m:
        proj = normalize_project_name(m.group(1))
        if proj:
            return proj

    # 3. Workspace paths
    m = _WORKSPACE_PATH_RE.search(s)
    if m:
        proj = normalize_project_name(m.group(1))
        if proj:
            return proj

    return None


def extract_project_from_tool_args(tc_args: Any) -> str | None:
    """Extracts project name from tool call arguments dict or string."""
    if not tc_args:
        return None

    if isinstance(tc_args, dict):
        # Priority check on standard path keys
        for key in (
            "Path",
            "path",
            "TargetFile",
            "target_file",
            "AbsolutePath",
            "SearchPath",
            "DirectoryPath",
            "SearchDirectory",
            "search_directory",
            "TargetDirectory",
            "target_directory",
            "Cwd",
            "cwd",
            "CommandLine",
            "command_line",
        ):
            val = tc_args.get(key)
            if val:
                proj = extract_project_from_text(str(val))
                if proj:
                    return proj

        # Fallback to stringifying the entire dictionary
        try:
            return extract_project_from_text(json.dumps(tc_args))
        except Exception:
            return extract_project_from_text(str(tc_args))

    elif isinstance(tc_args, str):
        return extract_project_from_text(tc_args)

    return None


class _DisjointSet:
    """Disjoint Set Union (DSU) for grouping conversations into swarms."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        if item not in self.parent:
            self.parent[item] = item
            return item
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, item1: str, item2: str) -> None:
        root1 = self.find(item1)
        root2 = self.find(item2)
        if root1 != root2:
            self.parent[root1] = root2


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.cursor()
    return (
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        is not None
    )


def backfill_db_projects(conn: sqlite3.Connection) -> int:
    """Repairs, extracts, and bidirectionally propagates project names across conversations in the DB.

    Returns the total number of conversation rows updated.
    """
    if not _table_exists(conn, "conversations"):
        return 0

    orig_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        conv_rows = cur.execute(
            "SELECT id, title, project, parent_id, root_id, depth FROM conversations"
        ).fetchall()

        if not conv_rows:
            return 0

        total_updates = 0
        updates_to_apply: dict[str, str | None] = {}
        current_projects: dict[str, str] = {}

        # 1. Normalize existing non-empty project names
        for row in conv_rows:
            cid = row["id"]
            proj = row["project"]
            if proj:
                norm = normalize_project_name(proj)
                if norm != proj:
                    # If norm is None (e.g. '(unassigned)'), schedule setting to NULL
                    # If norm is a cleaned project name (e.g. 'ledger.' -> 'ledger'), schedule update
                    updates_to_apply[cid] = norm
                if norm:
                    current_projects[cid] = norm

        # 2. Identify unassigned conversations
        unassigned_cids = {
            row["id"] for row in conv_rows if row["id"] not in current_projects
        }

        if unassigned_cids:
            # 3a. Extract from tool_executions (target_path, command_line)
            if _table_exists(conn, "tool_executions"):
                tool_rows = cur.execute(
                    """
                    SELECT conversation_id, target_path, command_line
                    FROM tool_executions
                    WHERE conversation_id IN (
                        SELECT id FROM conversations
                        WHERE project IS NULL OR project = '' OR lower(project) IN ('(unassigned)', 'unassigned')
                    )
                    AND (target_path IS NOT NULL OR command_line IS NOT NULL)
                    """
                ).fetchall()
                for r in tool_rows:
                    cid = r["conversation_id"]
                    if cid in unassigned_cids and cid not in current_projects:
                        proj = None
                        if r["target_path"]:
                            proj = extract_project_from_text(r["target_path"])
                        if not proj and r["command_line"]:
                            proj = extract_project_from_text(r["command_line"])
                        if proj:
                            current_projects[cid] = proj
                            updates_to_apply[cid] = proj

            # 3b. Extract from transcript_blocks (content)
            if _table_exists(conn, "transcript_blocks"):
                tb_rows = cur.execute(
                    """
                    SELECT conversation_id, content
                    FROM transcript_blocks
                    WHERE conversation_id IN (
                        SELECT id FROM conversations
                        WHERE project IS NULL OR project = '' OR lower(project) IN ('(unassigned)', 'unassigned')
                    )
                    AND (
                        content LIKE '%Workspace%'
                        OR content LIKE '%workspace%'
                        OR content LIKE '%worktree%'
                        OR content LIKE '%<user_information>%'
                    )
                    """
                ).fetchall()
                for r in tb_rows:
                    cid = r["conversation_id"]
                    if cid in unassigned_cids and cid not in current_projects:
                        proj = extract_project_from_text(r["content"])
                        if proj:
                            current_projects[cid] = proj
                            updates_to_apply[cid] = proj

            # 3c. Extract from steps (detail)
            if _table_exists(conn, "steps"):
                step_rows = cur.execute(
                    """
                    SELECT conversation_id, detail
                    FROM steps
                    WHERE conversation_id IN (
                        SELECT id FROM conversations
                        WHERE project IS NULL OR project = '' OR lower(project) IN ('(unassigned)', 'unassigned')
                    )
                    AND detail IS NOT NULL
                    AND (
                        detail LIKE '%Workspace%'
                        OR detail LIKE '%workspace%'
                        OR detail LIKE '%worktree%'
                    )
                    """
                ).fetchall()
                for r in step_rows:
                    cid = r["conversation_id"]
                    if cid in unassigned_cids and cid not in current_projects:
                        proj = extract_project_from_text(r["detail"])
                        if proj:
                            current_projects[cid] = proj
                            updates_to_apply[cid] = proj

            # 3d. Extract from conversations (title)
            title_rows = cur.execute(
                """
                SELECT id, title
                FROM conversations
                WHERE (project IS NULL OR project = '' OR lower(project) IN ('(unassigned)', 'unassigned'))
                AND title IS NOT NULL
                AND (
                    title LIKE '%Workspace%'
                    OR title LIKE '%workspace%'
                    OR title LIKE '%worktree%'
                )
                """
            ).fetchall()
            for r in title_rows:
                cid = r["id"]
                if cid in unassigned_cids and cid not in current_projects:
                    proj = extract_project_from_text(r["title"])
                    if proj:
                        current_projects[cid] = proj
                        updates_to_apply[cid] = proj

        # 4. Bidirectional swarm propagation
        dsu = _DisjointSet()
        for row in conv_rows:
            cid = row["id"]
            dsu.find(cid)
            p_id = row["parent_id"]
            r_id = row["root_id"]
            if p_id:
                dsu.union(cid, p_id)
            if r_id:
                dsu.union(cid, r_id)

        components: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
        for row in conv_rows:
            root_key = dsu.find(row["id"])
            components[root_key].append(row)

        for root_key, members in components.items():
            # Check if any member has a known project
            swarm_project = None
            # First preference: Root conversation's project
            for m in members:
                m_cid = m["id"]
                if (m["depth"] == 0 or not m["parent_id"]) and m_cid in current_projects:
                    swarm_project = current_projects[m_cid]
                    break

            # Second preference: Any member's project
            if not swarm_project:
                for m in members:
                    m_cid = m["id"]
                    if m_cid in current_projects:
                        swarm_project = current_projects[m_cid]
                        break

            # If swarm project was found, propagate to unassigned members
            if swarm_project:
                for m in members:
                    m_cid = m["id"]
                    if m_cid not in current_projects:
                        current_projects[m_cid] = swarm_project
                        updates_to_apply[m_cid] = swarm_project

        # 5. Apply batch updates
        if updates_to_apply:
            params = [(proj, cid) for cid, proj in updates_to_apply.items()]
            cur.executemany("UPDATE conversations SET project = ? WHERE id = ?", params)
            conn.commit()
            total_updates = len(params)

        return total_updates
    finally:
        conn.row_factory = orig_factory
