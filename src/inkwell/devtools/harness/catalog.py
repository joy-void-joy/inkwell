"""Root of inkwell's harness declaration graph.

The declaration leaves — skills, agents, guidance, settings — live under
``content/``. This module assembles them with the hook policy into the
portable ``Harness`` that generation compiles into inkwell's native tree.
Generated guidance points here as the file owning URL scopes, protected edit
roots, and the shell vocabulary, so its path is part of the documented
surface.
"""

from pathlib import Path

from pydantic import AnyHttpUrl

from lup.adapters.claude.harness import ClaudeSpellings
from lup.codescan.boundaries import ApplicationRoots, generated_tree_paths
from lup.codescan.common import RuleSelection
from lup.devtools.dev.workflow import WorkflowSpec
from lup.devtools.project import DevProject
from lup.harness.contracts import NativeSpellings
from lup.harness.models import (
    Harness,
    HookPathRole,
    HookSandbox,
    HookSet,
    HookUrlScope,
    LiteralWord,
    McpServer,
    Plugin,
    ProjectRootWord,
    ResolveSpec,
    SkillInvocation,
)
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.vocabulary import runner_target_rules
from lup.workspace.paths import project_root, read_project_name
from inkwell.devtools.harness.content.catalog import AGENTS, PLUGIN_NAME, SKILLS
from inkwell.devtools.harness.content.guidance import document as guidance_document
from inkwell.devtools.harness.content.shell_vocabulary import SHELL_RULES

HARNESS_SESSION = "harness"
"""The session a natively launched tool server opens for itself.

One name per worktree, shared by every group's process, so the tools of one
native session write where the next one will find them."""

TOOL_GROUPS = ["source", "research", "docs"]
"""The tool groups inkwell offers a native session, by server name.

The names `agent serve-tools --server` answers to, which is how a session
selects one; a name declared here that the command does not serve is a server
that starts and immediately exits.
"""


def agent_tool_servers() -> list[McpServer]:
    """Offer inkwell's own agent tools to whichever runtime is reading."""
    return [
        McpServer(
            id=f"mcp.{name}",
            name=name,
            description=f"Agent tools in the {name} group, served over stdio",
            command="uv",
            arguments=[
                LiteralWord(text="run"),
                LiteralWord(text="--directory"),
                ProjectRootWord(),
                LiteralWord(text="lup-devtools"),
                LiteralWord(text="agent"),
                LiteralWord(text="serve-tools"),
                LiteralWord(text="--server"),
                LiteralWord(text=name),
                LiteralWord(text="--session"),
                LiteralWord(text=HARNESS_SESSION),
            ],
        )
        for name in TOOL_GROUPS
    ]


def declared_plugin() -> Plugin:
    """The one plugin inkwell publishes, as generation renders it."""
    return portable_harness().plugins[0]


def declared_hook_set() -> HookSet:
    """The hook set inkwell declares, for a session composed in process.

    Generated plugins read it off the harness they are compiled from. A
    session inkwell builds itself has to reach the same declaration, or it
    enforces something the generated tree does not.
    """
    return portable_harness().declared_hooks


WORKFLOW = WorkflowSpec(branches=["main", "dev"], system_packages=["poppler-utils"])
"""Inkwell's gate: `dev` integrates and `main` carries what has landed, so
both deserve a run of their own.

poppler is what reads a PDF's page count, and no lock file installs it — a
runner without it registers every PDF with no pages, which surfaces as a
test asserting a count several steps from the cause."""


RETIRED_RULES = RuleSelection(
    retired=[
        "constant-declaration",
        "default-factory",
        "model-config",
        "model-free-function",
    ]
)
"""The library rules inkwell does not hold itself to, and why each.

`constant-declaration` and `model-free-function` are architecture rules the
library settled for a framework, where every value is somebody else's to
override and every model is part of a published surface. Inkwell is the
application at the end of that chain: a stage's model list or a writer's
prompt budget has no caller above it to be handed to, and a free function
over one of its models is a pipeline step rather than an operation the model
owes anyone. `default-factory` and `model-config` are spellings — pydantic
does the same thing either way — so enforcing them would buy consistency
with the library at the price of churn through every model here.

Retiring them is a judgement about this repository, not a claim the rules are
wrong: they still run in lup, where the reasoning that produced them holds.
"""

NATIVE_RUNTIMES: list[NativeSpellings] = [ClaudeSpellings()]
"""Every runtime inkwell generates a tree for. Claude Code alone — inkwell
carries no `.codex/` tree, and generating one would publish a harness for a
runtime nothing here drives."""


def application_roots() -> ApplicationRoots:
    """Where inkwell composes concrete native implementations."""
    package = Path(__file__).resolve().parents[2].relative_to(project_root()).as_posix()
    harness = f"{package}/devtools/harness/"
    plugins = [plugin.name for plugin in portable_harness().plugins]
    return ApplicationRoots(
        composition=[
            *generated_tree_paths(NATIVE_RUNTIMES, plugins),
            "tests/",
            # The provider selection boundary: `provider_factory` names a
            # concrete adapter here so nothing above it has to.
            f"{package}/agent/client.py",
            f"{package}/agent/core.py",
            f"{package}/agent/pipeline.py",
            f"{package}/devtools/subapps.py",
            harness,
        ],
        portable_prose=[f"{harness}content/"],
    )


