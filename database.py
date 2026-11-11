"""Local initialization only; no network listener or external dependencies."""
import argparse
import hashlib
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'data' / 'personal.sqlite3'

def now():
    return datetime.now(timezone.utc).isoformat()

def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn

def initialize(path=DEFAULT_DB):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = (ROOT / 'schema.sql').read_text(encoding='utf-8')
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    conn = connect(path)
    try:
        existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if existing:
            row = conn.execute('SELECT * FROM schema_migrations ORDER BY version DESC LIMIT 1').fetchone()
            if not row or row['version'] != 1 or row['checksum'] != checksum:
                raise RuntimeError('Database schema version/checksum mismatch; existing data was not changed.')
            return path
        # executescript opens the explicit transaction; DDL and migration record commit together.
        conn.executescript('BEGIN IMMEDIATE;\n' + sql)
        conn.execute('INSERT INTO schema_migrations VALUES (?,?,?,?)',
                     (1, 'initial_schema', checksum, now()))
        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Integrity check failed')
        return path
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    print('Initialized/verified:', initialize(args.db))

