#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["jinja2"]
# ///
import os
import sys
import json
import sqlite3
import argparse
import webbrowser
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from jinja2 import Environment, FileSystemLoader

_src = Path(__file__).resolve().parent.parent.parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from context_monitor.db import get_db_connection, init_db  # noqa: E402
from context_monitor.dashboard import api_routes, sse_stream  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parent
env = Environment(loader=FileSystemLoader(DASHBOARD_DIR / "templates"))

# In-memory API response cache invalidated automatically via SQLite PRAGMA data_version
_API_CACHE_LOCK = threading.Lock()
_API_CACHE = {}
_MONITOR_CONNS = {}
_MONITOR_INOS = {}
_LAST_DATA_VERSIONS = {}
_ACTIVE_DB = None


def clear_api_cache():
    """Explicitly clears the in-memory API cache and resets tracked data versions."""
    global _ACTIVE_DB
    with _API_CACHE_LOCK:
        _API_CACHE.clear()
        _LAST_DATA_VERSIONS.clear()
        _ACTIVE_DB = None


# Hook clear_api_cache into SSE change notification stream
sse_stream.register_change_callback(clear_api_cache)


def _extract_db_path(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("PRAGMA database_list;").fetchall()
        for row in rows:
            name = (
                row["name"] if hasattr(row, "keys") and "name" in row.keys() else row[1]
            )
            if name == "main":
                f = (
                    row["file"]
                    if hasattr(row, "keys") and "file" in row.keys()
                    else row[2]
                )
                if f and str(f).strip():
                    return str(Path(f).resolve())
    except Exception:
        pass
    return None


def get_cached_api_response(conn, cache_key: str, compute_fn):
    global _ACTIVE_DB
    db_path = _extract_db_path(conn)

    with _API_CACHE_LOCK:
        if db_path:
            cur_ino = None
            try:
                cur_ino = Path(db_path).stat().st_ino
            except OSError:
                pass

            mon_conn = _MONITOR_CONNS.get(db_path)
            last_ino = _MONITOR_INOS.get(db_path)

            # If file was replaced or recreated at the same path, reconnect monitor
            if mon_conn is not None and (cur_ino is None or cur_ino != last_ino):
                try:
                    mon_conn.close()
                except Exception:
                    pass
                mon_conn = None
                _MONITOR_CONNS.pop(db_path, None)
                _MONITOR_INOS.pop(db_path, None)
                _LAST_DATA_VERSIONS.pop(db_path, None)

            if mon_conn is None and cur_ino is not None:
                try:
                    mon_conn = sqlite3.connect(
                        db_path, timeout=30.0, check_same_thread=False
                    )
                    mon_conn.execute("PRAGMA busy_timeout = 5000;")
                    _MONITOR_CONNS[db_path] = mon_conn
                    _MONITOR_INOS[db_path] = cur_ino
                except Exception:
                    mon_conn = None

            if mon_conn is not None:
                try:
                    dv = mon_conn.execute("PRAGMA data_version;").fetchone()[0]
                except Exception:
                    try:
                        mon_conn.close()
                    except Exception:
                        pass
                    try:
                        mon_conn = sqlite3.connect(
                            db_path, timeout=30.0, check_same_thread=False
                        )
                        mon_conn.execute("PRAGMA busy_timeout = 5000;")
                        _MONITOR_CONNS[db_path] = mon_conn
                        dv = mon_conn.execute("PRAGMA data_version;").fetchone()[0]
                    except Exception:
                        dv = conn.execute("PRAGMA data_version;").fetchone()[0]
            else:
                dv = conn.execute("PRAGMA data_version;").fetchone()[0]
            target_key = db_path
        else:
            try:
                base_dv = conn.execute("PRAGMA data_version;").fetchone()[0]
                schema_v = conn.execute("PRAGMA schema_version;").fetchone()[0]
                changes = getattr(conn, "total_changes", 0)
                dv = (base_dv, schema_v, changes)
            except Exception:
                dv = id(conn)
            target_key = f"in_memory_{id(conn)}"

        last_dv = _LAST_DATA_VERSIONS.get(target_key)
        if target_key != _ACTIVE_DB or last_dv is None or dv != last_dv:
            _API_CACHE.clear()
            _ACTIVE_DB = target_key
            _LAST_DATA_VERSIONS[target_key] = dv

        if cache_key in _API_CACHE:
            return _API_CACHE[cache_key]

    data = compute_fn()
    if isinstance(data, tuple) and data[0] == "error":
        return data

    serialized = json.dumps(data).encode("utf-8")
    with _API_CACHE_LOCK:
        if _ACTIVE_DB == target_key:
            if db_path and mon_conn is not None:
                try:
                    current_dv = mon_conn.execute("PRAGMA data_version;").fetchone()[0]
                except Exception:
                    current_dv = dv
            else:
                current_dv = dv
            _LAST_DATA_VERSIONS[target_key] = current_dv
            _API_CACHE[cache_key] = serialized
    return serialized


class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        client_tz = qs.get("tz", [None])[0]
        project_param = qs.get("project", [None])[0]
        if project_param == "all" or not project_param:
            project_param = None

        if path == "/" or path == "/index.html":
            template = env.get_template("index.html")
            html = template.render(IS_STATIC_EXPORT=False)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if path == "/static/style.css":
            self.send_response(200)
            self.send_header("Content-Type", "text/css")
            self.end_headers()
            with open(DASHBOARD_DIR / "static" / "style.css", "rb") as f:
                self.wfile.write(f.read())
            return

        if path == "/static/script.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            with open(DASHBOARD_DIR / "static" / "script.js", "rb") as f:
                self.wfile.write(f.read())
            return

        if path in ("/api/events", "/events", "/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            client_q = sse_stream.queue.Queue(maxsize=50)
            with sse_stream.SSE_LOCK:
                sse_stream.SSE_CLIENTS.add(client_q)

            try:
                conn_quota = get_db_connection()
                try:
                    quota_initial = api_routes.query_quota_data(conn_quota)
                except Exception:
                    quota_initial = None
                finally:
                    conn_quota.close()

                init_data = {"status": "ok"}
                if quota_initial is not None:
                    init_data["quota"] = quota_initial
                init_msg = f"event: connected\ndata: {json.dumps(init_data)}\n\n"
                self.wfile.write(init_msg.encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        msg = client_q.get(timeout=15)
                        sse_msg = f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                        self.wfile.write(sse_msg.encode("utf-8"))
                        self.wfile.flush()
                    except sse_stream.queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with sse_stream.SSE_LOCK:
                    sse_stream.SSE_CLIENTS.discard(client_q)
            return

        if path == "/api/quota":
            model_param = qs.get("model", [None])[0]
            cost_param = qs.get("cost", qs.get("billed_cost", [0.0]))[0]
            try:
                cost_val = float(cost_param) if cost_param is not None else 0.0
            except (ValueError, TypeError):
                cost_val = 0.0
            conn = get_db_connection()
            try:
                data = api_routes.query_quota_data(
                    conn, model_name=model_param, billed_cost=cost_val
                )
                serialized = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(serialized)
            finally:
                conn.close()
            return

        # API Routes
        conn = get_db_connection()
        target_tz = api_routes.get_target_tz(client_tz)
        try:
            cache_key = self.path

            def compute():
                if path == "/api/projects":
                    cur = conn.cursor()
                    rows = cur.execute(
                        "SELECT DISTINCT project FROM conversations WHERE project IS NOT NULL AND project != '' ORDER BY project ASC"
                    ).fetchall()
                    return [r["project"] for r in rows]
                elif path == "/api/quota":
                    model_param = qs.get("model", [None])[0]
                    cost_param = qs.get("cost", qs.get("billed_cost", [0.0]))[0]
                    try:
                        cost_val = float(cost_param) if cost_param is not None else 0.0
                    except (ValueError, TypeError):
                        cost_val = 0.0
                    return api_routes.query_quota_data(
                        conn, model_name=model_param, billed_cost=cost_val
                    )
                elif path == "/api/summary":
                    return api_routes.query_summary_data(conn, target_tz, project_param)
                elif path == "/api/timeline":
                    days = qs.get("days", [None])[0]
                    return api_routes.query_timeline_data(
                        conn, days, target_tz, project_param
                    )
                elif path == "/api/cost-by-project":
                    return api_routes.query_cost_by_project(
                        conn, target_tz, project_param
                    )
                elif path == "/api/tools":
                    return api_routes.query_tools_data(conn, project_param)
                elif path == "/api/mcp":
                    return api_routes.query_mcp_data(conn, project_param)
                elif path == "/api/hot-files":
                    return api_routes.query_hot_files_data(conn, project_param)
                elif path == "/api/activity-matrix":
                    return api_routes.query_activity_matrix(
                        conn, target_tz, project_param
                    )
                elif path == "/api/conversations":
                    limit = int(qs.get("limit", [200])[0])
                    view_mode = qs.get("view", qs.get("mode", ["hierarchy"]))[0]
                    return api_routes.query_conversations_list(
                        conn, limit, target_tz, project_param, view_mode
                    )
                elif path.startswith("/api/session/") or (
                    path.startswith("/api/conversations/")
                    and path.rstrip("/") != "/api/conversations"
                ):
                    session_id = path.rstrip("/").split("/")[-1]
                    session_data = api_routes.query_session_details(
                        conn, session_id, target_tz
                    )
                    if session_data is None:
                        return ("error", 404, "Session not found")
                    return session_data
                elif path == "/api/turn-diff":
                    conv_id = qs.get("conv_id", qs.get("cid", [None]))[0]
                    step_a_param = qs.get("a", qs.get("step_a", [None]))[0]
                    step_b_param = qs.get("b", qs.get("step_b", [None]))[0]
                    cumulative_param = qs.get("cumulative", ["true"])[0].lower()
                    cumulative = cumulative_param in ("true", "1", "yes")

                    if not conv_id or step_a_param is None or step_b_param is None:
                        return ("error", 400, "Missing required params: conv_id, a, b")
                    try:
                        step_a = int(step_a_param)
                        step_b = int(step_b_param)
                    except ValueError:
                        return (
                            "error",
                            400,
                            "Invalid step parameters: a and b must be integers",
                        )

                    data = api_routes.query_turn_diff(
                        conn, conv_id, step_a, step_b, cumulative, target_tz
                    )
                    if data is None:
                        return ("error", 404, "Conversation not found")
                    return data
                else:
                    return ("error", 404, "Not found")

            res = get_cached_api_response(conn, cache_key, compute)
            if isinstance(res, tuple) and res[0] == "error":
                self.send_error(res[1], res[2])
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(res)
        except Exception as e:
            self.send_error(500, str(e))
        finally:
            conn.close()

    def log_message(self, format, *args):
        # Suppress logging
        pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        cls, err, tb = sys.exc_info()
        if cls is BrokenPipeError or cls is ConnectionResetError:
            pass
        else:
            super().handle_error(request, client_address)


def export_standalone_html(
    output_path: Path, include_conversations: bool = False, tz: str = None
):
    init_db()
    conn = get_db_connection()
    target_tz = api_routes.get_target_tz(tz)
    try:
        summary = api_routes.query_summary_data(conn, target_tz)
        timeline = api_routes.query_timeline_data(conn, None, target_tz)
        cost_by_project = api_routes.query_cost_by_project(conn, target_tz)
        tools = api_routes.query_tools_data(conn)
        mcp = api_routes.query_mcp_data(conn)
        hot_files = api_routes.query_hot_files_data(conn)
        activity_matrix = api_routes.query_activity_matrix(conn, target_tz)
        conversations = (
            api_routes.query_conversations_list(conn, 200, target_tz)
            if include_conversations
            else []
        )
        quota = api_routes.query_quota_data(conn)
    finally:
        conn.close()

    embedded_js = f"""
    <script>
      const IS_STATIC_EXPORT = true;
      const STATIC_DATA = {{
        summary: {json.dumps(summary)},
        timeline: {json.dumps(timeline)},
        cost_by_project: {json.dumps(cost_by_project)},
        tools: {json.dumps(tools)},
        mcp: {json.dumps(mcp)},
        hot_files: {json.dumps(hot_files)},
        activity_matrix: {json.dumps(activity_matrix)},
        conversations: {json.dumps(conversations)},
        quota: {json.dumps(quota)}
      }};
      
      window.fetch = async function(url) {{
        if (url.includes('/api/summary')) return {{ json: async () => STATIC_DATA.summary }};
        if (url.includes('/api/quota')) return {{ json: async () => STATIC_DATA.quota }};
        if (url.includes('/api/timeline')) return {{ json: async () => STATIC_DATA.timeline }};
        if (url.includes('/api/cost-by-project')) return {{ json: async () => STATIC_DATA.cost_by_project }};
        if (url.includes('/api/tools')) return {{ json: async () => STATIC_DATA.tools }};
        if (url.includes('/api/mcp')) return {{ json: async () => STATIC_DATA.mcp }};
        if (url.includes('/api/hot-files')) return {{ json: async () => STATIC_DATA.hot_files }};
        if (url.includes('/api/activity-matrix')) return {{ json: async () => STATIC_DATA.activity_matrix }};
        if (url.includes('/api/conversations')) return {{ json: async () => STATIC_DATA.conversations }};
        if (url.includes('/api/projects')) return {{ json: async () => [] }};
        return {{ json: async () => ({{}}) }};
      }};
    </script>
    """

    with open(DASHBOARD_DIR / "static" / "style.css", "r") as f:
        inline_css = f"<style>{f.read()}</style>"

    with open(DASHBOARD_DIR / "static" / "script.js", "r") as f:
        inline_js = f"<script>{f.read()}</script>"

    template = env.get_template("index.html")
    gen_time_str = datetime.now(target_tz).strftime("%b %d, %Y %H:%M %Z")

    html_content = template.render(
        IS_STATIC_EXPORT=True,
        EMBEDDED_JS=embedded_js,
        INLINE_CSS=inline_css,
        INLINE_JS=inline_js,
        GEN_TIME_STR=gen_time_str,
        INCLUDE_CONVERSATIONS=include_conversations,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Exported static dashboard to {output_path.absolute()}")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Analytics Dashboard")
    parser.add_argument(
        "--port", type=int, default=8765, help="Port to run the dashboard server on"
    )
    parser.add_argument(
        "--host", type=str, default="localhost", help="Host to bind the server to"
    )
    parser.add_argument(
        "--export-html",
        type=str,
        help="Export a standalone offline HTML report to this path",
    )
    parser.add_argument(
        "--include-conversations",
        action="store_true",
        help="Include full conversation explorer in exported HTML",
    )
    parser.add_argument(
        "--tz",
        type=str,
        help="Target timezone (e.g. 'America/New_York') for exported report. Defaults to system local.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Automatically open the dashboard in the default web browser",
    )
    args = parser.parse_args()

    if args.export_html:
        export_standalone_html(
            Path(args.export_html), args.include_conversations, args.tz
        )
        sys.exit(0)

    try:
        from context_monitor.db import DEFAULT_DB_PATH, init_db

        db_path = DEFAULT_DB_PATH
        init_db(db_path)
    except ImportError:
        try:
            from db import DEFAULT_DB_PATH, init_db

            db_path = DEFAULT_DB_PATH
            init_db(db_path)
        except ImportError:
            db_path = Path(os.path.expanduser("~/.gemini/antigravity/analytics.db"))

    watcher = sse_stream.DbChangeWatcher(db_path)
    watcher.start()

    server_address = (args.host, args.port)
    try:
        httpd = DashboardServer(server_address, DashboardHTTPHandler)
        url = f"http://{args.host}:{args.port}"
        print(f"Antigravity Analytics Live Dashboard: {url}")
        if args.open:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server...")
    except OSError as e:
        if e.errno == 98 or e.errno == 48:
            print(f"Error: Port {args.port} is already in use.")
        else:
            raise


if __name__ == "__main__":
    main()
