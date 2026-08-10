"""Inkwell's harness content: what it inherits, and what only it has.

The skills that automate agent work are the library's, taken whole. Nothing
is declared here yet that is inkwell's own — the rosters exist so a skill or
agent about *writing*, rather than about working on a lup project, has a
declared home the moment one is written, instead of arriving as a hand-edited
markdown file the next generation would revert.

The template-only skills stay behind: standing up a project, installing the
plugin, and initializing a domain are jobs with nothing to say inside a
project that is already all three.
"""

import lup.harness.models as models
from lup.devtools.harness.content.catalog import LIBRARY_AGENTS, LIBRARY_SKILLS

PROJECT_SKILLS: list[models.Skill] = []
"""The skills only inkwell has, because only inkwell writes."""

PROJECT_AGENTS: list[models.Agent] = []
"""The agents only inkwell has."""

SKILLS = [*LIBRARY_SKILLS, *PROJECT_SKILLS]
"""Every skill inkwell's plugin ships, inherited half first."""

AGENTS = [*LIBRARY_AGENTS, *PROJECT_AGENTS]
"""Every agent inkwell's plugin ships."""

PLUGIN_NAME: models.NativeName = "lup"
"""The plugin every declared skill is invoked through.

Stays `lup` rather than `inkwell`: it is the framework's identity, and every
`/lup:*` invocation in the guidance and in muscle memory resolves through it.
Only the *marketplace* is named per project, because that namespace is global
per runtime and a shared name shadows every repo that registers it.
"""
