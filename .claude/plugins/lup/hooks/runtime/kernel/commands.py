# lup: ignore[empty-collection, import-re, re-call, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Per-command shell classification for the judged executables."""

import posixpath
import re
from typing import TypedDict

from .decision import KernelDecision, unjudged
from .rows import ShellRuleRow, UrlScopeRow
from .words import (
    INTERPRETERS,
    flag_matches,
    opaque_argument,
    uv_run_words,
)
from .fetch import decide_fetch

SED_SAFE_SHORT_FLAGS = "nErsuz"
SED_SAFE_LONG_OPTIONS = (  # lup: ignore[library-default] — sed's own long spellings of the short flags above
    "--quiet",
    "--silent",
    "--regexp-extended",
    "--separate",
    "--null-data",
)
SED_SUBSTITUTE_FLAG_CHARS = "0123456789gpiImM"


def apply_command_row(row: ShellRuleRow, arguments: list[str]) -> KernelDecision:
    """Return a row's effect, downgrading an allow to ask on a guarded flag.

    On a flag-guarded row an unresolved expansion could become the guarded
    flag at runtime, so opaque words deny toward an explicit literal binding.
    A non-allow row with ``allow_flags`` de-escalates only when every
    argument is exactly one of those flags — the command's declared pure
    read-only form. One with ``read_verbs`` de-escalates when a declared
    verb appears and every word is a literal free of guarded flags — the
    verb pins the invocation to its query action.
    """
    if row["effect"] != "allow" and row["allow_flags"] and arguments:
        if all(word in row["allow_flags"] for word in arguments):
            return KernelDecision(
                "allow", "every argument is a declared read-only flag"
            )
    if row["effect"] != "allow" and row["read_verbs"] and arguments:
        clean = not any(
            opaque_argument(word) or flag_matches(word, row["ask_flags"])
            for word in arguments
        )
        if clean and any(word in row["read_verbs"] for word in arguments):
            return KernelDecision(
                "allow", "a declared read-only verb pins the query action"
            )
    if row["effect"] == "allow" and row["ask_flags"]:
        opaque = next(
            (word for word in arguments if opaque_argument(word)),
            None,
        )
        if opaque is not None:
            return unjudged(
                f"argument {opaque!r} could expand into a guarded flag — bind"
                " it to a literal value first"
            )
        guarded = next(
            (word for word in arguments if flag_matches(word, row["ask_flags"])),
            None,
        )
        if guarded is not None:
            return KernelDecision(
                "ask", row["reason"] or f"{guarded} requires approval"
            )
    return KernelDecision(row["effect"], row["reason"])


class Subcommand(TypedDict):
    """The subcommand word a command line names, and the arguments after it.

    ``word`` is empty when the line carried only global flags, which leaves
    the default row to answer for it.
    """

    word: str
    remainder: list[str]


def split_subcommand(
    executable: str, arguments: list[str], default: ShellRuleRow | None
) -> Subcommand | KernelDecision:
    """Find the subcommand word, honoring global value-taking and guarded flags."""
    ask_flags = default["ask_flags"] if default else []
    value_flags = default["value_flags"] if default else []
    position = 0
    while position < len(arguments):
        word = arguments[position]
        if not word.startswith("-"):
            return Subcommand(word=word, remainder=arguments[position + 1 :])
        if flag_matches(word, ask_flags):
            return KernelDecision(
                "ask", f"{executable} global flag {word} requires approval"
            )
        position += 2 if word in value_flags else 1
    return Subcommand(word="", remainder=[])


def decide_command_rows(words: list[str], rows: list[ShellRuleRow]) -> KernelDecision:
    """Classify a command against the erased vocabulary rows by name and depth."""
    executable = posixpath.basename(words[0])
    matches = [row for row in rows if row["command"] == executable]
    if not matches:
        return unjudged(f"command {executable!r} is not classified")
    arguments = words[1:]
    if not any(row["subcommand"] for row in matches):
        return apply_command_row(
            next(row for row in matches if not row["subcommand"]), arguments
        )
    default = next((row for row in matches if not row["subcommand"]), None)
    split = split_subcommand(executable, arguments, default)
    if isinstance(split, KernelDecision):
        return split
    subword = split["word"]
    remainder = split["remainder"]
    subrows = [row for row in matches if subword and row["subcommand"] == subword]
    if not subrows:
        if default is None:
            return unjudged(f"{executable} {subword} is not classified")
        return apply_command_row(default, arguments)
    if any(row["operation"] for row in subrows):
        opword = next((word for word in remainder if not word.startswith("-")), "")
        oprows = [row for row in subrows if opword and row["operation"] == opword]
        if oprows:
            return apply_command_row(oprows[0], remainder)
        subdefault = next((row for row in subrows if not row["operation"]), None)
        if subdefault is not None:
            return apply_command_row(subdefault, remainder)
        return unjudged(f"{executable} {subword} {opword} is not classified")
    return apply_command_row(subrows[0], remainder)


