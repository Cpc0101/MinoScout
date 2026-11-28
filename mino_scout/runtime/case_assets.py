"""Three file states: this task's output, the reusable library, and this case's input copy."""
from __future__ import annotations

import re
from pathlib import Path
from mino_scout.runtime.asset_cache import AssetCache, AssetTransferError

_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class CaseAssets:
    def __init__(self, case, owner):
        self.case, self.owner = case, owner
        self._cache = None
        self._scratch = None

    def _store(self):
        if self._cache is None:
            from mino_scout.config import load_config
            self._cache = AssetCache(self.owner, quota_bytes=load_config().get("asset_cache_bytes", 5*1024**3))
        return self._cache

    def close(self):
        if self._scratch is not None:
            self._store().release_scratch(*self._scratch)
            self._scratch = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass  # OS locks release on process exit; the next worker reclaims only dead scratch.

    def _call(self, op, params):
        self.case._guard()
        from mino_scout.server_comm.nexus_link import call
        remaining = 30
        if self.case.deadline:
            import time
            remaining = max(0.1, min(remaining, self.case.deadline - time.monotonic()))
        return call(self.case, "asset." + op, params, remaining)

    def _error(self, exc):
        from mino_scout.runtime.script_exec import ScriptAbort
        message = {
            "asset_not_task_output": "生成文件必须写在本次任务的 output_dir",
            "asset_case_input_not_publishable": "用例执行副本不能再发布成资产",
            "asset_not_on_this_scout": "本机库没有这份以前生成的文件",
        }.get(exc.code, "测试资产尚未登记；本机库里未确认的文件仍保留")
        raise ScriptAbort(message, reason_code=exc.code, status="blocked") from None

    def _record(self, event, data):
        # Transfer tickets never enter execution evidence or runtime outputs.
        self.case.data._record("asset/" + event, data)

    def find(self, kind, *, attributes=None, limit=50):
        return self._call("list", {"kind": kind, "attributes": attributes or {}, "limit": limit})

    def _output_root(self):
        root = Path(self.case.output_dir).resolve()
        if not root.is_dir() or root.is_symlink():
            raise AssetTransferError("asset_output_unavailable")
        return root

    def _generated_file(self, path):
        """A file this task just wrote. Previous library files and case input copies are not sources."""
        root = self._output_root()
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise AssetTransferError("asset_source_file_invalid")
        source = source.resolve()
        if not source.is_relative_to(root):
            raise AssetTransferError("asset_not_task_output")
        if ".assets" in source.relative_to(root).parts:
            raise AssetTransferError("asset_case_input_not_publishable")
        return source

    def _case_copy(self, asset_id, version, filename):
        """Where this case execution reads its own copy. Not the library and not the generator task."""
        if not isinstance(filename, str) or not filename or any(c in filename for c in ("/", "\\", "\x00")) or filename in (".", ".."):
            raise AssetTransferError("asset_manifest_invalid")
        if not _SEGMENT.fullmatch(str(asset_id)) or type(version) is not int or version < 1:
            raise AssetTransferError("asset_manifest_invalid")
        root = self._output_root()
        folder = root / ".assets" / str(asset_id) / f"v{version}"
        if any(part.is_symlink() for part in (root / ".assets", folder.parent, folder) if part.exists()):
            raise AssetTransferError("asset_case_copy_invalid")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not folder.resolve().is_relative_to(root):
            raise AssetTransferError("asset_case_copy_invalid")
        return folder / filename

    def fetch(self, asset_id, *, version=1, kind="", attributes=None):
        found = self._call("download", {"asset_id": asset_id, "version": version})
        if found.get("location") != "scout_library" or found.get("transfer"):
            from mino_scout.runtime.script_exec import ScriptAbort
            raise ScriptAbort("资产引用只对应本机库，没有可下载的地址", reason_code="asset_location_invalid", status="blocked")
        asset = found["asset"]
        if (kind and asset["kind"] != kind) or any(asset["attributes"].get(k) != v for k, v in (attributes or {}).items()):
            from mino_scout.runtime.script_exec import ScriptAbort
            raise ScriptAbort("资产版本不满足用例声明的类型或属性", reason_code="asset_requirements_mismatch", status="blocked")
        # This attempt gets its own copy. The library and the task that generated the file stay unchanged.
        try:
            destination = self._case_copy(asset_id, version, asset["filename"])
            path, hit = self._store().copy_verified(asset, destination, self.case._guard)
            root = self._output_root()
        except AssetTransferError as exc:
            self._error(exc)
        self._record("fetch", {"asset_id": asset_id, "version": version, "sha256": asset["sha256"],
                              "size_bytes": asset["size_bytes"], "role": "case_input", "library_hit": hit,
                              "output_relpath": path.relative_to(root).as_posix()})
        return path

    def require(self, name, *, kind, attributes=None, description=""):
        reference = self.case.data.get(name)
        if reference is None:
            matches = self.find(kind, attributes=attributes)["items"]
            if matches:
                reference = {"asset_id": matches[0]["asset_id"], "version": matches[0]["version"]}
                self.case.data.bind(name, reference)
            else:
                return self.case.data.require(name, kind=kind, attributes=attributes or {},
                    description=description or f"缺少测试资产 {name}；应查找或准备后恢复本用例")
        if not isinstance(reference, dict) or not reference.get("asset_id"):
            from mino_scout.runtime.script_exec import ScriptAbort
            raise ScriptAbort("资产引用必须包含 asset_id 和 version", reason_code="asset_reference_invalid", status="blocked")
        return self.fetch(reference["asset_id"], version=reference.get("version", 1), kind=kind, attributes=attributes)

    def publish(self, path, *, asset_id, version=1, kind, title="", attributes=None, source=None):
        self.case._guard()
        manifest = {"asset_id": asset_id, "version": version, "filename": Path(path).name,
                    "kind": kind, "title": title or asset_id, "attributes": attributes or {}, "source": {**(source or {}), "task_id": self.case.run_id, "case_id": self.case._case_id, "app_id": self.case.app_id}}
        # Queue stores only sanitized metadata and file bytes, never a signed URL.
        from mino_scout.runtime.records import scrub
        if scrub(manifest) != manifest:
            from mino_scout.runtime.script_exec import ScriptAbort
            raise ScriptAbort("资产元数据不能包含凭据", reason_code="asset_metadata_invalid", status="blocked")
        try:
            source = self._generated_file(path)
            store = self._store()
            ident, manifest = store.stage(source, manifest, self.case.app_id, self.case._guard)
            store.commit(ident, self.case._guard)
            self._record("pending_register", {"asset_id": asset_id, "version": version, "sha256": manifest["sha256"], "role": "generated"})
            return self._publish_pending(ident, manifest)
        except AssetTransferError as exc:
            self._error(exc)

    def _publish_pending(self, ident, manifest):
        result = self._call("begin", {"asset": manifest})
        if result.get("location") != "scout_library" or result.get("transfer") or result["asset"]["state"] != "ready":
            raise AssetTransferError("asset_location_invalid")
        if result["asset"]["sha256"] != manifest["sha256"]:
            raise AssetTransferError("asset_checksum_mismatch")
        self._store().acknowledge(ident, self.case._guard)
        asset = result["asset"]
        reference = {"asset_id": asset["asset_id"], "version": asset["version"], "sha256": asset["sha256"], "role": "generated"}
        self._record("published", reference)
        return reference

    def flush(self):
        """Retry registration for files already committed in this Scout's library."""
        self.case._guard()
        try:
            store = self._store()
            rows = store.pending(self.case.app_id)
            for row in rows:
                store.commit(row["id"], self.case._guard)
            return [self._publish_pending(row["id"], row["asset"]) for row in rows]
        except AssetTransferError as exc:
            self._error(exc)
