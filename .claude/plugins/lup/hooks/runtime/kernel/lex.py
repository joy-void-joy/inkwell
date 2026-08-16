# lup: ignore[empty-collection]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Shell lexing: tokens, redirections, heredocs, and segment grouping."""

import posixpath
from typing import Literal, TypedDict

from .decision import (
    BACKTICK_REASON,
    KernelDecision,
    SUBSTITUTION_REASON,
    SUBSTITUTION_SENTINEL,
    unjudged,
)
from .roles import path_role
from .rows import PathRoleRow, PathRuleRow
from .words import (
    SCRATCH_VERB_FLAGS,
    effective_command,
    git_restore_operands,
    path_verb_operands,
    protected_write_target,
    refuses_generated_plugin_target,
)


class Scan(TypedDict):
    """A balanced scan's inner text, and the position just past its close.

    ``text`` is ``None`` when the construct never closed. Every caller turns
    that into an unjudged verdict rather than guessing at the remainder, so
    the position still says how far the scan got.
    """

    text: str | None
    end: int


class Lexeme(TypedDict):
    """One lexed operator or expansion, and the position just past it."""

    text: str
    end: int


class Heredoc(TypedDict):
    """A delimiter awaiting its body, and whether quoting made the body literal."""

    delimiter: str
    quoted: bool


class Redirection(TypedDict):
    """What classifying one redirection decided, and where to resume.

    ``decision`` of ``None`` means the redirection is safe and consumed;
    ``resume`` is the token index the caller continues from either way.
    """

    decision: KernelDecision | None
    resume: int


class ShellToken:
    """One lexed shell token: a word, an operator, or a substituted command.

    ``procsub`` carries a ``<(...)`` inner command and ``cmdsub`` a ``$(...)``
    one; both are classified recursively. ``quoted`` records whether any part
    of a word came from quotes or escapes, which decides whether a heredoc
    delimiter suppresses body expansion.
    """

    kind: Literal["word", "op", "procsub", "cmdsub"]
    text: str
    quoted: bool

    def __init__(
        self,
        kind: Literal["word", "op", "procsub", "cmdsub"],
        text: str,
        quoted: bool = False,
    ) -> None:
        self.kind = kind
        self.quoted = quoted
        self.text = text


def read_process_substitution(command: str, position: int) -> Scan:
    """Scan a balanced ``<(...)`` body, honoring quotes, returning its inner text."""
    start = position
    depth = 1
    length = len(command)
    while position < length:
        character = command[position]
        if character == "'":
            closing = command.find("'", position + 1)
            if closing == -1:
                return Scan(text=None, end=position)
            position = closing + 1
            continue
        if character == '"':
            position += 1
            while position < length and command[position] != '"':
                position += 2 if command[position] == "\\" else 1
            if position >= length:
                return Scan(text=None, end=position)
            position += 1
            continue
        if character == "\\":
            position += 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return Scan(text=command[start:position], end=position + 1)
        position += 1
    return Scan(text=None, end=position)


def read_command_substitution(command: str, position: int) -> Scan:
    """Scan a balanced ``$(...)`` expansion, returning its inner text.

    ``position`` sits on the ``$``; the returned end position follows the
    closing parenthesis, so ``command[position:end]`` is the raw expansion
    the enclosing word keeps as its opaque spelling.
    """
    return read_process_substitution(command, position + 2)


def read_redirection(command: str, position: int) -> Lexeme:
    """Read a maximal redirection operator, returning its text and end position."""
    start = position
    length = len(command)
    if command[position] == "&":
        position += 1
    core = command[position]
    position += 1
    if position < length and command[position] == core:
        position += 1
        if core == "<" and position < length and command[position] == "<":
            position += 1
        elif core == "<" and position < length and command[position] == "-":
            position += 1
    if position < length and command[position] == "&":
        position += 1
        while position < length and (
            command[position].isdigit() or command[position] == "-"
        ):
            position += 1
    return Lexeme(text=command[start:position], end=position)


