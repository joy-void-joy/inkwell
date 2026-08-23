"""Where an archive or compression verb writes, read from the command alone.

These verbs ask by default because they place content and can replace what
stands where it lands. Often they replace nothing — an archive unpacked into
a directory that does not exist yet destroys nothing — and the ask there buys
a prompt and no decision, which is the same argument the delete verbs already
answer with :func:`confined_to_recoverable_roots`.

What is answerable here is bounded, and the boundary is the design. An
extraction's written set lives *inside* the archive, and establishing it would
mean running an archive tool over untrusted input at the moment of judging. So
the question asked instead is about the destination: an extraction landing
somewhere empty overwrites nothing whatever the archive holds. A destination
already occupied keeps the verb's ask, because what would be replaced there is
exactly what cannot be known without the listing.

Every parser returns ``None`` for a line it does not fully model, and ``None``
means the verb's own ask stands. That is the conservative direction: a flag
not modelled here could move the destination, name a second archive, or let
members land outside the directory the rest of the command promised.
"""

import posixpath
from pathlib import PurePosixPath
from typing import TypedDict

# Flags that let an extraction place members outside the directory selected
# for it, which is the one assumption the destination question rests on.
ESCAPING_TAR_FLAGS = ("-P", "--absolute-names")
# Long flags carrying a value as a separate word, which would otherwise read
# as an operand and be mistaken for a path the verb writes.
VALUED_TAR_FLAGS = ("--file", "--directory", "--transform", "--strip-components")


class ArchiveWrite(TypedDict):
    """What one archive or compression verb would put where.

    The two file lists are separate because absence means opposite things of
    them. An ``authored`` path is brought into being, so nothing being there
    is the whole reason it costs nothing. A ``consumed`` path is destroyed —
    gzip replaces its operand rather than adding beside it — and there
    absence is not a grant but a fact the host could not establish, which for
    a destructive verb reads as a question rather than an answer.

    ``directory`` is a tree unpacked into, whose contents come from the
    archive rather than from the command, so it is judged by whether anything
    is there to replace. It is ``None`` for an extraction that named no
    destination and therefore unpacks where it stands: the working directory
    is normally the repository itself, so it is both certain to be occupied
    and the last place an unread archive should land unasked. Naming nothing
    leaves every field empty, which grants nothing.
    """

    authored: list[str]
    consumed: list[str]
    directory: str | None


def tar_write(
    words: list[str],
    escaping_flags: tuple[str, ...] = ESCAPING_TAR_FLAGS,
    valued_flags: tuple[str, ...] = VALUED_TAR_FLAGS,
) -> ArchiveWrite | None:
    """Where a tar invocation writes, or ``None`` where the line is unmodelled.

    Mode comes from the operation letter wherever it sits — bundled as
    ``-xzf``, alone as ``-x``, or spelled ``--extract`` — because tar accepts
    all three and recognizing one spelling would leave the others unjudged.
    Appending and updating (``-r``, ``-u``, ``-A``) rewrite an existing
    archive, so they are not modelled and keep the ask.
    """
    mode: str | None = None
    archive: str | None = None
    directory: str | None = None
    operands: list[str] = []  # lup: ignore[empty-collection]
    expecting: str | None = None
    for word in words[1:]:
        if expecting is not None:
            archive, directory = (
                (word, directory) if expecting == "f" else (archive, word)
            )
            expecting = None
            continue
        if word in escaping_flags:
            return None
        if word.startswith("--"):
            name, _, value = word.partition("=")  # lup: ignore[string-split]
            match name:
                case "--extract" | "--get":
                    mode = "x"
                case "--create":
                    mode = "c"
                case "--file" if value:
                    archive = value
                case "--directory" if value:
                    directory = value
                case _ if name in valued_flags and not value:
                    return None
                case _:
                    continue
            continue
        if word.startswith("-") and len(word) > 1:
            for letter in word[1:]:
                match letter:
                    case "x" | "c":
                        mode = letter
                    case "f" | "C":
                        expecting = "f" if letter == "f" else "C"
                    case "P":
                        return None
            continue
        operands.append(word)
    if expecting is not None:
        return None
    # The bare `tar xf a.tgz` form puts the operation letters in the first
    # operand, which the flag scan above never sees.
    if mode is None and operands and not operands[0].startswith("-"):
        return None
    match mode:
        case "c":
            return ArchiveWrite(
                authored=[archive] if archive else [], consumed=[], directory=None
            )
        case "x":
            return ArchiveWrite(authored=[], consumed=[], directory=directory)
        case _:
            return None


def unzip_write(words: list[str]) -> ArchiveWrite | None:
    """Where an unzip invocation extracts, or ``None`` where it is unmodelled.

    ``-d`` selects the destination and every other flag either reads or
    changes how members are written inside it. Overwrite and never-overwrite
    (``-o``, ``-n``) are immaterial to a destination with nothing in it.
    """
    directory: str | None = None
    expecting = False
    for word in words[1:]:
        if expecting:
            directory = word
            expecting = False
            continue
        if word == "-d":
            expecting = True
            continue
        if word.startswith("-") and len(word) > 1:
            if any(letter not in "onqjaCLXKVMTfu" for letter in word[1:]):
                return None
            continue
    if expecting:
        return None
    return ArchiveWrite(authored=[], consumed=[], directory=directory)


def compression_write(executable: str, words: list[str]) -> ArchiveWrite | None:
    """Where gzip or gunzip puts its output, or ``None`` where unmodelled.

    Both replace their operand rather than adding beside it: gzip authors
    ``f.gz`` and removes ``f``, gunzip the reverse. Both paths are named, so
    both are judged — the delete half is what makes ``gzip`` on an untracked
    file rightly keep its ask while a committed, unmodified one does not.

    ``-c`` writes to standard output and touches no operand, but a redirection
    then decides where that lands and is judged on its own. ``-r`` walks a
    tree, so which paths are touched stops being what the line says.
    """
    authored: list[str] = []  # lup: ignore[empty-collection]
    consumed: list[str] = []  # lup: ignore[empty-collection]
    for word in words[1:]:
        if word.startswith("-") and len(word) > 1:
            if word.startswith("--") or any(
                letter not in "fkqv" for letter in word[1:]
            ):
                return None
            continue
        suffixed = PurePosixPath(word)
        if executable == "gzip":
            authored.append(f"{word}.gz")
            consumed.append(word)
            continue
        if suffixed.suffix not in (".gz", ".tgz"):
            return None
        authored.append(str(suffixed.with_suffix("")))
        consumed.append(word)
    if not consumed:
        return None
    return ArchiveWrite(authored=authored, consumed=consumed, directory=None)


def archive_write(words: list[str]) -> ArchiveWrite | None:
    """Where an archive or compression verb writes, or ``None`` when unmodelled.

    The dispatch is on the utility's own name because what each does with its
    operands is a fact about that utility, not a shape they share.
    """
    if not words:
        return None
    match posixpath.basename(words[0]):
        case "tar":
            return tar_write(words)
        case "unzip":
            return unzip_write(words)
        case "gzip" | "gunzip" as executable:
            return compression_write(executable, words)
        case _:
            return None
