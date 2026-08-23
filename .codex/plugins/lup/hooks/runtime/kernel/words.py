# lup: ignore[empty-collection, set-shape, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Word-level shell helpers: expansion safety, flags, and payloads."""

import posixpath
from typing import TypedDict

from .archives import archive_write
from .decision import KernelDecision, SUBSTITUTION_SENTINEL
from .edit import path_rule_matches
from .roles import path_role
from .rows import PathRoleRow, PathRuleRow


class EffectiveCommand(TypedDict):
    """The words the shell finally executes, and whether a binding was dangerous."""

    words: list[str]
    dangerous: bool


class VerbOperands(TypedDict):
    """A path verb's operands, and whether every flag among them was inert."""

    operands: list[str]
    inert: bool


# lup: ignore[library-default] — real wrappers that exec the argument after them
PASS_THROUGH_WORDS = (
    "env",
    "command",
    "exec",
    "time",
    "nohup",
    "setsid",
    "stdbuf",
)
# lup: ignore[library-default] — variables the shell and language runtimes read to redirect execution
DANGEROUS_ENV_NAMES = (
    "PATH",
    "IFS",
    "ENV",
    "TMPDIR",
    "BASH_ENV",
    "CDPATH",
    "SHELL",
    "HOME",
    "XDG_CONFIG_HOME",
    "PAGER",
    "MANPAGER",
    "EDITOR",
    "VISUAL",
    "NODE_OPTIONS",
    "PERL5LIB",
    "PERL5OPT",
    "RUBYLIB",
    "RUBYOPT",
)
# lup: ignore[library-default] — variable prefixes the OS, those runtimes, and these tools read to redirect execution or retarget a command
DANGEROUS_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHON", "GIT_", "GH_", "BASH_FUNC_")
# lup: ignore[library-default] — the native runtimes' own plugin directory names
GENERATED_PLUGIN_ROOTS = (".claude/plugins", ".codex/plugins")
# lup: ignore[library-default] — real interpreter executables; omitting one is a hole, not a preference
INTERPRETERS = (
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "deno",
    "bun",
    "php",
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "fish",
)


def timeout_payload(segment: list[str], position: int) -> int:
    """Skip timeout's own options and duration to its wrapped command."""
    value_options = ("-k", "--kill-after", "-s", "--signal")
    while position < len(segment) and segment[position].startswith("-"):
        option = segment[position]
        position += 2 if option in value_options else 1
    return position + 1


def nice_payload(segment: list[str], position: int) -> int:
    """Skip nice's adjustment options to its wrapped command."""
    while position < len(segment) and segment[position].startswith("-"):
        option = segment[position]
        position += 2 if option == "-n" else 1
    return position


def effective_command(segment: list[str]) -> EffectiveCommand:
    """Skip assignments and transparent wrappers, noting dangerous assignments.

    Wrappers that take values (``timeout 5``, ``nice -n 10``) consume them, so
    the returned words start at the command the shell finally executes. A bare
    ``env`` with nothing to wrap is itself the command, and ``command -v`` asks
    where a program is rather than running one, so it is the command too.
    """
    dangerous = False
    position = 0
    while position < len(segment):
        word = segment[position]
        name, separator, _value = word.partition("=")
        if separator and name.isidentifier():
            dangerous = dangerous or dangerous_env_name(name)
            position += 1
            continue
        executable = posixpath.basename(word)
        if executable == "env" and position + 1 == len(segment):
            return EffectiveCommand(words=segment[position:], dangerous=dangerous)
        if executable == "command" and segment[position + 1 : position + 2] in (
            ["-v"],
            ["-V"],
        ):
            return EffectiveCommand(words=segment[position:], dangerous=dangerous)
        if executable in PASS_THROUGH_WORDS:
            position += 1
            continue
        if executable == "timeout":
            position = timeout_payload(segment, position + 1)
            continue
        if executable == "nice":
            position = nice_payload(segment, position + 1)
            continue
        return EffectiveCommand(words=segment[position:], dangerous=dangerous)
    return EffectiveCommand(words=[], dangerous=dangerous)


def command_words(words: list[str]) -> list[str]:
    """Skip assignments and transparent wrappers to the effective command."""
    return effective_command(words)["words"]