def read_control(command: str, position: int) -> Lexeme:
    """Read a maximal control operator, returning its text and end position."""
    character = command[position]
    length = len(command)
    if character == "&":
        if position + 1 < length and command[position + 1] == "&":
            return Lexeme(text="&&", end=position + 2)
        return Lexeme(text="&", end=position + 1)
    if character == "|":
        if position + 1 < length and command[position + 1] == "|":
            return Lexeme(text="||", end=position + 2)
        if position + 1 < length and command[position + 1] == "&":
            return Lexeme(text="|&", end=position + 2)
        return Lexeme(text="|", end=position + 1)
    if character == ";":
        if command[position : position + 3] == ";;&":
            return Lexeme(text=";;&", end=position + 3)
        if command[position : position + 2] in (";;", ";&"):
            return Lexeme(text=command[position : position + 2], end=position + 2)
        return Lexeme(text=";", end=position + 1)
    return Lexeme(text=character, end=position + 1)


def read_arithmetic(command: str, position: int) -> Scan:
    """Scan a balanced ``$((...))`` expansion whose interior is pure data.

    ``position`` sits on the ``$``. Arithmetic cannot run commands, so the
    expansion joins its word verbatim — unless the interior nests a command
    substitution, in which case the scan stops on it and returns ``None``.
    """
    depth = 2
    cursor = position + 3
    length = len(command)
    while cursor < length:
        character = command[cursor]
        if character == "`" or command[cursor : cursor + 2] == "$(":
            return Scan(text=None, end=cursor)
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return Scan(text=command[position : cursor + 1], end=cursor + 1)
        cursor += 1
    return Scan(text=None, end=cursor)


def arithmetic_token(command: str, position: int) -> Lexeme | KernelDecision:
    """Read one ``$((...))`` expansion or explain why it cannot join a word."""
    scan = read_arithmetic(command, position)
    expansion = scan["text"]
    end = scan["end"]
    if expansion is not None:
        return Lexeme(text=expansion, end=end)
    if command[end : end + 2] == "$(" or command[end : end + 1] == "`":
        return KernelDecision("deny", SUBSTITUTION_REASON)
    return unjudged("arithmetic expansion does not parse")


def without_leading_tabs(line: str) -> str:
    """The line with its ``<<-``-style leading tab indentation removed."""
    first = next(
        (index for index, character in enumerate(line) if character != "\t"),
        len(line),
    )
    return line[first:]


def read_heredoc_bodies(
    command: str, position: int, pending: list[Heredoc]
) -> int | KernelDecision:
    """Consume heredoc bodies after a newline, gating unquoted expansion.

    A quoted delimiter makes the body literal data. An unquoted one lets the
    shell substitute inside the body, so any substitution syntax there is
    refused with the quoting recipe.
    """
    for heredoc in pending:
        delimiter = heredoc["delimiter"]
        is_quoted = heredoc["quoted"]
        lines: list[str] = []
        terminated = False
        while position <= len(command):
            newline = command.find("\n", position)
            end = len(command) if newline == -1 else newline
            line = command[position:end]
            position = end + 1
            if line == delimiter or without_leading_tabs(line) == delimiter:
                terminated = True
                break
            lines.append(line)
            if newline == -1:
                break
        if not terminated:
            return unjudged("heredoc does not terminate")
        if not is_quoted:
            body = "\n".join(lines)
            if "`" in body or "$(" in body:
                return KernelDecision(
                    "deny",
                    "an unquoted heredoc substitutes commands — quote the"
                    " delimiter (<<'EOF') to make the body literal",
                )
    return position


