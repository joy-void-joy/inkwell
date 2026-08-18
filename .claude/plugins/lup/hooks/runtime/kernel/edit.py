# lup: ignore[empty-collection, import-re, re-call, set-shape, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Edit gates: anti-patterns, protected paths, markers, and size."""

import ast
import io
import posixpath
import re
import tokenize
from collections.abc import Callable, Iterator
from typing import TypedDict

from .decision import KernelDecision
from .roles import normalized_path, path_role, root_matches
from .rows import AcceptanceGuardRow, AntiPatternRow, PathRoleRow, PathRuleRow

MARKER_RE = re.compile(r"(#|//)\s*lup\s*:", re.IGNORECASE)
# A review note is any marker whose keyword is not `ignore`, which is the
# anti-pattern escape hatch rather than feedback — the same split
# `lup.codescan.markers` makes when it gathers notes.
# The whitespace sits inside the lookahead: leaving it outside lets the regex
# backtrack to zero width and match `# lup: ignore` after all.
NOTE_RE = re.compile(r"(#|//)\s*lup\s*:(?!\s*ignore\b)", re.IGNORECASE)
# An open note is one still owed an answer, so `solved` is excluded alongside
# `ignore`: a resolution claim is a note that has been acted on and is waiting
# to be checked, and counting it as open would make converting one read as
# deleting it. `template` is excluded for the reason `ignore` is: it is not
# feedback anybody is owed an answer to. A customization marker is answered by
# writing the domain's own code where the scaffold's placeholder stood, which
# leaves no original ask for a claim to be checked against — so it is removed
# outright, and `/lup:init` removing one must not read as deleting feedback.
OPEN_NOTE_RE = re.compile(
    r"(#|//)\s*lup\s*:(?!\s*(?:ignore|solved|template)\b)", re.IGNORECASE
)
SOLVED_NOTE_RE = re.compile(r"(#|//)\s*lup\s*:\s*solved\b", re.IGNORECASE)
IGNORE_RE = re.compile(
    r"(#|//)\s*lup\s*:\s*ignore\b(?:\s*\[(?P<ids>[^\]]*)\])?",
    re.IGNORECASE,
)
# The file-level form is the same directive standing alone on its own line, so
# the leading anchor is what separates it from a trailing inline one. It may
# carry a reason after the ids, introduced by a dash or a colon the way the
# inline form and `defer[<condition>]:` already do — a suppression that cannot
# say why it exists is the shape these rules were written to discourage. The
# audit reads the same object: a kernel that stopped at `]` would deny every
# added line in a file whose directive explains itself, while `dev check`
# called that file exempt.
FILE_IGNORE_RE = re.compile(
    r"^\s*(#|//)\s*lup\s*:\s*ignore\b(?:\s*\[(?P<ids>[^\]]*)\])?"
    r"\s*(?:[-—–:]\s*(?P<reason>\S.*?))?\s*$",
    re.IGNORECASE,
)

SUPPRESSION_COLUMN_LIMIT = 88
"""How wide a line carrying its own directive may be before the reason moves up.

The formatter's limit is what decides whether the canonical inline placement
can hold a directive and the reason that justifies it at all; past it the
choice is a shorter reason or a shorter identifier, and both are the defect
this budget exists to avoid. A project whose formatter is configured
differently passes its own.
"""

# lup: ignore[constant-declaration] — the kernel carries no config, and the
# hook compiled from it must read the same width the sweep does
ABBREVIATION_CHARS = 16
"""The widest prefix that abbreviates an identifier rather than cutting a text.

A git SHA shortens to seven or twelve, a uuid to sixteen, a content hash to
whatever still collides with nothing; all of them stay the name of the thing
they came from. Nothing anyone wrote survives being shortened this far, so a
bound at or under this is read as naming rather than keeping — which is what
lets `silent-truncation` flag every wider slice without a table of receivers
it would have to guess from.
"""


def standalone_suppression(line: str) -> re.Match[str] | None:
    """The directive on a line that carries nothing but the directive."""
    match = IGNORE_RE.search(line)
    return None if match is None or line[: match.start()].strip() else match


def suppression_reaches(
    lines: list[str], directive_line: int, violation_line: int
) -> bool:
    """Whether a directive on `directive_line` may silence `violation_line`.

    One policy, for every rule alike: a directive sits on the violation's own
    line — the canonical placement — or heads the comment block standing
    directly above it, which is where a reason too long for the column budget
    goes. No rule widens this and none narrows it, so a marker shape valid
    against one rule is valid against all of them.

    Standing alone is what makes the line above a placement rather than an
    accident: an inline directive guards the line it was written on, and a
    rule tripping the line below cannot quietly borrow it.

    A reason worth reading often outruns one line, so the directive may be
    followed by its own continuation comments before what it guards. They are
    continuations rather than neighbours because a directive of their own ends
    the block: two markers cannot share one, and the nearer takes its lines.
    """
    if directive_line == violation_line:
        return True
    if directive_line < 1 or directive_line >= violation_line:
        return False
    if standalone_suppression(lines[directive_line - 1]) is None:
        return False
    return all(
        lines[number - 1].lstrip().startswith(("#", "//"))
        and IGNORE_RE.search(lines[number - 1]) is None
        for number in range(directive_line + 1, violation_line)
    )


def inline_suppression(code: str, directive: str) -> str:
    """One code line carrying a directive that had been standing above it."""
    return f"{code.rstrip()}  {directive.strip()}"


def hoisted_suppression(line: str, match: re.Match[str]) -> list[str]:
    """The two lines one inline directive becomes with its reason hoisted.

    The directive comes first, taking the code's own indentation, which is
    what makes it read as belonging to the line beneath rather than to the
    block around it.
    """
    indent = line[: len(line) - len(line.lstrip())]
    return [f"{indent}{line[match.start() :].strip()}", line[: match.start()].rstrip()]


def python_parses(source: str) -> bool:
    """Whether source is valid Python, which tokenizing alone does not decide.

    A comment inserted after a backslash continuation still tokenizes and
    still will not parse, so only the grammar can say whether moving a line
    left the file intact.
    """
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return True


