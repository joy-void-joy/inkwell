"""Project-specific permission policy that keeps pipeline scratch usable."""

from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.rows import PathRoleRow

from inkwell.devtools.harness.catalog import declared_hook_set


def policy_roles() -> list[PathRoleRow]:
    hooks = declared_hook_set()
    return [
        PathRoleRow(root=role.root.as_posix(), role=role.role)
        for role in hooks.path_roles
    ]


def full_write(path: str):
    return decide_edit(
        path,
        None,
        "finished output\n",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=policy_roles(),
        operation="create",
    )


def test_a_stage_output_full_write_needs_no_human_approval() -> None:
    decision = full_write(
        "notes/traces/0.2.0/sessions/id/pipeline_notes/work/stage/output.md"
    )

    assert decision.effect == "allow"


def test_shared_container_scratch_needs_no_human_approval() -> None:
    decision = full_write(
        "notes/traces/0.2.0/sessions/id/pipeline_notes/artifacts/shared/note.md"
    )

    assert decision.effect == "allow"


def test_durable_artifacts_keep_the_full_write_gate() -> None:
    decision = full_write(
        "notes/traces/0.2.0/sessions/id/pipeline_notes/artifacts/plan.md"
    )

    assert decision.effect == "ask"
