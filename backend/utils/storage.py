import json
import sqlite3
from pathlib import Path


class MeetingStorage:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    project TEXT NOT NULL,
                    meeting_date TEXT NOT NULL,
                    uploaded_at INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    insights_json TEXT
                )
                """
            )

            # Lightweight migration for older DBs without insights_json.
            columns = [row[1] for row in conn.execute("PRAGMA table_info(meetings)").fetchall()]
            if "insights_json" not in columns:
                conn.execute("ALTER TABLE meetings ADD COLUMN insights_json TEXT")
            conn.commit()

    def save_meeting(self, meeting_record):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO meetings (
                    meeting_id,
                    filename,
                    project,
                    meeting_date,
                    uploaded_at,
                    metadata_json,
                    insights_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_record["meeting_id"],
                    meeting_record["filename"],
                    meeting_record["project"],
                    meeting_record["meeting_date"],
                    meeting_record["uploaded_at"],
                    json.dumps(meeting_record["metadata"]),
                    json.dumps(meeting_record.get("insights", {})),
                ),
            )
            conn.commit()

    def update_meeting_insights(self, meeting_id, insights):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE meetings
                SET insights_json = ?
                WHERE meeting_id = ?
                """,
                (json.dumps(insights or {}), meeting_id),
            )
            conn.commit()

    def list_meetings(self, project=None, meeting_date=None):
        query = (
            "SELECT meeting_id, filename, project, meeting_date, uploaded_at, metadata_json, insights_json "
            "FROM meetings"
        )
        conditions = []
        params = []

        if project:
            conditions.append("project = ?")
            params.append(project)

        if meeting_date:
            conditions.append("meeting_date = ?")
            params.append(meeting_date)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY uploaded_at DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        records = []
        for row in rows:
            records.append(
                {
                    "meeting_id": row[0],
                    "filename": row[1],
                    "project": row[2],
                    "meeting_date": row[3],
                    "uploaded_at": row[4],
                    "metadata": json.loads(row[5]),
                    "insights": json.loads(row[6]) if row[6] else {},
                }
            )

        return records

    def dashboard_summary(self):
        ordered = self.list_meetings()
        total_meetings = len(ordered)
        total_projects = len({item.get("project") for item in ordered if item.get("project")})
        total_words = sum(int((item.get("metadata") or {}).get("word_count") or 0) for item in ordered)

        total_action_items = 0
        for item in ordered:
            insights = item.get("insights") or {}
            total_action_items += len(insights.get("action_items") or [])

        return {
            "total_meetings": total_meetings,
            "total_projects": total_projects,
            "total_words": total_words,
            "total_action_items": total_action_items,
            "recent_meetings": ordered[:10],
        }
