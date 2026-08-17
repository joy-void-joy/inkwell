# lup: ignore[empty-collection, import-re, re-call, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Shell segment, structure, and whole-command classification."""

import posixpath
import re
from typing import TypedDict

from .decision import (
    ESCALATE_HINT,
    KernelDecision,
    RESHAPE_HINT,
    SANDBOX_TRAPPED_REASON,
    SUBSTITUTION_SENTINEL,
    SandboxPlacement,
    unjudged,
)
from .rows import (
    PathRoleRow,
    PathRuleRow,
    RunnerTargetRow,
    ShellRuleRow,
    UrlScopeRow,
)
from .words import (
    INTERPRETERS,
    asks_before_removing_a_directory,
    command_words,
    dangerous_env_name,
    effective_command,
    is_help_probe,
    is_trusted_script,
    opaque_argument,
    archive_lands_on_nothing,
    confined_to_recoverable_roots,
    refuses_generated_plugin_write,
    xargs_payload,
)
from .lex import parse_shell_words
from .commands import (
    decide_awk_words,
    decide_command_rows,
    decide_curl_words,
    decide_gh_api_words,
    decide_sed_words,
    decide_uv,
    git_checkout_pathspec,
    git_restore_source,
    git_restore_unchanged,
    git_symbolic_ref_read,
)

ESCALATE_RE = re.compile(
    r"^\s*#[ \t]*lup[ \t]*:[ \t]*escalate\b[ \t]*:?[ \t]*(?P<why>[^\n]*)(?:\n|$)",
    re.IGNORECASE,
)


class ShellContext(TypedDict):
    """The declarations and host facts every segment is judged against.

    Each value is threaded unchanged through the whole recursion, so carrying
    them as one bundle is what makes a construct that forgets one impossible
    to write: a loop body, a conditional branch, and a ``find -exec`` payload
    are judged against exactly what the top-level command was.
    """

    rows: list[ShellRuleRow]
    allowed_scopes: list[UrlScopeRow]
    denied_scopes: list[UrlScopeRow]
    trusted_script_roots: list[str]
    path_roles: list[PathRoleRow]
    path_rules: list[PathRuleRow]
    existing_targets: list[str] | None
    recoverable_targets: list[str]
    directory_targets: list[str]
    empty_directories: list[str]
    recoverable_target_limit: int
    runner_targets: list[RunnerTargetRow]
    target_tables: list[ShellRuleRow]


def shell_context(
    rows: list[ShellRuleRow],
    allowed_scopes: list[UrlScopeRow] | None = None,
    denied_scopes: list[UrlScopeRow] | None = None,
    trusted_script_roots: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
    recoverable_targets: list[str] | None = None,
    directory_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
    recoverable_target_limit: int = 5,
    runner_targets: list[RunnerTargetRow] | None = None,
    target_tables: list[ShellRuleRow] | None = None,
) -> ShellContext:
    """Bundle one classification's declarations, normalizing absent lists.

    ``existing_targets`` keeps its ``None``, because that is a fact about the
    caller rather than an empty list of paths: nothing was established, so
    every write target is treated as already there.
    """
    return ShellContext(
        rows=rows,
        allowed_scopes=allowed_scopes or [],
        denied_scopes=denied_scopes or [],
        trusted_script_roots=trusted_script_roots or [],
        path_roles=path_roles or [],
        path_rules=path_rules or [],
        existing_targets=existing_targets,
        recoverable_targets=recoverable_targets or [],
        directory_targets=directory_targets or [],
        empty_directories=empty_directories or [],
        recoverable_target_limit=recoverable_target_limit,
        runner_targets=runner_targets or [],
        target_tables=target_tables or [],
    )


