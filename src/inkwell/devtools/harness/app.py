"""Inkwell's `harness` tree, wired over the targets it declares.

The command tree is the library's; what is inkwell's is the target roster it
generates, the repository-wide writers that belong to no native tree, and the
model a resolver session runs on. Claude is the only adapter inkwell declares
a tree for, so it is the only one a configured model can reach.
"""

from lup.devtools.harness.app import create_harness_app
from lup.devtools.harness.resolve import ConfiguredModel
from inkwell.agent.config import settings
from inkwell.devtools.harness.composition import REPOSITORY_WIDE, TARGETS
from inkwell.devtools.profiles import inkwell_profile_directory

app = create_harness_app(
    TARGETS,
    REPOSITORY_WIDE,
    ConfiguredModel(name=settings.model, adapter="claude"),
    inkwell_profile_directory(),
)