def tokenize_shell(command: str) -> list[ShellToken] | KernelDecision:
    """Lex a command into words and operators, refusing opaque or unsafe syntax.

    Heredoc bodies are consumed here: a delimiter registered by a ``<<``
    redirection queues until the next newline, where the body lines are
    skipped as data (quoted delimiter) or gated on substitution syntax
    (unquoted delimiter).
    """
    tokens: list[ShellToken] = []
    word: list[str] = []
    started = False
    quoted = False
    heredoc_expected = False
    pending_heredocs: list[Heredoc] = []
    length = len(command)
    position = 0

    def flush() -> None:
        nonlocal started, quoted, heredoc_expected
        if started:
            tokens.append(ShellToken("word", "".join(word), quoted))
            if heredoc_expected:
                pending_heredocs.append(Heredoc(delimiter="".join(word), quoted=quoted))
                heredoc_expected = False
            word.clear()
            started = False
            quoted = False

    while position < length:
        character = command[position]
        if character == "'":
            closing = command.find("'", position + 1)
            if closing == -1:
                return unjudged("shell quoting does not parse")
            word.extend(command[position + 1 : closing])
            started = True
            quoted = True
            position = closing + 1
            continue
        if character == '"':
            position += 1
            quoted = True
            while position < length and command[position] != '"':
                inner = command[position]
                if inner == "\\" and position + 1 < length:
                    word.append(command[position + 1])
                    position += 2
                    continue
                if inner == "$" and command[position + 1 : position + 3] == "((":
                    outcome = arithmetic_token(command, position)
                    if isinstance(outcome, KernelDecision):
                        return outcome
                    position = outcome["end"]
                    word.append(outcome["text"])
                    continue
                if inner == "`":
                    return KernelDecision("deny", BACKTICK_REASON)
                if (
                    inner == "$"
                    and position + 1 < length
                    and command[position + 1] == "("
                ):
                    substitution = read_command_substitution(command, position)
                    body = substitution["text"]
                    if body is None:
                        return unjudged("command substitution does not parse")
                    tokens.append(ShellToken("cmdsub", body))
                    word.append(SUBSTITUTION_SENTINEL)
                    position = substitution["end"]
                    continue
                word.append(inner)
                position += 1
            if position >= length:
                return unjudged("shell quoting does not parse")
            started = True
            position += 1
            continue
        if character == "\\":
            if position + 1 < length and command[position + 1] != "\n":
                word.append(command[position + 1])
                started = True
                quoted = True
            position += 2
            continue
        if character in " \t\r":
            flush()
            position += 1
            continue
        if character == "$" and command[position + 1 : position + 3] == "((":
            outcome = arithmetic_token(command, position)
            if isinstance(outcome, KernelDecision):
                return outcome
            position = outcome["end"]
            word.append(outcome["text"])
            started = True
            continue
        if character == "`":
            return KernelDecision("deny", BACKTICK_REASON)
        if character == "$" and position + 1 < length and command[position + 1] == "(":
            substitution = read_command_substitution(command, position)
            body = substitution["text"]
            if body is None:
                return unjudged("command substitution does not parse")
            tokens.append(ShellToken("cmdsub", body))
            word.append(SUBSTITUTION_SENTINEL)
            started = True
            position = substitution["end"]
            continue
        if character == ">" and position + 1 < length and command[position + 1] == "(":
            return KernelDecision(
                "ask", "writing process substitution is never auto-allowed"
            )
        if character == "<" and position + 1 < length and command[position + 1] == "(":
            if started:
                return unjudged("process substitution inside a word is not classified")
            substitution = read_process_substitution(command, position + 2)
            inner = substitution["text"]
            if inner is None:
                return unjudged("process substitution does not parse")
            tokens.append(ShellToken("procsub", inner))
            position = substitution["end"]
            continue
        if character == "#" and not started:
            newline = command.find("\n", position)
            if newline == -1:
                break
            position = newline
            continue
        if character in "<>" or (
            character == "&" and position + 1 < length and command[position + 1] == ">"
        ):
            fd = ""
            if started and word and all(digit.isdigit() for digit in word):
                fd = "".join(word)
                word.clear()
                started = False
                quoted = False
            else:
                flush()
            redirection = read_redirection(command, position)
            operator = redirection["text"]
            position = redirection["end"]
            tokens.append(ShellToken("op", fd + operator))
            if "<<" in operator and "<<<" not in operator:
                heredoc_expected = True
            continue
        if character in ";&|\n":
            flush()
            control = read_control(command, position)
            operator = control["text"]
            position = control["end"]
            tokens.append(ShellToken("op", operator))
            if operator == "\n" and pending_heredocs:
                consumed = read_heredoc_bodies(command, position, pending_heredocs)
                if isinstance(consumed, KernelDecision):
                    return consumed
                position = consumed
                pending_heredocs.clear()
            continue
        if character == "(":
            if started:
                return unjudged(
                    "shell arrays and function definitions are not classified"
                )
            tokens.append(ShellToken("op", "("))
            position += 1
            continue
        if character == ")":
            flush()
            tokens.append(ShellToken("op", ")"))
            position += 1
            continue
        word.append(character)
        started = True
        position += 1
    flush()
    if heredoc_expected:
        return unjudged("heredoc has no delimiter")
    if pending_heredocs:
        return unjudged("heredoc does not terminate")
    return tokens


