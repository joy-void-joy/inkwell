"""`dev pr create` must invoke gh correctly and read back the PR it made.

``gh pr create`` has no ``--json`` flag — it prints the new PR's URL on stdout.
Passing ``--json`` made every create fail with "unknown flag". This pins that
the command never sends ``--json`` again and that it recovers the PR number from
the URL gh returns.
"""

import json

import pytest

from inkwell.devtools.dev import pr


def test_create_never_passes_json_flag_and_parses_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str) -> str:
        calls.append(args)
        return "https://github.com/owner/repo/pull/42\n"

    monkeypatch.setattr(pr, "gh", fake_gh)
    pr.create(base="dev", title="t", body="b", as_json=True)

    assert calls and "--json" not in calls[0]
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == {"number": 42, "url": "https://github.com/owner/repo/pull/42"}
