#!/usr/bin/env python3
# Generated from lup.policy.assets.host and lup.adapters.claude.assets.policy_dispatcher by `uv run lup-devtools harness generate all` — edit the source, not this file.
# See docs/harness.md.

"""Claude Code hook dispatcher over the canonical semantic kernel.

Runs as a bare script beside its own runtime directory, reaching only
the standard library and the kernel copied beside it.
"""

import json
import os
import sys
from pathlib import Path

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and `runtime/` is a plain sibling directory holding
# the kernel package and this plugin's policy data rather than an installed
# distribution. Naming it as a search path is what lets the imports below
# resolve, for the interpreter and for a type checker alike.
sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from kernel.decision import KernelDecision, escalation_offer, sandbox_escaped
from policy_data import (
    AGENT_IDENTITY_ENV,
    AUTONOMOUS_AGENT_IDENTITIES,
    DIAGNOSTICS_COMMAND,
)

# lup: ignore[subprocess] — `sh` is third-party and this half is compiled into a bare script that has no virtual environment to resolve it from
import subprocess
from kernel.edit import (
    awaits_resolution,
    decide_edit,
    relocated_edit_text,
    relocated_suppressions,
)
from kernel.fetch import decide_fetch
from kernel.lex import shell_path_verb_targets, shell_write_targets
from kernel.shell import decide_shell
from kernel.tools import decide_tool
from policy_data import (
    ACCEPTANCE_GUARD,
    ALLOWANCE_GRANTS_ENV,
    ALLOWED_FETCH_SCOPES,
    ANTI_PATTERN_ROWS,
    RESOLUTION_COMMAND,
    DENIED_FETCH_SCOPES,
    EDIT_RULES,
    KNOWN_ALLOWANCES,
    MAXIMUM_ADDED_LINES,
    PATH_ROLES,
    PATH_RULES,
    RECOVERABLE_TARGET_LIMIT,
    REFUSED_TOOLS,
    RUNNER_TARGET_TABLES,
    RUNNER_TARGETS,
    SANDBOX_EXCLUDED_COMMANDS,
    SHELL_RULES,
)


def sandbox_active() -> bool:
    """Whether the launcher confined this session to an OS sandbox."""
    environ = os.environ  # lup: ignore[os-environ]
    return "LUP_SANDBOX_ACTIVE" in environ and environ["LUP_SANDBOX_ACTIVE"] == "1"


def managed_script_roots(root: Path | None) -> list[str]:
    """Name the package roots a runtime installed and therefore trusts.

    The workspace-local plugin directory is deliberately not a root: it is
    agent-adjacent and verified only at launch, so an approved write there
    must not grant silent execution rights for the rest of the session.
    """
    if root is None or not root.is_absolute():
        return []
    return [str(root / "skills"), str(root / "plugins" / "cache")]


def worktree_path(path_text: str) -> str:
    """Relativize against the worktree holding the path, not the cwd.

    Every repo-relative rule — human-owned files, protected directories, the
    role a path carries — matches on this answer, so anchoring it anywhere
    but the file's own worktree decides policy by where the runtime happened
    to be launched. A sibling worktree is writable and is not under the
    launch directory; the cwd this script is promised nothing about is not a
    root at all. Both leave the path absolute, and every rule silently misses.
    """
    root = worktree_root(path_text)
    if not root:
        return path_text
    return Path(path_text).resolve().relative_to(root).as_posix()


def worktree_root(path_text: str) -> str:
    """The checkout a path belongs to, or "" when it belongs to none.

    The same walk :func:`worktree_path` relativizes against, kept whole
    rather than discarded, because the root is the answer to a second
    question: a language server asked about this file resolves its imports
    against exactly this directory, and resolving them against wherever the
    session was launched reads the same module names out of another tree.
    """
    path = Path(path_text)
    if not path.is_absolute():
        return ""
    resolved = path.resolve()
    # The path itself is a candidate, not only its parents: a file never
    # holds a `.git`, so nothing changes for one, and a directory that is
    # already a checkout root would otherwise be answered for by whatever
    # encloses it — or by nothing at all.
    for root in [resolved, *resolved.parents]:
        if (root / ".git").exists():
            return str(root)
    return ""