# lup: ignore[library-default] — POSIX shell grammar operators
SENTINEL_OPS = ("(", ")", ";;", ";&", ";;&")


def is_control_operator(text: str) -> bool:
    """Return whether an operator token separates command segments."""
    return text in (";", "&", "&&", "||", "|", "|&", "\n")


def literal_target(word: str) -> bool:
    """Whether a redirection target names exactly the path it spells.

    Existence is established against the word as written, so a target
    carrying an unexpanded parameter, a substitution, or a tilde names a
    different file at run time than the one that was stat'd. Such a target
    never qualifies for the create-versus-overwrite relaxation, and keeps
    the strict verdict the operator alone earns.
    """
    return not any(marker in word for marker in ("$", "~", "`", SUBSTITUTION_SENTINEL))


def redirection_writes(operator: str) -> bool:
    """Whether one redirection operator opens its target for writing.

    Every writing form spells ``>``, which also separates them from the
    control operators that carry a following word of their own.
    """
    if ">" not in operator:
        return False
    if "<<" in operator and "<<<" not in operator:
        return False
    return not ("&" in operator and (operator[-1].isdigit() or operator[-1] == "-"))


def shell_write_targets(command: str, depth: int = 0) -> list[str]:
    """Name every path this command's redirections would open for writing.

    A caller that can reach the filesystem stats these and hands back the
    ones that already exist, so the kernel can tell creating a file from
    overwriting one without ever reading the filesystem itself. A command
    that does not lex yields nothing and keeps its unjudged verdict.
    """
    tokens = tokenize_shell(command)
    if isinstance(tokens, KernelDecision):
        return []
    targets: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind in ("cmdsub", "procsub"):
            if depth < 2:
                targets.extend(shell_write_targets(token.text, depth + 1))
            index += 1
            continue
        if token.kind != "op" or not redirection_writes(token.text):
            index += 1
            continue
        following = index + 1
        if following < len(tokens) and tokens[following].kind == "word":
            targets.append(tokens[following].text)
            index = following + 1
            continue
        index += 1
    return targets


