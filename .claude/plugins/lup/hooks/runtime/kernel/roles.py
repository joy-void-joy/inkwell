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

import posixpath

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


def normalized_path(path: str) -> str:
    """Normalize one portable path without resolving against the filesystem."""
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

    Resolution is lexical, so it needs no filesystem call and ``..`` cannot
    climb out of a declared root into a role it was never given. A symlink
    inside a root that points beyond it is not settleable without a syscall
    and stays a known limit; it is narrow, because a role only ever relaxes
    verbs acting on paths already inside the root and never grants execution.

    How far each declaration reaches is the row's own, through
    :func:`root_matches`: a root anchored at the repository top, or a
    directory recognized wherever it sits.

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
        kind = row["kind"] if "kind" in row else "subtree"
        if root_matches(normalized, row["root"], kind):
            return row["role"]
    return "production"