def shared_git_directory(path_text: str) -> str:
    """The one directory every worktree of a repository can name alike.

    The publisher sits in whichever checkout was edited and the reader in
    whichever one its server was launched from, and neither can see the
    other's. What they share is git's own directory: a linked worktree's
    ``.git`` is a file naming its per-worktree directory beneath the common
    one, and a main checkout's ``.git`` is that common directory. So both
    ends resolve to one place without either being told where the other is,
    and without depending on a variable reaching a hook and a server alike.

    The common directory rather than the main checkout, because a checkout
    is not guaranteed to be there: a repository can keep its git directory
    beside its worktrees rather than inside one, and then the path above
    `worktrees/` is not a checkout at all — it is whatever happens to
    enclose the repository, which is nobody's to write into.
    """
    root = worktree_root(path_text)
    if not root:
        return ""
    marker = Path(root) / ".git"
    if marker.is_dir():
        return str(marker)
    try:
        named = marker.read_text(encoding="utf-8")
    except OSError:
        return ""
    gitdir = named.removeprefix("gitdir:").strip()
    if not gitdir:
        return ""
    # `<common>/worktrees/<name>` — two levels up in either layout.
    linked = Path(gitdir)
    if len(linked.parents) < 2:
        return ""
    return str(linked.parents[1])