def relocated_suppressions(text: str, limit: int = SUPPRESSION_COLUMN_LIMIT) -> str:
    """Rewrite one file so every directive sits at its canonical placement.

    Both placements are accepted, so this changes nothing about what a
    directive silences — only where it is written. Inline is canonical and
    holds until the reason stops fitting: a comment is the one thing the
    formatter cannot wrap, so past the budget an inline directive costs either
    its reason or its line's width, and the first of those is the pressure
    that ends in a shortened identifier.

    Both directions are what make this a normal form rather than a nudge. An
    over-wide inline directive is hoisted onto the line above; a directive
    standing above a line it would fit on is folded back onto it. Every input
    reaches the same output and reaching it twice changes nothing, which is
    what keeps the placement from needing a sweep each time a tree drifts —
    hoisting alone would leave a directive written above a line it fits on
    correct forever, with nothing that ever moves it.

    Non-Python text is returned untouched, and so is any move that would stop
    the file parsing: a directive trailing a backslash continuation has no
    line above it a comment may occupy.
    """
    columns = python_comment_columns(text)
    if columns is None:
        return text
    lines = text.splitlines()
    file_level = file_level_line(text)

    def directive_at(number: int) -> re.Match[str] | None:
        """The directive one line carries, if a comment really opens there.

        The whole-file opt-out is not one: it governs the file rather than the
        line beneath it, and folding it onto that line would silently narrow
        it to one statement.
        """
        if number < 1 or number > len(lines) or number == file_level:
            return None
        match = IGNORE_RE.search(lines[number - 1])
        if match is None or number not in columns or columns[number] != match.start():
            return None
        return match

    def folds_onto(number: int) -> bool:
        """Whether the line below a standalone directive can take it inline.

        It has to be code — a blank or a comment is not what the directive
        guards — and it has to be carrying no directive of its own, since two
        on one line leaves the second unread.
        """
        below = lines[number] if number < len(lines) else ""
        if not below.strip() or below.lstrip().startswith(("#", "//")):
            return False
        return directive_at(number + 1) is None

    def placed() -> Iterator[str]:
        folded = 0
        for number, line in enumerate(lines, start=1):
            if number == folded:
                continue
            match = directive_at(number)
            if match is None:
                yield line
                continue
            if standalone_suppression(line) is None:
                if len(line) <= limit:
                    yield line
                    continue
                yield from hoisted_suppression(line, match)
                continue
            merged = inline_suppression(
                lines[number] if number < len(lines) else "", line
            )
            if not folds_onto(number) or len(merged) > limit:
                yield line
                continue
            folded = number + 1
            yield merged

    revised = "\n".join(placed()) + ("\n" if text.endswith("\n") else "")
    # Hoisting into a file's opening comment block would land the directive
    # where the whole-file form is read, silently widening one line's excuse
    # to every line. A move that changes which directive heads the file is
    # declined, and the reason stays inline where it was written.
    if file_level_line(revised) != file_level_line(text):
        return text
    return revised if python_parses(revised) else text


def relocated_edit_text(
    after: str, start: int, end: int, limit: int = SUPPRESSION_COLUMN_LIMIT
) -> str | None:
    """What an edit's own span becomes once placement is settled, or ``None``.

    ``start`` and ``end`` bound the region of the finished document the edit
    wrote. Placement has to be decided on the whole document — which line a
    directive guards is the line beneath it, and an edit's fragment need not
    contain it — and the answer is then read back out of that region.

    ``None`` says the edit cannot express the move: either nothing moved, or
    the move reached text this call does not write. Declining is free, because
    both placements are valid and the directive stays where its author put it.
    """
    revised = relocated_suppressions(after, limit)
    if revised == after:
        return None
    tail = len(after) - end
    if (
        revised[:start] != after[:start]
        or revised[len(revised) - tail :] != after[end:]
    ):
        return None
    return revised[start : len(revised) - tail]


def covering_suppression_line(lines: list[str], violation_line: int) -> int:
    """The line holding the directive that covers `violation_line`, or 0.

    Both accepted placements, nearest first, so an inline directive is
    preferred over one standing above and the site a refusal quotes is the one
    whoever wrote it would recognize. Above means the head of the comment
    block, which a multi-line reason puts further up than the line itself.
    """
    for candidate in range(violation_line, 0, -1):
        if IGNORE_RE.search(lines[candidate - 1]) is not None:
            return (
                candidate
                if suppression_reaches(lines, candidate, violation_line)
                else 0
            )
        if candidate != violation_line and not lines[candidate - 1].lstrip().startswith(
            ("#", "//")
        ):
            return 0
    return 0


def suppression_placement(violation_line: int) -> str:
    """Name the lines a refusal expected the directive it did not find on.

    The reported failure this answers is a directive that went spurious while
    the violation it meant to guard stayed missing, with nothing in either
    message saying where the two were supposed to meet.
    """
    if violation_line <= 1:
        return "line 1"
    return f"line {violation_line}, or line {violation_line - 1} directly above it"


def python_tokens(source: str) -> list[tokenize.TokenInfo] | None:
    """Tokenize Python source, returning ``None`` for incomplete syntax."""
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def python_comment_columns(source: str) -> dict[int, int] | None:
    """Map Python line numbers to real comment-token columns."""
    tokens = python_tokens(source)
    if tokens is None:
        return None
    return {
        token.start[0]: token.start[1]
        for token in tokens
        if token.type == tokenize.COMMENT
    }


def docstring_lines(source: str) -> set[int]:
    """Return lines occupied by bare string-expression documentation."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.end_lineno is not None
        ):
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


# lup: ignore[library-default] — Python's own token taxonomy for string text
STRING_TOKEN_TYPES = (
    tokenize.STRING,
    tokenize.FSTRING_START,
    tokenize.FSTRING_MIDDLE,
    tokenize.FSTRING_END,
)
"""Every token type whose characters are string text, not code.

