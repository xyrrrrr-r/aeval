from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from aeval.cli import app
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.suite_loader.transfer import CONVERTER_VERSION, export_suite, import_suite
from aeval.suite_models import SuiteError


def test_native_import_export_roundtrip(native_suite_dir, tmp_path):
    source = native_suite_dir
    (source / "NOTICE").write_bytes(b"Synthetic fixture copyright notice\r\n")
    original = (source / "suite.yaml").read_bytes()
    imported = import_suite(source, tmp_path / "imported", version="2.0.0")
    suite = load_suite(imported)
    assert suite.version == "2.0.0"
    assert suite.overlay.provenance.data_imported
    assert suite.overlay.provenance.converter_version == CONVERTER_VERSION
    assert suite.overlay.provenance.original_id == "native-example"
    assert suite.overlay.provenance.imported_at
    archive = imported / "provenance" / f"suite-{sha256(original).hexdigest()}.yaml"
    assert archive.read_bytes() == original
    assert (source / "suite.yaml").read_bytes() == original
    job = compose_harbor_job(suite)
    assert job.tasks[0].path == imported / "tasks/example"
    exported = export_suite(imported, tmp_path / "exported")
    for path in imported.rglob("*"):
        target = exported / path.relative_to(imported)
        if path.is_file():
            assert target.read_bytes() == path.read_bytes()
        else:
            assert target.is_dir()
    assert compose_harbor_job(load_suite(exported)).tasks[0].path == exported / "tasks/example"


@pytest.mark.parametrize("license", ["UNKNOWN", "NONE_DECLARED"])
@pytest.mark.parametrize("operation", ["import", "export"])
def test_license_gate_creates_no_output(native_suite_dir, tmp_path, license, operation):
    manifest = native_suite_dir / "suite.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["provenance"]["license"] = license
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    out = tmp_path / "blocked"
    with pytest.raises(SuiteError, match="skeletons only"):
        if operation == "import":
            import_suite(native_suite_dir, out, version="2")
        else:
            export_suite(native_suite_dir, out)
    assert not out.exists()
    assert not list(tmp_path.glob(".aeval-import-*"))


def test_task_license_overrides_suite_permission(native_suite_dir, tmp_path):
    task = native_suite_dir / "tasks/example/task.toml"
    task.write_text(
        '[metadata.aeval.provenance]\nsource="third-party"\nlicense="NONE_DECLARED"\n',
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="skeletons only"):
        import_suite(native_suite_dir, tmp_path / "blocked", version="2")
    assert not (tmp_path / "blocked").exists()


def test_existing_output_is_not_overwritten(native_suite_dir, tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    (out / "keep").write_bytes(b"keep")
    with pytest.raises(SuiteError, match="never overwrite"):
        export_suite(native_suite_dir, out)
    assert (out / "keep").read_bytes() == b"keep"


def test_new_version_required(native_suite_dir, tmp_path):
    with pytest.raises(SuiteError, match="new, non-empty"):
        import_suite(native_suite_dir, tmp_path / "out", version="1.0.0")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("format", ["dsh-eval-harness", "standards-profile", "frontier-tasks"])
def test_unverified_formats_fail_explicitly(native_suite_dir, tmp_path, format):
    with pytest.raises(SuiteError, match="upstream schema"):
        import_suite(native_suite_dir, tmp_path / "out", version="2", format=format)
    assert not (tmp_path / "out").exists()


def test_nested_output_rejected(native_suite_dir):
    with pytest.raises(SuiteError, match="overlap"):
        export_suite(native_suite_dir, native_suite_dir / "nested")


def test_invalid_native_task_creates_no_output(native_suite_dir, tmp_path):
    (native_suite_dir / "tasks/example/tests/test.sh").unlink()
    with pytest.raises(SuiteError, match="Invalid native Harbor task"):
        import_suite(native_suite_dir, tmp_path / "out", version="2")
    assert not (tmp_path / "out").exists()


def test_copy_failure_cleans_only_owned_staging(native_suite_dir, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected copy error")

    monkeypatch.setattr("aeval.suite_loader.transfer.shutil.copy2", fail)
    with pytest.raises(OSError, match="injected"):
        import_suite(native_suite_dir, tmp_path / "out", version="2")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".aeval-import-*"))
    assert (native_suite_dir / "suite.yaml").is_file()


