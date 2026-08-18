"""Inkwell's `report` tree, wired over the targets it declares.

The command is the library's — it reads the surfaces any project on lup has:
open notes, deferred work, unverified claims, stale generated trees, unlanded
branches, resolver leases. What is inkwell's is which trees are generated and
which writers own the repository-wide ones, which is the same roster the
harness commands are built over rather than a second list to keep in step.
"""

from lup.devtools.report.app import create_report_app

from inkwell.devtools.harness.composition import REPOSITORY_WIDE, TARGETS

app = create_report_app(TARGETS, REPOSITORY_WIDE)