def decide_find_words(words: list[str], context: ShellContext) -> KernelDecision:
    """Classify find, recursing into -exec payloads with {} as a path word.

    Expansions of ``{}`` inherit find's ``./``-prefixed paths, so the payload
    is judged with a literal path word in each placeholder position. The
    interactive ``-ok`` forms would hang a non-interactive shell.
    """
    remaining = [words[0]]
    position = 1
    while position < len(words):
        word = words[position]
        if word in ("-ok", "-okdir"):
            return KernelDecision(
                "deny", "find -ok prompts on a tty — use -exec instead"
            )
        if word in ("-exec", "-execdir"):
            terminator = next(
                (
                    index
                    for index in range(position + 1, len(words))
                    if words[index] in (";", "+")
                ),
                None,
            )
            if terminator is None:
                return unjudged("find -exec payload does not terminate")
            payload = [
                "./x" if piece == "{}" else piece
                for piece in words[position + 1 : terminator]
            ]
            if not payload:
                return unjudged("find -exec payload is empty")
            verdict = decide_shell_segment(payload, context)
            if verdict.effect != "allow":
                return verdict
            position = terminator + 1
            continue
        remaining.append(word)
        position += 1
    return decide_command_rows(remaining, context["rows"])


def decide_shell_segment(segment: list[str], context: ShellContext) -> KernelDecision:
    """Classify one parsed shell segment against the vocabulary and handlers."""
    while segment and segment[0] == "!":
        segment = segment[1:]
    if not segment:
        return unjudged("shell segment has no command")
    if segment[0] == "[[":
        return KernelDecision("allow", "test expression is read-only")
    effective = effective_command(segment)
    words = effective["words"]
    dangerous = effective["dangerous"]
    if dangerous:
        return KernelDecision(
            "ask", "a security-sensitive environment assignment requires approval"
        )
    if not words:
        return unjudged("shell segment has no command")
    if SUBSTITUTION_SENTINEL in words[0]:
        return unjudged("a command substitution in command position is not classified")
    if any(
        SUBSTITUTION_SENTINEL in word for word in words[1:]
    ) and not argument_safe_words(words, context):
        return unjudged(
            "a command substitution result could become a guarded flag — run"
            " it in its own call and splice the literal output"
        )
    executable = posixpath.basename(words[0])
    if is_help_probe(words[1:]):
        return KernelDecision("allow", "a help probe only prints usage")
    if executable in INTERPRETERS:
        if len(words) > 1 and is_trusted_script(
            words[1], context["trusted_script_roots"]
        ):
            return KernelDecision("allow", "native-managed skill script")
        return KernelDecision(
            "deny", "bare interpreters and inline code are not allowed"
        )
    if executable == "git" and any("ext::" in word for word in words):
        return KernelDecision(
            "ask", "the git ext transport can execute commands — requires approval"
        )
    if executable == "git":
        recognized = (
            git_checkout_pathspec(words)
            or git_restore_source(words)
            or git_restore_unchanged(
                words, context["recoverable_targets"], context["path_rules"]
            )
            or git_symbolic_ref_read(words)
        )
        if recognized is not None:
            return recognized
    refused = refuses_generated_plugin_write(words)
    if refused is not None:
        return refused
    recoverable = confined_to_recoverable_roots(
        words,
        context["path_roles"],
        context["recoverable_targets"],
        context["recoverable_target_limit"],
        context["path_rules"],
        context["existing_targets"],
    )
    if recoverable is not None:
        return recoverable
    landed = archive_lands_on_nothing(
        words,
        context["path_roles"],
        context["recoverable_targets"],
        context["path_rules"],
        context["existing_targets"],
        context["empty_directories"],
    )
    if landed is not None:
        return landed
    directory = asks_before_removing_a_directory(
        words, context["path_roles"], context["directory_targets"]
    )
    if directory is not None:
        return directory
    if executable == "xargs":
        payload = xargs_payload(words)
        if not payload:
            return unjudged("xargs payload is not classified")
        return decide_shell_segment(payload, context)
    if executable == "curl":
        return decide_curl_words(
            words, context["allowed_scopes"], context["denied_scopes"]
        )
    if executable == "gh" and len(words) > 1 and words[1] == "api":
        return decide_gh_api_words(words)
    if executable == "find":
        return decide_find_words(words, context)
    if executable == "sed":
        return decide_sed_words(words)
    if executable in ("awk", "gawk", "mawk"):
        return decide_awk_words(words)
    if executable == "uvx":
        if len(words) > 1 and posixpath.basename(words[1]) in INTERPRETERS:
            return KernelDecision("deny", "inline code is not allowed")
        return unjudged("uvx command is not classified")
    if executable == "uv" and len(words) > 1:
        return decide_uv(words, context["runner_targets"], context["target_tables"])
    return decide_command_rows(words, context["rows"])


