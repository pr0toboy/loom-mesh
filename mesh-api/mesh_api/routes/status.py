from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from ..auth import require_auth
from ..models import StatusResponse, AgentStatus, HostStatus, HostHealth
from ..lib.status import get_agent_status, get_services, get_host, get_hosts
from ..peers import AGENTS

router = APIRouter()


@router.get("/status", response_model=StatusResponse)
async def status(_: str = Depends(require_auth)):
    agents = {}
    for ag in AGENTS:
        raw = get_agent_status(ag)
        agents[ag] = AgentStatus(**raw)

    services = get_services()
    host = HostStatus(**get_host())
    hosts = {name: HostHealth(**data) for name, data in get_hosts().items()}
    ts = datetime.now(timezone.utc).isoformat()

    return StatusResponse(agents=agents, services=services, host=host,
                          hosts=hosts, generated_at=ts)
