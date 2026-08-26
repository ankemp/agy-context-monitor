import time
import queue
import threading
from datetime import datetime
from pathlib import Path

SSE_CLIENTS = set()
SSE_LOCK = threading.Lock()

def broadcast_sse(event_name: str, data: dict):
    with SSE_LOCK:
        dead = set()
        for q in SSE_CLIENTS:
            try:
                q.put_nowait({"event": event_name, "data": data})
            except Exception:
                dead.add(q)
        SSE_CLIENTS.difference_update(dead)

class DbChangeWatcher(threading.Thread):
    def __init__(self, db_path: Path, check_interval: float = 0.5):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.wal_path = Path(str(db_path) + "-wal")
        self.check_interval = check_interval
        self.last_state = self._get_state()

    def _get_state(self):
        db_mtime = self.db_path.stat().st_mtime if self.db_path.exists() else 0
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        wal_mtime = self.wal_path.stat().st_mtime if self.wal_path.exists() else 0
        wal_size = self.wal_path.stat().st_size if self.wal_path.exists() else 0
        return (db_mtime, db_size, wal_mtime, wal_size)

    def run(self):
        while True:
            time.sleep(self.check_interval)
            try:
                cur_state = self._get_state()
                if cur_state != self.last_state:
                    self.last_state = cur_state
                    time.sleep(0.3)
                    self.last_state = self._get_state()
                    broadcast_sse("db_update", {"timestamp": datetime.now().isoformat()})
            except Exception:
                pass
