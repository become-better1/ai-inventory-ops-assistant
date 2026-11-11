"""Cooperative local locks for offline maintenance and serialized direct API writes."""
from contextlib import contextmanager
import os
from pathlib import Path
import time

@contextmanager
def file_lock(path, *, timeout=0):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    if path.stat().st_size==0:
        handle.write(b'0')
        handle.flush()
    started=time.monotonic()
    locked=False
    try:
        while True:
            try:
                handle.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
                break
            except (OSError,BlockingIOError):
                if time.monotonic()-started>=timeout:
                    raise ValueError('Database is in use; stop its MCP service before maintenance')
                time.sleep(0.025)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
        handle.close()

def lock_path(database,kind):
    return Path(str(Path(database).resolve())+'.'+kind+'.lock')

