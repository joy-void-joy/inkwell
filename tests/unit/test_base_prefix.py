"""One build serves both directly and behind rlaif's prefix-stripping proxy.

When ``INKWELL_BASE_PATH`` is set, the SPA is built for that sub-path, so its
router and every ``{base}api``/``{base}ws`` call live under it. Reached directly
on its own port the request still carries the prefix; behind rlaif it arrives
stripped. The app strips a present prefix so both route the same, and bounces the
bare origin root into the base so the SPA's router has a path to match.

TestClient is used without its context manager on purpose: that skips the
Docker-building lifespan, leaving only the routing under test.
"""

import pytest
from fastapi.testclient import TestClient

import inkwell.agent.config as config
from inkwell.environment.web.app import FRONTEND_DIST, create_app

# What these routes serve is the built SPA, so there is nothing to route to
# until it is built. dist/ is generated, not tracked, so a checkout that has
# not run `bun run build` — CI included — has no document and no assets.
pytestmark = pytest.mark.skipif(
    not (FRONTEND_DIST / "assets").is_dir(),
    reason="frontend not built — run `bun run build` in the frontend directory",
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config.settings, "base_path", "/inkwell/")
    return TestClient(create_app(), follow_redirects=False)


def test_bare_root_redirects_into_the_base(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/inkwell/"


def test_proxied_root_serves_the_document_in_place(client: TestClient) -> None:
    resp = client.get("/", headers={"x-forwarded-prefix": "/inkwell"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_prefixed_asset_resolves_to_the_real_file(client: TestClient) -> None:
    asset = next((FRONTEND_DIST / "assets").iterdir())
    resp = client.get(f"/inkwell/assets/{asset.name}")
    assert resp.status_code == 200
    assert not resp.headers["content-type"].startswith("text/html")
