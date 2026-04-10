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
                    metadata_json TEXT NOT NULL
                )
                """
            )
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
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_record["meeting_id"],
                    meeting_record["filename"],
                    meeting_record["project"],
                    meeting_record["meeting_date"],
                    meeting_record["uploaded_at"],
                    json.dumps(meeting_record["metadata"]),
                ),
            )
            conn.commit()

    def list_meetings(self, project=None, meeting_date=None):
        query = (
            "SELECT meeting_id, filename, project, meeting_date, uploaded_at, metadata_json "
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
                }
            )

        return records
