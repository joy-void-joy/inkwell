#!/usr/bin/env python3
"""PreToolUse hook that auto-allows small, safe Edit operations.

Decision order:
1. Protected files (.claude/, pyproject.toml, .env*) -> always defer
2. Anti-patterns in .py files (typing: Any, # type: ignore, Generic[], __all__,
   dict[str, object]; string manipulation: import re, re.*, .replace, .split;
   except Exception, dataclasses, subprocess, argparse, rich.progress, _prefixes):
   - file already has `# claude: ignore` on disk -> allow (skip checks)
   - violating line has inline `# claude: ignore` -> ask (user prompt)
   - no marker -> deny with hint about `# claude: ignore`
3. Edit introduces `# claude: ignore` -> ask (user prompt)
4. Pure deletion (new_string is empty) -> allow
5. replace_all: allow
6. Count nontrivial added lines per change block (using a state machine
   for context-aware classification) -> allow if every block <= MAX_REAL_CHANGES
"""

import difflib
import json
import re
import sys

from pydantic import BaseModel, ValidationError

MAX_REAL_CHANGES = 3

PROTECTED_PATTERNS = [
    r"(^|/)\.claude/",
    r"(^|/)pyproject\.toml$",
    r"(^|/)\.env($|\.)",
]

CLAUDE_IGNORE_MARKER = "# claude: ignore"

ANTI_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bAny\b"),
        "Never use Any — use specific types, TypedDict, or BaseModel",
    ),
    (
        re.compile(r"#\s*type:\s*ignore"),
        "Never use # type: ignore — fix the type error properly",
    ),
    (re.compile(r"#\s*noqa\b"), "Never use # noqa — fix the lint issue properly"),
    (
        re.compile(r"\bGeneric\["),
        "Use Python 3.12+ class[T] syntax instead of Generic[T]",
    ),
    (
        re.compile(r"__all__\s*[=:]"),
        "No __all__ — import directly from the defining module",
    ),
    (
        re.compile(r"\bdict\[\s*str\s*,\s*object\s*\]"),
        "Never use dict[str, object] — use TypedDict or BaseModel",
    ),
    (
        re.compile(r"\bimport\s+re\b"),
        "`import re` is a code smell — use structured APIs (json, pathlib, urllib.parse, etc.)",
    ),
    (
        re.compile(r"\bfrom\s+re\s+import\b"),
        "`from re import` is a code smell — use structured APIs instead",
    ),
    (
        re.compile(r"\bre\.(compile|search|match|fullmatch|sub|findall|split)\s*\("),
        "Avoid regex for structured data — use proper parsers (json, pathlib, urllib.parse, xml, etc.)",
    ),
    (
        re.compile(r"\.replace\s*\("),
        "Avoid .replace() for structured data — use proper parsers",
    ),
    (
        re.compile(r"\.split\s*\("),
        "Avoid .split() for structured data — use proper parsers",
    ),
    (
        re.compile(r"\bexcept\s+Exception\b"),
        "No bare `except Exception` — catch specific exceptions (KeyError, ValueError, OSError, etc.)",
    ),
    (
        re.compile(r"@dataclass|\bfrom\s+dataclasses\s+import\b"),
        "Use Pydantic BaseModel (or TypedDict) instead of dataclasses",
    ),
    (
        re.compile(r"\bimport\s+subprocess\b|\bfrom\s+subprocess\s+import\b"),
        "Use the `sh` library instead of subprocess",
    ),
    (
        re.compile(r"\bimport\s+argparse\b|\bfrom\s+argparse\s+import\b"),
        "Use `typer` instead of argparse",
    ),
    (
        re.compile(r"\brich\.progress\b|\bfrom\s+rich\.progress\s+import\b"),
        "Use `tqdm` instead of rich progress bars",
    ),
    (
        re.compile(r"\bdef\s+_[a-zA-Z]"),
        "No `_` prefix on functions/methods — nothing is private (nest inside caller if needed)",
    ),
    (
        re.compile(r"\bclass\s+_[A-Z]"),
        "No `_` prefix on classes — nothing is private",
    ),
    (
        re.compile(r"^_[a-zA-Z]\w*\s*[=:]"),
        "No `_` prefix on variables/constants — nothing is private",
    ),
]


def is_protected_file(file_path: str) -> bool:
    return any(re.search(p, file_path) for p in PROTECTED_PATTERNS)


def has_file_level_ignore(file_path: str) -> bool:
    """Check if the file on disk has a `# claude: ignore` marker in the first 10 lines."""
    try:
        with open(file_path) as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                if line.strip() == CLAUDE_IGNORE_MARKER:
                    return True
    except OSError:
        pass
    return False


STRING_PREFIX_RE = re.compile(r"^[fFbBrRuU]*")


def is_string_literal(stripped: str) -> bool:
    """Check if a stripped line is a string literal (possibly with trailing comma)."""
    s = stripped.rstrip(",").rstrip()
    if len(s) < 2:
        return False
    bare = STRING_PREFIX_RE.sub("", s)
    if len(bare) < 2:
        return False
    for q in ('"""', "'''"):
        if bare.startswith(q) and bare.endswith(q) and len(bare) >= 2 * len(q):
            return True
    for q in ('"', "'"):
        if bare.startswith(q) and bare.endswith(q):
            return True
    return False


def is_trivial_content(stripped: str) -> bool:
    if not stripped:
        return True
    # Non-alpha lines: ), ], }, ):, etc.
    if not any(c.isalpha() for c in stripped):
        return True
    if stripped.startswith("#"):
        return True
    if stripped.startswith(("import ", "from ")):
        return True
    if stripped == "pass":
        return True
    if is_string_literal(stripped):
        return True
    # Type annotations / field definitions: name: Type, name: Type = value
    if re.match(r"^\w+\s*:\s*\S", stripped):
        return True
    return False