def uv_run_words(words: list[str]) -> list[str]:
    """Return the executable portion of a ``uv run`` invocation."""
    position = 2
    value_options = (
        "--directory",
        "--package",
        "--project",
        "--with",
        "--with-editable",
    )
    while position < len(words) and words[position].startswith("-"):
        option = words[position]
        if option in ("-c", "-m", "--script"):
            return words[position:]
        position += 2 if option in value_options else 1
    return words[position:]


# Every verb that acts on paths, paired with the short flags whose presence
# does not change what the verb does to them. A long flag or an unrecognized
# cluster falls through to the verb's own effect. Membership is about taking
# paths, not about asking: `mkdir` and `touch` are allowed and still listed,
# because the refusals that read this map — a write inside a generated plugin
# tree, above all — are owed by every verb that names a path.
# lup: ignore[library-default] — each verb's own POSIX flags, fixed by what the utility does rather than by who is asking
SCRATCH_VERB_FLAGS = {
    "rm": "rfv",
    "rmdir": "pv",
    "mv": "fnv",
    "cp": "aprRvL",
    "mkdir": "pv",
    "touch": "acm",
}


def is_trusted_script(word: str, roots: list[str]) -> bool:
    """Recognize an absolute script confined to a native-managed package root."""
    if "$" in word or not word.startswith("/"):
        return False
    normalized = posixpath.normpath(word)
    return any(
        normalized.startswith(posixpath.join(posixpath.normpath(root), ""))
        for root in roots
        if root.startswith("/") and posixpath.normpath(root) != "/"
    )


# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
GENERATED_PLUGIN_REFUSAL = (
    "a native plugin tree is compiled from typed source, and the running"
    " runtime already loaded it — edit the policy source, run"
    " `lup-devtools harness generate all`, then ask the user to restart"
    " claude or codex so the change takes effect"
)


def is_generated_plugin_target(word: str) -> bool:
    """Recognize a path confined to a native plugin tree the harness renders.

    Every file there is compiled from typed source, so writing one by hand
    edits a build product: the change is reverted by the next generation and
    never reaches the runtime that already loaded it. The roots stop at
    ``plugins`` because their parents also hold settings, trust state, and
    hand-written skills and commands that no generator can restore.

    The roots are matched as path segments wherever they occur, so an absolute
    spelling and a sibling worktree's tree are recognized too. A relaxation
    may safely decline to resolve those, because declining leaves the ask in
    place; a refusal that only knew the repo-relative spelling would instead
    fail open on the one form that reaches past this worktree.
    """
    segments = posixpath.normpath(word).split("/")
    return any(
        segments[index : index + len(parts)] == parts
        for parts in [root.split("/") for root in GENERATED_PLUGIN_ROOTS]
        for index in range(len(segments))
    )


def path_verb_operands(words: list[str]) -> VerbOperands:
    """A path verb's operands, and whether every flag among them was inert.

    An inert flag does not change what the verb does to its operands, so the
    operand list means what it reads. An unrecognized one can move the
    destination, add one, or change which paths are touched at all — which is
    why the two directions diverge on it: a caller granting something must
    decline outright, and a caller refusing something must widen to every
    operand rather than trust their positions.
    """
    allowed = SCRATCH_VERB_FLAGS[posixpath.basename(words[0])]
    operands: list[str] = []
    inert = True
    for word in words[1:]:
        if word == "--":
            continue
        if word.startswith("-") and len(word) > 1:
            if word.startswith("--") or not all(
                letter in allowed for letter in word[1:]
            ):
                inert = False
            continue
        operands.append(word)
    return VerbOperands(operands=operands, inert=inert)


class RestoreOperands(TypedDict):
    """A ``git restore``'s source ref, if it named one, and the paths it rewrites.

    ``source`` is ``None`` for the index-sourced form, which is the one whose
    safety depends on whether those paths carry uncommitted work.
    """

    source: str | None
    paths: list[str]