def loop_leader(segment: list[str]) -> str:
    """The segment's effective first word, looking through a leading ``do``."""
    if segment and segment[0] == "do" and len(segment) > 1:
        return segment[1]
    return segment[0] if segment else ""


def find_loop_end(segments: list[list[str]], start: int) -> int | None:
    """Locate the bare ``done`` segment closing the loop opened at ``start``."""
    depth = 1
    for index in range(start + 1, len(segments)):
        if segments[index] == ["done"]:
            depth -= 1
            if depth == 0:
                return index
        elif loop_leader(segments[index]) in ("for", "while", "until"):
            depth += 1
    return None


def variable_reference_end(word: str, position: int, name: str) -> int | None:
    """The index just past a ``$name``/``${name}`` reference at ``position``."""
    rest = word[position + 1 :]
    if rest.startswith("{" + name + "}"):
        return position + len(name) + 3
    if rest.startswith(name):
        follow = position + 1 + len(name)
        if follow >= len(word) or not (word[follow].isalnum() or word[follow] == "_"):
            return follow
    return None


def references_variable(word: str, name: str) -> bool:
    """Detect a live ``$name`` or ``${name}`` reference inside one shell word."""
    return any(
        character == "$" and variable_reference_end(word, position, name) is not None
        for position, character in enumerate(word)
    )


def substitute_variable(word: str, name: str, value: str) -> str:
    """Replace every ``$name``/``${name}`` reference in one word with ``value``."""
    pieces: list[str] = []
    position = 0
    while True:
        found = word.find("$", position)
        if found == -1:
            pieces.append(word[position:])
            return "".join(pieces)
        end = variable_reference_end(word, found, name)
        if end is None:
            pieces.append(word[position : found + 1])
            position = found + 1
            continue
        pieces.append(word[position:found])
        pieces.append(value)
        position = end


def literal_loop_word(word: str) -> bool:
    """A word whose runtime expansion is exactly its lexed text."""
    return not word.startswith(("~", "/dev/fd/")) and not any(
        character in "$*?[" for character in word
    )


def uv_post_target_words_safe(
    words: list[str], runner_targets: list[RunnerTargetRow]
) -> bool:
    """True when every unknown word sits strictly after a blessed uv run target.

    uv stops parsing its own options at the first positional word, so a word
    after a literal blessed target only ever reaches that target's argv —
    the trust literal arguments already receive there. An unknown word at or
    before the target could become a uv flag, a flag value, or the target
    itself, so any such word keeps the conservative gate.
    """
    if len(words) < 3 or words[1] != "run":
        return False
    for word in words[2:]:
        if opaque_argument(word):
            return False
        if word.startswith("-"):
            continue
        return "/" not in word and any(row["name"] == word for row in runner_targets)
    return False


def argument_safe_words(words: list[str], context: ShellContext) -> bool:
    """True when the command's row allows regardless of argument content.

    A loop variable bound to a non-literal word list can expand to any word,
    including a flag-shaped one, so only a single unguarded command-level
    allow row qualifies — flag-guarded rows and the specially parsed
    executables do not. ``uv run`` is the carved-out exception: unknown
    words strictly behind a literal blessed target are inert.
    """
    executable = posixpath.basename(words[0])
    if executable == "uv":
        return uv_post_target_words_safe(words, context["runner_targets"])
    if executable in INTERPRETERS or executable in ("sed", "git", "uvx", "xargs"):
        return False
    matches = [row for row in context["rows"] if row["command"] == executable]
    return (
        len(matches) == 1
        and not matches[0]["subcommand"]
        and matches[0]["effect"] == "allow"
        and not matches[0]["ask_flags"]
    )


