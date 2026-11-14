"""Fail-closed POSIX permissions for isolated authentication material.

No chmod of unknown existing files; same-UID hostile processes are out of scope.
"""
import os
from pathlib import Path
import stat


def validate_private_store(store):
    directory=Path(store.data_dir).absolute()
    # Reject symlink ancestors BEFORE resolving. Ancestors may be standard 0755
    # but must be current-user-owned and not group/other writable from workspace.
    workspace=Path(__file__).resolve().parents[2]
    if workspace not in directory.parents and directory!=workspace:
        raise ValueError('authentication store must be inside project')
    current=directory
    while True:
        info=current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid():
            raise ValueError('unsafe authentication directory')
        if current==directory:
            if stat.S_IMODE(info.st_mode)!=0o700:raise ValueError('authentication directory requires 0700')
        elif info.st_mode & 0o022:raise ValueError('writable authentication ancestor')
        if current==workspace:break
        current=current.parent
    for suffix in ('','-wal','-shm','-journal'):
        path=Path(str(store.path)+suffix)
        if not path.exists() and not path.is_symlink():continue
        info=path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600
                or info.st_nlink!=1):raise ValueError('authentication files require private regular 0600')


class PrivateAuthStore:
    def __init__(self,store):
        validate_private_store(store)
        self._store=store;self.data_dir=store.data_dir;self.path=store.path

    def connect(self):
        validate_private_store(self._store)
        db=self._store.connect()
        try:validate_private_store(self._store)
        except Exception:
            db.close();raise
        return db