def git_restore_operands(words: list[str]) -> RestoreOperands | None:
    """Split ``git restore`` into the ref it reads from and the paths it writes.

    ``None`` where the line is not a restore, carries a flag beyond the source
    and target selectors, or holds a word that expands at run time — each of
    which leaves the restore row's ask to answer for it, because a flag this
    does not know could move which paths are touched.
    """
    if len(words) < 3 or posixpath.basename(words[0]) != "git" or words[1] != "restore":
        return None
    source: str | None = None
    paths: list[str] = []
    position = 2
    while position < len(words):
        word = words[position]
        if word == "--source" and position + 1 < len(words):
            source = words[position + 1]
            position += 2
            continue
        if word.startswith("--source="):
            source = word[len("--source=") :]
            position += 1
            continue
        if word in ("--staged", "--worktree", "-S", "-W", "--"):
            position += 1
            continue
        if word.startswith("-"):
            return None
        paths.append(word)
        position += 1
    if not paths or (source is not None and source.startswith("-")):
        return None
    if opaque_argument(source or "") or any(opaque_argument(word) for word in paths):
        return None
    return RestoreOperands(source=source, paths=paths)


def written_operands(executable: str, operands: list[str]) -> list[str]:
    """The operands a path verb modifies, as opposed to the ones it reads.

    Copying reads every source and writes only the destination, so a path
    named as a source is an ordinary read however protected it is. Every other
    verb here removes or creates each path it is given.
    """
    if executable == "cp" and len(operands) > 1:
        return operands[-1:]
    return operands


def created_destination(
    executable: str,
    operands: list[str],
    existing_targets: list[str] | None,
    path_roles: list[PathRoleRow],
) -> str | None:
    """The operand a copy or move would bring into being, if it would.

    Both verbs write their last operand and read the rest, so a destination
    nothing occupies yet is a creation, and creating a file destroys nothing
    — the same reason a redirection to a fresh path is written freely. That
    is what leaves ``mv`` and ``rm`` agreeing about a tracked, clean file
    instead of the move asking where the delete did not.

    Destroying nothing is only half of it, because a create also *places*
    content, and the edit gate reads every line that enters production.
    Content already in production has passed it; content in a scratch root
    never did, and arriving by rename is how it would skip it. So a source
    there withholds the grant even though the destination is empty.

    ``existing_targets`` of ``None`` means no caller established anything, so
    every destination is treated as occupied. An expansion is never resolved:
    it names a different path at run time than the one that was stat'd.
    """
    if executable not in ("cp", "mv") or len(operands) < 2:
        return None
    destination = operands[-1]
    if existing_targets is None or destination in existing_targets:
        return None
    if opaque_argument(destination) or destination.startswith("~"):
        return None
    if path_role(destination, path_roles) != "scratch" and any(
        path_role(source, path_roles) == "scratch" for source in operands[:-1]
    ):
        return None
    return destination


def refuses_generated_plugin_target(word: str) -> KernelDecision | None:
    """Refuse one path that would write inside a generated plugin tree.

    Every writing form routes its targets here — a path verb's operands, a
    redirection's target — so the refusal and the reason it carries are
    written once and cannot drift between the paths that reach them.
    """
    if not is_generated_plugin_target(word):
        return None
    return KernelDecision("deny", GENERATED_PLUGIN_REFUSAL)


def refuses_generated_plugin_write(words: list[str]) -> KernelDecision | None:
    """Refuse a verb that would write inside a generated plugin tree.

    Every verb naming a path owes this, not only the ones the flag map
    models: an archive unpacked over a generated tree replaces it exactly as
    a copy would, and the regeneration that repairs it is the same one.
    """
    executable = posixpath.basename(words[0])
    archived = archive_write(words)
    if archived is not None:
        directory = archived["directory"]
        for word in [
            *archived["authored"],
            *archived["consumed"],
            *([directory] if directory is not None else []),
        ]:
            refused = refuses_generated_plugin_target(word)
            if refused is not None:
                return refused
        return None
    if executable not in SCRATCH_VERB_FLAGS:
        return None
    verb = path_verb_operands(words)
    operands = verb["operands"]
    inert = verb["inert"]
    targets = written_operands(executable, operands) if inert else operands
    for word in targets:
        refused = refuses_generated_plugin_target(word)
        if refused is not None:
            return refused
    return None


def asks_before_removing_a_directory(
    words: list[str],
    path_roles: list[PathRoleRow],
    directory_targets: list[str] | None = None,
) -> KernelDecision | None:
    """Ask before a verb destroys a directory, naming the way through.

    Git restores a file it tracks, so a delete confined to files is bounded by
    the files named. A directory is not: its size is whatever it happens to
    hold, nothing in the command says what that is, and untracked work inside
    it is restored by nothing. The ask says which route is open rather than
    leaving a refusal the agent can only guess at. A scratch root keeps its
    own grant, because there the tree is disposable by declaration.
    """
    executable = posixpath.basename(words[0])
    if executable not in ("rm", "mv"):
        return None
    operands = path_verb_operands(words)["operands"]
    named = [
        word
        for word in operands
        if word in (directory_targets or [])
        and path_role(word, path_roles) != "scratch"
    ]
    if not named:
        return None
    return KernelDecision(
        "ask",
        "removing a directory is never granted, because nothing in the command"
        " bounds what it holds — name the files instead, or approve this",
    )


