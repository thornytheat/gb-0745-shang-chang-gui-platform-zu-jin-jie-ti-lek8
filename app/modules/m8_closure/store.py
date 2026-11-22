import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class ClosureStore:
    """Immutable run payloads plus a per-notice version chain.

    Only one version of a notice is published at a time. A new ledger for the
    same notice supersedes the previous version instead of stacking on top of
    it, so the fixed pool is never paid twice.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS closure_runs ("
                "id TEXT PRIMARY KEY, notice TEXT NOT NULL, version INTEGER NOT NULL,"
                "status TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS closure_runs_notice "
                "ON closure_runs(notice, version)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS closure_runs_published "
                "ON closure_runs(notice) WHERE status = 'published'"
            )

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.isolation_level = None
        return connection

    def get(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT payload, version, status FROM closure_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["版本"], payload["状态"] = row[1], row[2]
        return payload

    def current(self, notice_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT payload, version, status FROM closure_runs "
                "WHERE notice=? AND status='published' ORDER BY version DESC LIMIT 1",
                (notice_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["版本"], payload["状态"] = row[1], row[2]
        return payload

    def versions(self, notice_id):
        with closing(self.connect()) as db:
            return [
                {"批次": row[0], "版本": row[1], "状态": row[2], "生成时间": row[3]}
                for row in db.execute(
                    "SELECT id, version, status, created FROM closure_runs "
                    "WHERE notice=? ORDER BY version DESC",
                    (notice_id,),
                )
            ]

    def history(self):
        with closing(self.connect()) as db:
            return [
                {
                    **json.loads(row[0])["运行清单"],
                    "版本": row[1],
                    "状态": row[2],
                }
                for row in db.execute(
                    "SELECT payload, version, status FROM closure_runs "
                    "ORDER BY rowid DESC LIMIT 40"
                )
            ]

    def commit(self, notice_id, run_id, payload):
        """Store a new published version, superseding the prior one atomically.

        Returns (payload, reused). If the exact run id already exists the stored
        payload is returned untouched, so re-running the same notice adds nothing.
        """
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT payload, version, status FROM closure_runs WHERE id=?", (run_id,)
                ).fetchone()
                if existing is not None:
                    db.execute("COMMIT")
                    payload = json.loads(existing[0])
                    payload["版本"], payload["状态"] = existing[1], existing[2]
                    return payload, True
                row = db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM closure_runs WHERE notice=?",
                    (notice_id,),
                ).fetchone()
                version = row[0] + 1
                db.execute(
                    "UPDATE closure_runs SET status='superseded' "
                    "WHERE notice=? AND status='published'",
                    (notice_id,),
                )
                stored = {**payload, "版本": version, "状态": "published"}
                db.execute(
                    "INSERT INTO closure_runs VALUES (?, ?, ?, 'published', ?, ?)",
                    (
                        run_id,
                        notice_id,
                        version,
                        created,
                        json.dumps(stored, ensure_ascii=False),
                    ),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return stored, False