class ShellBinding(TypedDict):
    """One frozen variable binding: a name, and its literal value or None.

    ``value`` is ``None`` where the word could not be read as a literal, which
    is what makes the binding opaque to every later substitution.
    """

    name: str
    value: str | None


def bind_name(
    bindings: tuple[ShellBinding, ...], name: str, value: str | None
) -> tuple[ShellBinding, ...]:
    """Rebind one name immutably, shadowing any earlier binding of it."""
    kept = tuple(pair for pair in bindings if pair["name"] != name)
    return (*kept, ShellBinding(name=name, value=value))


def pure_assignment_names(segment: list[str]) -> list[ShellBinding] | None:
    """The bindings of an assignment-only segment."""
    pairs: list[ShellBinding] = []
    for word in segment:
        name, separator, value = word.partition("=")
        if not separator or not name.isidentifier():
            return None
        pairs.append(
            ShellBinding(name=name, value=value if literal_loop_word(value) else None)
        )
    return pairs


def read_bindings(
    words: list[str], bindings: tuple[ShellBinding, ...]
) -> tuple[ShellBinding, ...] | KernelDecision:
    """Bindings extended by the read builtin's targets as opaque values."""
    names: list[str] = []
    for word in words[1:]:
        if word == "-r":
            continue
        if word.startswith("-"):
            return unjudged(f"read option {word!r} is not classified")
        if not word.isidentifier():
            return unjudged("read target is not a plain variable")
        names.append(word)
    for name in names or ["REPLY"]:
        if dangerous_env_name(name):
            return KernelDecision(
                "ask", "binding a security-sensitive variable requires approval"
            )
        bindings = bind_name(bindings, name, None)
    return bindings


def resolve_segment_bindings(
    segment: list[str],
    bindings: tuple[ShellBinding, ...],
    context: ShellContext,
    gate_opaque: bool,
) -> list[str] | KernelDecision:
    """Substitute literal bindings and gate opaque ones by argument safety.

    A literal binding instantiates its references exactly, so guarded flags
    are judged as the words they become. An opaque binding (``read``, a
    non-literal assignment) can expand to any word, so a referencing segment
    must name an argument-safe command.
    """
    resolved = segment
    for binding in bindings:
        name = binding["name"]
        value = binding["value"]
        if not any(references_variable(word, name) for word in resolved):
            continue
        if value is not None:
            resolved = [substitute_variable(word, name, value) for word in resolved]
            continue
        if not gate_opaque:
            continue
        words = command_words(resolved)
        if not words or not argument_safe_words(words, context):
            return unjudged("an opaquely bound variable could become a guarded flag")
    return resolved


def decide_for_body(
    name: str,
    loop_words: list[str],
    body: list[list[str]],
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...] = (),
) -> list[KernelDecision]:
    """Classify a ``for`` body once per literal loop word, or gated when opaque.

    A literal word list instantiates the body exactly, so a word landing in a
    guarded flag position is judged as the flag it becomes. A non-literal list
    (globs, expansions) can become any word, so every segment referencing the
    variable must name an argument-safe command before one placeholder pass.
    """
    if dangerous_env_name(name):
        return [
            KernelDecision(
                "ask",
                "a security-sensitive environment assignment requires approval",
            )
        ]
    if len(loop_words) > 16:
        return [unjudged("loop word list is too long to instantiate")]

    def instantiations(values: list[str]) -> list[KernelDecision]:
        return [
            decision
            for value in values
            for decision in decide_segment_list(
                [
                    [substitute_variable(word, name, value) for word in segment]
                    for segment in body
                ],
                context,
                depth + 1,
                bindings,
            )
        ]

    if all(literal_loop_word(word) for word in loop_words):
        return instantiations(loop_words or ["x"])
    for segment in body:
        if any(references_variable(word, name) for word in segment):
            words = command_words(segment)
            if not words or not argument_safe_words(words, context):
                return [
                    unjudged(
                        "loop words are not literal, so a variable argument"
                        " could become a guarded flag"
                    )
                ]
    return instantiations(["x"])


