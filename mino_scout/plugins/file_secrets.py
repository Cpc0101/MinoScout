"""Cross-platform private files for node-local plugin secrets.

Values are plaintext JSON protected by user-private permissions. The directory is
created as 0700 and every value/lock file as 0600. Writes use an exclusive
temporary file followed by an atomic replacement. A per-account process lock
serializes updates across Scout helpers.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

_JSON_VERSION = 1
_ROOT_MODE = 0o700
_FILE_MODE = 0o600


def root() -> Path:
    """Return the profile-scoped private storage directory."""
    from mino_scout.plugins.paths import data_dir

    return data_dir() / "plugin-secrets"


def _check_fd(fd: int, *, directory: bool) -> None:
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or (not directory and info.st_nlink != 1):
        raise ValueError("Credential storage ownership or link count is unsafe")
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        raise ValueError("Credential storage type is unsafe")
    expected = _ROOT_MODE if directory else _FILE_MODE
    if stat.S_IMODE(info.st_mode) != expected:
        os.fchmod(fd, expected)
        info = os.fstat(fd)
    if stat.S_IMODE(info.st_mode) != expected:
        raise ValueError("Credential storage permissions must be 0700/0600")


def _ensure_root(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir(mode=_ROOT_MODE)
    except FileExistsError:
        pass
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Credential storage directory is unsafe")
    if os.name == "nt":
        try:
            directory.chmod(_ROOT_MODE)
        except OSError as exc:
            raise ValueError("Credential storage permissions cannot be secured") from exc
        return
    try:
        directory.chmod(_ROOT_MODE)
    except OSError as exc:
        raise ValueError("Credential storage permissions cannot be secured") from exc
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _check_fd(fd, directory=True)
    finally:
        os.close(fd)


@contextmanager
def locked_account(account: str):
    """Serialize access to one account and yield its value entry."""
    directory = root()
    _ensure_root(directory)
    name = hashlib.sha256(str(account).encode("utf-8")).hexdigest()
    if os.name == "nt":
        with _windows_lock(directory, name) as lock_fd:
            try:
                yield Entry(directory, name + ".json", lock_fd=lock_fd)
            finally:
                _windows_unlock(lock_fd)
        return

    import fcntl

    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        _check_fd(directory_fd, directory=True)
        lock_fd = os.open(
            directory / (name + ".lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            _FILE_MODE,
        )
        os.fchmod(lock_fd, _FILE_MODE)
        _check_fd(lock_fd, directory=False)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield Entry(directory, name + ".json", directory_fd=directory_fd)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


@contextmanager
def _windows_lock(directory: Path, name: str):
    import msvcrt

    path = directory / (name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(path, flags, _FILE_MODE)
    try:
        os.chmod(path, _FILE_MODE)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield fd
    finally:
        os.close(fd)


def _windows_unlock(fd: int) -> None:
    import msvcrt

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


class Entry:
    def __init__(self, directory: Path, name: str, *, directory_fd: int | None = None,
                 lock_fd: int | None = None):
        self.directory = directory
        self.name = name
        self.directory_fd = directory_fd
        self.lock_fd = lock_fd

    @property
    def path(self) -> Path:
        return self.directory / self.name

    def read(self) -> tuple[bool, str | None]:
        """Return (exists, value); None is a deletion tombstone."""
        if self.directory_fd is None:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                return False, None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError("Credential storage type is unsafe")
            fd = os.open(self.path, os.O_RDONLY)
        else:
            try:
                fd = os.open(
                    self.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=self.directory_fd,
                )
            except FileNotFoundError:
                return False, None
        try:
            if self.directory_fd is not None:
                _check_fd(fd, directory=False)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                data = json.load(stream)
            if (
                not isinstance(data, dict)
                or data.get("version") != _JSON_VERSION
                or "value" not in data
                or (data["value"] is not None and not isinstance(data["value"], str))
            ):
                raise ValueError("Invalid credential file")
            return True, data["value"]
        finally:
            os.close(fd)

    def write(self, value: str | None) -> None:
        self.read()
        temporary = "." + self.name + "." + uuid.uuid4().hex
        if self.directory_fd is None:
            target = self.path
            temp_path = self.directory / temporary
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
            os.chmod(temp_path, _FILE_MODE)
        else:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _FILE_MODE,
                dir_fd=self.directory_fd,
            )
        if os.name != "nt":
            os.fchmod(fd, _FILE_MODE)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        try:
            with stream:
                json.dump(
                    {"version": _JSON_VERSION, "value": value},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            if self.directory_fd is None:
                os.replace(temp_path, target)
            else:
                os.replace(
                    temporary,
                    self.name,
                    src_dir_fd=self.directory_fd,
                    dst_dir_fd=self.directory_fd,
                )
                os.fsync(self.directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                if self.directory_fd is None:
                    temp_path.unlink()
                else:
                    os.unlink(temporary, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def remove(self) -> None:
        self.read()
        try:
            if self.directory_fd is None:
                self.path.unlink()
            else:
                os.unlink(self.name, dir_fd=self.directory_fd)
                os.fsync(self.directory_fd)
        except FileNotFoundError:
            pass