def protected_write_target(
    targets: list[str], path_rules: list[PathRuleRow], path_exists: bool
) -> KernelDecision | None:
    """Ask before granting a write to a path the declared rules protect.

    Every grant below answers "what would destroying this cost" — nothing,
    for a scratch file; a checkout, for one Git can restore. That is the
    wrong question for a file protected by who owns it rather than by what
    it would cost to rebuild, and answering it anyway is how ``rm sync.json``
    and ``cp x README.md`` passed a gate the Edit tool stops. The rules are
    the edit gate's own, so the two cannot come to disagree about a path.

    ``path_exists`` is the caller's own established fact, because the rule
    kinds that fire only on a path that is not there yet — a new subtree, a
    new devtools module — mean the opposite thing when it is. A grant over a
    scratch or Git-clean operand has settled that it exists; a redirection
    knows from the targets the host stat'd.
    """
    for word in targets:
        matched = next(
            (row for row in path_rules if path_rule_matches(word, path_exists, row)),
            None,
        )
        if matched is not None:
            return KernelDecision("ask", matched["reason"])
    return None


def confined_to_recoverable_roots(
    words: list[str],
    path_roles: list[PathRoleRow],
    recoverable_targets: list[str] | None = None,
    recoverable_target_limit: int = 5,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
) -> KernelDecision | None:
    """Recognize a path-taking judged-ask verb whose every target is disposable.

    A scratch role names a tree of disposable files, so destroying one is as
    safe as writing it and creating one there settles nothing. A path the host
    reports as recoverable is committed with no uncommitted change, so
    destroying it costs a checkout rather than any information — the host
    establishes that, because the kernel never reads the filesystem. Only the
    operands the verb writes are judged, so copying any source into a scratch
    root is as disposable as the root it lands in. A long flag, an opaque
    word, or a single written target outside those roots falls through to the
    verb's ask, so a mixed command still asks.

    The two grants are bounded differently because what backs them differs. A
    scratch root is disposable by declaration, so emptying one is one act
    whatever it holds. Restoring committed work is instead a repair somebody
    has to know to perform, so that grant is capped: past the limit a delete
    is a sweep, and a sweep is worth a question even when every file in it
    could be brought back.

    A destination nothing occupies yet is neither: it is brought into being,
    and the cap does not reach it because there is nothing there to restore
    — provided the content arriving there already lives in production, where
    the edit gate has read it.
    """
    executable = posixpath.basename(words[0])
    if executable not in SCRATCH_VERB_FLAGS:
        return None
    verb = path_verb_operands(words)
    operands = verb["operands"]
    inert = verb["inert"]
    if not inert or not operands:
        return None
    targets = written_operands(executable, operands)
    created = created_destination(executable, operands, existing_targets, path_roles)
    disposable = [word for word in targets if path_role(word, path_roles) == "scratch"]
    restorable = [
        word
        for word in targets
        if word not in disposable and word in (recoverable_targets or [])
    ]
    fresh = [word for word in targets if word == created and word not in disposable]
    if len(disposable) + len(restorable) + len(fresh) != len(targets):
        return None
    if len(restorable) > recoverable_target_limit:
        return None
    protected = protected_write_target(targets, path_rules or [], True)
    if protected is not None:
        return protected
    return KernelDecision("allow", "confined to recoverable roots")


