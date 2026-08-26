#!/usr/bin/env -S uv run --no-project
# /// script
# requires-python = ">=3.11"
# dependencies = ["jinja2"]
# ///
import os
import sys
import json
import argparse
import webbrowser
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from jinja2 import Environment, FileSystemLoader

import sys
from pathlib import Path
_src = Path(__file__).resolve().parent.parent.parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from context_monitor.db import get_db_connection
from context_monitor.dashboard import api_routes, sse_stream

DASHBOARD_DIR = Path(__file__).resolve().parent
env = Environment(loader=FileSystemLoader(DASHBOARD_DIR / 'templates'))

class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        client_tz = qs.get("tz", [None])[0]

        if path == "/" or path == "/index.html":
            template = env.get_template('index.html')
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

        if path == "/api/events":
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
                init_msg = f"event: connected\ndata: {json.dumps({'status': 'ok'})}\n\n"
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

        # API Routes
        conn = get_db_connection()
        target_tz = api_routes.get_target_tz(client_tz)
        try:
            if path == "/api/summary":
                data = api_routes.query_summary_data(conn, target_tz)
            elif path == "/api/timeline":
                days = int(qs.get("days", [7])[0])
                data = api_routes.query_timeline_data(conn, days, target_tz)
            elif path == "/api/tools":
                data = api_routes.query_tools_data(conn)
            elif path == "/api/mcp":
                data = api_routes.query_mcp_data(conn)
            elif path == "/api/hot-files":
                data = api_routes.query_hot_files_data(conn)
            elif path == "/api/activity-matrix":
                data = api_routes.query_activity_matrix(conn, target_tz)
            elif path == "/api/conversations":
                limit = int(qs.get("limit", [200])[0])
                data = api_routes.query_conversations_list(conn, limit, target_tz)
            elif path.startswith("/api/session/"):
                session_id = path.split("/")[-1]
                data = api_routes.query_session_details(conn, session_id, target_tz)
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
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

def export_standalone_html(output_path: Path, include_conversations: bool = False, tz: str = None):
    conn = get_db_connection()
    target_tz = api_routes.get_target_tz(tz)
    try:
        summary = api_routes.query_summary_data(conn, target_tz)
        timeline = api_routes.query_timeline_data(conn, None, target_tz)
        tools = api_routes.query_tools_data(conn)
        mcp = api_routes.query_mcp_data(conn)
        hot_files = api_routes.query_hot_files_data(conn)
        activity_matrix = api_routes.query_activity_matrix(conn, target_tz)
        conversations = api_routes.query_conversations_list(conn, 200, target_tz) if include_conversations else []
    finally:
        conn.close()

    embedded_js = f"""
    <script>
      const IS_STATIC_EXPORT = true;
      const STATIC_DATA = {{
        summary: {json.dumps(summary)},
        timeline: {json.dumps(timeline)},
        tools: {json.dumps(tools)},
        mcp: {json.dumps(mcp)},
        hot_files: {json.dumps(hot_files)},
        activity_matrix: {json.dumps(activity_matrix)},
        conversations: {json.dumps(conversations)}
      }};
      
      window.fetch = async function(url) {{
        if (url.includes('/api/summary')) return {{ json: async () => STATIC_DATA.summary }};
        if (url.includes('/api/timeline')) return {{ json: async () => STATIC_DATA.timeline }};
        if (url.includes('/api/tools')) return {{ json: async () => STATIC_DATA.tools }};
        if (url.includes('/api/mcp')) return {{ json: async () => STATIC_DATA.mcp }};
        if (url.includes('/api/hot-files')) return {{ json: async () => STATIC_DATA.hot_files }};
        if (url.includes('/api/activity-matrix')) return {{ json: async () => STATIC_DATA.activity_matrix }};
        if (url.includes('/api/conversations')) return {{ json: async () => STATIC_DATA.conversations }};
        return {{ json: async () => ({{}}) }};
      }};
    </script>
    """
    
    with open(DASHBOARD_DIR / "static" / "style.css", "r") as f:
        inline_css = f"<style>{f.read()}</style>"
    
    with open(DASHBOARD_DIR / "static" / "script.js", "r") as f:
        inline_js = f"<script>{f.read()}</script>"

    template = env.get_template('index.html')
    gen_time_str = datetime.now(target_tz).strftime('%b %d, %Y %H:%M %Z')

    html_content = template.render(
        IS_STATIC_EXPORT=True,
        EMBEDDED_JS=embedded_js,
        INLINE_CSS=inline_css,
        INLINE_JS=inline_js,
        GEN_TIME_STR=gen_time_str,
        INCLUDE_CONVERSATIONS=include_conversations
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Exported static dashboard to {output_path.absolute()}")

def main():
    parser = argparse.ArgumentParser(description="Antigravity Analytics Dashboard")
    parser.add_argument("--port", type=int, default=8765, help="Port to run the dashboard server on")
    parser.add_argument("--host", type=str, default="localhost", help="Host to bind the server to")
    parser.add_argument("--export-html", type=str, help="Export a standalone offline HTML report to this path")
    parser.add_argument("--include-conversations", action="store_true", help="Include full conversation explorer in exported HTML")
    parser.add_argument("--tz", type=str, help="Target timezone (e.g. 'America/New_York') for exported report. Defaults to system local.")
    parser.add_argument("--open", action="store_true", help="Automatically open the dashboard in the default web browser")
    args = parser.parse_args()

    if args.export_html:
        export_standalone_html(Path(args.export_html), args.include_conversations, args.tz)
        sys.exit(0)

    try:
        from db import DEFAULT_DB_PATH
        db_path = DEFAULT_DB_PATH
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