def scan_sed_delimited(script: str, position: int, parts: int) -> int | None:
    """Scan ``parts`` sections after the delimiter at ``position``.

    The delimiter is whatever character sits at ``position``; backslash
    escapes are honored inside sections.
    """
    if position >= len(script):
        return None
    delimiter = script[position]
    if delimiter.isalnum() or delimiter in " \t\n;\\":
        return None
    cursor = position + 1
    seen = 0
    while cursor < len(script) and seen < parts:
        character = script[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == delimiter:
            seen += 1
        cursor += 1
    return cursor if seen == parts else None


def scan_sed_address(script: str, position: int) -> int | None:
    """Scan one address: a line-number form, ``$``, or a regex form."""
    character = script[position]
    if character == "$":
        return position + 1
    if character.isdigit():
        cursor = position + 1
        while cursor < len(script) and script[cursor].isdigit():
            cursor += 1
        if cursor < len(script) and script[cursor] == "~":
            cursor += 1
            while cursor < len(script) and script[cursor].isdigit():
                cursor += 1
        return cursor
    if character == "+":
        cursor = position + 1
        while cursor < len(script) and script[cursor].isdigit():
            cursor += 1
        return cursor if cursor > position + 1 else None
    end = (
        scan_sed_delimited(script, position, 1)
        if character == "/"
        else scan_sed_delimited(script, position + 1, 1)
        if character == "\\" and position + 1 < len(script)
        else None
    )
    if end is None:
        return None
    while end < len(script) and script[end] in "IM":
        end += 1
    return end


def scan_sed_command(script: str, position: int) -> int | None:
    """Scan one address-guarded command, returning the position after it.

    Accepted commands read the input and write standard output only: print,
    delete, hold-space, branching, labels, blocks, line numbering, text
    insertion, file reading, transliteration, and flag-screened substitution.
    The write and execute forms (``w``, ``W``, ``e``, ``s///e``, ``s///w``)
    fall out as unrecognized trailing characters.
    """
    length = len(script)
    address = scan_sed_address(script, position)
    if address is not None:
        position = address
        while position < length and script[position] in " \t":
            position += 1
        if position < length and script[position] == ",":
            position += 1
            while position < length and script[position] in " \t":
                position += 1
            if position >= length:
                return None
            second = scan_sed_address(script, position)
            if second is None:
                return None
            position = second
    while position < length and script[position] in " \t!":
        position += 1
    if position >= length:
        return None
    command = script[position]
    if command in "pPdDnNgGhHxz=F{}":
        return position + 1
    if command in "qQl":
        cursor = position + 1
        while cursor < length and (script[cursor].isdigit() or script[cursor] == " "):
            cursor += 1
        return cursor
    if command in "btT:":
        cursor = position + 1
        while cursor < length and script[cursor] in " \t":
            cursor += 1
        while cursor < length and (script[cursor].isalnum() or script[cursor] == "_"):
            cursor += 1
        return cursor
    if command in "aicrR":
        newline = script.find("\n", position)
        return length if newline == -1 else newline
    if command == "s":
        end = scan_sed_delimited(script, position + 1, 2)
        if end is None:
            return None
        while end < length and script[end] in SED_SUBSTITUTE_FLAG_CHARS:
            end += 1
        return end
    if command == "y":
        return scan_sed_delimited(script, position + 1, 2)
    return None


def safe_sed_script(script: str) -> bool:
    """Accept only scripts whose every command reads input and prints output."""
    length = len(script)
    position = 0
    while position < length:
        if script[position] in " \t\n;":
            position += 1
            continue
        end = scan_sed_command(script, position)
        if end is None:
            return False
        position = end
    return True


def decide_sed_words(words: list[str]) -> KernelDecision:
    """Allow only read-only sed: safe flags plus a safe script grammar.

    ``--sandbox`` makes sed itself reject the write and execute commands, so
    the script screen is skipped under it; in-place editing and script files
    stay denied toward the Edit tool and inline scripts.
    """
    scripts: list[str] = []
    positional: list[str] = []
    script_expected = False
    script_from_options = False
    sandbox = False
    for word in words[1:]:
        if script_expected:
            scripts.append(word)
            script_expected = False
            continue
        if word.startswith("--"):
            name, separator, value = word.partition("=")
            if name in ("--in-place", "--inplace"):
                return KernelDecision(
                    "deny", "in-place sed bypasses the edit policy — use Edit"
                )
            if name == "--file":
                return KernelDecision(
                    "deny", "sed script files are not screened — inline the script"
                )
            if name == "--sandbox" and not separator:
                sandbox = True
                continue
            if name == "--expression":
                if separator:
                    scripts.append(value)
                script_expected = not separator
                script_from_options = True
                continue
            if name in SED_SAFE_LONG_OPTIONS and not separator:
                continue
            return unjudged(f"sed option {name!r} is not classified")
        if word.startswith("-") and len(word) > 1:
            flags = word[1:]
            if "i" in flags:
                return KernelDecision(
                    "deny", "in-place sed bypasses the edit policy — use Edit"
                )
            if "f" in flags:
                return KernelDecision(
                    "deny", "sed script files are not screened — inline the script"
                )
            if flags.endswith("e"):
                script_expected = True
                script_from_options = True
                flags = flags[:-1]
            if any(flag not in SED_SAFE_SHORT_FLAGS for flag in flags):
                return unjudged(f"sed option {word!r} is not classified")
            continue
        positional.append(word)
    if script_expected:
        return unjudged("sed expression flag has no script")
    if not script_from_options and positional:
        scripts.append(positional.pop(0))
    if not sandbox and not all(safe_sed_script(script) for script in scripts):
        return unjudged("sed script is not classified as read-only")
    return KernelDecision("allow", "read-only sed script")


def safe_awk_program(program: str) -> bool:
    """Accept only awk programs with no exec, command input, or write path.

    ``system`` and ``getline`` reach commands and files, ``@`` covers gawk's
    include/load directives and indirect calls, and a pipe that is not ``||``
    feeds or reads a command. A bare ``>`` (not ``>=``) only writes when the
    program can also ``print``; without a print there is no write path, so
    comparison-only programs like ``$3 > 5`` stay read-only.
    """
    if any(token in program for token in ("system", "getline", "@")):
        return False
    if re.search(r"(?<!\|)\|(?!\|)", program) is not None:
        return False
    redirects = re.search(r">(?!=)", program) is not None
    return not (redirects and "print" in program)


def decide_awk_words(words: list[str]) -> KernelDecision:
    """Allow only read-only awk: separator and variable flags plus a safe program."""
    positional: list[str] = []
    value_expected = False
    options_ended = False
    for word in words[1:]:
        if value_expected:
            value_expected = False
            continue
        if options_ended or not word.startswith("-") or word == "-":
            positional.append(word)
            options_ended = True
            continue
        if word == "--":
            options_ended = True
            continue
        if word in ("-F", "-v"):
            value_expected = True
            continue
        if word.startswith(("-F", "-v")) and not word.startswith("--"):
            continue
        return unjudged(f"awk option {word!r} is not classified")
    if value_expected:
        return unjudged("awk option flag has no value")
    if not positional:
        return unjudged("awk has no program")
    if not safe_awk_program(positional[0]):
        return unjudged("awk program is not classified as read-only")
    return KernelDecision("allow", "read-only awk program")


# lup: ignore[library-default] — curl's own flags that change reporting and not the request; the value follows curl's manual, not a project's taste
CURL_SAFE_FLAGS = (
    "-s",
    "--silent",
    "-S",
    "--show-error",
    "-f",
    "--fail",
    "--fail-with-body",
    "-i",
    "--include",
    "-I",
    "--head",
    "-v",
    "--verbose",
    "--compressed",
    "--no-progress-meter",
    "-g",
    "--globoff",
    "-4",
    "-6",
)
# The single letters above, which curl accepts clustered as readily as apart.
# `-sS` is one word to every shell and to curl, and reading it as an unknown
# option refused the request's most ordinary spellings while admitting the
# same flags written with spaces between them.
CURL_SAFE_CLUSTER_LETTERS = "".join(
    flag[1] for flag in CURL_SAFE_FLAGS if len(flag) == 2 and not flag[1].isdigit()
)


def curl_safe_flag(word: str) -> bool:
    """Whether one curl word is a declared reporting flag, clustered or alone."""
    if word in CURL_SAFE_FLAGS:
        return True
    return (
        len(word) > 1
        and word.startswith("-")
        and not word.startswith("--")
        and all(letter in CURL_SAFE_CLUSTER_LETTERS for letter in word[1:])
    )


CURL_VALUE_FLAGS = (  # lup: ignore[library-default] — curl's own value-taking flags; misreading one shifts the argument scan
    "-H",
    "--header",
    "-m",
    "--max-time",
    "--connect-timeout",
    "--retry",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-r",
    "--range",
)


def curl_url(word: str) -> str:
    """Spell one curl operand the way curl itself resolves it.

    curl accepts a URL with no ``scheme://`` and guesses one, defaulting to
    HTTP — which is how a liveness probe is actually typed, and how its own
    manual documents it. Reading the bare form as malformed put an approval
    question on ``curl localhost:8000/health`` while the identical request
    spelled in full was already declared safe.

    Guessing HTTP where curl guesses HTTP keeps the verdict conservative on
    its own terms: a scope declared for ``https`` alone does not match the
    guess, so an origin reachable only over TLS still asks rather than
    inheriting a grant its scheme never gave.
    """
    return word if "://" in word else f"http://{word}"


def decide_curl_words(
    words: list[str],
    allowed_scopes: list[UrlScopeRow],
    denied_scopes: list[UrlScopeRow],
) -> KernelDecision:
    """Allow only read-method curl against the declared fetch scopes.

    Every positional word must be a URL the fetch policy allows; unlisted
    origins stay an approval question and denied origins deny. Flags that
    write files, send data, or carry credentials are not classified.
    """
    urls: list[str] = []
    expect_value = False
    expect_method = False
    method = "GET"
    for word in words[1:]:
        if expect_value:
            expect_value = False
            continue
        if expect_method:
            method = word
            expect_method = False
            continue
        if word in ("-X", "--request"):
            expect_method = True
            continue
        if word.startswith("--request="):
            method = word.partition("=")[2]
            continue
        if curl_safe_flag(word):
            continue
        if word in CURL_VALUE_FLAGS:
            expect_value = True
            continue
        if (
            word.startswith("--")
            and "=" in word
            and word.partition("=")[0] in CURL_VALUE_FLAGS
        ):
            continue
        if word.startswith("-"):
            return unjudged(f"curl option {word!r} is not classified")
        urls.append(word)
    if expect_value or expect_method:
        return unjudged("curl option has no value")
    if method not in ("GET", "HEAD"):
        return KernelDecision(
            "ask", f"curl {method} can change remote state — requires approval"
        )
    if not urls:
        return unjudged("curl has no URL")
    for url in urls:
        verdict = decide_fetch(curl_url(url), allowed_scopes, denied_scopes)
        if verdict.effect != "allow":
            return verdict
    return KernelDecision("allow", "read-only curl within declared scopes")


def decide_uv(words: list[str], runner_targets: list[str]) -> KernelDecision:
    """Classify a uv invocation, gating dependency and inline-code forms."""
    subcommand = words[1]
    if subcommand in ("add", "sync"):
        return KernelDecision(
            "ask", "dependency changes fetch and execute external code"
        )
    if subcommand in ("remove", "lock"):
        return KernelDecision("allow")
    if subcommand == "run" and len(words) > 2:
        run_words = uv_run_words(words)
        if not run_words:
            return unjudged("uv run has no command")
        run_command = posixpath.basename(run_words[0])
        bare_target = "/" not in run_words[0]
        if run_command in INTERPRETERS or run_command in ("-c", "-m", "--script"):
            return KernelDecision("deny", "inline code is not allowed")
        risky = ("--with", "--with-editable", "--with-requirements", "--env-file")
        if any(
            word == option or word.startswith(option + "=")
            for word in words[2:]
            for option in risky
        ):
            return KernelDecision(
                "ask", "uv run --with fetches and executes external code"
            )
        if bare_target and run_command in runner_targets:
            return KernelDecision("allow")
        if bare_target and len(run_words) == 2 and run_words[1] == "--help":
            return KernelDecision("allow", "command help is read-only")
    return unjudged(f"uv {words[1]} is not classified")


def git_checkout_pathspec(words: list[str]) -> KernelDecision | None:
    """Recognize ``git checkout <ref> -- <path>...`` — a ref-sourced restore.

    Content comes from a named commit, so committed state is recoverable
    through the reflog; the branch-switch and index-sourced ``checkout --
    <path>`` forms fall through to their redirect rows, and opaque words
    deny toward explicit literal bindings.
    """
    if len(words) < 5 or words[1] != "checkout" or words[3] != "--":
        return None
    ref = words[2]
    if ref.startswith("-") or opaque_argument(ref):
        return None
    if any(opaque_argument(word) for word in words[4:]):
        return None
    return KernelDecision(
        "allow", "checkout from a named ref restores committed file state"
    )


def git_restore_source(words: list[str]) -> KernelDecision | None:
    """Recognize ``git restore --source=<ref> [--staged|--worktree] <path>...``.

    The ref-sourced twin of ``git checkout <ref> -- <path>``: content comes
    from a named commit, so committed state stays recoverable through the
    reflog. The index-sourced form and opaque words fall through to the
    restore row's ask.
    """
    if len(words) < 4 or words[1] != "restore":
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
    if source is None or not paths:
        return None
    if source.startswith("-") or opaque_argument(source):
        return None
    if any(opaque_argument(word) for word in paths):
        return None
    return KernelDecision(
        "allow", "restore from a named ref recovers committed file state"
    )