An f-string stopped being one STRING token in 3.12: it lexes as a start
marker, literal middle fragments, and an end marker, with real code tokens
between them for each interpolation. Masking only STRING left f-string
prose readable as code — converting a prompt's r-string to an rf-string
exposed its English to the anti-pattern rules — while the interpolations
stay visible here because they are code.
"""


def string_literal_lines(source: str) -> set[int]:
    """Return every line touched by a Python string token."""
    tokens = python_tokens(source)
    if tokens is None:
        return set()
    lines: set[int] = set()
    for token in tokens:
        if token.type in STRING_TOKEN_TYPES:
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines


def mask_python_string_literals(source: str) -> list[str]:
    """Blank string-token characters while preserving line and column positions."""
    lines = [list(line) for line in source.splitlines()]
    tokens = python_tokens(source)
    if tokens is None:
        return source.splitlines()
    for token in tokens:
        if token.type not in STRING_TOKEN_TYPES:
            continue
        start_line, start_column = token.start
        end_line, end_column = token.end
        for line_number in range(start_line, end_line + 1):
            line = lines[line_number - 1]
            first = start_column if line_number == start_line else 0
            last = end_column if line_number == end_line else len(line)
            line[first:last] = [" "] * (last - first)
            opens_string = token.type in (tokenize.STRING, tokenize.FSTRING_START)
            if line_number == start_line and last - first >= 2 and opens_string:
                line[first : first + 2] = ["'", "'"]
    return ["".join(line) for line in lines]


def python_code_lines(source: str) -> list[str]:
    """Blank string and comment tokens while preserving line and column positions."""
    lines = [list(line) for line in mask_python_string_literals(source)]
    tokens = python_tokens(source)
    if tokens is None:
        return ["".join(line) for line in lines]
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        start_line, start_column = token.start
        line = lines[start_line - 1]
        line[start_column : token.end[1]] = [" "] * (token.end[1] - start_column)
    return ["".join(line) for line in lines]


def quoted_example(line: str, position: int) -> bool:
    """Whether a match at ``position`` sits inside a backtick code span.

    Prose that documents the marker syntax writes it in backticks, which is
    how a reader tells an example from an instruction. Counting those as
    notes made documenting the convention indistinguishable from leaving
    feedback — and made the gate fire on the very text explaining it. Odd
    single-backtick parity catches a marker mid-span; a run directly before
    the marker catches double-backtick quoting, whose even-length run defeats
    the parity check.
    """
    prefix = line[:position]
    return prefix.count("`") % 2 == 1 or prefix.endswith("`")


def count_outside_examples(pattern: re.Pattern[str], text: str) -> int:
    """Count matches in one chunk of prose, skipping backtick-quoted examples."""
    return sum(
        1
        for line in text.splitlines()
        for match in pattern.finditer(line)
        if not quoted_example(line, match.start())
    )


def count_in_prose(
    source: str, pattern: re.Pattern[str], python_source: bool = False
) -> int | None:
    """Count pattern matches where prose belongs, not inside ordinary strings.

    `None` where a Python source does not tokenize, because there is then no
    way to tell a comment from a string and the honest answer is that the
    count is unknown for that revision. Returning a whole-text tally instead
    counts a different population, so differencing it against a tokenised
    one measures the change of regime rather than the change of notes: a
    conflicted file mentioning the marker in a string literal counted one
    higher until its last conflict marker went, and that drop read as
    deleted feedback.
    """
    if not python_source:
        return count_outside_examples(pattern, source)
    tokens = python_tokens(source)
    if tokens is None:
        return None
    documentation = docstring_lines(source)
    return sum(
        count_outside_examples(pattern, token.string)
        for token in tokens
        if token.type == tokenize.COMMENT
        or (
            token.type == tokenize.STRING
            and any(
                line in documentation
                for line in range(token.start[0], token.end[0] + 1)
            )
        )
    )


def review_marker_count(source: str, python_source: bool = False) -> int | None:
    """Count review notes — the feedback whose removal is the gated act."""
    return count_in_prose(source, NOTE_RE, python_source)


def open_note_count(source: str, python_source: bool = False) -> int | None:
    """Count notes still owed an answer, excluding resolution claims."""
    return count_in_prose(source, OPEN_NOTE_RE, python_source)


def solved_note_count(source: str, python_source: bool = False) -> int | None:
    """Count resolution claims waiting to be checked."""
    return count_in_prose(source, SOLVED_NOTE_RE, python_source)


def marker_decision(
    previous: str, updated: str, python_source: bool
) -> KernelDecision | None:
    """Judge what this edit did to the file's review notes.

    Deleting feedback is denied rather than asked. An ask is something an
    agent argues its way through in the same turn that wanted the deletion,
    and the deletion is exactly the act nobody can review afterwards — the
    note is gone, so its absence looks identical to a note that never
    existed. What the agent does instead is convert it: `# lup: solved:` in
    front of the original words leaves the claim in the tree, against the
    text it claims to answer, for a later pass to check.

    That later pass retires a claim — or sends it back to being open
    feedback — through its own instrument, `dev comments --retire` and
    `--restore`, which touches only `solved:` claims. No session's edits are
    exempt here: an environment cannot carry this authority, so a grant that
    claims to is ignored.
    """
    # A revision whose note count could not be established has not been shown
    # to have lost anything, and denying on an unmeasurable difference is
    # what blocked the completing step of every merge resolution: removing a
    # file's last conflict marker is what makes it parse for the first time.
    opened_now = open_note_count(updated, python_source)
    opened_before = open_note_count(previous, python_source)
    claimed_now = solved_note_count(updated, python_source)
    claimed_before = solved_note_count(previous, python_source)
    if (
        opened_now is None
        or opened_before is None
        or claimed_now is None
        or claimed_before is None
    ):
        return None
    opened = opened_now - opened_before
    claimed = claimed_now - claimed_before
    if opened < 0 and opened + claimed == 0:
        return None
    if opened < 0:
        return KernelDecision(
            "deny",
            "this edit removes inline review feedback. Resolving a note means "
            "replacing `# lup:` with `# lup: solved:` and keeping its text, so "
            "the claim can be checked against what was asked; deleting it "
            "leaves nothing to check",
        )
    if claimed < 0:
        return KernelDecision(
            "deny",
            "this edit removes a `# lup: solved:` claim. Only the review pass "
            "retires one — it either confirms the claim and removes the note "
            "(`dev comments --retire file:line`), or restores it to open "
            "feedback (`dev comments --restore file:line`)",
        )
    if opened > 0:
        return KernelDecision("ask", "edit adds inline review feedback")
    return None


def empty_collection_literal(node: ast.expr | None) -> bool:
    """Whether an expression builds an empty dict, list, or set on the spot.

    The one question two rules ask: `empty-collection` to tell a deliberate
    default from a build-then-append seed, and `default-factory` to tell a
    factory an annotated literal could replace from one that does real work.
    Answering it in one place is what keeps the two from disagreeing about the
    same line.
    """
    match node:
        case ast.Dict(keys=[]) | ast.List(elts=[]):
            return True
        case ast.Call(func=ast.Name(id="set"), args=[], keywords=[]):
            return True
    return False


def empty_collection_exempt_lines(source: str) -> set[int]:
    """Return empty-collection lines whose AST context makes the seed deliberate."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    def is_self_attribute(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    exempt: set[int] = set()

    def mark(value: ast.expr | None) -> None:
        if value is not None and empty_collection_literal(value):
            exempt.add(value.lineno)

    def is_loop(node: ast.AST) -> bool:
        return isinstance(node, ast.For | ast.AsyncFor | ast.While)

    def mutated_name(node: ast.AST) -> str | None:
        match node:
            case ast.Call(func=ast.Attribute(value=ast.Name(id=name), attr=attr)) if (
                attr
                in (
                    "append",
                    "appendleft",
                    "extend",
                    "add",
                    "update",
                    "insert",
                    "setdefault",
                )
            ):
                return name
            case ast.Assign(targets=[ast.Subscript(value=ast.Name(id=name))]):
                return name
            case ast.AugAssign(
                target=ast.Name(id=name) | ast.Subscript(value=ast.Name(id=name))
            ):
                return name
        return None

    def exempt_scope_seeds(scope: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        feeding: dict[str, list[bool]] = {}
        for loop in ast.walk(scope):
            if not is_loop(loop):
                continue
            tolerant = any(isinstance(inner, ast.Try) for inner in ast.walk(loop))
            for inner in ast.walk(loop):
                name = mutated_name(inner)
                if name is not None:
                    feeding.setdefault(name, []).append(tolerant)

        def visit(node: ast.AST, in_loop: bool) -> None:
            for child in ast.iter_child_nodes(node):
                match child:
                    case (
                        ast.FunctionDef()
                        | ast.AsyncFunctionDef()
                        | ast.ClassDef()
                        | ast.Lambda()
                    ):
                        continue
                    case (
                        ast.Assign(targets=[ast.Name(id=name)], value=value)
                        | ast.AnnAssign(target=ast.Name(id=name), value=value)
                    ) if empty_collection_literal(value):
                        loops = feeding[name] if name in feeding else None
                        if in_loop:
                            if loops is not None:
                                mark(value)
                        elif loops is None or all(loops):
                            mark(value)
                visit(child, in_loop or is_loop(child))

        visit(scope, False)

    for node in ast.walk(tree):
        match node:
            case ast.Call(keywords=keywords):
                for keyword in keywords:
                    mark(keyword.value)
            case ast.ExceptHandler(body=handler_body):
                for statement in handler_body:
                    match statement:
                        case ast.Assign(value=value) | ast.AnnAssign(value=value):
                            mark(value)
            case ast.Module(body=body) | ast.ClassDef(body=body):
                for statement in body:
                    if isinstance(statement, ast.AnnAssign):
                        mark(statement.value)
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                if node.name == "__init__":
                    for statement in ast.walk(node):
                        match statement:
                            case ast.Assign(targets=targets, value=value) if all(
                                is_self_attribute(target) for target in targets
                            ):
                                mark(value)
                            case ast.AnnAssign(target=target, value=value) if (
                                is_self_attribute(target)
                            ):
                                mark(value)
                exempt_scope_seeds(node)
    return exempt


def empty_collection_factory(node: ast.expr) -> bool:
    """Whether a ``default_factory=`` argument only ever returns an empty collection.

    ``list``/``dict``/``set`` named as the factory, and the lambda spellings of
    the same thing, are what an annotated literal default says instead. The
    literal question is :func:`empty_collection_literal`'s, asked once.
    """
    match node:
        case ast.Name(id="list" | "dict" | "set"):
            return True
        case ast.Lambda(body=body):
            return empty_collection_literal(body)
    return False


def default_factory_exempt_lines(source: str) -> set[int]:
    """Return ``default_factory=`` lines an annotated literal could not replace.

    The rule's replacement is ``items: list[B] = []``: the annotation carries
    the type and pydantic copies the literal per instance. That says the same
    thing only where the factory builds an empty collection — a factory that
    reads another declaration, stamps a value, or constructs a model does work
    no literal expresses, so its line is cleared rather than suppressed.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    return {
        keyword.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "default_factory"
        and not empty_collection_factory(keyword.value)
    }


def dict_get_exempt_lines(source: str) -> set[int]:
    """Return `.get(` lines whose AST context makes the call a decorator.

    A decorator is not payload access. ``@app.get("/path")`` names a route on
    a framework object, and no schema is being read out of a dict. Which
    lines are decorators is decidable from the tree alone, so this holds for
    a fragment that carries no types — and that is what lets a suppression
    here be retired instead of being demanded by the audit and refused by
    the kernel.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                last = decorator.end_lineno or decorator.lineno
                exempt.update(range(decorator.lineno, last + 1))
    return exempt


def tuple_shape_exempt_lines(source: str) -> set[int]:
    """Return `tuple[` lines the tree shows to be variadic rather than positional.

    ``tuple[X, ...]`` is not a shape whose positions want names — it is an
    immutable sequence, and the rule's replacement says nothing about it:
    ``extra_exceptions: tuple[type[Exception], ...]`` must be a tuple because
    that is what ``except`` takes, and a list default would be a mutable-default
    bug. Fixed arity is the defect the rule names, so a line is cleared only
    when every ``tuple[`` on it is variadic — a line carrying both keeps its
    finding rather than being cleared by its neighbour.

    Nesting is why this needs the tree and not a wider regex: in
    ``tuple[dict[str, int], ...]`` the trailing ellipsis sits behind a bracket
    no character class can step over.

    Source that does not parse clears every line. The rule is strong, so a
    verdict it cannot justify would be a denial with no escape, and "cannot
    tell" must fail toward silence — the audit sees the site again as soon as
    the file parses.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set(range(1, len(source.splitlines()) + 2))

    def is_ellipsis(node: ast.expr) -> bool:
        return isinstance(node, ast.Constant) and node.value is Ellipsis

    variadic: set[int] = set()
    fixed: set[int] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Subscript(
                value=ast.Name(id="tuple"), slice=ast.Tuple(elts=elts)
            ) if elts:
                target = variadic if is_ellipsis(elts[-1]) else fixed
                target.add(node.lineno)
            case ast.Subscript(value=ast.Name(id="tuple")):
                fixed.add(node.lineno)
    return variadic - fixed


def slice_exempt_lines(source: str) -> set[int]:
    """Return bounded-prefix-slice lines the tree shows to be something else.

    The pattern reads every ``[:n]``, because what separates an honest prefix
    from a destroyed tail is what becomes of the remainder, and no character
    class can see that. Four contexts settle it without types.

    A slice of a digest derives an identifier. ``hexdigest()[:12]`` shortens a
    hash whose prefix identifies the same thing the whole of it did, and there
    is no content behind it to lose.

    A bound at or under ``ABBREVIATION_CHARS`` abbreviates rather than cuts.
    Nothing anyone wrote survives being shortened to sixteen characters, so a
    slice that narrow is never someone keeping part of a text — it is a git
    SHA, a uuid, a short key. Reading the width rather than the receiver's
    name is what makes this decidable: ``commit[:12]`` carries no type saying
    it is a hash, and a rule that guessed from the identifier would clear a
    variable called ``commit_message`` for the same reason.

    A line carrying the complementary slice is a split. ``x[:n]`` beside
    ``x[n:]`` keeps everything it started with, which is this rule's remedy
    rather than its defect.

    A slice a test reads is sniffing. ``raw[:100].startswith(...)`` asks a
    question of a prefix and stores nothing, so whatever it came from is still
    whole wherever it lives.

    Source that does not parse clears nothing. The rule is soft, so the most an
    unrefined finding costs is a directive carrying a reason.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()

    def prefix_slices(node: ast.AST) -> Iterator[ast.Subscript]:
        """Every ``[:n]`` at or under this node."""
        for inner in ast.walk(node):
            match inner:
                case ast.Subscript(slice=ast.Slice(lower=None, upper=upper)) if (
                    upper is not None
                ):
                    yield inner

    def spanned(node: ast.Subscript) -> range:
        """Every line the sliced expression covers.

        A chain that opens on one line and closes with its ``[:n]`` on another
        reports at the bracket while the node starts at the receiver, so
        clearing only ``lineno`` clears a line the pattern never matched and
        leaves the one it did.
        """
        return range(node.lineno, (node.end_lineno or node.lineno) + 1)

    def derives_a_digest(node: ast.expr) -> bool:
        """Whether the sliced value came from a hash or a uuid."""
        return any(
            isinstance(inner, ast.Attribute) and inner.attr in ("hexdigest", "hex")
            for inner in ast.walk(node)
        )

    def abbreviates(bounds: ast.expr | None) -> bool:
        """Whether the bound is too narrow to be a portion of anything written."""
        return (
            isinstance(bounds, ast.Constant)
            and isinstance(bounds.value, int)
            and bounds.value <= ABBREVIATION_CHARS
        )

    split_lines = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Slice)
        and node.slice.lower is not None
        and node.slice.upper is None
    }

    def tested() -> Iterator[int]:
        """Lines whose prefix slice is read by a comparison rather than kept."""
        for node in ast.walk(tree):
            match node:
                case (
                    ast.Compare()
                    | ast.Call(func=ast.Attribute(attr="startswith" | "endswith"))
                ):
                    for found in prefix_slices(node):
                        yield from spanned(found)

    def settled() -> Iterator[int]:
        """Lines whose prefix slice is a digest, a split, or an abbreviation."""
        for node in prefix_slices(tree):
            if (
                node.lineno in split_lines
                or derives_a_digest(node.value)
                or (isinstance(node.slice, ast.Slice) and abbreviates(node.slice.upper))
            ):
                yield from spanned(node)

    return set(tested()) | set(settled())