def test_publication_failure_cleans_owned_output(native_suite_dir, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected publication error")

    monkeypatch.setattr("aeval.suite_loader.transfer.shutil.copyfileobj", fail)
    with pytest.raises(OSError, match="injected publication"):
        export_suite(native_suite_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".aeval-import-*"))
    assert (native_suite_dir / "suite.yaml").is_file()


def test_output_created_during_validation_is_preserved(native_suite_dir, tmp_path, monkeypatch):
    import aeval.suite_loader.transfer as transfer

    original = transfer.compose_harbor_job
    out = tmp_path / "out"
    calls = 0

    def compose(suite):
        nonlocal calls
        result = original(suite)
        calls += 1
        if calls == 2:
            out.mkdir()
            (out / "keep").write_bytes(b"created independently")
        return result

    monkeypatch.setattr(transfer, "compose_harbor_job", compose)
    with pytest.raises(SuiteError, match="refusing overwrite"):
        export_suite(native_suite_dir, out)
    assert (out / "keep").read_bytes() == b"created independently"
    assert not list(tmp_path.glob(".aeval-import-*"))


def test_cache_exclusion_and_empty_context(native_suite_dir, tmp_path):
    (native_suite_dir / ".git").mkdir()
    (native_suite_dir / ".git/config").write_bytes(b"not suite content")
    (native_suite_dir / "tasks/example/environment/Dockerfile").unlink()
    out = export_suite(native_suite_dir, tmp_path / "out")
    assert not (out / ".git").exists()
    assert (out / "tasks/example/environment").is_dir()


def test_directory_metadata_is_restored_after_children(native_suite_dir, tmp_path, monkeypatch):
    import os
    import stat
    import aeval.suite_loader.transfer as transfer

    directories = [(native_suite_dir, 0o700), (native_suite_dir / "private", 0o700),
                   (native_suite_dir / "scratch", 0o1777)]
    for directory, mode in directories:
        directory.mkdir(exist_ok=True)
        os.chmod(directory, mode)
    original = transfer.shutil.copystat
    copied = []

    def copystat(source, target, **kwargs):
        if Path(source).is_dir():
            copied.append((Path(source), Path(target)))
        return original(source, target, **kwargs)

    monkeypatch.setattr(transfer.shutil, "copystat", copystat)
    out = export_suite(native_suite_dir, tmp_path / "out")
    assert copied[-1] == (native_suite_dir, out)
    for directory, _ in directories:
        target = out / directory.relative_to(native_suite_dir)
        assert (directory, target) in copied
        assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(directory.stat().st_mode)


def test_env_files_are_not_transferred(native_suite_dir, tmp_path):
    (native_suite_dir / ".env").write_bytes(b"SYNTHETIC=value\n")
    with pytest.raises(SuiteError, match="Environment files"):
        export_suite(native_suite_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_linked_input_is_rejected(native_suite_dir, tmp_path):
    target = tmp_path / "external"
    target.write_bytes(b"external")
    link = native_suite_dir / "linked"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink creation is unavailable")
    with pytest.raises(SuiteError, match="linked"):
        export_suite(native_suite_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_cli_native_transfer_and_probe(native_suite_dir, tmp_path):
    runner = CliRunner()
    out = tmp_path / "imported"
    result = runner.invoke(app, [
        "import", "--src", str(native_suite_dir), "--out", str(out), "--version", "2.0.0",
        "--format", "harbor-task",
    ])
    assert result.exit_code == 0, result.output
    probe = runner.invoke(app, ["probe", "--suite", str(out)])
    assert probe.exit_code == 0, probe.output
    exported = runner.invoke(app, ["export", "--suite", str(out), "--out", str(tmp_path / "exported")])
    assert exported.exit_code == 0, exported.output


def test_cli_list_rejects_same_id_across_versions(native_suite_dir, tmp_path):
    import_suite(native_suite_dir, tmp_path / "imported", version="2.0.0")
    result = CliRunner().invoke(app, ["list", "--suites-dir", str(tmp_path)])
    assert result.exit_code == 3
    assert "duplicate suite identity" in result.output
