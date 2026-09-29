from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from aeval.suite_loader.composition import compose_harbor_job, read_mapping
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import ProvenanceInfo, SuiteError

CONVERTER_VERSION = "harbor-task@v1"
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
_DATA_LICENSES = {"CC0", "MIT", "Apache-2.0"}


def _no_links(path: Path) -> None:
    for entry in (path, *path.parents):
        if entry.is_symlink() or entry.is_junction():
            raise SuiteError(f"Suite transfer does not follow linked paths: {entry}")


def _source_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            _no_links(entry)
            if entry.name in _SKIP_DIRS:
                continue
            if entry.name == ".env" or entry.name.startswith(".env."):
                raise SuiteError(f"Environment files cannot be transferred as suite data: {entry}")
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise SuiteError(f"Suite contains a non-regular file: {entry}")
            entries.append(entry)
    return entries


def _license(info: ProvenanceInfo, source: Path) -> None:
    if info.license not in _DATA_LICENSES:
        raise SuiteError(
            f"{source}: license {info.license} allows format skeletons only; "
            "native suite transfer would copy task data and is refused"
        )


def _transfer(src: Path, out: Path, *, format: str, version: str | None) -> Path:
    if format != "harbor-task":
        raise SuiteError(
            f"Unsupported import/export format {format!r}; only harbor-task is verified. "
            "External converters require an upstream schema and license review."
        )
    src, out = Path(src).absolute(), Path(out).absolute()
    _no_links(src)
    _no_links(out)
    if out.exists():
        raise SuiteError(f"Output already exists; suite imports never overwrite: {out}")
    src, out = src.resolve(), out.resolve()
    if out.is_relative_to(src) or src.is_relative_to(out):
        raise SuiteError("Source and destination suite trees must not overlap")
    if not out.parent.is_dir():
        raise SuiteError(f"Output parent directory does not exist: {out.parent}")
    suite = load_suite(src)
    if suite.extends:
        raise SuiteError(
            f"{src}: suite inherits {suite.extends} — transfer copies only this "
            "directory, so it would silently drop the base file(s). Refused. "
            "Either move the bases inside this suite directory, or inline the "
            "resolved facts (see `aeval explain`) before transferring."
        )
    _license(suite.overlay.provenance, src)
    entries = _source_entries(src)
    for file in entries:
        if file.name == "task.toml" and file.is_file():
            metadata = read_mapping(file).get("metadata", {})
            if not isinstance(metadata, dict) or not isinstance(metadata.get("aeval", {}), dict):
                raise SuiteError(f"Task metadata and metadata.aeval must be mappings: {file}")
            aeval = metadata.get("aeval", {})
            for provenance in (metadata.get("provenance"), aeval.get("provenance")):
                if provenance is not None:
                    _license(ProvenanceInfo.model_validate(provenance), file)
    compose_harbor_job(suite)
    if version is not None and (not version.strip() or version == suite.version):
        raise SuiteError("Import requires a new, non-empty suite version")
    with tempfile.TemporaryDirectory(prefix=".aeval-import-", dir=out.parent) as temp:
        staging = Path(temp) / "suite"
        staging.mkdir()
        for entry in entries:
            target = staging / entry.relative_to(src)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
        if version is not None:
            from hashlib import sha256

            original = (staging / "suite.yaml").read_bytes()
            archive = staging / "provenance" / f"suite-{sha256(original).hexdigest()}.yaml"
            archive.parent.mkdir(exist_ok=True)
            if archive.exists() and archive.read_bytes() != original:
                raise SuiteError(f"Conflicting provenance archive: {archive.name}")
            archive.write_bytes(original)
            data = read_mapping(staging / "suite.yaml")
            data["version"] = version
            provenance = suite.overlay.provenance.model_dump(exclude_none=True)
            provenance.update(
                original_id=provenance.get("original_id", suite.id),
                imported_at=datetime.now(timezone.utc).isoformat(),
                converter_version=CONVERTER_VERSION,
                data_imported=True,
            )
            data["provenance"] = provenance
            (staging / "suite.yaml").write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
        compose_harbor_job(load_suite(staging))
        try:
            out.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise SuiteError(f"Output appeared during import; refusing overwrite: {out}") from exc
        created: list[Path] = []
        try:
            for entry in sorted(staging.rglob("*"), key=lambda p: (len(p.parts), str(p))):
                target = out / entry.relative_to(staging)
                if entry.is_dir():
                    target.mkdir(mode=0o700)
                else:
                    with entry.open("rb") as source, target.open("xb") as destination:
                        created.append(target)
                        shutil.copyfileobj(source, destination)
                    shutil.copystat(entry, target)
                    continue
                created.append(target)
            for directory in sorted((p for p in entries if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                shutil.copystat(directory, out / directory.relative_to(src))
            shutil.copystat(src, out)
        except BaseException:
            for target in reversed(created):
                if target.is_dir():
                    target.rmdir()
                else:
                    target.unlink()
            out.rmdir()
            raise
    return out


def import_suite(src: Path, out: Path, *, version: str, format: str = "harbor-task") -> Path:
    return _transfer(src, out, format=format, version=version)


def export_suite(src: Path, out: Path, *, format: str = "harbor-task") -> Path:
    return _transfer(src, out, format=format, version=None)