class Group(TypedDict):
    """What one compound construct decided, and the segment to resume at."""

    decisions: list[KernelDecision]
    resume: int


def decide_loop(
    segments: list[list[str]],
    start: int,
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...] = (),
) -> Group | KernelDecision:
    """Classify one loop construct, returning its decisions and the next index.

    A while/until condition and body classify as one sequential list, so a
    ``read`` in the condition binds for the body without shared mutation.
    """
    if depth >= 2:
        return unjudged("loops nest too deeply")
    end = find_loop_end(segments, start)
    if end is None:
        return unjudged("loop construct is never closed by `done`")
    interior = segments[start + 1 : end]
    do_index = next(
        (position for position, seg in enumerate(interior) if seg[0] == "do"), None
    )
    if do_index is None:
        return unjudged("loop construct has no `do` introducing its body")
    body = [seg for seg in [interior[do_index][1:], *interior[do_index + 1 :]] if seg]
    if not body:
        return unjudged("loop body is empty")
    condition = interior[:do_index]
    match segments[start]:
        case ["for", name, "in", *loop_words] if name.isidentifier():
            if condition:
                return unjudged("a `for` loop cannot carry a condition")
            return Group(
                decisions=decide_for_body(
                    name, loop_words, body, context, depth, bindings
                ),
                resume=end + 1,
            )
        case ["for", *_rest]:
            return unjudged("loop form is not classified")
        case [_keyword, *condition_head]:
            conditions = [seg for seg in [condition_head, *condition] if seg]
            if not conditions:
                return unjudged("loop condition is empty")
            decisions = decide_segment_list(
                [*conditions, *body], context, depth + 1, bindings
            )
            return Group(decisions=decisions, resume=end + 1)
    return unjudged("loop construct does not parse")


# lup: ignore[library-default] — POSIX shell `case` clause terminators
CASE_TERMINATORS = (";;", ";&", ";;&")


def strip_structure_keywords(segment: list[str]) -> list[str]:
    """Drop leading conditional keywords — pure structure, never commands."""
    index = 0
    while index < len(segment) and segment[index] in ("then", "else", "elif"):
        index += 1
    return segment[index:]


def find_conditional_end(segments: list[list[str]], start: int) -> int | None:
    """Locate the bare ``fi`` segment closing the conditional at ``start``."""
    depth = 1
    for index in range(start + 1, len(segments)):
        if segments[index] == ["fi"]:
            depth -= 1
            if depth == 0:
                return index
        else:
            stripped = strip_structure_keywords(segments[index])
            if stripped and stripped[0] == "if":
                depth += 1
    return None


def decide_conditional(
    segments: list[list[str]],
    start: int,
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...] = (),
) -> Group | KernelDecision:
    """Classify one ``if`` construct: conditions and branches recursively."""
    if depth >= 2:
        return unjudged("conditionals nest too deeply")
    end = find_conditional_end(segments, start)
    if end is None:
        return unjudged("conditional construct does not parse")
    interior: list[list[str]] = []
    for segment in [segments[start][1:], *segments[start + 1 : end]]:
        stripped = strip_structure_keywords(segment)
        if stripped:
            interior.append(stripped)
    if not interior:
        return unjudged("conditional is empty")
    return Group(
        decisions=decide_segment_list(interior, context, depth + 1, bindings),
        resume=end + 1,
    )