def refiner_named(name: str) -> Callable[[str], set[int]] | None:
    """The AST context one row names, where the row names one.

    A rule earns a refiner when its pattern is wider than the defect it names
    and the difference is decidable without types. Which rule has which lives
    at the declaration in `lup.codescan.antipatterns` and travels in the row,
    so this side holds only the functions a row may name — a rule that gains a
    refiner reaches the gate by construction rather than by someone also
    remembering to widen a list of ids here.
    """
    match name:
        case "default_factory_exempt_lines":
            return default_factory_exempt_lines
        case "empty_collection_exempt_lines":
            return empty_collection_exempt_lines
        case "dict_get_exempt_lines":
            return dict_get_exempt_lines
        case "tuple_shape_exempt_lines":
            return tuple_shape_exempt_lines
        case "slice_exempt_lines":
            return slice_exempt_lines
    return None


def refined_exempt_lines(
    source: str, rows: list[AntiPatternRow]
) -> dict[str, set[int]]:
    """Where each refined rule is cleared in this source, computed once.

    A row without the field reads as declaring no refiner, which is what an
    empty name already means here. The table is generated, so a rule can
    arrive from a branch older than the field — and this gate is compiled
    into the dispatcher that decides every edit, whose own recovery is
    regenerating the table. Raising on the malformed table would take down
    the one path that repairs it, and the rule then simply runs unrefined,
    which reports more than it should rather than less.
    """
    found: dict[str, set[int]] = {}
    for row in rows:
        refiner = refiner_named(row["refiner"] if "refiner" in row else "")
        if refiner is not None:
            found[row["id"]] = refiner(source)
    return found