def dev_project() -> DevProject:
    """What inkwell tells the shared development tooling about itself.

    The roles and the rule selection come from the same hook set the generated
    tree enforces, so a scan and a hook cannot disagree about what a path is
    for, nor about which rules are live here.
    """
    hooks = declared_hook_set()
    return DevProject(
        package=Path(__file__).resolve().parents[2].name,
        roots=application_roots(),
        rules=hooks.rules,
        path_roles=[
            PathRoleRow(root=role.root.as_posix(), role=role.role)
            for role in hooks.path_roles
        ],
    )


def portable_harness(version: str = "0.2.0", root: Path | None = None) -> Harness:
    """Build the canonical declaration graph the Claude adapter compiles."""
    plugin = Plugin(
        id=f"plugin.{PLUGIN_NAME}",
        name=PLUGIN_NAME,
        # Marketplace names share one global namespace per runtime, so this is
        # the project's own name rather than `lup`: registering under `lup`
        # collides with every other repo that installed the plugin, and
        # whichever one loaded last serves its copy to all of them.
        marketplace=read_project_name(root or project_root()),
        version=version,
        description=(
            "Writing harness with feedback, review, and safe resolution flows"
        ),
        skills=SKILLS,
        agents=AGENTS,
        mcp_servers=agent_tool_servers(),
        hooks=HookSet(
            id="hooks.inkwell-policy",
            policy_ids=["fetch", "shell", "edit", "unknown-tool"],
            allowed_fetch=[
                HookUrlScope(origin=AnyHttpUrl("https://docs.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("https://code.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("https://platform.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("https://claude.ai")),
                HookUrlScope(origin=AnyHttpUrl("https://github.com")),
                HookUrlScope(origin=AnyHttpUrl("https://api.github.com")),
                HookUrlScope(
                    origin=AnyHttpUrl("https://githubusercontent.com"),
                    include_subdomains=True,
                ),
                HookUrlScope(origin=AnyHttpUrl("https://pypi.org")),
                HookUrlScope(origin=AnyHttpUrl("https://files.pythonhosted.org")),
                # The sources a writing session researches from, and the
                # surfaces it publishes to.
                HookUrlScope(origin=AnyHttpUrl("https://arxiv.org")),
                HookUrlScope(origin=AnyHttpUrl("https://en.wikipedia.org")),
                HookUrlScope(
                    origin=AnyHttpUrl("https://googleapis.com"),
                    include_subdomains=True,
                ),
                HookUrlScope(origin=AnyHttpUrl("https://docs.google.com")),
                # This machine's own services: the web session API and
                # whatever a session is running to look at. No port is named
                # because each surface takes `--port`, and a scope that went
                # stale on a flag would put the question back.
                HookUrlScope(origin=AnyHttpUrl("http://127.0.0.1"), any_port=True),
                HookUrlScope(origin=AnyHttpUrl("http://localhost"), any_port=True),
            ],
            protected_edit_roots=[
                Path(".claude"),
                Path("pyproject.toml"),
                Path("sync.json"),
            ],
            path_roles=[
                HookPathRole(root=Path("tests"), role="test"),
                HookPathRole(root=Path("tmp"), role="scratch"),
                # A stage writes its native-tool output here before the host
                # process promotes it into the durable artifact tree.  The
                # directory is unique per stage and disposable once that
                # promotion succeeds, so a full Write must not become a
                # human approval question in an unattended pipeline.
                HookPathRole(root=Path("**/pipeline_notes/work"), role="scratch"),
                # `/shared` is this directory inside a stage container.  It
                # deliberately holds cross-stage scratch, not pipeline state;
                # classifying its host spelling the same way keeps native
                # Write and execute_code under one policy.
                HookPathRole(
                    root=Path("**/pipeline_notes/artifacts/shared"),
                    role="scratch",
                ),
            ],
            rules=RETIRED_RULES,
            shell_rules=SHELL_RULES,
            runner_targets=runner_target_rules(
                session_opening=("lup-devtools", "inkwell")
            ),
            sandbox=HookSandbox(
                extra_domains=["api.anthropic.com"],
                credential_paths=["~/.ssh", "~/.aws/credentials"],
                # Every command reaches the toolchain through `uv`, which locks
                # its cache whenever it resolves dependencies — which a changed
                # pyproject.toml forces, and an integration merge is what
                # changes pyproject.toml.
                writable_paths=["~/.cache/uv"],
            ),
        ),
    )
    return Harness(
        generator_version=version,
        source_evidence={"content": "typed-python"},
        plugins=[plugin],
        guidance=guidance_document(plugin.hooks.rules if plugin.hooks else None),
        resolver=ResolveSpec(
            id="resolver.inkwell",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin=PLUGIN_NAME, skill="implementer"),
            review_skill=SkillInvocation(plugin=PLUGIN_NAME, skill="resolve-reviewer"),
            merge_skill=SkillInvocation(plugin=PLUGIN_NAME, skill="merge"),
        ),
    )