def decide_case(
    segments: list[list[str]],
    start: int,
    context: ShellContext,
    depth: int,
    bindings: tuple[ShellBinding, ...] = (),
) -> Group | KernelDecision:
    """Classify one ``case`` construct: patterns are match data, bodies recurse."""
    if depth >= 2:
        return unjudged("case constructs nest too deeply")
    opener = segments[start]
    if len(opener) < 3 or opener[2] != "in":
        return unjudged("case construct does not parse")
    body: list[list[str]] = []
    collecting = False
    nested = 0
    end = None
    for index in range(start + 1, len(segments)):
        segment = segments[index]
        if nested == 0 and segment == ["esac"]:
            end = index
            break
        if nested == 0 and segment == [")"]:
            collecting = True
        elif nested == 0 and len(segment) == 1 and segment[0] in CASE_TERMINATORS:
            collecting = False
        elif nested == 0 and segment == ["("]:
            continue
        elif nested == 0 and not collecting:
            continue
        elif segment == ["esac"]:
            nested -= 1
            body.append(segment)
        elif segment[0] == "case":
            nested += 1
            body.append(segment)
        else:
            body.append(segment)
    if end is None:
        return unjudged("case construct does not parse")
    if not body:
        return Group(decisions=[], resume=end + 1)
    return Group(
        decisions=decide_segment_list(body, context, depth + 1, bindings),
        resume=end + 1,
    )


def decide_segment_list(
    segments: list[list[str]],
    context: ShellContext,
    depth: int = 0,
    bindings: tuple[ShellBinding, ...] = (),
) -> list[KernelDecision]:
    """Classify a segment list, grouping structured constructs recursively.

    Bindings are frozen pairs: an assignment or read rebinds by producing a
    new tuple for the segments that follow, recursion receives the current
    value, and nothing mutates across scopes. A reference the walk cannot
    resolve to a literal stays a live ``$`` word for the guarded-flag gates.
    """
    segments = list(segments)
    decisions: list[KernelDecision] = []
    index = 0
    while index < len(segments):
        segment = segments[index]
        if segment in (["("], [")"]):
            index += 1
            continue
        if len(segment) == 1 and segment[0] in CASE_TERMINATORS:
            return [
                *decisions,
                unjudged("case terminator outside a case construct"),
            ]
        while segment and segment[0] == "{":
            segment = segment[1:]
        while segment and segment[-1] == "}":
            segment = segment[:-1]
        if not segment:
            index += 1
            continue
        structural = segment[0] in ("for", "while", "until", "if", "case")
        resolved = resolve_segment_bindings(
            segment, bindings, context, gate_opaque=not structural
        )
        if isinstance(resolved, KernelDecision):
            return [*decisions, resolved]
        segment = resolved
        segments[index] = segment
        if structural:
            match segment[0]:
                case "for" | "while" | "until":
                    outcome = decide_loop(segments, index, context, depth, bindings)
                case "if":
                    outcome = decide_conditional(
                        segments, index, context, depth, bindings
                    )
                case _:
                    outcome = decide_case(segments, index, context, depth, bindings)
            if isinstance(outcome, KernelDecision):
                return [*decisions, outcome]
            index = outcome["resume"]
            decisions.extend(outcome["decisions"])
            continue
        assignments = pure_assignment_names(segment)
        if assignments is not None:
            if any(dangerous_env_name(pair["name"]) for pair in assignments):
                decisions.append(
                    KernelDecision(
                        "ask",
                        "a security-sensitive environment assignment requires approval",
                    )
                )
                index += 1
                continue
            for pair in assignments:
                bindings = bind_name(bindings, pair["name"], pair["value"])
            index += 1
            continue
        words = effective_command(segment)["words"]
        if words and posixpath.basename(words[0]) == "read":
            extended = read_bindings(words, bindings)
            if isinstance(extended, KernelDecision):
                return [*decisions, extended]
            bindings = extended
            index += 1
            continue
        decisions.append(decide_shell_segment(segment, context))
        index += 1
    return decisions


def joined_placement(decisions: list[KernelDecision]) -> SandboxPlacement:
    """Where a whole command runs, given what each of its segments needs.

    One command line is one process, so its segments cannot be placed
    apart. Confinement outranks escape: a segment that has to stay inside
    keeps the whole line inside, and only a line where something needs the
    outside and nothing needs the inside leaves.
    """
    if any(item.sandbox == "inside" for item in decisions):
        return "inside"
    if any(item.sandbox == "outside" for item in decisions):
        return "outside"
    if any(item.sandbox == "escalable" for item in decisions):
        return "escalable"
    return "ambient"


