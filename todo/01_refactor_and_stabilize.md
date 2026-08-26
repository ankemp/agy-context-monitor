# Task 01: Refactor and Stabilize

## Checklist

- [x] Extract HTML/CSS/JS frontend logic from `dashboard.py` into distinct `templates/` and `static/` directories.
- [x] Split backend logic in `dashboard.py` into smaller, focused modules (e.g., `server.py`, `api_routes.py`, `sse_stream.py`).
- [x] Adopt a standard layered architecture (api, services, models) per the `python-project-structure` skill.
- [x] Verify core invariants from `AGENTS.md` remain intact (Zero polling for UI, static charts).
- [x] Ensure SQLite WAL mode and DB connection logic is properly segregated in `db.py`.

## Breakdown

The current `dashboard.py` file has grown to ~130KB, mixing HTML string literals, Server-Sent Events (SSE) logic, HTTP routing, and data aggregation. Before adding new parsing and memory features, we need a stable foundation.

## Full Details

1. **Frontend Extraction**:
   - Move the raw HTML/JS currently embedded in `dashboard.py` into a `templates/index.html` file.
   - If JS/CSS gets too large, move them to a `static/` folder.
   - Use a lightweight templating engine (like Jinja2 or just standard string formatting if keeping dependencies low) to serve the HTML.
2. **Backend Modularization**:
   - `server.py`: Entry point to start the HTTP server.
   - `api_routes.py`: Endpoints for fetching historical data or triggering manual syncs.
   - `sse_stream.py`: Dedicated logic for the Server-Sent Events stream, ensuring thread safety and connection reliability.
3. **Database Layer Check**:
   - Ensure `db.py` handles the SQLite connections correctly with WAL mode and `busy_timeout` to prevent locking when multiple hooks fire simultaneously.