def archive_lands_on_nothing(
    words: list[str],
    path_roles: list[PathRoleRow],
    recoverable_targets: list[str] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
) -> KernelDecision | None:
    """Grant an archive or compression verb that would replace nothing.

    These verbs ask because they place content over whatever stands there.
    Where nothing stands there, the ask is answering a question that has no
    second side: unpacking into a directory that does not exist, or into one
    holding nothing, destroys nothing, and neither does authoring an archive
    at a path nothing occupies.

    A named file is judged exactly as a delete's operand is — disposable by
    role, restorable from Git, or not yet there — because the cost of
    replacing it is the same cost whichever verb does the replacing. That is
    what leaves ``gzip`` and ``rm`` agreeing about a committed, unmodified
    file instead of one asking where the other does not.

    A destination directory cannot be judged that way, because what would
    land in it comes from the archive rather than from the command. So it is
    judged by being empty: nothing there is nothing to replace, whatever the
    archive turns out to hold. Everything the extraction then writes is new,
    and a later edit to any of it passes the edit gate on its own path.

    ``None`` wherever the answer is not established — an unmodelled line, an
    expansion that names a different path at run time, a caller that resolved
    no filesystem facts — and ``None`` leaves the verb's own ask standing.
    """
    write = archive_write(words)
    if write is None:
        return None
    authored, consumed = write["authored"], write["consumed"]
    directory = write["directory"]
    named = [*authored, *consumed, *([directory] if directory is not None else [])]
    # An extraction that named no destination unpacks where it stands, which
    # is the repository itself — certain to be occupied, and the one place an
    # unread archive should never land without a question.
    if not named:
        return None
    if any(opaque_argument(word) or word.startswith("~") for word in named):
        return None
    for word in authored:
        if path_role(word, path_roles) == "scratch":
            continue
        if existing_targets is not None and word not in existing_targets:
            continue
        if word in (recoverable_targets or []):
            continue
        return None
    # A consumed path is destroyed rather than created, so being absent is
    # not the licence it is above: it is a fact the host could not establish,
    # and for a destructive verb an unestablished fact is a question.
    for word in consumed:
        if path_role(word, path_roles) == "scratch":
            continue
        if word in (recoverable_targets or []):
            continue
        return None
    if directory is not None and path_role(directory, path_roles) != "scratch":
        if existing_targets is None:
            return None
        if directory in existing_targets and directory not in (empty_directories or []):
            return None
    protected = protected_write_target(named, path_rules or [], True)
    if protected is not None:
        return protected
    return KernelDecision("allow", "archive lands where nothing stands")


def dangerous_env_name(name: str) -> bool:
    """Recognize an environment variable that can redirect a command's execution."""
    return name in DANGEROUS_ENV_NAMES or any(
        name.startswith(prefix) for prefix in DANGEROUS_ENV_PREFIXES
    )


def flag_matches(word: str, flags: list[str]) -> bool:
    """Match a word against a rule's ask-flags, allowing clusters and ``=`` forms."""
    for flag in flags:
        if word == flag:
            return True
        if flag.startswith("--") and word.startswith(flag + "="):
            return True
        if (
            len(flag) == 2
            and flag.startswith("-")
            and word.startswith("-")
            and not word.startswith("--")
            and flag[1] in word[1:]
        ):
            return True
    return False


def opaque_argument(word: str) -> bool:
    """A word whose runtime expansion could inject a guarded flag.

    A substitution sentinel anywhere in the word marks it: the substitution's
    output word-splits at expansion, so even a mid-word result can become new
    words.
    """
    if word.startswith("$") or SUBSTITUTION_SENTINEL in word:
        return True
    return "}" in word and ("{-" in word or ",-" in word)


HELP_UNSAFE = set("/=$*?~<>|&;`\\'\" \t\n")


def is_help_probe(arguments: list[str], unsafe: set[str] = HELP_UNSAFE) -> bool:
    """Recognize an invocation that only prints usage.

    ``--help`` is inert wherever it sits among plain subcommand words, so
    an unclassified command is still readable through it. Bare ``-h`` counts
    only when it stands alone, because several commands spend it on a value
    (``mysql -h host``) rather than on help.
    """
    if not arguments:
        return False
    if any(character in unsafe for word in arguments for character in word):
        return False
    if arguments == ["-h"]:
        return True
    return "--help" in arguments


def xargs_payload(words: list[str]) -> list[str]:
    """Return the command xargs would run, skipping only xargs's own options."""
    value_options = ("-I", "-i", "-n", "-d", "-P", "-s", "-L", "-a", "-E", "-e")
    position = 1
    while position < len(words) and words[position].startswith("-"):
        option = words[position]
        if "=" in option or (len(option) > 2 and not option.startswith("--")):
            position += 1
        elif option in value_options:
            position += 2
        else:
            position += 1
    return words[position:]
