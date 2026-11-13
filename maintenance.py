"""Consistent SQLite backups and confirmed offline restore. No credential storage."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from database import DEFAULT_DB,ROOT,now,validate_schema
from file_lock import file_lock,lock_path

def read_only(path):
    path=Path(path).resolve()
    if not path.is_file():
        raise ValueError('Database file does not exist')
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
    c.execute('PRAGMA foreign_keys=ON')
    return c

def inspect_database(path):
    with closing(read_only(path)) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Database integrity check failed')
        if c.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Database has foreign key errors')
        return validate_schema(c)

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()

def snapshot(source,directory,*,prefix='backup'):
    """Online-safe backup API, never a raw copy of a live main file."""
    source=Path(source).resolve()
    if not source.is_file():
        raise ValueError('Database file does not exist')
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination=directory/f'{prefix}-{stamp}-{uuid4().hex[:8]}.sqlite3'
    stage=directory/(destination.name+'.partial')
    try:
        with closing(read_only(source)) as src, closing(sqlite3.connect(stage)) as dst:
            src.backup(dst,pages=256,sleep=0.05)
        metadata=inspect_database(stage)
        metadata.update(created_at=now(),sha256=digest(stage),size_bytes=stage.stat().st_size)
        os.replace(stage,destination)
        manifest=Path(str(destination)+'.manifest.json')
        manifest.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        return dict(backup=str(destination),manifest=str(manifest),**metadata)
    finally:
        if stage.exists():
            stage.unlink()

def verify_backup(source):
    source=Path(source).resolve()
    manifest=Path(str(source)+'.manifest.json')
    if not manifest.is_file():
        raise ValueError('Backup manifest missing; use a backup created by maintenance.py')
    data=json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('sha256')!=digest(source) or data.get('size_bytes')!=source.stat().st_size:
        raise ValueError('Backup checksum mismatch')
    inspect_database(source)
    return data

def restore(source,target=DEFAULT_DB,*,confirmed=False,safety_directory=None):
    if confirmed is not True:
        raise ValueError('Restore requires explicit confirmation; newer target data will be replaced')
    source,target=Path(source).resolve(),Path(target).resolve()
    if source==target:
        raise ValueError('Backup source and restore target must differ')
    verify_backup(source)  # Reject incompatible/corrupt backups before touching the target.
    target.parent.mkdir(parents=True,exist_ok=True)
    safety_directory=Path(safety_directory) if safety_directory else target.parent/'backups'
    with file_lock(lock_path(target,'service')),file_lock(lock_path(target,'write'),timeout=5):
        safety=None
        if target.exists():
            inspect_database(target)
            # Drain WAL only while this application's MCP process is stopped.
            with closing(sqlite3.connect(target,timeout=5)) as c:
                if c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]!=0:
                    raise ValueError('Database still has active transactions')
            if any(Path(str(target)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
                raise ValueError('Database sidecar still in use; close all database tools')
            safety=snapshot(target,safety_directory,prefix='before-restore')['backup']
        stage=target.parent/(target.name+'.restore-'+uuid4().hex)
        try:
            # Restore via SQLite, not by copying a potentially active database file.
            with closing(read_only(source)) as src,closing(sqlite3.connect(stage)) as dst:
                src.backup(dst)
            inspect_database(stage)
            verify_backup(source)
            # No open database connection belongs to this process at replacement time.
            os.replace(stage,target)
        finally:
            if stage.exists():
                stage.unlink()
        return dict(restored=str(target),source=str(source),safety_backup=safety)

def main():
    parser=argparse.ArgumentParser(description='Local SQLite backup and offline recovery')
    sub=parser.add_subparsers(dest='command',required=True)
    backup=sub.add_parser('backup')
    backup.add_argument('--db',type=Path,default=DEFAULT_DB)
    backup.add_argument('--directory',type=Path,default=ROOT/'backups')
    verify=sub.add_parser('verify')
    verify.add_argument('source',type=Path)
    recovery=sub.add_parser('restore')
    recovery.add_argument('source',type=Path)
    recovery.add_argument('--db',type=Path,default=DEFAULT_DB)
    recovery.add_argument('--confirm-replace',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='backup':
            result=snapshot(args.db,args.directory)
        elif args.command=='verify':
            result=verify_backup(args.source)
        else:
            result=restore(args.source,args.db,confirmed=args.confirm_replace)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,sqlite3.Error,OSError,json.JSONDecodeError) as error:
        parser.exit(1,'Operation refused/failed: '+str(error)+'\n')

if __name__=='__main__':
    main()