def added_line_numbers(before: str | None, after: str | None) -> dict[int, bool]:
    """Identify added line positions with duplicate-line accounting."""
    remaining = before.splitlines() if before is not None else []
    added: dict[int, bool] = {}
    for number, line in enumerate((after or "").splitlines(), start=1):
        if line in remaining:
            remaining.remove(line)
        else:
            added[number] = True
    return added


def added_lines(before: str | None, after: str | None) -> list[str]:
    """Return added lines with duplicate-line accounting."""
    remaining = before.splitlines() if before is not None else []
    added: list[str] = []
    for line in (after or "").splitlines():
        if line in remaining:
            remaining.remove(line)
        else:
            added.append(line)
    return added


def removed_lines(before: str | None, after: str | None) -> list[str]:
    """Return lines the edit took away, with duplicate-line accounting."""
    remaining = (after or "").splitlines()
    removed: list[str] = []
    for line in (before or "").splitlines():
        if line in remaining:
            remaining.remove(line)
        else:
            removed.append(line)
    return removed


def resites_a_suppression(line: str, gone: list[str]) -> bool:
    """Whether this added directive only re-sites one the same edit removed.

    A suppression gate that reads the added line alone cannot tell
    `# lup: ignore[a, b]` becoming `# lup: ignore[a]` from a suppression
    appearing out of nowhere, and asks about both. The first is the edit the
    audit *demands* when it reports a directive spurious, so asking to approve
    it makes one gate request what the other grants — the same split
    `refined_exempt_lines` exists to avoid.

    Moving one is the same shape and was refused for the same reason. Adopting
    a placement policy necessarily rewrites the markers the old policy allowed,
    so a concern approved to do exactly that met a gate demanding
    `antipattern-suppression` — an allowance that also authorizes genuinely new
    suppressions, which is a narrow action buying a wide permission.

    So the question asked is which *rules* this file suppresses, not which
    lines carry the directives: an added directive naming only ids some removed
    directive named suppresses nothing here that was not already suppressed.
    ``None`` is the bare directive, covering every rule. A new id, a widened
    list and a typed list going bare all still reach the gate.

    Two limits, named rather than implied. Re-siting a rule's directive onto a
    *different* violation of that same rule passes, because rule ids are the
    granularity a per-file text gate can honestly compute — the audit reports
    that one afterwards. And both halves must be one edit: split across two,
    the adding half sees nothing removed and asks.
    """
    match = IGNORE_RE.search(line)
    if match is None:
        return False
    kept = ignore_rule_ids(match)
    for previous in gone:
        earlier = IGNORE_RE.search(previous)
        if earlier is None:
            continue
        covered = ignore_rule_ids(earlier)
        if covered is None:
            return True
        if kept is not None and set(kept) <= set(covered):
            return True
    return False


def is_record_class(node: ast.ClassDef) -> bool:
    """Recognize a declarative record whose field lines are not real changes."""
    records = ("BaseModel", "TypedDict", "Enum", "IntEnum", "StrEnum", "NamedTuple")
    return any(
        (isinstance(base, ast.Name) and base.id in records)
        or (isinstance(base, ast.Attribute) and base.attr in records)
        for base in node.bases
    )


def non_real_lines(source: str) -> set[int]:
    """Return line numbers that do not count toward the small-change gate.

    Blank lines, comments, string and docstring bodies (already blanked by
    ``python_code_lines``), imports, and the field declarations of a pydantic
    or ``TypedDict`` record are boilerplate rather than logic.
    """
    code = python_code_lines(source)
    excluded = {
        number
        for number, line in enumerate(code, start=1)
        if not line or line.isspace()
    }
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return excluded
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and node.end_lineno:
            excluded.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.ClassDef) and is_record_class(node):
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign | ast.Assign)
                    and statement.end_lineno
                ):
                    excluded.update(range(statement.lineno, statement.end_lineno + 1))
    return excluded


def real_added_line_count(
    before: str | None, after: str | None, python_source: bool
) -> int:
    """Count added lines that carry real code, ignoring boilerplate."""
    added = added_line_numbers(before, after)
    lines = (after or "").splitlines()
    if not python_source:
        return sum(
            1
            for number in added
            if lines[number - 1] and not lines[number - 1].isspace()
        )
    excluded = non_real_lines(after or "")
    return sum(1 for number in added if number not in excluded)