def publish_edition(path_text: str) -> None:
    """Say which checkout an edit landed in, for the servers that would guess.

    A language server and the code-intelligence tools are started once and
    hold the directory the session opened in for the rest of their lives.
    Editing moves — this project asks that it move, into a worktree — and
    nothing about that reaches them, so they answer about the launch tree
    with no sign that they have. The edited file is the one thing that knows
    where editing is happening, and this is the only place that sees it.

    Written after the tool ran, never before: the same call from the
    permission path would put a filesystem failure between an edit and its
    verdict, and this must not be able to decide anything. For the same
    reason an unwritable destination is reported and dropped — a session
    whose diagnostics stay rooted where they were is worth strictly more
    than one that stopped editing over it.

    The rename is the whole guarantee, and the temporary is dot-prefixed, for
    the reasons ``lup.channels.models.write_atomic`` gives. This cannot call
    that one: it is compiled into a bare script with no ``lup`` to import.
    """
    root = worktree_root(path_text)
    shared = shared_git_directory(path_text)
    if not root or not shared:
        return
    destination = Path(shared) / "lup" / "edition.json"
    record = json.dumps(
        {"workspace": root, "file": str(Path(path_text).resolve())}, indent=2
    )
    temporary_path = destination.with_name(f".{destination.name}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(record + "\n", encoding="utf-8")
        temporary_path.replace(destination)
    except OSError as error:
        print(f"lup: could not publish the edition: {error}", file=sys.stderr)


def project_environment(
    root: Path,
    variable: str = "UV_PROJECT_ENVIRONMENT",
    default: str = ".venv",
) -> Path:
    """Where a sync puts *root*'s environment.

    ``.venv`` beside the manifest is `uv`'s default and this project's own
    layout, but it is a default rather than the answer: *variable* redirects
    it, which is how one environment gets shared across worktrees, kept off a
    slow filesystem, or placed where a container expects it. A relative value
    resolves against the project, the way `uv` resolves it, rather than
    against whatever directory a command happened to run in.

    Lives in this half because this is the constrained one: the hook is
    compiled to a bare script that may import nothing but the standard
    library, so logic it needs cannot sit anywhere it would have to import
    from. Everything else reads it from here, which is what keeps one answer
    rather than two that agree until they do not.

    Both names arrive as defaults rather than as module constants because the
    compiler that splices this half into each hook carries functions alone —
    a name declared beside one would be left behind, and the script would
    reference it undefined.
    """
    environ = os.environ  # lup: ignore[os-environ] — uv's own configuration
    declared = (environ[variable] if variable in environ else "").strip()
    if not declared:
        return root / default
    return Path(declared) if Path(declared).is_absolute() else root / declared


def declared_program(root: str, declared: str) -> str:
    """Where a declared program is, or "" when it is not there to run.

    The checkout answers first, whatever the spelling. That is what makes the
    verdict the edited tree's rather than whichever environment the session
    was launched from, and it is the property worth keeping — a sibling
    worktree holds the same relative path with different contents.

    Only where the checkout holds nothing does the spelling decide. A bare
    name goes to the OS to find on ``PATH``, for a project whose toolchain
    lives somewhere else entirely: a conda environment, a pyenv shim, a
    system or user-level install, an environment ``UV_PROJECT_ENVIRONMENT``
    put outside the project. A path that resolved to nothing stays nothing,
    because a project that named a location meant that location.

    Accepting only the first is what made this gate unavailable rather than
    configurable. A declared program it could not resolve produced no
    diagnostics and said nothing about why, so a project outside one layout
    did not get a weaker check — it got silence indistinguishable from a
    clean file, on every edit.

    A bare name is asked of the checkout's own environment before ``PATH``,
    because that is where a project's toolchain is installed and asking is
    what keeps the declaration from naming a layout. Spelling the path in
    would answer only for the layout it spelled: ``.venv`` is `uv`'s default
    and nothing else's, so a project that redirected it, or that installs
    through conda or pyenv, resolved to nothing and was gated in silence.
    The scripts directory comes from the running interpreter — ``bin`` on
    POSIX, ``Scripts`` on Windows — because that is a property of how Python
    is installed rather than of any project, and reading it is what keeps
    this from being a second layout assumption behind the one it replaces.
    """
    located = Path(root) / declared
    if located.is_file():
        return str(located)
    if "/" in declared or "\\" in declared:
        return ""
    installed = (
        project_environment(Path(root)) / Path(sys.executable).parent.name / declared
    )
    return str(installed) if installed.is_file() else declared


def conflicted(path_text: str) -> bool:
    """Whether a file is holding a merge open, and so is not source yet.

    Both markers, at the start of a line. A lone `<<<<<<<` is reachable in
    honest text — a diff quoted in a docstring, a fixture about conflicts, this
    very sentence — and answering yes to one would silence the checker for a
    file nothing is wrong with. A conflict always writes the pair, so requiring
    both costs nothing it was meant to catch.

    Unreadable is not conflicted. Whatever the reason, the checker is about to
    meet the same file and is the one that should say so.
    """
    try:
        lines = (
            Path(path_text).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return False
    return any(line.startswith("<<<<<<<") for line in lines) and any(
        line.startswith(">>>>>>>") for line in lines
    )


def file_diagnostics(
    path_text: str,
    command: list[str],
    suffixes: tuple[str, ...] = (".py", ".pyi"),
    timeout_seconds: float = 20.0,
) -> list[str]:
    """Type-check one edited file, in the checkout that actually holds it.

    A language server the runtime starts is rooted once, where the session
    opened, and goes on resolving imports there after work moves to another
    checkout — same module names, different source, diagnostics about a file
    nobody edited. Running the checker per edit has no root to go stale:
    the file names its own checkout, and that is where the check runs.

    The checkout alone does not decide it. A checker finds the interpreter
    whose packages it resolves against by looking down ``PATH``, and a hook
    inherits whichever one the session was launched with, so a check running
    in one tree reads another tree's installed packages and calls every
    third-party import unresolvable. The checker's own directory goes first:
    that is the environment it was installed into, and therefore the one
    belonging to the checkout that holds the file. A checker the checkout
    does not hold has no such directory to prefer, and keeps the ``PATH`` it
    inherited — that is where the OS is about to find it.

    Reported for the edited file alone. The checker resolves whatever the
    file imports, so it can have opinions about the whole tree, and a hook
    that repeated them would answer every edit with the same backlog.

    Anything that goes wrong is no diagnostics. A checker that is missing, or
    slow, or writes something this cannot read, is not evidence about the
    edit — and this runs after the tool, so the alternative to saying
    nothing is failing an edit that already happened.

    *suffixes* is what the checker can read. A type checker handed a manifest,
    a document, or a lockfile parses it as source and reports the whole file
    as broken, so every edit to one answers with a wall of errors about lines
    the edit never touched — which teaches a reader to scroll past the output
    that exists to be read. It is a default rather than a constant because the
    checker is the caller's choice: a project whose checker reads more than
    Python says so instead of editing this.

    A file mid-merge is the same case arriving from the other direction. Its
    conflict markers are not source in any language, so the checker reports the
    file as broken from the first one onward and every line it names is about
    the merge rather than about the edit — during a resolution, which is
    exactly when a reader is editing that file and has the least attention to
    spare for a wall of output that cannot be acted on.
    """
    if not command or Path(path_text).suffix.lower() not in suffixes:
        return []
    if conflicted(path_text):
        return []
    root = worktree_root(path_text)
    if not root:
        return []
    located = declared_program(root, command[0])
    if not located:
        return []
    edited = str(Path(path_text).resolve())
    environ = os.environ  # lup: ignore[os-environ] — the checker inherits this
    inherited = environ["PATH"] if "PATH" in environ else ""
    # Only a checker the checkout holds has an own directory to put first.
    # A bare name is about to be found on the PATH this inherits, so that
    # PATH is already the environment it belongs to.
    searched = (
        f"{Path(located).parent}{os.pathsep}{inherited}"
        if Path(located).is_absolute()
        else inherited
    )
    try:
        finished = subprocess.run(
            [located, *command[1:], edited],
            capture_output=True,
            text=True,
            cwd=root,
            env={**environ, "PATH": searched},
            timeout=timeout_seconds,
            check=False,
        )
        reported = json.loads(finished.stdout)["generalDiagnostics"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []
    return [
        f"{item['severity']} {item['range']['start']['line'] + 1}: {item['message']}"
        for item in reported
        if item["file"] == edited and item["severity"] != "information"
    ]


def resolved_refutations(
    path_text: str,
    proposed: str,
    command: list[str],
    timeout_seconds: float = 30.0,
) -> dict[str, list[int]] | None:
    """What a checker refutes in the text about to be written, or None.

    The kernel decides from primitive rows and reads nothing, which is what
    keeps a verdict a pure function of its inputs. Resolving a receiver's
    declaration is not a decision — it is a fact about the machine, the same
    kind this half already resolves — so it is answered here and passed in.

    Run in the checkout holding the file, like every other checker this half
    starts, and handed the proposed text on stdin: the change is judged before
    it is written, so the copy on disk is the one being replaced. *path_text*
    still names where the content belongs, because imports and the module's
    own name resolve against it and against nothing else.

    None where no answer was had — no declared resolver, none installed, a
    crash, a timeout, output that will not decode — and it has to stay
    distinct from an empty refutation. Empty means a checker looked and
    refuted nothing, which is evidence; None means nothing looked, which is
    the gate's cue to ask rather than refuse. Collapsing the two would turn
    every unresolvable session into a wall of confident denials.
    """
    if not command:
        return None
    root = worktree_root(path_text)
    if not root:
        return None
    located = declared_program(root, command[0])
    if not located:
        return None
    try:
        finished = subprocess.run(
            [located, *command[1:], "--path", str(Path(path_text).resolve())],
            capture_output=True,
            text=True,
            input=proposed,
            cwd=root,
            timeout=timeout_seconds,
            check=False,
        )
        reported = json.loads(finished.stdout)
        if not reported["resolved"]:
            return None
        return {rule: list(lines) for rule, lines in reported["refuted"].items()}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def existing_write_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which of a command's write targets already exist on disk.

    The kernel never reads the filesystem, so it cannot tell creating a file
    from overwriting one. Resolving that here keeps the decision itself a
    pure function of the command text and this list.
    """
    where = Path.cwd() if root is None else root
    return [target for target in targets if (where / target).exists()]


def git_answers(arguments: list[str], root: Path) -> list[str] | None:
    """One read-only Git query's lines, or None when Git cannot answer.

    Git missing, the path outside a repository, a malformed pathspec, and a
    non-zero exit all collapse to None, so a caller reading this as evidence
    that something is safe to destroy treats an unanswerable question as a no.
    """
    try:
        finished = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return finished.stdout.splitlines() if finished.returncode == 0 else None


def recoverable_write_targets(
    targets: list[str], root: Path | None = None
) -> list[str]:
    """Report which targets Git could restore byte for byte after a delete.

    Recoverable means tracked, carrying no uncommitted change, and a regular
    file: the object store then holds exactly what is on disk, so destroying
    it costs a checkout rather than any information.

    A directory is never reported, however clean everything beneath it is.
    One grant would otherwise cover a tree of unbounded size and depth, and
    the point of resolving this per path is that each grant stays the size of
    the thing named.
    """
    where = Path.cwd() if root is None else root
    return [
        target
        for target in targets
        if (where / target).is_file()
        and git_answers(["ls-files", "--error-unmatch", "--", target], where)
        is not None
        and git_answers(["status", "--porcelain", "--", target], where) == []
    ]


def empty_directory_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which targets are directories with nothing in them.

    An archive unpacked into one replaces nothing, whatever the archive
    holds — which is the only way to answer that without reading the archive
    itself. A path that is absent, a file, or unreadable is not reported, so
    an unanswerable question reads as "something is already there".
    """
    where = Path.cwd() if root is None else root
    found: list[str] = []
    for target in targets:
        path = where / target
        if not path.is_dir():
            continue
        try:
            if not any(path.iterdir()):
                found.append(target)
        except OSError:
            continue
    return found


def directory_write_targets(targets: list[str], root: Path | None = None) -> list[str]:
    """Report which of a command's targets are directories on disk.

    A refusal that can name this says which way out is open — remove the
    files it holds — rather than leaving the agent to guess why a delete it
    expected to pass did not.
    """
    where = Path.cwd() if root is None else root
    return [target for target in targets if (where / target).is_dir()]


def read_document(path_text: str) -> str | None:
    """Read a path's current text, or None when nothing is there yet."""
    path = Path(path_text)
    return path.read_text(encoding="utf-8") if path.exists() else None


def declared_identity(identity_env: str) -> str:
    """The identity this session's launcher declared, if it declared one.

    A hook payload need not carry an agent identity at all, so the
    environment is the only channel every launcher is guaranteed to have.
    """
    environ = os.environ  # lup: ignore[os-environ]
    return environ[identity_env] if identity_env in environ else ""


def document_allowances(document_text: str, known: list[str]) -> list[str]:
    """Edit gates one document grants, as it reads at this instant.

    Read here rather than carried in, because a grant is answered while the
    session it answers is already running: a list resolved when the process
    started can neither gain the gate a human just approved nor lose one they
    took back. The document is named from outside and its contents are read
    afresh, so the current state governs in both directions.

    Only names in the compiled vocabulary count. The document is a transport,
    not an authority: a name no launcher can legitimately publish — a typo, or
    a gate this policy never grants this way — is dropped rather than
    honoured, so hand-writing one buys nothing.

    Nothing to read is no grant, and so is anything unreadable: a missing
    document, a directory, a half-written replacement, a payload that is not a
    list of names. Every one of them leaves the gate exactly where it was, so
    the failure is a refusal a human can answer rather than a grant nobody
    made.
    """
    if not document_text:
        return []
    try:
        declared = json.loads(Path(document_text).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(declared, list):
        return []
    return [str(name) for name in declared if str(name) in known]


def granted_allowances(grants_env: str, known: list[str]) -> list[str]:
    """Edit gates a human approved for the lease this session is working.

    The environment names the document rather than carrying the grants, so
    what a hook process inherits at launch is a place to look and never an
    answer that has since moved on.
    """
    environ = os.environ  # lup: ignore[os-environ]
    return document_allowances(
        environ[grants_env] if grants_env in environ else "", known
    )


def bash_decision(
    command: str,
    managed_root: Path | None,
    sandboxed: bool,
    interactive: bool,
    escapable: bool,
    cwd: Path | None,
) -> KernelDecision:
    """Judge one shell command against the declared vocabulary.

    The kernel reads no filesystem, so every fact about the paths this command
    would touch is resolved here and passed as data: which of the paths it
    would write already exist, which operands Git could restore, and which
    are directories.

    Existence and recoverability both cover redirection targets and path-verb
    operands alike, because the questions they ask are the same ones —
    whether writing here brings something into being or replaces it, and what
    replacing it would cost. Resolving them for only one of the two writing
    forms is what left ``rm f`` granted while ``echo x > f`` asked about the
    same clean, tracked file.

    ``cwd`` is where the calling session is, which the command's relative
    operands resolve against. It is a parameter rather than a read of this
    process, because a hook is promised nothing about where it runs, and
    resolving a target against the wrong tree answers a different question.

    ``escapable`` is the one thing here a runtime answers rather than the host:
    whether it can put a single call outside its own sandbox. It arrives as an
    argument for the same reason the rest does — a fact one dispatcher stopped
    passing is a rule that silently stopped applying.
    """
    acted_on = shell_path_verb_targets(command)
    return decide_shell(
        command,
        SHELL_RULES,
        ALLOWED_FETCH_SCOPES,
        DENIED_FETCH_SCOPES,
        sandboxed=sandboxed,
        excluded_commands=SANDBOX_EXCLUDED_COMMANDS,
        trusted_script_roots=managed_script_roots(managed_root),
        path_roles=PATH_ROLES,
        path_rules=PATH_RULES,
        existing_targets=existing_write_targets(
            [*shell_write_targets(command), *acted_on], cwd
        ),
        recoverable_targets=recoverable_write_targets(
            [*shell_write_targets(command), *acted_on], cwd
        ),
        directory_targets=directory_write_targets(acted_on, cwd),
        empty_directories=empty_directory_targets(acted_on, cwd),
        recoverable_target_limit=RECOVERABLE_TARGET_LIMIT,
        runner_targets=RUNNER_TARGETS,
        target_tables=RUNNER_TARGET_TABLES,
        interactive=interactive,
        escapable=escapable,
    )


def fetch_decision(url: str) -> KernelDecision:
    """Judge one outbound fetch against the declared scopes."""
    return decide_fetch(url, ALLOWED_FETCH_SCOPES, DENIED_FETCH_SCOPES)


def refused_tool_decision(name: str, values: list[str]) -> KernelDecision | None:
    """Judge one native call against the calls this project refuses outright.

    ``None`` leaves the routing runtime's own answer for a tool no refusal
    mentions, because the table says what a project decided against and never
    what it approved — an unmentioned tool is still unclassified.
    """
    return decide_tool(name, values, REFUSED_TOOLS)


def placed_document(path_text: str, after: str) -> str:
    """One file's text with every suppression at its canonical placement.

    Only Python has a placement to settle here: the policy is written in terms
    of a comment the formatter cannot wrap, and the tokenizer that says where
    a comment really opens is Python's.
    """
    if Path(path_text).suffix.lower() not in (".py", ".pyi"):
        return after
    return relocated_suppressions(after)


def placed_edit_text(path_text: str, after: str, start: int, end: int) -> str | None:
    """The replacement for an edit's own span, or ``None`` to place nothing."""
    if Path(path_text).suffix.lower() not in (".py", ".pyi"):
        return None
    return relocated_edit_text(after, start, end)


def edit_decision(
    path_text: str,
    before: str | None,
    after: str | None,
    path_exists: bool,
    autonomous: bool,
    operation: str = "modify",
) -> KernelDecision:
    """Judge one file's before and after against the declared edit policy.

    The path is relativized against the worktree holding it rather than the
    directory the runtime started in, because every repo-relative rule matches
    on that answer and a session may be launched anywhere.

    The gates this lease holds are read here, per call, rather than resolved
    when the session started: a grant is answered by a human while the session
    that asked for it is still running, and one resolved at launch could not
    have carried the answer.

    A checker is started only where its answer decides something. The kernel
    is asked first, from the tree and the tables alone, whether this edit
    trips a rule whose verdict turns on a resolved declaration; almost none
    do, and those are judged for nothing. Only the rest pay for a language
    server, which is the difference between a gate that costs a second per
    edit and one that costs a second on the edits that need it.
    """
    suffix = Path(path_text).suffix.lower()
    python_source = suffix in (".py", ".pyi")
    rows = ANTI_PATTERN_ROWS[suffix] if suffix in ANTI_PATTERN_ROWS else []
    refuted = (
        resolved_refutations(path_text, after, RESOLUTION_COMMAND)
        if after is not None and awaits_resolution(before, after, rows, python_source)
        else None
    )
    return decide_edit(
        worktree_path(path_text),
        before,
        after,
        path_exists=path_exists,
        path_rules=PATH_RULES,
        antipattern_rows=rows,
        path_roles=PATH_ROLES,
        maximum_added_lines=MAXIMUM_ADDED_LINES,
        autonomous=autonomous,
        allowances=granted_allowances(ALLOWANCE_GRANTS_ENV, KNOWN_ALLOWANCES),
        python_source=python_source,
        acceptance_guard=ACCEPTANCE_GUARD,
        refuted=refuted,
        suffix=suffix,
        operation=operation,
        edit_rules=EDIT_RULES,
    )


def managed_root():
    """The root Claude Code installs and trusts packages beneath."""
    environ = os.environ  # lup: ignore[os-environ]
    if "CLAUDE_CONFIG_DIR" in environ:
        return Path(environ["CLAUDE_CONFIG_DIR"])
    if "HOME" in environ:
        return Path(environ["HOME"]) / ".claude"
    return None


def edit_documents(path, old_text, new_text, replace_all):
    """Build the before and after documents one Edit call would produce.

    A `replace_all` edit rewrites every occurrence, so requiring exactly one
    would reject the tool's own semantics — and a rejection here is not a
    judgment: it reaches the agent as an approval prompt that no rule
    produced, which is how a whole class of edit went ungoverned.
    """
    current = Path(path).read_text(encoding="utf-8")
    occurrences = current.count(old_text)
    if occurrences == 0:
        raise ValueError("Edit preimage does not occur in the file")
    if replace_all:
        # Reproducing the Edit tool's own splice: source text has no parser
        # here, and the preimage is a literal the caller already chose.
        spliced = current.replace(old_text, new_text)  # lup: ignore[string-replace]
        return current, spliced
    if occurrences != 1:
        raise ValueError("Edit preimage must occur exactly once")
    position = current.find(old_text)
    updated = current[:position] + new_text + current[position + len(old_text) :]
    return current, updated


def spent_escape(tool_input):
    """Whether the call as written already asked to run outside the sandbox.

    Claude Code's own spelling of the escape, read here rather than compared
    against in two places: what it means is the kernel's `sandbox_escaped`,
    which both this boundary and the in-process seam ask.
    """
    return (
        "dangerouslyDisableSandbox" in tool_input
        and tool_input["dangerouslyDisableSandbox"] is True
    )


def placed_input(payload):
    """The tool arguments that land the same edit with its directives placed.

    The correcting route rather than the refusing one: where a directive was
    written somewhere the placement policy does not keep it, the call goes out
    rewritten instead of coming back as a complaint, so nobody weighs a reason
    against a column count while writing one. This is what `ruff --add-noqa`
    does for its own directives, moved to the gate that already reads the
    edit.

    ``None`` says place nothing. An edit can only rewrite the text it supplies,
    so a move reaching outside that text declines rather than guesses — and a
    `replace_all` edit has no single span to read a move back out of.
    """
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    if name not in ("Edit", "Write"):
        return None
    path = tool_input["file_path"]
    if Path(path).suffix.lower() not in (".py", ".pyi"):
        return None
    if name == "Write":
        content = tool_input["content"]
        revised = placed_document(path, content)
        return None if revised == content else {**tool_input, "content": revised}
    if "replace_all" in tool_input and tool_input["replace_all"] is True:
        return None
    before, after = edit_documents(
        path, tool_input["old_string"], tool_input["new_string"], False
    )
    start = before.find(tool_input["old_string"])
    revised = placed_edit_text(
        path, after, start, start + len(tool_input["new_string"])
    )
    return None if revised is None else {**tool_input, "new_string": revised}


def dispatch(payload):
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    agent_type = payload["agent_type"] if "agent_type" in payload else ""
    autonomous = (
        agent_type in AUTONOMOUS_AGENT_IDENTITIES
        or declared_identity(AGENT_IDENTITY_ENV) in AUTONOMOUS_AGENT_IDENTITIES
    )
    if name == "Bash":
        unsandboxed = spent_escape(tool_input)
        return bash_decision(
            tool_input["command"],
            managed_root(),
            sandbox_active() and not unsandboxed,
            interactive=True,
            # A call's sandbox is an argument of the call here, so a verdict
            # that has to leave the sandbox is carried out rather than refused.
            escapable=True,
            cwd=Path(payload["cwd"]) if "cwd" in payload else None,
        )
    if name == "WebFetch":
        return fetch_decision(tool_input["url"])
    if name == "Edit":
        path = tool_input["file_path"]
        before, after = edit_documents(
            path,
            tool_input["old_string"],
            tool_input["new_string"],
            "replace_all" in tool_input and tool_input["replace_all"] is True,
        )
        return edit_decision(
            path, before, after, Path(path).exists(), autonomous, "modify"
        )
    if name == "Write":
        path = tool_input["file_path"]
        exists = Path(path).exists()
        return edit_decision(
            path,
            read_document(path),
            tool_input["content"],
            exists,
            autonomous,
            "overwrite" if exists else "create",
        )
    # Asked of whatever reached here rather than of a listed few: which tools
    # are worth refusing is the declaration's answer, and naming any of them
    # here would be this file holding a second, narrower copy of it. The
    # branches above keep their calls, which have semantics to be judged by.
    refused = refused_tool_decision(
        name, [value for value in tool_input.values() if isinstance(value, str)]
    )
    if refused is not None:
        return refused
    return KernelDecision("ask", "tool is not classified")


def rendered(decision, payload, placed):
    """Answer one call on the permission channel, and place it on the other.

    Claude Code takes a call's sandbox as an argument of the call rather than
    as part of the verdict, so a placed decision goes out as the permission
    decision plus a rewrite of the arguments — which is what makes an
    unprompted placement reachable at all. Three things in the runtime make
    that rewrite carry the flag rather than swallow it, each read out of the
    shipped Claude Code binary at version 2.1.228 — the baseline to re-check
    against, rather than a conclusion to remember: the PreToolUse hook
    schema types `updatedInput` as an open record of arbitrary keys;
    `dangerouslyDisableSandbox` is a declared field of the shell tool's own
    input schema, so it is not an unknown key for the schema validation a
    returned `updatedInput` has to pass; and the one per-tool key filter
    applied to it before execution is keyed by a table naming a different
    tool, so for the shell tool the object arrives whole and the sandbox is
    chosen from it. What remains outside this file's reach is the session
    itself: a host that forbids unsandboxed commands ignores the flag, and
    the call runs confined with the verdict unchanged.

    The rewrite replaces the arguments rather than merging into them, so the
    whole input is carried through. A deferral is placed nowhere, which is
    also why nothing here reads a payload a deferral may not have parsed.

    ``placed`` carries the other rewrite this channel can hand back: the same
    edit with its suppression directives at their canonical placement. It
    rides along with the verdict rather than replacing it — placing a
    directive settles where it is written and says nothing about whether the
    edit may happen, so an ask still asks, over the placed text, which is what
    the approver should be reading. A denied call runs nothing, so there is
    nothing to place. The two rewrites never contend for the one
    ``updatedInput`` field: a directive is placed only in an ``Edit`` or
    ``Write``, and the sandbox argument belongs only to ``Bash``.

    Both sandbox questions are answered yes here, from that one field: the
    rewrite is how a verdict places a call, and the same field on the call the
    agent writes is how the agent places its own — which is what an
    ``escalable`` verdict offers it. Both halves of that offer are decided in
    the kernel rather than spelled here: `escalation_offer` says whether it is
    extended, and `sandbox_escaped` whether a call that spent it still leaves.
    The in-process seam asks the same two, because one field two boundaries
    fill from two conditions is a field they can fill differently — which is
    how the rewrite once revoked an offer the permission channel had granted.
    """
    settled = decision.placed(escapable=True, agent_escalates=True)
    if settled.effect == "defer":
        return {}
    answer = {
        "hookEventName": "PreToolUse",
        "permissionDecision": settled.effect,
        "permissionDecisionReason": settled.reason,
    }
    offer = escalation_offer(settled.sandbox, settled.reason)
    if offer:
        answer["additionalContext"] = offer
    if placed is not None and settled.effect != "deny":
        return {"hookSpecificOutput": {**answer, "updatedInput": placed}}
    if settled.sandbox == "ambient" or payload["tool_name"] != "Bash":
        return {"hookSpecificOutput": answer}
    return {
        "hookSpecificOutput": {
            **answer,
            "updatedInput": {
                **payload["tool_input"],
                "dangerouslyDisableSandbox": sandbox_escaped(
                    settled.sandbox, spent_escape(payload["tool_input"])
                ),
            },
        }
    }


def observe(payload):
    """Record where an edit landed and type-check it, deciding nothing.

    Claude Code names the file the same way for both editing tools, so the
    one key is the whole reading. A payload without it is a call this event
    is registered for and has nothing to say about, which is not a failure —
    the matcher is narrow, but the runtime owns it, and a tool that stops
    carrying a path should cost a recorded edition rather than an error.
    """
    tool_input = payload["tool_input"] if "tool_input" in payload else {}
    path = tool_input["file_path"] if "file_path" in tool_input else ""
    if not path:
        return []
    publish_edition(path)
    return file_diagnostics(path, DIAGNOSTICS_COMMAND)


def main():
    payload = {}
    placed = None
    try:
        payload = json.load(sys.stdin)
        event = payload["hook_event_name"] if "hook_event_name" in payload else ""
        # Watching and deciding are separate events, and this one returns
        # before a verdict exists: the tool has already run, so there is
        # nothing left to permit, and the conservative ask below would be an
        # approval prompt for work already done.
        if event == "PostToolUse":
            found = observe(payload)
            # Exit 2 is the one channel this event has to the agent: the tool
            # already ran, so nothing is undone, and stdout on a clean exit
            # reaches a debug log nobody reads. Silence when the file checks
            # out, so the channel means something when it is used.
            if found:
                sys.stderr.write("\n".join(found))
                raise SystemExit(2)
            json.dump({}, sys.stdout)
            return
        decision = dispatch(payload)
        placed = placed_input(payload)
    # Every way this can fail means one thing — the call went unjudged — and
    # one answer is right for all of them. Naming the exceptions instead is
    # what let a plain unreadable file escape, and the traceback exit reaches
    # PreToolUse as a non-blocking error, so the call proceeded ungoverned.
    # Nothing is swallowed: the reason carries whatever went wrong, and an
    # interrupt still passes through as the BaseException it is.
    except Exception as error:
        decision = KernelDecision(
            "ask", f"Malformed hook input requires approval: {error}"
        )
    json.dump(rendered(decision, payload, placed), sys.stdout)


if __name__ == "__main__":
    main()