def classify_shell(
    command: str,
    rows: list[ShellRuleRow],
    allowed_scopes: list[UrlScopeRow] | None = None,
    denied_scopes: list[UrlScopeRow] | None = None,
    trusted_script_roots: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    existing_targets: list[str] | None = None,
    recoverable_targets: list[str] | None = None,
    directory_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
    recoverable_target_limit: int = 5,
    runner_targets: list[RunnerTargetRow] | None = None,
    target_tables: list[ShellRuleRow] | None = None,
) -> KernelDecision:
    """Conservatively classify every segment in one shell command."""
    segments = parse_shell_words(
        command, 0, existing_targets, path_roles, path_rules, recoverable_targets
    )
    if isinstance(segments, KernelDecision):
        return segments
    # Named rather than positional: twelve lists of the same shape, and a
    # thirteenth inserted anywhere but the end silently re-seats every one
    # after it — passing a limit where a path list belongs.
    context = shell_context(
        rows,
        allowed_scopes=allowed_scopes,
        denied_scopes=denied_scopes,
        trusted_script_roots=trusted_script_roots,
        path_roles=path_roles,
        path_rules=path_rules,
        existing_targets=existing_targets,
        recoverable_targets=recoverable_targets,
        directory_targets=directory_targets,
        empty_directories=empty_directories,
        recoverable_target_limit=recoverable_target_limit,
        runner_targets=runner_targets,
        target_tables=target_tables,
    )
    decisions = decide_segment_list(segments, context)
    placement = joined_placement(decisions)
    denied = next((item for item in decisions if item.effect == "deny"), None)
    if denied is not None:
        return denied
    asked = next((item for item in decisions if item.effect == "ask"), None)
    if asked is not None:
        return KernelDecision("ask", asked.reason, placement)
    deferred = next((item for item in decisions if item.effect == "defer"), None)
    if deferred is not None:
        return deferred
    return KernelDecision("allow", "every shell segment is declared safe", placement)


def excluded_prefix(pattern: str) -> list[str]:
    """The literal words a sandbox-exclusion pattern matches a segment by.

    Reading a pattern down to its literal head is the conservative half of
    its meaning: whatever a wildcard goes on to match, the boundary drops at
    least everything the head covers, so taking the head for the whole rule
    can only classify more commands as unconfined, never fewer.
    """
    words = pattern.split()
    wildcards = [index for index, word in enumerate(words) if "*" in word]
    return words[: wildcards[0]] if wildcards else words


def sandbox_excluded(command: str, patterns: list[str]) -> bool:
    """Whether the boundary was told to leave this command out of isolation.

    Exclusion is the sandbox's only per-command lever, and it removes the
    command entirely rather than lifting one rule — so a command that
    matches runs with nothing beneath it, and work the policy would have
    handed to the boundary has to be judged here instead. Every segment is
    tested, because a compound command carries an exclusion as a whole, and
    one the lexer cannot read falls back to a single segment, which is what
    the boundary does with it too.
    """
    prefixes = [excluded_prefix(pattern) for pattern in patterns]
    segments = parse_shell_words(command)
    lexed = segments if isinstance(segments, list) else [command.split()]
    return any(
        bool(prefix) and segment[: len(prefix)] == prefix
        for segment in lexed
        for prefix in prefixes
    )


def auto_escape_matches(command: str, prefixes: list[list[str]]) -> bool:
    """Whether one simple command has a native auto-escape prefix."""
    segments = parse_shell_words(command)
    if not isinstance(segments, list) or len(segments) != 1:
        return False
    words = segments[0]
    return any(bool(prefix) and words[: len(prefix)] == prefix for prefix in prefixes)