def ignore_rule_ids(match: re.Match[str]) -> tuple[str, ...] | None:
    """Return typed ids, or ``None`` for a bare suppression."""
    raw = match.group("ids")
    if raw is None:
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class FileIgnore(TypedDict):
    """Whether a file-level suppression exists, and the ids it names.

    ``ids`` is ``None`` for the bare directive, which disables every rule, and
    distinguishing that from the empty tuple of the no-directive case is why
    ``present`` is carried rather than inferred.
    """

    present: bool
    ids: tuple[str, ...] | None


def file_level_line(source: str, max_lines: int = 10) -> int:
    """The line holding the whole-file directive, or 0 where there is none.

    A file-level opt-out lives in the header, before anything it could be
    mistaken for governing: the run of comments and blank lines a file opens
    with. That boundary is what keeps it apart from a directive standing above
    the one line it guards, which is the same characters in the same shape and
    stops being distinguishable the moment code has intervened.
    """
    for number, line in enumerate(source.splitlines()[:max_lines], start=1):
        if FILE_IGNORE_RE.match(line) is not None:
            return number
        if line.strip() and not line.lstrip().startswith(("#", "//")):
            return 0
    return 0


def file_ignore(source: str) -> FileIgnore:
    """Return whether a file-level suppression exists and the ids it names."""
    number = file_level_line(source)
    if number == 0:
        return FileIgnore(present=False, ids=())
    match = FILE_IGNORE_RE.match(source.splitlines()[number - 1])
    return FileIgnore(present=True, ids=ignore_rule_ids(match) if match else None)


def suppression_site(number: int, line: str) -> str:
    """One suppression, located, named, and quoted before it is approved.

    The rules are named rather than left to be read back out of the quote, so
    an approval states what it approves without anyone parsing the line again.
    The line itself is quoted whole: a directive is written at the end of what
    it guards, which is the end a cut would take first.
    """
    match = IGNORE_RE.search(line)
    named = ignore_rule_ids(match) if match is not None else None
    silenced = ", ".join(named) if named else "every rule"
    return f"line {number} silences {silenced}: {line.strip()}"


def suppression_reason(sites: list[str], creation: bool = False) -> str:
    """Name every suppression this edit declares, not merely that it declares one.

    A permission prompt carries the reason and nothing else, so a verdict
    that said only what kind of thing happened left the reviewer to find the
    line themselves — in a diff they were being asked to approve precisely
    because it needed reading. Every site is listed rather than the first,
    since approving is one decision over the whole batch.

    A creation is the case where that matters most and reads least. The whole
    file arrives at once, so its directives are approved along with everything
    else about it — the layout and the shape, which is what a reviewer is
    reading a new file for — and a line in the middle of a new module is the
    easiest thing in an edit to approve without having seen.
    """
    header = (
        "this new file arrives carrying antipattern suppressions, and writing "
        "it approves them"
        if creation
        else "edit introduces an antipattern suppression"
    )
    return header + "\n" + "\n".join(sites)


class AntiPatternHit(TypedDict):
    """One added line and the rule it matched, before any suppression applies."""

    line: int
    row: AntiPatternRow


def anti_pattern_hits(
    added: dict[int, bool],
    rows: list[AntiPatternRow],
    code_lines: list[str],
    scanned_lines: list[str],
    exempt: dict[str, set[int]],
    tokenized: bool,
) -> Iterator[AntiPatternHit]:
    """Every added line and the rule it matches, before suppressions apply.

    Separating matching from deciding is what lets the strong rules be
    consulted ahead of the suppression gate without the match logic existing
    twice — two copies being how a gate starts disagreeing with itself.
    """
    for number in added:
        masked = scanned_lines[number - 1].strip()
        if not tokenized and masked.startswith("#") and "type:" not in masked:
            continue
        code = code_lines[number - 1].strip()
        for row in rows:
            stripped = code if tokenized and row["context"] == "code" else masked
            if not stripped:
                continue
            if row["id"] in exempt and number in exempt[row["id"]]:
                continue
            if re.search(row["pattern"], stripped) is not None:
                yield AntiPatternHit(line=number, row=row)


def anti_pattern_denial(number: int, row: AntiPatternRow) -> KernelDecision:
    """Deny one matched line, saying whether a directive could have helped."""
    refusal = (
        " (no suppression: write the replacement)"
        if row["strength"] == "strong"
        else ""
    )
    placement = refusal or f" — suppress on {suppression_placement(number)}"
    return KernelDecision(
        "deny",
        f"line {number}: {row['message']}{placement} "
        f"(rule {row['id']} — see docs/rules.md)",
    )


def spurious_refusal(number: int, dead: list[str], live: list[str]) -> KernelDecision:
    """Refuse a directive for the rules it names that nothing it guards trips.

    Asking spends a human turn on ids that were never going to silence
    anything, and approving them is worse than refusing: they read as reviewed
    exceptions while the audit is already calling them dead.

    The remedy is stated as dropping the ids rather than the directive,
    because the rest of one may be doing its job and dropping the whole would
    resurface the denial it was silencing. The reach is stated with it, since
    a directive written one line off from its violation is dead for a reason
    that wants it moved instead. What the line trips is named too, because
    that is the directive the site actually wanted.
    """
    named = ", ".join(dead)
    instead = f" — the line trips {', '.join(live)} instead" if live else ""
    return KernelDecision(
        "deny",
        f"line {number}: this suppression names {named}, which nothing it guards "
        f"trips{instead}. Drop {named} from it: a rule that does not fire is "
        f"silenced by nothing, and the audit reports the directive spurious. One "
        f"written on line {number} reaches that line, and where it stands alone "
        "the line beneath its comment block (see docs/rules.md)",
    )


