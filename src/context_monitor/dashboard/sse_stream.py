import queue
import threading
from datetime import datetime
from pathlib import Path

__all__ = [
    "broadcast_sse",
    "register_change_callback",
    "unregister_change_callback",
    "queue",
    "SSE_CLIENTS",
    "SSE_LOCK",
    "DbChangeWatcher",
]

SSE_CLIENTS = set()
SSE_LOCK = threading.Lock()
_CHANGE_CALLBACKS = []


def register_change_callback(callback):
    """Register a callback to be called when db changes occur."""
    with SSE_LOCK:
        if callback not in _CHANGE_CALLBACKS:
            _CHANGE_CALLBACKS.append(callback)


def unregister_change_callback(callback):
    with SSE_LOCK:
        if callback in _CHANGE_CALLBACKS:
            _CHANGE_CALLBACKS.remove(callback)


def broadcast_sse(event_name: str, data: dict):
    if event_name == "db_update":
        try:
            from context_monitor.dashboard.server import clear_api_cache

            clear_api_cache()
        except Exception:
            pass

        try:
            from context_monitor.quota import invalidate_activity_profile_cache

            invalidate_activity_profile_cache()
        except Exception:
            pass

        if isinstance(data, dict) and "quota" not in data:
            try:
                from context_monitor.db import get_db_connection
                from context_monitor.dashboard import api_routes

                with get_db_connection() as conn:
                    data["quota"] = api_routes.query_quota_data(conn)
            except Exception:
                pass

        with SSE_LOCK:
            callbacks = list(_CHANGE_CALLBACKS)
        for cb in callbacks:
            try:
                cb()
            except Exception:
                pass

    with SSE_LOCK:
        dead = set()
        for q in SSE_CLIENTS:
            try:
                q.put_nowait({"event": event_name, "data": data})
            except Exception:
                dead.add(q)
        SSE_CLIENTS.difference_update(dead)


class DbChangeWatcher(threading.Thread):
    def __init__(self, db_path: Path | str, check_interval: float = 0.5):
        super().__init__(daemon=True)
        self.db_path = Path(db_path)
        self.wal_path = Path(str(db_path) + "-wal")
        self.check_interval = check_interval
        self._stop_event = threading.Event()
        self.last_state = self._get_state()

    def stop(self):
        self._stop_event.set()

    def _get_state(self):
        try:
            st = self.db_path.stat()
            db_mtime, db_size = st.st_mtime, st.st_size
        except OSError:
            db_mtime, db_size = 0, 0
        try:
            st_wal = self.wal_path.stat()
            wal_mtime, wal_size = st_wal.st_mtime, st_wal.st_size
        except OSError:
            wal_mtime, wal_size = 0, 0
        return (db_mtime, db_size, wal_mtime, wal_size)

    def run(self):
        while not self._stop_event.is_set():
            if self._stop_event.wait(self.check_interval):
                break
            try:
                cur_state = self._get_state()
                if cur_state != self.last_state:
                    self.last_state = cur_state
                    if self._stop_event.wait(0.3):
                        break
                    self.last_state = self._get_state()
                    broadcast_sse(
                        "db_update", {"timestamp": datetime.now().isoformat()}
                    )
            except Exception:
                pass
