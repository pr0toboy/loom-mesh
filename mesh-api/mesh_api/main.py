import asyncio
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from .routes import health, status, inbox, send, tickets, graph, stream, devices, contexts, conversation, usage, skills, hooks, projtime, kanban
from .routes.stream import _status_watcher_loop
from .lib.tickets_io import ensure_dirs
from .notifications import fcm as fcm_module


def _bind_address() -> str:
    """Where this process is listening, as best as it can know.

    Set by the shipped systemd unit. Falling back to the uvicorn command line
    covers a manual launch; an unknown address is treated as loopback, because
    guessing "open" would refuse to start a service that is in fact private.
    """
    explicit = os.environ.get("MESH_API_BIND", "").strip()
    if explicit:
        return explicit
    import sys

    argv = sys.argv
    if "--host" in argv:
        try:
            return argv[argv.index("--host") + 1]
        except IndexError:
            pass
    return "127.0.0.1"


def _is_loopback(addr: str) -> bool:
    return addr in {"127.0.0.1", "::1", "localhost"} or addr.startswith("127.")


def check_startup_safety() -> None:
    """Refuse the one combination that reads as safe and is not.

    ``MESH_API_NO_AUTH=1`` was designed for a dashboard on loopback. Combined
    with a bind that is reachable from a network, it means: anything that can
    route here may read every inbox — and an inbox is the input an agent acts
    on. That combination is almost never intended; it is arrived at by copying
    a bind address from an example while the bypass is still on, and nothing
    about the running service looks wrong afterwards.

    Explicit override for the person who does mean it, with the firewall to
    back it: ``MESH_API_ALLOW_OPEN_BIND=1``.
    """
    if os.environ.get("MESH_API_NO_AUTH") != "1":
        return
    if os.environ.get("MESH_API_ALLOW_OPEN_BIND") == "1":
        return
    addr = _bind_address()
    if _is_loopback(addr):
        return
    raise RuntimeError(
        f"refusing to start: authentication is disabled (MESH_API_NO_AUTH=1) while "
        f"listening on {addr}, which is reachable from the network. Anyone who can "
        f"reach this port could read every agent's inbox. Either bind to 127.0.0.1, "
        f"or drop MESH_API_NO_AUTH, or — if a firewall really does restrict this "
        f"port and you mean it — set MESH_API_ALLOW_OPEN_BIND=1."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_startup_safety()
    ensure_dirs()
    fcm_module.init_fcm()  # best-effort; logs warning if credentials absent
    watcher_task = asyncio.create_task(_status_watcher_loop())
    yield
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass


# The API schema (/docs, /redoc, /openapi.json) is closed: no free map of the
# attack surface, not even on a private network.
app = FastAPI(title="Mesh API", version="0.1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

app.include_router(health.router)
app.include_router(status.router)
app.include_router(inbox.router)
app.include_router(send.router)
app.include_router(tickets.router)
app.include_router(graph.router)
app.include_router(stream.router)
app.include_router(devices.router)
app.include_router(contexts.router)
app.include_router(conversation.router)
app.include_router(usage.router)
app.include_router(skills.router)
app.include_router(hooks.router)
app.include_router(projtime.router)
app.include_router(kanban.router)

# StaticFiles is mounted LAST so it cannot shadow the API routes.
#
# The web UI directory is OPTIONAL, and its absence must not stop the API from
# starting: StaticFiles raises RuntimeError at construction when the directory
# does not exist, which killed the whole service for anyone whose tree did not
# match the original layout exactly. So /ui is mounted only when there is
# something to serve, and the API stays usable on its own.
# Where to find it, in order: an explicit setting, then the dashboard shipped
# with the repository, then a webui dropped next to the API. The middle one
# matters — with only the last, a fresh checkout served no interface at all
# while the README advertised one, and nothing said why.
def _webui_dir() -> Path:
    explicit = os.environ.get("MESH_WEBUI_DIR")
    if explicit:
        return Path(explicit)
    api_base = Path(__file__).resolve().parent.parent
    for candidate in (api_base.parent / "dashboard-web", api_base / "webui"):
        if candidate.is_dir():
            return candidate
    return api_base / "webui"


_WEBUI_DIR = _webui_dir()
if _WEBUI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_WEBUI_DIR), html=True), name="ui")
else:  # pragma: no cover - depends on the deployment
    import logging
    logging.getLogger(__name__).warning(
        "web UI not found (%s): /ui is not mounted, the API runs without an "
        "interface. Set MESH_WEBUI_DIR to serve one.", _WEBUI_DIR)
