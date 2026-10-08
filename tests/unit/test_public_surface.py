"""Keep internal jargon off the public surface.

The project's history carries vocabulary that means nothing to a reader who was
not in the room: work-package ids (``P0-8``), the internal lab host name, the
pre-archive path of the internal documents, defect ids, and section markers
copied out of design drafts. All of it was scrubbed before the first community
release; this guard keeps it from creeping back in.

The scope is exactly the published surface, derived from two sources the
packaging already trusts: the sdist's own exclude list in ``pyproject.toml``,
and git's ignore rules (hatchling drops ignored files, so a stray force-added
browser profile must not be scanned either). ``docs/internal/`` and
``ops/e2b/`` are deliberately out of scope: the archive preserves the original
wording on purpose, and neither ships.

This module is itself excluded from the sdist, because a denylist has to spell
out the terms it bans and the published package must not contain them. The
guard picks that up automatically, since it reads its scope from the same list.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# What a reader can actually read. Binary payloads (images, wheels, sqlite
# stores) are skipped: a byte pattern inside a PNG is not documentation.
_TEXT_SUFFIXES = frozenset({
    ".cfg", ".css", ".html", ".ini", ".js", ".json", ".jsonl", ".md", ".mjs",
    ".py", ".sh", ".toml", ".ts", ".txt", ".yaml", ".yml",
})

# Directories that never hold hand-written text we own.
_SKIP_DIRS = frozenset({
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".test-dist",
    ".uv-cache", ".venv", "__pycache__", "node_modules",
})

_DIGIT = r"[0-9]"

# (label, pattern, sample, suffixes)
#
# ``suffixes`` narrows a low-precision pattern to the files where it is
# unambiguous; ``None`` means every text file. Precision matters more than
# coverage here: a guard that cries wolf gets switched off. Bare ``M0``/``P2``
# tokens are only jargon in prose, for instance — in the SVG path data of the
# architecture diagram, ``M7.8`` is a moveto command.
_DENYLIST: tuple[tuple[str, str, str, tuple[str, ...] | None], ...] = (
    # "P0-8", "P2-5b": work-package / milestone ids from the internal plan docs
    ("work_package_id", rf"\bP{_DIGIT}-{_DIGIT}", "P0-8", None),
    # the same tokens standing alone in prose: "P0", "P2", "M0"
    ("bare_priority_token",
     rf"(?<![A-Za-z0-9])[PM]{_DIGIT}(?![A-Za-z0-9-])", "M0", (".md",)),
    # the internal lab host name
    ("internal_host", "example-lab", "example-lab", None),
    # the pre-archive home of the internal documents
    ("archive_path", "docs/TESTS", "docs/TESTS/PLAN.md", None),
    # archive document titles, which only describe internal history
    ("internal_doc_name",
     "AGENT-ABSTRACTION|AGENT-ADAPTER|TRAJECTORY-GRADER|FULL-CHAIN-REPORT|"
     "SUITE-INHERITANCE|DEEPAGENTS-FACTS|HARBOR_DSH",
     "FULL-CHAIN-REPORT-x.md", None),
    # a design-draft section marker ("§6", "§ 2"), as opposed to the noise
    # fixtures in suites/ that deliberately spray bare "§§§" characters
    ("design_section_marker", "\u00a7\\s*" + _DIGIT, "\u00a7 6", None),
    # internal jargon in Chinese: 源方案 / 工作包 / 验收轮 / 线测试 / 实测数字
    # ("本包" is deliberately absent: as ordinary Chinese for "this package" it
    # is a false positive, and it reads that way in pyproject.toml.)
    ("internal_zh_jargon",
     "\u6e90\u65b9\u6848|\u5de5\u4f5c\u5305|"
     "\u9a8c\u6536\u8f6e|\u7ebf\u6d4b\u8bd5|\u5b9e\u6d4b\u6570\u5b57",
     "\u6e90\u65b9\u6848", None),
    # defect ids from the internal tracker
    ("defect_id", rf"\bD{_DIGIT}{{2}}\b", "D14", None),
)


def _sdist_excludes() -> tuple[str, ...]:
    """The packaging exclude list — the guard's scope is the shipped scope."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw = data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    return tuple(entry.lstrip("/").rstrip("/") for entry in raw)


