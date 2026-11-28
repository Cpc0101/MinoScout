#!/usr/bin/env python3
"""Isolated local-library smoke. No object storage and no network."""
import hashlib
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from mino_scout.runtime.asset_cache import AssetCache, AssetTransferError
    with tempfile.TemporaryDirectory(prefix="mino-asset-cache-") as directory:
        root = Path(directory)
        source = root / "image.png"
        payload = b"test-resource" * 120000
        source.write_bytes(payload)
        store = AssetCache("owner", root=root / "state", quota_bytes=10 * 1024 ** 2)
        manifest = {"asset_id": "image", "version": 1, "kind": "image", "title": "image", "filename": "image.png", "attributes": {}, "source": {}}
        ident, staged = store.stage(source, manifest, "APP003")
        restored = AssetCache("owner", root=root / "state", quota_bytes=10 * 1024 ** 2)
        assert restored.pending("APP003")[0]["id"] == ident
        assert restored.pending("APP004") == []
        asset = {**staged, "project_id": "PRJ001", "state": "ready"}
        try:
            restored.fetch(asset, root / "outputs" / "missing.png")
        except AssetTransferError as exc:
            assert exc.code == "asset_not_on_this_scout"
        else:
            raise AssertionError("missing library file was readable")
        library = restored.commit(ident)
        assert library.read_bytes() == payload and not (restored.root / "pending" / (ident + ".blob")).exists()
        path, hit = restored.fetch(asset, root / "outputs" / "first.png")
        assert hit and path.read_bytes() == payload
        path.write_bytes(b"modified-by-destructive-test")
        path, hit = restored.fetch(asset, root / "outputs" / "second.png")
        assert hit and path.read_bytes() == payload and library.read_bytes() == payload
        from mino_scout.runtime.case_assets import CaseAssets
        from mino_scout.runtime.script_exec import ScriptAbort
        class Task:
            def __init__(self, output):
                self.output_dir, self.app_id, self.run_id, self._case_id, self.deadline = output, "APP003", "task-1", "case-1", None
            def _guard(self):
                pass
        task_output = root / "execution" / "task-1" / "attempts" / "a1" / "output"
        case_output = root / "execution" / "task-2" / "attempts" / "a1" / "output"
        task_output.mkdir(parents=True)
        case_output.mkdir(parents=True)
        generated = task_output / "doll.3mf"
        generated.write_bytes(payload)
        generator = CaseAssets(Task(task_output), "owner")
        generator._cache = restored
        assert generator._generated_file(generated) == generated.resolve()
        try:
            generator._generated_file(source)
        except AssetTransferError as exc:
            assert exc.code == "asset_not_task_output"
        else:
            raise AssertionError("file outside this task was publishable")
        consumer = CaseAssets(Task(case_output), "owner")
        consumer._cache = restored
        case_file, case_hit = consumer._store().copy_verified(asset, consumer._case_copy("image", 1, "image.png"))
        assert case_hit and case_file.is_relative_to(case_output.resolve()) and ".assets" in case_file.parts
        case_file.write_bytes(b"case-mutated")
        assert library.read_bytes() == payload and generated.read_bytes() == payload
        try:
            consumer._generated_file(case_file)
        except AssetTransferError as exc:
            assert exc.code == "asset_case_input_not_publishable"
        else:
            raise AssertionError("case input copy was publishable")
        assert ScriptAbort
        restored.acknowledge(ident)
        assert not restored.pending("APP003") and source.read_bytes() == payload
        concurrent = AssetCache("another-owner", root=root / "state", quota_bytes=10 * 1024 ** 2)
        try:
            concurrent.fetch(asset, root / "outputs" / "other-owner.png")
        except AssetTransferError as exc:
            assert exc.code == "asset_not_on_this_scout"
        else:
            raise AssertionError("another account read this library")
        same = AssetCache("owner", root=root / "state", quota_bytes=10 * 1024 ** 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda n: same.fetch(asset, root / "outputs" / f"parallel-{n}.png"), [1, 2]))
        assert [item.read_bytes() for item, _hit in outcomes] == [payload, payload]
        assert len(list((same.root / "library").iterdir())) == 1
        corrupt = AssetCache("owner", root=root / "state", quota_bytes=10 * 1024 ** 2)
        library.write_bytes(b"x" * len(payload))
        try:
            corrupt.fetch(asset, root / "outputs" / "corrupt.png")
        except AssetTransferError as exc:
            assert exc.code == "asset_checksum_mismatch"
        else:
            raise AssertionError("corrupt file accepted")
        assert not (root / "outputs" / "corrupt.png").exists() and library.is_file()
        small = AssetCache("small", root=root / "state", quota_bytes=300)
        try:
            small.stage(source, manifest, "APP003")
        except AssetTransferError as exc:
            assert exc.code == "asset_cache_quota_exceeded_pending_preserved"
        else:
            raise AssertionError("quota ignored")
        library.write_bytes(payload)
        scratch1, hold1 = restored.scratch()
        (scratch1 / "input.bin").write_bytes(payload)
        scratch2, hold2 = restored.scratch()
        assert scratch1.exists()
        hold1.close()
        scratch3, hold3 = restored.scratch()
        assert not scratch1.exists() and scratch2.exists()
        restored.release_scratch(scratch2, hold2)
        restored.release_scratch(scratch3, hold3)
        assert not list(restored.root.rglob("*.json"))
        print("PASS: task output, reusable library, case input copy, SHA256 verification, account isolation, quota")


if __name__ == "__main__":
    main()
