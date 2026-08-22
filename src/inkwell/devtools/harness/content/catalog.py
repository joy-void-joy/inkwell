# lup: ignore[constant-declaration]
# Every constant here is inkwell's own composition — which skills and agents
# its plugin ships, and what that plugin is called. A composition root is
# where a judgement is finally made rather than passed on, so there is no
# caller above it to take these from.
"""Inkwell's harness content: what it inherits, and what only it has.

The skills that automate agent work are the library's, taken whole. Nothing
is declared here yet that is inkwell's own — the rosters exist so a skill or
agent about *writing*, rather than about working on a lup project, has a
declared home the moment one is written, instead of arriving as a hand-edited
markdown file the next generation would revert.

This is also where the library learns what this application is called. Several
of its skills name a path inside the reading project's own package, and only
the project knows that name, so it is supplied here rather than assumed there.
"""

from pathlib import Path

import lup.harness.models as models
from lup.devtools.harness.content.application import ApplicationLayout
from lup.devtools.harness.content.catalog import library_content

LAYOUT = ApplicationLayout(package=Path(__file__).resolve().parents[3].name)
"""Where inkwell's own code sits, for the library prose that names it.

Derived from where this file actually sits rather than written down, for the
reason ``DevProject.package`` derives its own: a package that is renamed
leaves a literal naming one that is gone.
"""

PROJECT_SKILLS: list[models.Skill] = []
"""The skills only inkwell has, because only inkwell writes."""

PROJECT_AGENTS: list[models.Agent] = []
"""The agents only inkwell has."""

RETIRED = models.ContentSelection()
"""Which of lup's own skills and agents inkwell does not ship.

Empty: the skills whose subject is standing a project up are the template's
rather than the library's, so they are already absent from what inkwell
inherits instead of being declined here. The seat stays so a later judgement
is one line rather than a restated roster, and so `dev check` can name what
was declined."""

CONTENT = (
    library_content(LAYOUT).selected(RETIRED).extended(PROJECT_SKILLS, PROJECT_AGENTS)
)
"""Everything inkwell's plugin ships, inherited half first."""

SKILLS = CONTENT.skills
"""Every skill inkwell's plugin ships."""

AGENTS = CONTENT.agents
"""Every agent inkwell's plugin ships."""

PLUGIN_NAME: models.NativeName = "lup"
"""The plugin every declared skill is invoked through.

Stays `lup` rather than `inkwell`: it is the framework's identity, and every
`/lup:*` invocation in the guidance and in muscle memory resolves through it.
Only the *marketplace* is named per project, because that namespace is global
per runtime and a shared name shadows every repo that registers it.
"""