def _is_excluded(rel: str, excludes: tuple[str, ...]) -> bool:
    return any(rel == entry or rel.startswith(entry + "/") for entry in excludes)


def _git(*args: str) -> list[str] | None:
    """Run a NUL-separated ``git ls-files`` query, or None outside a checkout."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in done.stdout.decode("utf-8").split("\0") if p]


def _tracked_files() -> list[str] | None:
    """Repo-relative tracked paths that git does not ignore.

    ``git ls-files`` on its own also returns files that are tracked *and*
    matched by ``.gitignore`` — force-added junk such as a stray browser
    profile. Hatchling leaves those out of the sdist, so the guard must too;
    otherwise it reports on third-party files that never ship.
    """
    tracked = _git("ls-files", "-z")
    if tracked is None:
        return None
    ignored = _git("ls-files", "-z", "-c", "-i", "--exclude-standard")
    keep = set(tracked)
    if ignored is not None:
        keep -= set(ignored)
    return sorted(keep)


def _walked_files() -> list[str]:
    """Fallback for an unpacked sdist, which has no git metadata.

    Without git there is no ignore file to consult, so dot-directories are
    skipped wholesale: a shipped tree has none (only the ``.gitignore`` file),
    while a working copy keeps caches and stray tool profiles inside them.
    """
    found: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT)
        if any(
            part in _SKIP_DIRS or part.startswith(".")
            for part in rel.parts[:-1]
        ):
            continue
        if path.is_file():
            found.append(str(rel))
    return found


def surface_files() -> list[Path]:
    """Files on the published surface: shipped, and readable text."""
    excludes = _sdist_excludes()
    rels = _tracked_files()
    if rels is None:
        rels = _walked_files()
    return [
        REPO_ROOT / rel
        for rel in rels
        if not _is_excluded(rel, excludes)
        and (REPO_ROOT / rel).suffix.lower() in _TEXT_SUFFIXES
    ]


def test_no_internal_jargon_on_the_public_surface():
    offenders: list[str] = []
    for path in surface_files():
        suffix = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern, _, suffixes in _DENYLIST:
            if suffixes is not None and suffix not in suffixes:
                continue
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{line}: [{label}] {match.group(0)!r}")
    assert not offenders, (
        "内部黑话回流到对外发布面。这些词只允许出现在 docs/internal/ 与 "
        "ops/e2b/（两者都不随包分发）；清理后重跑本测试：\n"
        + "\n".join(offenders[:40])
    )


def test_scope_is_the_packaging_scope():
    """The guard must scan what ships; drift here would hide a leak."""
    excludes = _sdist_excludes()
    assert "docs/internal" in excludes
    assert "ops/e2b" in excludes
    scanned = {str(p.relative_to(REPO_ROOT)) for p in surface_files()}
    assert scanned, "guard scanned no files — file enumeration is broken"
    for entry in excludes:
        hit = [p for p in scanned if p == entry or p.startswith(entry + "/")]
        assert not hit, f"excluded path is being scanned: {hit[:3]}"
    # git-ignored files never ship, so they are not the guard's business
    ignored = _git("ls-files", "-z", "-c", "-i", "--exclude-standard") or []
    assert not (set(ignored) & scanned)


def test_this_guard_itself_is_not_published():
    """A denylist must name what it bans, so it must not ship."""
    rel = str(Path(__file__).resolve().relative_to(REPO_ROOT))
    assert _is_excluded(rel, _sdist_excludes()), (
        f"{rel} contains the banned tokens by design; add it to "
        "[tool.hatch.build.targets.sdist] exclude in pyproject.toml"
    )


def test_every_denylist_entry_still_matches_its_sample():
    """A pattern that silently stopped matching is worse than no guard."""
    for label, pattern, sample, _ in _DENYLIST:
        assert re.search(pattern, sample), f"{label}: {sample!r} no longer matches"