def resolve_redirection(
    tokens: list[ShellToken],
    index: int,
    existing_targets: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    recoverable_targets: list[str] | None = None,
) -> Redirection:
    """Classify one redirection, consuming its target and stripping safe forms.

    What a write costs decides it, exactly as it decides for ``rm`` and
    ``cp``. Creating a file destroys nothing, so it passes once the caller
    has established the target is not there. Replacing one Git can restore
    byte for byte costs a checkout rather than any information, so it passes
    too. Anything else overwrites something no one can bring back, and asks.
    ``existing_targets`` of ``None`` means no caller established anything,
    and every target is treated as already there.

    The shape of the writing command is deliberately not consulted. A heredoc
    body and an ``echo`` argument author identical content, and the create
    case already admits both — so gating one of them on an existing path drew
    the line where the cost was lowest rather than where the risk was. What
    the edit gate reads is content, and that gate is reached through Edit and
    Write, not by re-deriving a weaker copy of it here.

    A generated plugin tree is refused ahead of every relaxation, including
    the create case: authoring a file there by hand is editing a build
    product, whether or not one is already sitting at that path.
    """
    operator = tokens[index].text
    if "<<" in operator and "<<<" not in operator:
        target = index + 1
        if target >= len(tokens) or tokens[target].kind != "word":
            return Redirection(
                decision=unjudged("heredoc has no delimiter"), resume=index + 1
            )
        return Redirection(decision=None, resume=target + 1)
    if "&" in operator and (operator[-1].isdigit() or operator[-1] == "-"):
        return Redirection(decision=None, resume=index + 1)
    target = index + 1
    if target >= len(tokens) or tokens[target].kind != "word":
        return Redirection(
            decision=KernelDecision("ask", "file redirection is never auto-allowed"),
            resume=index + 1,
        )
    if "<" in operator:
        return Redirection(decision=None, resume=target + 1)
    if posixpath.normpath(tokens[target].text) == "/dev/null":
        return Redirection(decision=None, resume=target + 1)
    refused = refuses_generated_plugin_target(tokens[target].text)
    if refused is not None:
        return Redirection(decision=refused, resume=target + 1)
    protected = protected_write_target(
        [tokens[target].text],
        path_rules or [],
        existing_targets is None or tokens[target].text in existing_targets,
    )
    if protected is not None:
        return Redirection(decision=protected, resume=target + 1)
    if path_role(tokens[target].text, path_roles or []) == "scratch":
        return Redirection(decision=None, resume=target + 1)
    if (
        existing_targets is not None
        and literal_target(tokens[target].text)
        and tokens[target].text not in existing_targets
    ):
        return Redirection(decision=None, resume=target + 1)
    if tokens[target].text in (recoverable_targets or []):
        return Redirection(decision=None, resume=target + 1)
    # lup: solved: This asks too often too. "file redirection is never
    # auto-allowed" fires on the everyday `2>&1` / `>/dev/null` shapes that
    # carry no write a reviewer would care about; auto-allow those the way the
    # rest of the read-only vocabulary is meant to be.
    # Landing review-fixes added the recoverable/inert target handling above:
    # `ls -la >/dev/null` and `uv run pytest -q 2>&1|tail -3` both classify
    # allow now, verified with `lup-devtools hooks classify`.
    return Redirection(
        decision=KernelDecision("ask", "file redirection is never auto-allowed"),
        resume=target + 1,
    )


