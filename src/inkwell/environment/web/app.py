"""FastAPI application factory for the Inkwell web environment."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from inkwell.environment.web.routes import profiles as profiles_route
from inkwell.environment.web.routes import sessions as sessions_route
from inkwell.environment.web.routes import ws as ws_route
from inkwell.environment.web.session_manager import SessionManager

FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"

WS_DIAG_HTML = """\
<!DOCTYPE html>
<html><head><title>WS Diagnostic</title></head>
<body style="font-family:monospace;padding:2em">
<h2>WebSocket Diagnostic</h2>
<div id="log" style="white-space:pre-wrap;border:1px solid #ccc;padding:1em;max-height:70vh;overflow:auto"></div>
<script>
const log = document.getElementById('log');
function w(msg) { log.textContent += msg + '\\n'; log.scrollTop = log.scrollHeight; }

function testWs(url, label) {
  w(`\\n--- ${label}: ${url} ---`);
  const ws = new WebSocket(url);
  const t0 = performance.now();
  ws.onopen = () => w(`  [${((performance.now()-t0)|0)}ms] OPEN`);
  ws.onmessage = (e) => w(`  [${((performance.now()-t0)|0)}ms] MSG: ${e.data}`);
  ws.onclose = (e) => w(`  [${((performance.now()-t0)|0)}ms] CLOSE code=${e.code} reason="${e.reason}" wasClean=${e.wasClean}`);
  ws.onerror = () => w(`  [${((performance.now()-t0)|0)}ms] ERROR`);
  return ws;
}

const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
w('Testing bare ping endpoint...');
const ws1 = testWs(`${proto}//${location.host}/ws/_ping`, 'PING');

setTimeout(() => {
  w('\\nTesting session endpoint...');
  testWs(`${proto}//${location.host}/ws/20260607_170720`, 'SESSION');
}, 2000);

setTimeout(() => {
  w('\\nSending data on ping socket...');
  if (ws1.readyState === WebSocket.OPEN) {
    ws1.send('hello');
    w('  sent: hello');
  } else {
    w('  CANNOT SEND: readyState=' + ws1.readyState);
  }
}, 4000);
</script>
</body></html>
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    manager = SessionManager()
    sessions_route.set_manager(manager)
    ws_route.set_manager(manager)
    try:
        yield
    finally:
        await manager.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Inkwell", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sessions_route.formats_router)
    app.include_router(sessions_route.router)
    app.include_router(profiles_route.router)
    app.include_router(ws_route.router)

    @app.get("/ws-diag")
    async def ws_diag() -> HTMLResponse:
        return HTMLResponse(WS_DIAG_HTML)

    if FRONTEND_DIST.is_dir():
        index_html = FRONTEND_DIST / "index.html"

        app.mount(
            "/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets"
        )

        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            static = FRONTEND_DIST / path
            if static.is_file():
                return FileResponse(static)
            return FileResponse(index_html)

    return app
