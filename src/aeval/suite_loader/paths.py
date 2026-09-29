"""Portable path resolution for suite inputs (single source of truth).

Both the Harbor-reference resolver and the inheritance resolver must apply
exactly the same portability/containment rules. Keeping one implementation
here means a base manifest can never get a laxer path rule than a Harbor
input — traversal, absolute paths, Windows drives, reserved names and
linked ancestors are all refused identically.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from aeval.suite_models import SuiteError

__all__ = ["portable_relative", "suite_path"]


def portable_relative(reference: str | Path) -> str:
    """Return ``reference`` as a portable relative posix path or raise.

    Rejects absolute paths, Windows drives/roots, ``..`` traversal, control
    characters, Windows-reserved names and trailing dots/spaces.
    """
    reference = reference.as_posix() if isinstance(reference, Path) else reference
    posix, windows = PurePosixPath(reference), PureWindowsPath(reference)
    if (
        not posix.parts
        or posix.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or ".." in windows.parts
        or any(c in '<>:"\\|?*' or ord(c) < 32 for c in reference)
        or any(p.endswith((".", " ")) or PureWindowsPath(p).is_reserved() for p in posix.parts)
    ):
        raise SuiteError(f"Suite reference must be a portable relative path: {reference!r}")
    return posix.as_posix()


def suite_path(root: Path, reference: str | Path) -> Path:
    """Resolve a portable relative reference inside ``root``, or raise.

    Symlinked ancestors between ``root`` and the target are refused so a
    linked file cannot smuggle in content from outside the declared root.
    """
    reference = portable_relative(reference)
    root = root.resolve()
    path = root / reference
    try:
        path.resolve().relative_to(root)
        for candidate in (path, *path.parents):
            if candidate == root:
                break
            if candidate.is_symlink() or candidate.is_junction():
                raise SuiteError(f"Linked suite inputs are not supported: {candidate}")
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, SuiteError):
            raise
        raise SuiteError(f"Suite reference escapes its root: {reference!r}") from exc
    return path