def classify_trivial(lines: list[str]) -> list[bool]:
    result: list[bool] = []
    in_docstring = False
    docstring_delim = ""
    in_import = False
    in_type_def = False
    type_def_indent = 0

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else 0

        if in_docstring:
            result.append(True)
            if docstring_delim in stripped:
                in_docstring = False
            continue

        if in_import:
            result.append(True)
            if ")" in stripped:
                in_import = False
            continue

        for delim in ('"""', "'''"):
            if delim in stripped:
                if stripped.count(delim) == 1:
                    in_docstring = True
                    docstring_delim = delim
                result.append(True)
                break
        else:
            if is_trivial_content(stripped) and "(" in stripped and ")" not in stripped:
                in_import = True
                result.append(True)
                continue

            if in_type_def and stripped and indent <= type_def_indent:
                in_type_def = False

            m = re.match(
                r"(\s*)class\s+\w+\s*\(.*(?:TypedDict|BaseModel).*\)\s*:", line
            )
            if m:
                in_type_def = True
                type_def_indent = len(m.group(1))
                result.append(True)
            elif in_type_def:
                result.append(True)
            else:
                result.append(is_trivial_content(stripped))

    return result


def count_real_additions(old_string: str, new_string: str) -> int:
    """Return the max nontrivial addition count across change blocks.

    Consecutive insert/replace/delete opcodes form a single block.
    Each block is checked independently, so multiple small changes
    scattered through the diff are each allowed up to MAX_REAL_CHANGES.
    """
    old_lines = old_string.splitlines() if old_string else []
    new_lines = new_string.splitlines() if new_string else []

    matcher = difflib.SequenceMatcher(
        None,
        [ln.strip() for ln in old_lines],
        [ln.strip() for ln in new_lines],
    )

    trivial = classify_trivial(new_lines)

    max_nontrivial = 0
    current_block: set[int] = set()

    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            current_block.update(range(j1, j2))
        elif tag == "equal":
            if current_block:
                n = sum(1 for j in current_block if not trivial[j])
                max_nontrivial = max(max_nontrivial, n)
                current_block = set()
        # "delete" doesn't add indices but doesn't break the block

    if current_block:
        n = sum(1 for j in current_block if not trivial[j])
        max_nontrivial = max(max_nontrivial, n)

    return max_nontrivial


AllowDecision = dict[str, dict[str, str]]


class EditInput(BaseModel):
    file_path: str = ""
    old_string: str = ""
    new_string: str = ""
    replace_all: bool = False


class HookEvent(BaseModel):
    tool_name: str = ""
    tool_input: EditInput = EditInput()


def allow_decision() -> AllowDecision:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Auto-allowed: safe edit pattern detected",
        }
    }


def deny_decision(reason: str) -> AllowDecision:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def ask_decision(reason: str) -> AllowDecision:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def find_anti_pattern_violations(
    old_string: str, new_string: str, file_path: str = ""
) -> tuple[str, str] | None:
    """Check newly added lines for typing anti-patterns.

    Returns (decision, reason) or None. Decision is "allow", "ask", or "deny".
    - File-level `# claude: ignore` already on disk -> "allow"
    - Inline `# claude: ignore` on the violating line -> "ask"
    - No marker -> "deny" with hint
    """
    if file_path and has_file_level_ignore(file_path):
        return ("allow", "File has `# claude: ignore` marker")

    old_lines = old_string.splitlines() if old_string else []
    new_lines = new_string.splitlines() if new_string else []

    matcher = difflib.SequenceMatcher(
        None,
        [ln.strip() for ln in old_lines],
        [ln.strip() for ln in new_lines],
    )

    ask_reasons: list[str] = []

    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("insert", "replace"):
            continue
        for idx in range(j1, j2):
            stripped = new_lines[idx].strip()
            if not stripped or stripped.startswith("#") and "type:" not in stripped:
                continue
            for pattern, reason in ANTI_PATTERNS:
                if not pattern.search(stripped):
                    continue
                preview = stripped[:80]
                if CLAUDE_IGNORE_MARKER in new_lines[idx]:
                    ask_reasons.append(f"{reason} | line: {preview}")
                else:
                    hint = f"Add `{CLAUDE_IGNORE_MARKER}` to the line (or file-level) to request approval"
                    return ("deny", f"Denied: {reason} | line: {preview}. {hint}")
                break

    if ask_reasons:
        return ("ask", ask_reasons[0])
    return None


def decide(tool_input: EditInput) -> AllowDecision | None:
    file_path = tool_input.file_path
    old_string = tool_input.old_string
    new_string = tool_input.new_string
    replace_all = tool_input.replace_all

    if is_protected_file(file_path):
        return None

    if file_path.endswith(".py") and new_string:
        violation = find_anti_pattern_violations(old_string, new_string, file_path)
        if violation:
            decision, reason = violation
            match decision:
                case "allow":
                    return allow_decision()
                case "ask":
                    return ask_decision(reason)
                case "deny":
                    return deny_decision(reason)

    if CLAUDE_IGNORE_MARKER in (new_string or ""):
        return ask_decision(
            "Edit introduces `# claude: ignore` — requires user approval"
        )

    if old_string and not new_string:
        return allow_decision()

    if replace_all:
        return allow_decision()

    if count_real_additions(old_string, new_string) <= MAX_REAL_CHANGES:
        return allow_decision()

    return None


def main() -> None:
    try:
        event = HookEvent.model_validate_json(sys.stdin.read())
    except (ValidationError, OSError):
        sys.exit(0)

    if event.tool_name != "Edit":
        sys.exit(0)

    result = decide(event.tool_input)
    if result:
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
