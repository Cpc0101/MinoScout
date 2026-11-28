"""Account-isolated local asset library and durable registration spool."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path

CHUNK = 1024 * 1024


class AssetTransferError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(path, guard=lambda: None):
    sha = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as file:
        while block := file.read(CHUNK):
            guard()
            sha.update(block)
            size += len(block)
    return sha.hexdigest(), size


class AssetCache:
    def __init__(self, owner, *, root=None, quota_bytes=5*1024**3):
        from mino_scout.config import config_dir
        if not isinstance(owner, str) or not owner:
            raise AssetTransferError("asset_owner_required")
        if type(quota_bytes) is not int or quota_bytes <= 0:
            raise AssetTransferError("asset_cache_quota_invalid")
        self.root = Path(root or config_dir() / "assets") / hashlib.sha256(owner.encode()).hexdigest()
        self.quota = quota_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("library", "pending", "scratch"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self, guard=lambda: None):
        lock = (self.root / "cache.lock").open("a+b")
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        try:
            while True:
                guard()
                lock.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()

    @staticmethod
    def _scratch_lock(path):
        handle = path.open("a+b")
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
            return None

    def scratch(self, guard=lambda: None):
        with self.locked(guard):
            self._reclaim_scratch()
            directory = Path(tempfile.mkdtemp(prefix="inputs-", dir=self.root / "scratch"))
            return directory, self._scratch_lock(directory / "in-use.lock")

    def release_scratch(self, directory, handle):
        with self.locked():
            handle.close()
            shutil.rmtree(directory, ignore_errors=True)

    def _reclaim_scratch(self):
        for directory in (self.root / "scratch").iterdir():
            if not directory.is_dir():
                continue
            handle = self._scratch_lock(directory / "in-use.lock")
            if handle is not None:
                # A dead worker releases its OS lock; no active input copy is evicted.
                handle.close()
                shutil.rmtree(directory)

    def _files(self):
        return [path for folder in ("library", "pending", "scratch") for path in (self.root / folder).rglob("*") if path.is_file()]

    def _used(self):
        seen = set()
        total = 0
        for path in self._files():
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += stat.st_size
        return total

    def _room(self, extra):
        self._reclaim_scratch()
        # The library is the only copy. Never evict it to make room.
        if self._used() + extra > self.quota:
            raise AssetTransferError("asset_cache_quota_exceeded_pending_preserved")

    def _manifest(self, asset):
        filename = asset.get("filename", "")
        if not isinstance(filename, str) or not filename or any(c in filename for c in ("/", "\\", "\x00")) or filename in (".", ".."):
            raise AssetTransferError("asset_manifest_invalid")
        sha, size = asset.get("sha256"), asset.get("size_bytes")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha) or type(size) is not int or size <= 0:
            raise AssetTransferError("asset_manifest_invalid")
        return sha, size

    def _copy_locked(self, asset, destination, guard):
        sha, size = self._manifest(asset)
        target = self.root / "library" / sha
        if not target.is_file():
            raise AssetTransferError("asset_not_on_this_scout")
        if digest(target, guard) != (sha, size):
            raise AssetTransferError("asset_checksum_mismatch")
        destination = Path(destination)
        if destination.is_symlink():
            raise AssetTransferError("asset_case_copy_invalid")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        shutil.copyfile(target, temporary)
        if digest(temporary, guard) != (sha, size):
            temporary.unlink(missing_ok=True)
            raise AssetTransferError("asset_checksum_mismatch")
        temporary.replace(destination)
        return destination

    def fetch(self, asset, destination, guard=lambda: None):
        sha, size = self._manifest(asset)
        with self.locked(guard):
            self._room(size)
            return self._copy_locked(asset, destination, guard), True

    def copy_verified(self, asset, destination, guard=lambda: None):
        """Copy a library file out to a case attempt. The copy is not part of the library quota."""
        self._manifest(asset)
        with self.locked(guard):
            return self._copy_locked(asset, destination, guard), True

    def stage(self, path, manifest, scope, guard=lambda: None):
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise AssetTransferError("asset_source_file_invalid")
        sha, size = digest(path, guard)
        if size <= 0:
            raise AssetTransferError("asset_source_empty")
        md5 = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as file:
            while chunk := file.read(CHUNK):
                guard()
                md5.update(chunk)
        manifest = {**manifest, "sha256": sha, "md5": md5.hexdigest(), "size_bytes": size}
        encoded = json.dumps({"scope": scope, "asset": manifest}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ident = hashlib.sha256(encoded).hexdigest()
        metadata = self.root / "pending" / (ident + ".json")
        body = metadata.with_suffix(".blob")
        with self.locked(guard):
            if metadata.exists() and body.is_file() and digest(body, guard) == (sha, size):
                return ident, manifest
            temp = body.with_suffix(".tmp")
            temp.unlink(missing_ok=True)
            body.unlink(missing_ok=True)
            self._room(size + len(encoded))
            with path.open("rb") as source, temp.open("wb") as output:
                while chunk := source.read(CHUNK):
                    guard()
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest(temp, guard) != (sha, size):
                temp.unlink(missing_ok=True)
                raise AssetTransferError("asset_source_changed")
            temp.replace(body)
            tempmeta = metadata.with_suffix(".tmp")
            with tempmeta.open("wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            tempmeta.replace(metadata)
        return ident, manifest

    def pending(self, scope):
        with self.locked():
            result = []
            for path in (self.root / "pending").glob("*.json"):
                data = json.loads(path.read_text())
                if data.get("scope") == scope:
                    result.append({"id": path.stem, **data})
            return result

    def _ident(self, ident):
        if not isinstance(ident, str) or len(ident) != 64 or any(c not in "0123456789abcdef" for c in ident):
            raise AssetTransferError("asset_spool_id_invalid")

    def commit(self, ident, guard=lambda: None):
        """Move the spooled bytes into the durable library before Nexus records the reference."""
        self._ident(ident)
        with self.locked(guard):
            metadata = self.root / "pending" / (ident + ".json")
            body = metadata.with_suffix(".blob")
            if not metadata.is_file():
                raise AssetTransferError("asset_spool_id_invalid")
            asset = json.loads(metadata.read_text())["asset"]
            sha, size = self._manifest(asset)
            target = self.root / "library" / sha
            if target.is_file() and digest(target, guard) == (sha, size):
                body.unlink(missing_ok=True)
                return target
            if not body.is_file() or digest(body, guard) != (sha, size):
                raise AssetTransferError("asset_spool_checksum_mismatch")
            if target.exists():
                raise AssetTransferError("asset_checksum_mismatch")
            try:
                os.link(body, target)
            except OSError:
                self._room(size)
                temporary = target.with_suffix(".tmp")
                temporary.unlink(missing_ok=True)
                shutil.copyfile(body, temporary)
                if digest(temporary, guard) != (sha, size):
                    temporary.unlink(missing_ok=True)
                    raise AssetTransferError("asset_checksum_mismatch")
                temporary.replace(target)
            if digest(target, guard) != (sha, size):
                target.unlink(missing_ok=True)
                raise AssetTransferError("asset_checksum_mismatch")
            body.unlink(missing_ok=True)
            return target

    def acknowledge(self, ident, guard=lambda: None):
        self._ident(ident)
        with self.locked(guard):
            for suffix in (".json", ".blob"):
                (self.root / "pending" / (ident + suffix)).unlink(missing_ok=True)