def parse_shell_words(
    command: str,
    depth: int = 0,
    existing_targets: list[str] | None = None,
    path_roles: list[PathRoleRow] | None = None,
    path_rules: list[PathRuleRow] | None = None,
    recoverable_targets: list[str] | None = None,
) -> list[list[str]] | KernelDecision:
    """Group lexed tokens into command segments, resolving safe redirections.

    A read-side process substitution contributes a ``/dev/fd`` placeholder to
    its enclosing segment and its inner command joins the segment list, so the
    caller classifies it exactly like a piped command. Grouping parentheses
    and case terminators become single-word sentinel segments the segment
    walker interprets, and ``[[ ... ]]`` folds into one read-only test word.
    """
    tokens = tokenize_shell(command)
    if isinstance(tokens, KernelDecision):
        return tokens

    recoverable = recoverable_targets or []

    def substituted_segments(text: str) -> list[list[str]] | KernelDecision:
        if depth >= 2:
            return unjudged("command substitution nests too deeply")
        return parse_shell_words(
            text, depth + 1, existing_targets, path_roles, path_rules, recoverable
        )

    segments: list[list[str]] = []
    current: list[str] = []
    # A substitution's own command, held until the segment being built closes.
    # Spliced the moment it is read, it lands ahead of the words around it —
    # between a `for` header and its `do`, where the loop reader takes it for
    # a condition and refuses a construct that parsed fine. It cannot simply
    # flush the partial segment either: the tokenizer leaves a sentinel in the
    # word where the substitution stood, and cutting the segment there strands
    # that sentinel as a command of its own.
    pending: list[list[str]] = []

    def close_segment() -> None:
        """End the segment being built, then place what it substituted."""
        nonlocal current
        if current:
            segments.append(current)
            current = []
        segments.extend(pending)
        pending.clear()

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "word" and token.text == "[[" and not token.quoted:
            fold = index + 1
            while fold < len(tokens) and not (
                tokens[fold].kind == "word" and tokens[fold].text == "]]"
            ):
                if tokens[fold].kind == "procsub":
                    return unjudged(
                        "process substitution inside [[ ]] is not classified"
                    )
                if tokens[fold].kind == "cmdsub":
                    folded = substituted_segments(tokens[fold].text)
                    if isinstance(folded, KernelDecision):
                        return folded
                    segments.extend(folded)
                fold += 1
            if fold >= len(tokens):
                return unjudged("test expression does not parse")
            current.append("[[")
            index = fold + 1
            continue
        if token.kind == "word":
            current.append(token.text)
            index += 1
            continue
        if (
            token.kind == "op"
            and token.text == "("
            and current
            and current[-1] not in ("then", "else", "elif", "do", "if", "in", "!")
        ):
            return unjudged("shell function definitions are not classified")
        if token.kind == "op" and token.text in SENTINEL_OPS:
            close_segment()
            segments.append([token.text])
            index += 1
            continue
        if token.kind == "procsub":
            if depth >= 2:
                return unjudged("process substitution nests too deeply")
            inner = parse_shell_words(
                token.text,
                depth + 1,
                existing_targets,
                path_roles,
                path_rules,
                recoverable,
            )
            if isinstance(inner, KernelDecision):
                return inner
            pending.extend(inner)
            current.append("/dev/fd/63")
            index += 1
            continue
        if token.kind == "cmdsub":
            spliced = substituted_segments(token.text)
            if isinstance(spliced, KernelDecision):
                return spliced
            pending.extend(spliced)
            index += 1
            continue
        if is_control_operator(token.text):
            close_segment()
            index += 1
            continue
        redirection = resolve_redirection(
            tokens, index, existing_targets, path_roles, path_rules, recoverable
        )
        index = redirection["resume"]
        verdict = redirection["decision"]
        if verdict is not None:
            return verdict
    close_segment()
    if not segments:
        return unjudged("shell command has no executable segment")
    return segments


def shell_path_verb_targets(command: str) -> list[str]:
    """Name every operand a path-writing verb in this command acts on.

    ``git restore`` is one of them: it rewrites the paths it names from the
    index, so whether that costs anything is the same filesystem question the
    delete verbs ask.

    A caller that can reach the filesystem resolves facts about these — which
    are directories, which Git could restore — and hands them back, so the
    kernel decides from primitive data without ever reading a disk. Naming
    only these operands keeps that resolution proportional to the command: a
    session running ``ls`` pays for none of it.

    Over-naming is safe and under-naming is merely conservative, because the
    answers are consulted as a table of facts rather than trusted as a target
    list — the kernel decides for itself which words a verb writes. A command
    that does not lex yields nothing and keeps its unjudged verdict.
    """
    segments = parse_shell_words(command, 0)
    if isinstance(segments, KernelDecision):
        return []
    targets: list[str] = []
    for segment in segments:
        words = effective_command(segment)["words"]
        if not words:
            continue
        restore = git_restore_operands(words)
        if restore is not None:
            targets.extend(restore["paths"])
            continue
        if posixpath.basename(words[0]) not in SCRATCH_VERB_FLAGS:
            continue
        operands = path_verb_operands(words)["operands"]
        targets.extend(operands)
    return targets