def antipattern_decision(
    before: str | None,
    after: str,
    rows: list[AntiPatternRow],
    python_source: bool,
    allowances: list[str] | None = None,
) -> KernelDecision | None:
    """Reject newly added unsuppressed anti-patterns and ask on suppressions.

    Each row carries the syntactic context it inspects: a "code" rule is
    matched against token-masked Python (string literals and comments both
    blanked) so prose never trips it, while a "comment" rule targets comment
    directives and sees comments intact. Without a tokenizer (non-Python
    files, fragments that fail to tokenize) every rule scans the raw line.

    A declared suppression is judged before it is asked about. One naming a
    rule that nothing it guards trips is refused outright: it silences
    nothing, so the ask would spend a human turn admitting a marker the audit
    reports spurious the moment it lands — and a marker that suppresses
    nothing is the cheapest way past a gate that asks about every directive
    alike. Only the rules these rows carry are judged; another scanner owns
    `abc-capability` and its family, and a verdict this gate cannot reach is
    not one it may refuse over.

    A violation nothing covers is denied whatever else the edit declares, and
    only then are the declared suppressions asked about. Both halves of that
    order matter: without the first, a directive covering one line bought
    approval for every unsuppressed line beside it; without the second, an
    edit whose violations are all covered would be refused for suppressing
    them, which is the ordinary and approved way to write one.

    A granted ``antipattern-suppression`` allowance turns the two suppression
    asks into allows, because a human already approved the plan that needs
    them. It reaches neither denial: an allowance justifies a typed, argued
    suppression, never a bare anti-pattern and never a dead directive.
    """
    suppression = "allow" if "antipattern-suppression" in (allowances or []) else "ask"
    added = added_line_numbers(before, after)
    original_lines = after.splitlines()
    scanned_lines = (
        mask_python_string_literals(after) if python_source else original_lines
    )
    code_lines = python_code_lines(after) if python_source else original_lines
    exempt = refined_exempt_lines(after, rows) if python_source else {}
    comment_columns = python_comment_columns(after) if python_source else None
    file_level = file_ignore(after)
    has_file_ignore = file_level["present"]
    disabled_ids = file_level["ids"]
    gone = removed_lines(before, after)
    declared: list[int] = []
    for number in added:
        original = original_lines[number - 1]
        directive = IGNORE_RE.search(original)
        if (
            directive is not None
            and not resites_a_suppression(original, gone)
            and (
                not python_source
                or comment_columns is None
                or (
                    number in comment_columns
                    and comment_columns[number] == directive.start()
                )
            )
        ):
            declared.append(number)
    # Which of them this gate may judge for suppressing nothing. The
    # whole-file form governs lines an edit's own text need not contain, so it
    # is asked about like any other and judged by none of what follows.
    # Judging the rest rests on the refiners, and a refiner reads a tree:
    # where the document has none, `tuple_shape_exempt_lines` clears every
    # line by design, and reading a clearance as proof would refuse a
    # directive for this gate's own blindness.
    decidable = not declared or not python_source or python_parses(after)
    whole_file = file_level_line(after)
    judged = [
        number for number in (declared if decidable else []) if number != whole_file
    ]
    tokenized = comment_columns is not None
    hits = list(
        anti_pattern_hits(added, rows, code_lines, scanned_lines, exempt, tokenized)
    )

    def guarded_hits(number: int) -> list[AntiPatternHit]:
        """Every rule tripped by the lines one directive is written to guard.

        Read from the directive's side, through the one placement policy, and
        asked of it rather than guessed: `suppression_reaches` decides, and
        this only offers it the lines below. Offering a fixed pair was exactly
        complete while the policy stopped at the next line, and left a
        directive whose reason spans two lines guarding nothing it could see —
        so the forward check admitted an edit this one then called spurious.

        The rows and the refined exemptions are the ones the gate matched with
        above, so what counts as a trip here is what counts as a trip
        everywhere.
        """
        guarded = {
            candidate: True
            for candidate in range(number, len(original_lines) + 1)
            if suppression_reaches(original_lines, number, candidate)
        }
        return list(
            anti_pattern_hits(
                guarded, rows, code_lines, scanned_lines, exempt, tokenized
            )
        )

    # A strong rule outranks every suppression below it, including the declared
    # gate: its replacement is right every time, so a directive beside it
    # expresses nothing a human should be asked to approve — and approving one
    # would admit an edit `dev check` then refuses.
    for hit in hits:
        if hit["row"]["strength"] == "strong":
            return anti_pattern_denial(hit["line"], hit["row"])

    known_ids = {row["id"] for row in rows}
    for number in judged:
        directive = IGNORE_RE.search(original_lines[number - 1])
        named = ignore_rule_ids(directive) if directive is not None else None
        # Two directives are skipped, both because what they silence is not
        # visible from here. A bare one names no rule and covers every rule
        # there is, including those another scanner owns and these rows cannot
        # see, so nothing here can prove it guards nothing. One standing at the
        # end of the text is the same limit in a different form: the line it
        # guards is not in the text this gate was handed.
        alone = standalone_suppression(original_lines[number - 1]) is not None
        if named is None or (alone and number == len(original_lines)):
            continue
        fired = {hit["row"]["id"] for hit in guarded_hits(number)}
        dead = [rule for rule in named if rule in known_ids and rule not in fired]
        if dead:
            return spurious_refusal(
                number, dead, [rule for rule in sorted(fired) if rule not in named]
            )

    # The same precedence the strong-rule loop above takes, applied to the rest
    # of the table: a violation nothing covers outranks every suppression the
    # edit declares. Deciding the declared ask first left this loop unreachable
    # for any edit that added a directive at all, so one legitimate suppression
    # carried the unsuppressed violations beside it through on its approval —
    # an approval whose reason named only the directive.
    covering: dict[int, bool] = {}
    for hit in hits:
        number = hit["line"]
        rule_id = hit["row"]["id"]
        if has_file_ignore and (disabled_ids is None or rule_id in disabled_ids):
            continue
        holder = covering_suppression_line(original_lines, number)
        original = original_lines[holder - 1] if holder else ""
        directive = IGNORE_RE.search(original) if holder else None
        if directive is not None:
            covered = ignore_rule_ids(directive)
            if covered is None or rule_id in covered:
                # Both suppression asks need the same exemption. A re-sited
                # marker lands on the very line it was moved to cover, so
                # that line is added, trips its rule, and reaches here even
                # when the declaration gate above let it through.
                if resites_a_suppression(original, gone):
                    continue
                covering[holder] = True
                continue
        return anti_pattern_denial(number, hit["row"])

    def sites_at(numbers: list[int]) -> list[str]:
        """The directives written on these lines, rendered for the prompt."""
        return [
            suppression_site(number, original_lines[number - 1]) for number in numbers
        ]

    # Every violation the edit added is covered, so what is left to decide is
    # the suppressions themselves: the ones this edit declares, or the standing
    # one an added line has moved under.
    if declared:
        return KernelDecision(
            suppression, suppression_reason(sites_at(declared), before is None)
        )
    if covering:
        return KernelDecision(suppression, suppression_reason(sites_at(list(covering))))
    return None


def path_rule_matches(path: str, path_exists: bool, row: PathRuleRow) -> bool:
    """Evaluate one primitive protected-path rule.

    The two directory shapes are :func:`root_matches`, which the role table
    reads through as well, so a rule protecting a tree and a role classifying
    the same tree cannot come to disagree about which paths are in it.
    """
    kind = row["kind"]
    value = row["value"]
    portable = normalized_path(path)
    expected = normalized_path(value)
    parts = tuple(part for part in portable.split("/") if part)
    match kind:
        case "exact":
            return portable == expected
        case "subtree":
            return root_matches(path, value, "subtree")
        case "name_prefix":
            return posixpath.basename(portable).startswith(value)
        case "new_subtree":
            return root_matches(path, value, "subtree") and not path_exists
        case "contains_part":
            return root_matches(path, value, "contains_part")
        case "new_devtools":
            return (
                any(
                    parts[index] == "src"
                    and index + 2 < len(parts)
                    and parts[index + 2] == "devtools"
                    for index in range(len(parts))
                )
                and not path_exists
            )
        case _:
            raise ValueError(f"invalid path rule kind {kind!r}")


