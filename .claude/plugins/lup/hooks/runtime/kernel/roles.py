"""What a repository path is for, and which gates that answer relaxes.

The lattice judges an action by what it does. A role adds the missing half:
what the thing acted upon is *for*. Production code carries the conventions
because other code reads it; a test carries none of them because its subject
is production's behaviour rather than its own shape; scratch carries nothing
at all because every file there is disposable by construction.

Roles inside the repository are declared by the application, never the kernel
— that path vocabulary belongs to whoever laid out the tree. The session
scratchpad is the one root the kernel knows unaided, because the harness names
it rather than the repository: it lives outside every worktree, so no
repo-relative declaration could reach it, and its spelling is fixed by the
runtime that provides it.
"""

import fnmatch
import posixpath
from pathlib import PurePosixPath

from .rows import PathRoleKind, PathRoleName, PathRoleRow


def is_session_scratch_target(word: str) -> bool:
    """Recognize a path confined to the session scratchpad.

    ``$TMPDIR`` is the harness-provided scratch root and ``/tmp/claude-*`` its
    host-side spelling, so writes there are scratch by definition. A suffix
    that expands further or climbs out of the root stays unrecognized.
    """
    for prefix in ("$TMPDIR/", "${TMPDIR}/"):
        if word.startswith(prefix):
            suffix = word[len(prefix) :]
            normalized = posixpath.normpath(suffix)
            return "$" not in suffix and not normalized.startswith(("..", "/"))
    return "$" not in word and posixpath.normpath(word).startswith("/tmp/claude-")


def role_pattern_covers(pattern: str, path: str) -> bool:
    """Whether a declared role pattern reaches a repository-relative path.

    A role names a tree rather than a file, so the pattern is matched against
    the path's leading segments and covers everything below what it names:
    ``tmp`` reaches ``tmp/run/log``, and ``**/__pycache__`` reaches both the
    directory itself and ``src/a/__pycache__/m.pyc``.

    Matching is per segment, so a wildcard cannot cross a ``/`` and silently
    widen a pattern past the directory it names; ``**`` is the one spelling
    that spans segments, standing for any run of them including none. That
    distinction is the whole safety argument for declaring a role by pattern
    at all — ``**/__pycache__`` names every cache directory and nothing else,
    where a substring test would also have claimed ``notes/__pycache__.bak``.

    A literal root carrying glob metacharacters — a directory actually named
    ``run[1]`` — reads as a pattern under this. Nothing in the declarations is
    spelled that way, and a root that ever is escapes the class as ``run[[]1]``.
    """

    def covers(pattern: tuple[str, ...], segments: tuple[str, ...]) -> bool:
        """Whether the pattern matches a leading run of the path's segments."""
        if not pattern:
            return True
        head, rest = pattern[0], pattern[1:]
        if head == "**":
            return any(
                covers(rest, segments[index:]) for index in range(len(segments) + 1)
            )
        if not segments:
            return False
        return fnmatch.fnmatchcase(segments[0], head) and covers(rest, segments[1:])

    return covers(PurePosixPath(pattern).parts, PurePosixPath(path).parts)


def normalized_path(path: str) -> str:
    """Normalize one portable path without resolving against the filesystem."""
    # lup: ignore[string-replace] — a posix parser cannot read a Windows path,
    # so settling the separator convention is what makes the string parseable
    # at all, rather than something the parser below could have done instead
    return posixpath.normpath(path.replace("\\", "/"))


def root_matches(path: str, value: str, kind: PathRoleKind) -> bool:
    """Whether one path sits under a declared directory, however it is declared.

    The one answer both declaration tables need. A protected-path rule and a
    role row ask this of the same two shapes, and asking it in one place is
    what keeps a directory from meaning one thing to the gate that protects it
    and another to the gate that says what it is for.

    ``contains_part`` matches the directory wherever it sits, and excludes the
    system temporary directory, which is a different tree that happens to share
    the name. Wrapping both sides in separators is what makes a segment match a
    segment: ``tmpfoo`` holds the characters and is not the directory.
    """
    portable = normalized_path(path)
    expected = normalized_path(value)
    if kind == "contains_part":
        return f"/{expected}/" in f"/{portable}/" and not portable.startswith(
            f"/{expected}"
        )
    return portable == expected or portable.startswith(expected + "/")


def path_role(path: str, rows: list[PathRoleRow]) -> PathRoleName:
    """Classify a repository-relative path by the role its root declares.

    A root is a pattern, so a tree scattered through the repository rather
    than gathered under one prefix — every ``__pycache__``, every ``.bak`` —
    is declarable as what it is. Disposability stays something a project
    states: nothing is scratch for merely being untracked, which is what
    keeps an ignored ``.env.local`` or trace directory production.

    Resolution is lexical, so it needs no filesystem call and ``..`` cannot
    climb out of a declared root into a role it was never given. A symlink
    inside a root that points beyond it is not settleable without a syscall
    and stays a known limit; it is narrow, because a role only ever relaxes
    verbs acting on paths already inside the root and never grants execution.

    How far each declaration reaches is the row's own, through
    :func:`role_pattern_covers`: a bare root is anchored at the repository
    top, and a leading ``**/`` recognizes the directory wherever it sits.

    The session scratchpad answers first, because it is the one scratch root
    that is absolute — reaching the declared roots below would mean passing
    the guard that keeps an absolute path from claiming a role by prefix.
    Every other path outside the repository stays production: a file in some
    other tree is not disposable merely for being elsewhere.
    """
    normalized = posixpath.normpath(path)
    if is_session_scratch_target(path):
        return "scratch"
    if normalized.startswith(("/", "../")) or normalized == "..":
        return "production"
    for row in rows:
        if role_pattern_covers(row["root"], normalized):
            return row["role"]
    return "production"
