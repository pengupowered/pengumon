"""The backup download's temp folder (api/backup.py): where it is and how it is emptied. The API empties it before each
backup; the storage component (db/storage.py) empties it at start, so a crash in the middle of a download does not leave
a copy of the database there until someone asks for another backup."""

import errno
import os
import shutil
import stat
import sys
from pathlib import Path

from pengumon import paths

BACKUP_TEMP_FOLDER = ".backup-tmp"  # in the data directory: the container's root is read-only and /tmp is memory


_WINDOWS = sys.platform == "win32"  # read at each call; tests set it to run the Windows branch on Linux


def backup_temp_dir() -> Path:
    return paths.data_dir() / BACKUP_TEMP_FOLDER


def remove_backup_leftovers(folder: Path) -> None:
    """Empties the backup temp folder of what an earlier crash left (nothing else makes files there). Only while no
    backup is being made: at start, or with api.backup.backup_running held.

    The folder is opened without following a link: a symlink, a non-directory or a folder owned by someone else is
    refused (NotADirectoryError / PermissionError, both OSError), and the mode of an existing folder is set to 0700.
    Entries are removed relative to the opened folder, and links inside it are removed as links, never followed.

    Windows has no O_DIRECTORY, O_NOFOLLOW, owner ids, mode bits or dir_fd: there the folder is looked at with lstat
    (a link, a junction or other reparse point, or a non-directory is refused), nothing checks the owner or sets a
    mode (the profile's ACL protects the data directory, as it does secrets.json), and entries are removed by path,
    links as links."""
    if _WINDOWS:
        _remove_leftovers_windows(folder)
        return
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise NotADirectoryError(errno.ENOTDIR, "The backup temp folder is a link or not a directory", str(folder)) from None
        raise
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise PermissionError(errno.EPERM, "The backup temp folder is owned by someone else", str(folder))
        os.fchmod(fd, 0o700)
        with os.scandir(fd) as entries:
            leftovers = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
        for name, is_dir in leftovers:
            if is_dir:
                shutil.rmtree(name, ignore_errors=True, dir_fd=fd)
            else:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(fd)


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or on Windows a junction or any other reparse point (lstat reports it, whatever it points at)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_leftovers_windows(folder: Path) -> None:
    st = os.lstat(folder)  # lstat does not follow a link
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "The backup temp folder is a link or not a directory", str(folder))
    with os.scandir(folder) as entries:
        leftovers = [(entry.path, _is_link(entry.stat(follow_symlinks=False)), entry.is_dir(follow_symlinks=False))
                     for entry in entries]
    for path, link, is_dir in leftovers:
        if link:
            try:
                os.unlink(path)  # a file symlink
            except FileNotFoundError:
                pass
            except OSError:
                os.rmdir(path)  # a directory symlink or junction: removes the link, never what it points at
        elif is_dir:
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