def decide_shell(
    command: str,
    rows: list[ShellRuleRow],
    allowed_scopes: list[UrlScopeRow] | None = None,
    denied_scopes: list[UrlScopeRow] | None = None,
    sandboxed: bool = False,
    excluded_commands: list[str] | None = None,
    trusted_script_roots: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    interactive: bool = True,
    existing_targets: list[str] | None = None,
    recoverable_targets: list[str] | None = None,
    directory_targets: list[str] | None = None,
    empty_directories: list[str] | None = None,
    recoverable_target_limit: int = 5,
    runner_targets: list[RunnerTargetRow] | None = None,
    target_tables: list[ShellRuleRow] | None = None,
    escapable: bool = False,
) -> KernelDecision:
    """Classify one command, honoring an escalation marker and hinting denies.

    A leading ``# lup: escalate: <why>`` line promotes a classified deny or
    ask to an approval question carrying the agent's stated reason, so the
    human sees intent at the moment of judgment. A deny without a marker names
    the escalation recipe: unjudged work bounces back to the agent, which
    reshapes it into the allowed vocabulary or deliberately promotes it.
    When the execution is sandboxed, unjudged work defers instead: the OS
    boundary confines it, and only an unsandboxed escape returns to the
    deny lattice. A command the declaration excludes from the sandbox is
    such an escape without saying so — the boundary was told to leave it
    alone — so it is judged as though no sandbox were running at all.

    A non-interactive host has no approval channel, so a question it cannot
    put to a human is not a question — sandboxed, it rides the same OS
    boundary as unjudged work; unsandboxed, it fails closed. Such a host is
    never told to escalate, because that flow cannot complete there. A judged
    deny is never rescued by the sandbox in either mode.

    ``escapable`` is the third fact of that family: whether this host can put
    one call outside its own sandbox. A command declared ``outside`` is not
    advice — confined, it fails on whatever it writes first — so a host that
    cannot place it stops it here with that reason rather than letting it reach
    the shell. The pair is what makes the declaration safe to give a toolchain:
    where the escape is carried out it is unprompted, and where nothing can
    carry it out the refusal names the sandbox instead of a bare write error.
    """
    hint = ESCALATE_HINT if interactive else RESHAPE_HINT
    confined = sandboxed and not sandbox_excluded(command, excluded_commands or [])

    def resolve(decision: KernelDecision) -> KernelDecision:
        if sandboxed and not escapable and decision.sandbox == "outside":
            return KernelDecision("deny", SANDBOX_TRAPPED_REASON)
        match decision.effect:
            case "allow":
                return decision
            case "ask" if interactive:
                return decision
            case "defer" | "ask" if confined:
                return KernelDecision("defer", decision.reason)
            case _:
                return KernelDecision("deny", decision.reason + hint)

    marker = ESCALATE_RE.match(command)
    if marker is not None:
        why = marker.group("why").strip()
        if not why:
            return KernelDecision("deny", "escalation requires a stated reason" + hint)
        inner = classify_shell(
            command[marker.end() :],
            rows,
            allowed_scopes=allowed_scopes,
            denied_scopes=denied_scopes,
            trusted_script_roots=trusted_script_roots,
            path_roles=path_roles,
            path_rules=path_rules,
            existing_targets=existing_targets,
            recoverable_targets=recoverable_targets,
            directory_targets=directory_targets,
            empty_directories=empty_directories,
            recoverable_target_limit=recoverable_target_limit,
            runner_targets=runner_targets,
            target_tables=target_tables,
        )
        if inner.effect == "allow":
            return inner
        return resolve(
            KernelDecision("ask", f"escalated ({why}): {inner.reason}", inner.sandbox)
        )
    return resolve(
        classify_shell(
            command,
            rows,
            allowed_scopes=allowed_scopes,
            denied_scopes=denied_scopes,
            trusted_script_roots=trusted_script_roots,
            path_roles=path_roles,
            path_rules=path_rules,
            existing_targets=existing_targets,
            recoverable_targets=recoverable_targets,
            directory_targets=directory_targets,
            empty_directories=empty_directories,
            recoverable_target_limit=recoverable_target_limit,
            runner_targets=runner_targets,
            target_tables=target_tables,
        )
    )