PACKAGE_MARKER_FILES = ("__init__.py",)
"""Files whose name is their whole content, when they carry nothing else.

The full-write gate exists because creating a file asks a reviewer to read all
of it. A package marker is the case where there is nothing to read: the name
declares a package, and the conventions here say an internal one holds its
docstring and nothing more. A project that marks its packages differently
passes its own names.
"""


def documentation_only(source: str) -> bool:
    """Whether a Python source states nothing beyond what it is.

    An empty file and a lone docstring both qualify. Anything else — an
    import, an assignment, a re-export — is content somebody has to read, so
    the file stops being a marker and is judged as the new module it is.
    Source that does not parse is not a marker either: what it says is exactly
    what could not be established.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    match tree.body:
        case [] | [ast.Expr(value=ast.Constant(value=str()))]:
            return True
    return False


def acceptance_guard_decision(
    guard: AcceptanceGuardRow, autonomous: bool
) -> KernelDecision:
    """Judge one edit to a test-role path against the declared guard.

    This is the one gate where an autonomous identity is held to *more* than
    an ordinary session rather than less, and the inversion is the point
    rather than an oversight. Everywhere else, autonomy means the caller
    reviews its own edits, so a question it would only answer itself is
    dropped. Here the caller's whole contract is to satisfy these tests, so
    it is the one caller for whom editing them is never the right move —
    a human weighing whether a test encodes the wrong behaviour is exactly
    who the ordinary ask reaches, and exactly who an implementer is not.
    """
    if autonomous:
        return KernelDecision("deny", guard["autonomous_reason"])
    return KernelDecision("ask", guard["ask_reason"])


# lup: Editing `.claude/` or `.codex/` should be auto-deny here, carrying the
# redirecting guidance that the `.py` generating it is what to modify instead.
# `GENERATED_PLUGIN_REFUSAL` in the kernel's words module already says exactly
# that, but only the shell path reaches it — an Edit or Write to the same file
# is judged by the ordinary lattice.
#
# lup: It should be possible to *relocate* a note. The gate reads any edit that
# drops the marker line as a deletion, so moving one to the declaration it
# actually concerns is refused with "resolving a note means replacing it with
# solved" — which is not what a move is. This bites hardest in a merge, where
# both sides add at one spot and a note routinely lands against the wrong
# declaration. Recognize a marker whose text reappears elsewhere in the file.
#
def decide_edit(
    path: str,
    before: str | None,
    after: str | None,
    *,
    path_exists: bool,
    path_rules: list[PathRuleRow],
    antipattern_rows: list[AntiPatternRow],
    path_roles: list[PathRoleRow] | None = None,
    maximum_added_lines: int = 3,
    autonomous: bool = False,
    allowances: list[str] | None = None,
    python_source: bool = False,
    acceptance_guard: AcceptanceGuardRow | None = None,
    marker_files: tuple[str, ...] = PACKAGE_MARKER_FILES,
) -> KernelDecision:
    """Apply anti-pattern, path, marker, full-write, deletion, and size gates.

    ``allowances`` names what a human already approved for the concern this
    edit belongs to. A grant releases exactly the gate it names and nothing
    adjacent, so an ungranted session sees the unchanged lattice.

    The two marker families are gated in opposite directions. Feedback is
    judged by :func:`marker_decision`: adding it asks, deleting it is denied,
    and the way through is to convert a note into a claim the review pass can
    check. A suppression is gated where it is declared — an added line
    carrying an `ignore` directive, which :func:`antipattern_decision` asks
    about — while retiring one needs no gate at all: the same decision
    re-reads the uncovered line, allows it where nothing trips, and denies
    where the violation is still live. What makes that verdict trustworthy is
    :func:`refined_exempt_lines`, without which a rule broader than the defect
    it names holds its own suppression in place forever.

    ``acceptance_guard`` is the one gate that answers before the relaxations
    below rather than through them, because it asks whether the file may be
    edited at all. Undeclared, a project judges its tests by the same
    lattice as anything else, which is what every project did before the
    guard existed.

    Each gate reaches as far as its own reason. Anti-patterns, the size gate
    and the full-write gate are all about how production code reads and how
    much of it a reviewer can hold at once, so all three stop at production;
    the marker gate follows the feedback instead and stops only at scratch,
    where nothing persists to be read. A full write only ever asks about
    creating a file — an overwrite carries its predecessor as ``before`` —
    and creating one where the conventions do not reach costs a reviewer
    nothing, which pure deletion already assumed everywhere. ``marker_files``
    is the other end of that same reasoning: a file whose content is nothing
    but its own docstring costs a reviewer nothing either, wherever it sits.
    """
    granted = allowances or []
    previous = before or ""
    updated = after or ""
    role = path_role(path, path_roles or [])
    # Whether this file may be edited at all is prior to how the edit reads,
    # so the guard answers ahead of every gate below — including pure
    # deletion, which would otherwise allow removing the test outright, and
    # the protected-path rules, whose autonomous release must not survive a
    # refusal aimed at exactly that caller.
    if role == "test" and acceptance_guard is not None:
        return acceptance_guard_decision(acceptance_guard, autonomous)
    # The conventions describe how production code should read. A test's
    # subject is production's behaviour, and scratch is disposable, so
    # neither is judged against them.
    if after is not None and role == "production":
        antipattern = antipattern_decision(
            before, after, antipattern_rows, python_source, granted
        )
        # A granted suppression answers this gate and no other, so an allow
        # falls through to the rest of the lattice rather than ending it.
        if antipattern is not None and antipattern.effect != "allow":
            return antipattern
    protected = next(
        (
            row
            for row in path_rules
            if path_rule_matches(path, path_exists, row)
            and not (row["kind"] == "new_devtools" and "new-devtools-module" in granted)
        ),
        None,
    )
    if protected is not None and not (autonomous and protected["allow_autonomous"]):
        return KernelDecision("ask", protected["reason"])
    # Feedback is feedback wherever it is left, so this gate follows the file
    # rather than the conventions: a note on a test still names work somebody
    # owes. Scratch is the exception, and only because nothing there persists
    # to be read — a note in a disposable tree has no reader to protect.
    if role != "scratch":
        marker = marker_decision(previous, updated, python_source)
        if marker is not None:
            return marker
    if before is None and role == "production":
        if autonomous:
            return KernelDecision("allow", "reviewed autonomous full write")
        if posixpath.basename(path) in marker_files and documentation_only(updated):
            return KernelDecision("allow", "a package marker states nothing to review")
        return KernelDecision("ask", "full-file writes require approval")
    if after is None or after == "":
        return KernelDecision("allow", "pure deletion")
    if (
        role == "production"
        and real_added_line_count(before, after, python_source) > maximum_added_lines
    ):
        if autonomous:
            return KernelDecision("allow", "reviewed autonomous edit")
        return KernelDecision("defer", "edit exceeds the small-change gate")
    return KernelDecision("allow", "small safe edit")
