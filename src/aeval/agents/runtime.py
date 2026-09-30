"""Runtime requirements and the image table that can host them (方案二).

An agent's CLI has to exist *inside the sandbox*, and that is not something a
declaration can wish into being: Harbor builds the sandbox from the task's own
Dockerfile (or a digest-pinned image the operator names), and a mismatched
pairing fails late and confusingly — ``dcode: not found`` in the middle of a
trial, after the sandbox was built and the budget started.

So the fact is split the way the rest of the framework splits facts:

* the **declaration** says what the agent needs (``runtime.key`` — a slug like
  ``dsh`` or ``deepagents-code`` — plus human-readable ``needs``);
* this **table** says where that need is met: for a given suite, which
  digest-pinned image hosts that runtime (or ``image: null`` when the pairing
  deliberately relies on the task's own Dockerfile).

The check is fail-closed in the useful direction: a pairing the table does not
mention is refused with the fix spelled out (add an entry, or pass
``--sandbox-image``) instead of being discovered inside a sandbox. A pairing
whose entry pins an image must use exactly that image.

The table lives next to the declarations (``agents/_runtime/images.yaml``; the
``_`` prefix keeps declaration discovery from treating it as an agent) and can
be pointed elsewhere with ``AEVAL_RUNTIME_IMAGES``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aeval.suite_models import SuiteError

__all__ = [
    "RuntimeDeclaration",
    "image_problem_for",
    "RuntimeImage",
    "RuntimeImageTable",
    "default_runtime_images_path",
    "load_runtime_images",
    "resolve_runtime_image",
]

RUNTIME_IMAGES_ENV = "AEVAL_RUNTIME_IMAGES"
RUNTIME_TABLE_RELPATH = ("_runtime", "images.yaml")

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


class RuntimeDeclaration(BaseModel):
    """What the agent's sandbox must provide (declared, never assumed)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    needs: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key_is_a_slug(cls, value: str) -> str:
        if not _KEY_PATTERN.match(value):
            raise ValueError(
                f"runtime.key must be a lowercase slug (e.g. 'dsh'), got {value!r}"
            )
        return value

    @field_validator("needs")
    @classmethod
    def _needs_are_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("runtime.needs entries must be non-empty strings")
        return value


class RuntimeImage(BaseModel):
    """One row of the table: this suite, that runtime, and where it runs."""

    model_config = ConfigDict(extra="forbid")

    suite: str
    runtime: str
    #: Digest-pinned image (``ref@sha256:…``) or ``None`` when the pairing runs
    #: on the task's own Dockerfile (the pre-existing behavior, made explicit).
    image: str | None = None
    platform: str | None = None
    note: str | None = None

    @field_validator("image")
    @classmethod
    def _image_is_digest_pinned(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if "@sha256:" not in value:
            raise ValueError(
                f"a mapped image must be digest-pinned (ref@sha256:…), got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _pinned_image_needs_a_platform(self) -> "RuntimeImage":
        # The observed identity binds the architecture too (see --sandbox-image):
        # a pinned image without a platform could not be verified against the
        # live sandbox, so it is refused here rather than at run time.
        if self.image is not None and not self.platform:
            raise ValueError("a pinned image needs its platform (observed identity)")
        return self


class RuntimeImageTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    images: list[RuntimeImage] = Field(default_factory=list)

    def lookup(self, suite_id: str, runtime_key: str) -> RuntimeImage | None:
        for entry in self.images:
            if entry.suite == suite_id and entry.runtime == runtime_key:
                return entry
        return None


def default_runtime_images_path(agents_root: Path | None = None) -> Path:
    """Where the table lives: ``$AEVAL_RUNTIME_IMAGES`` or ``agents/_runtime``."""
    override = os.environ.get(RUNTIME_IMAGES_ENV)
    if override:
        return Path(override)
    from aeval.agents.declaration import default_agents_root

    root = Path(agents_root) if agents_root is not None else default_agents_root()
    return root.joinpath(*RUNTIME_TABLE_RELPATH)


def load_runtime_images(path: Path | None = None) -> RuntimeImageTable:
    """Read the table; a missing file is an empty table, not an error."""
    resolved = Path(path) if path is not None else default_runtime_images_path()
    if not resolved.is_file():
        return RuntimeImageTable()
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:  # noqa: BLE001
        raise SuiteError(f"cannot read the runtime image table {resolved}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SuiteError(f"the runtime image table {resolved} must be a mapping")
    try:
        return RuntimeImageTable.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic error carries the field
        raise SuiteError(f"invalid runtime image table {resolved}: {exc}") from exc


def resolve_runtime_image(
    table: RuntimeImageTable, suite_id: str, runtime_key: str
) -> RuntimeImage | None:
    """The table row for a pairing, or None when the table does not map it."""
    return table.lookup(suite_id, runtime_key)


def image_problem_for(
    table: RuntimeImageTable,
    *,
    suite_id: str,
    runtime: RuntimeDeclaration,
    image: str | None,
    platform: str | None,
    table_path: Path | None = None,
) -> str | None:
    """Why this (suite, runtime) pairing cannot run as configured — or None.

    Three outcomes, all explicit:

    * the table maps the pairing to a pinned image: the run must use exactly it
      (a different image is what "it worked in my terminal" looks like);
    * the table maps it to ``image: null``: the pairing deliberately rides the
      task's own Dockerfile, so nothing is required from the operator;
    * the table does not mention it: refused, with the fix named — unless the
      operator passes ``--sandbox-image``, which states the assumption instead
      of hiding it.
    """
    where = Path(table_path) if table_path is not None else default_runtime_images_path()
    entry = table.lookup(suite_id, runtime.key)
    if entry is None:
        if image is not None:
            return None
        needs = ", ".join(runtime.needs) or "unspecified"
        return (
            f"no sandbox image is mapped for suite {suite_id!r} × runtime "
            f"{runtime.key!r} (needs: {needs}); add a row to {where} or pass "
            "--sandbox-image ref@sha256:… --sandbox-platform <arch>"
        )
    if entry.image is None:
        return None
    if image != entry.image or platform != entry.platform:
        return (
            f"suite {suite_id!r} × runtime {runtime.key!r} is mapped to "
            f"{entry.image} ({entry.platform}), but this run passes "
            f"image={image!r} platform={platform!r} — they must match "
            f"(update the row in {where} if the mapping changed)"
        )
    return None
