"""What inkwell grants, refuses, and enables for itself.

The rendering is the library's (:mod:`lup.devtools.harness.settings`), which
derives the marketplace key, the plugin enablement, the served-tool grants,
and the sandbox boundaries from the declaration itself. Named here is only
the half no derivation can reach.
"""

from lup.devtools.harness.settings import Settings, project_settings as render
from lup.harness.models import Plugin
from lup.types import JsonObject

DECLARED = Settings(
    base={
        "coauthorship": False,
        "fileSuggestion": {
            "command": ".claude/plugins/lup/scripts/file_suggest.sh",
            "type": "command",
        },
    },
    official_plugins={
        "agent-sdk-dev@claude-plugins-official": True,
        "claude-md-management@claude-plugins-official": True,
        "github@claude-plugins-official": True,
        "pyright-lsp@claude-plugins-official": True,
    },
    allowed=[
        "WebSearch",
        "Skill(lup:hooks)",
        # Entering and leaving a worktree is the first thing a session does
        # and the last, and neither tool writes: EnterWorktree moves into a
        # tree `dev worktree create` already made, and ExitWorktree only
        # removes one when asked to, which is its own question.
        "EnterWorktree",
        "ExitWorktree",
        "Read(./.claude/settings.json.local*)",
        "Read(./sync.json.local)",
    ],
    denied=[
        "Read(./**/.env*.local)",
        "Read(./**/.env.local)",
        "Read(./**/secrets*.local)",
        "Read(./**/*.secret.local)",
    ],
)


def project_settings(plugin: Plugin | None) -> JsonObject:
    """Render inkwell's settings artifact from its declaration."""
    return render(DECLARED, plugin)
