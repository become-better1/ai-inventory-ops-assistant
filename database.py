"""Versioned SQLite initialization and explicit, backed-up offline migrations."""
import argparse
from contextlib import closing
import hashlib
from functools import lru_cache
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from file_lock import file_lock, lock_path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / 'data' / 'personal.sqlite3'
CURRENT_VERSION = 2
# Immutable migration identities; historical files must never be regenerated in place.
MIGRATIONS = [(1, 'initial_schema', 'schema.sql', '02d083ae13fb88f3b5a0463f0aadecd794d172d572f6aa2ebc19e2bbdd338ee4'), (2, 'behavior_routines', 'migrations/002_behavior_routines.sql', 'a43e923a31f05b8976089b60c38457a9c8c9cd2bef3a763b4e0d35b74653e596')]


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def migration_sql(entry):
    version, name, filename, expected = entry
    sql = (ROOT / filename).read_text(encoding='utf-8')
    if hashlib.sha256(sql.encode()).hexdigest() != expected:
        raise ValueError(f'Migration {version} file checksum mismatch')
    return sql


def execute_statements(conn, sql):
    """Avoid executescript's implicit COMMIT, keeping DDL inside caller's transaction."""
    pending = ''
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ''
    if pending.strip():
        raise ValueError('Incomplete migration SQL')


@lru_cache(maxsize=4)
def expected_objects(version, registry):
    with closing(sqlite3.connect(':memory:')) as conn:
        for entry in registry[:version]:
            execute_statements(conn, migration_sql(entry))
        return {r[1]: tuple(r) for r in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def validate_schema(conn, *, require_current=False):
    rows = conn.execute('SELECT version,name,checksum FROM schema_migrations ORDER BY version').fetchall()
    if not rows or len(rows) > CURRENT_VERSION:
        raise ValueError('Unsupported database schema version')
    for row, entry in zip(rows, MIGRATIONS):
        migration_sql(entry)
        if tuple(row) != (entry[0], entry[1], entry[3]):
            raise ValueError('Database schema version/checksum mismatch')
    version = rows[-1][0]
    if version != len(rows):
        raise ValueError('Incomplete migration history')
    if require_current and version != CURRENT_VERSION:
        raise ValueError('Database requires offline migration: database.py migrate --db PATH')
    required = {'items','purchase_records','item_events','consumption_rules','rooms',
                'operations','operation_changes','schema_migrations'}
    if version >= 2:
        required |= {'routine_flows','routine_stages','behavior_rules'}
    actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= actual:
        raise ValueError('Incomplete database schema')
    actual_objects = {r[1]: tuple(r) for r in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    for name, definition in expected_objects(version, tuple(MIGRATIONS)).items():
        if actual_objects.get(name) != definition:
            raise ValueError('Database schema definition mismatch: ' + name)
    return dict(schema_version=version, schema_checksum=rows[-1][2])


def check_integrity(conn):
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('Database integrity check failed')
    if conn.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Database foreign key check failed')


def apply_pending(conn, version):
    for entry in MIGRATIONS[version:]:
        execute_statements(conn, migration_sql(entry))
        conn.execute('INSERT INTO schema_migrations VALUES (?,?,?,?)',
                     (entry[0], entry[1], entry[3], now()))
    validate_schema(conn, require_current=True)
    check_integrity(conn)


def initialize(path=DEFAULT_DB):
    """Create a new v2 DB; never silently upgrade an existing v1 database."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path(path, 'write'), timeout=5), closing(connect(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if existing:
                validate_schema(conn, require_current=True)
            else:
                apply_pending(conn, 0)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return path


def migrate(path, *, backup_directory=None):
    """Explicit offline upgrade, preserving a verified v1 backup before any DDL."""
    from maintenance import snapshot, verify_backup
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Database file does not exist; initialize a new database instead')
    directory = Path(backup_directory) if backup_directory else path.parent / 'backups'
    with file_lock(lock_path(path, 'service')), file_lock(lock_path(path, 'write'), timeout=5):
        with closing(connect(path)) as conn:
            metadata = validate_schema(conn)
            check_integrity(conn)
            if metadata['schema_version'] == CURRENT_VERSION:
                return dict(**metadata, migrated=False, backup=None)
        saved = snapshot(path, directory, prefix='before-migrate')
        verify_backup(saved['backup'])
        with closing(connect(path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                metadata = validate_schema(conn)
                apply_pending(conn, metadata['schema_version'])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return dict(schema_version=CURRENT_VERSION, migrated=True, backup=saved['backup'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?', choices=['initialize','migrate'], default='initialize')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--backup-directory', type=Path)
    args = parser.parse_args()
    try:
        print(migrate(args.db, backup_directory=args.backup_directory) if args.command == 'migrate'
              else initialize(args.db))
    except (ValueError, sqlite3.Error, OSError) as error:
        parser.exit(1, 'Operation refused/failed: ' + str(error) + '\n')
