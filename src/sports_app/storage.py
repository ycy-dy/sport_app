"""Isolated SQLite storage; never derives paths from the foot project."""
from pathlib import Path
import sqlite3

FOOT_ROOT = Path("/Users/ycy/ycy/foot").resolve()


def reject_link_chain(value):
    """Inspect lexical ancestors before resolve or any filesystem mutation."""
    import stat
    path=Path(value).absolute()
    if '..' in path.parts:raise ValueError('parent traversal not allowed')
    for candidate in (path,*path.parents):
        try:info=candidate.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode):raise ValueError('symlink entry not allowed')
    return path


def safe_data_dir(data_dir: str | Path, *, app_root: str | Path) -> Path:
    """Allow only an explicit app-owned data subtree; reject foot and escapes."""
    raw_root=reject_link_chain(app_root)
    raw_path=reject_link_chain(data_dir)
    root = raw_root.resolve()
    path = raw_path.resolve()
    if path == FOOT_ROOT or FOOT_ROOT in path.parents:
        raise ValueError("存储目录不得指向原项目或其子目录")
    if path != root and root not in path.parents:
        raise ValueError("存储目录必须位于新App根目录内")
    return path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL, role TEXT NOT NULL,
 event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
 publication_id TEXT PRIMARY KEY, slot_key TEXT NOT NULL, selection_hash TEXT NOT NULL,
 manifest_hash TEXT NOT NULL, status TEXT NOT NULL, original_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_publication_slot
 ON publications(slot_key) WHERE status IN ('published','frozen');
CREATE TABLE IF NOT EXISTS publication_drafts (
 draft_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_reviews (
 review_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 reviewer_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_requests (
 request_id TEXT PRIMARY KEY, command_hash TEXT NOT NULL, publication_id TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS publication_original_immutable
 BEFORE UPDATE OF publication_id,slot_key,selection_hash,manifest_hash,original_json,created_at
 ON publications BEGIN SELECT RAISE(ABORT,'immutable original'); END;
CREATE TRIGGER IF NOT EXISTS publication_no_delete
 BEFORE DELETE ON publications BEGIN SELECT RAISE(ABORT,'immutable history'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
 BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
 BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS review_no_update BEFORE UPDATE ON publication_reviews
 BEGIN SELECT RAISE(ABORT,'immutable review'); END;
CREATE TRIGGER IF NOT EXISTS review_no_delete BEFORE DELETE ON publication_reviews
 BEGIN SELECT RAISE(ABORT,'immutable review'); END;
"""

class SQLiteStore:
    def __init__(self, data_dir: str | Path, *, app_root: str | Path):
        self.data_dir = safe_data_dir(data_dir, app_root=app_root)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data_dir / "app.sqlite3"
        if not self.path.exists():
            import os
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
            os.close(fd)
        with self.connect() as db:
            existing = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='publications'").fetchone()
            if existing and 'unique(slot_key,status)' in ''.join(existing[0].lower().split()):
                raise RuntimeError("旧发布表约束需经Chief显式迁移；拒绝自动改写历史")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def record_event(self, actor_id: str, role: str, event: str, payload: str, created_at: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO audit_events(actor_id,role,event,payload,created_at) VALUES(?,?,?,?,?)",
                       (actor_id, role, event, payload, created_at))
