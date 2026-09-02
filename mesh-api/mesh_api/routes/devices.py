"""Device registration and manual push endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any

from ..auth import require_auth, require_write_auth
from ..notifications import fcm as fcm_module

router = APIRouter()


class DeviceRegisterRequest(BaseModel):
    device_token: str
    name: str = "unknown"
    platform: str = "android"


class DeviceRegisterResponse(BaseModel):
    ok: bool = True
    device_id: str          # = device_token (contrat Android)
    name: str
    platform: str
    registered_at: str | None = None
    updated_at: str | None = None


class PushRequest(BaseModel):
    title: str
    body: str
    data: dict[str, Any] = {}


class PushResponse(BaseModel):
    sent: int
    failed: int
    total: int


@router.post("/devices/register", response_model=DeviceRegisterResponse)
async def register_device(req: DeviceRegisterRequest, _: str = Depends(require_write_auth)):
    if not req.device_token:
        raise HTTPException(status_code=422, detail="device_token is required")
    try:
        record = fcm_module.register_device(req.device_token, req.name, req.platform)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"FCM registration failed: {e}")
    return DeviceRegisterResponse(
        ok=True,
        device_id=record["device_token"],
        name=record.get("name", req.name),
        platform=record.get("platform", req.platform),
        registered_at=record.get("registered_at"),
        updated_at=record.get("updated_at"),
    )


@router.get("/devices", response_model=list[DeviceRegisterResponse])
async def list_devices(_: str = Depends(require_auth)):
    return [
        DeviceRegisterResponse(
            ok=True,
            device_id=d["device_token"],
            name=d.get("name", "unknown"),
            platform=d.get("platform", "android"),
            registered_at=d.get("registered_at"),
            updated_at=d.get("updated_at"),
        )
        for d in fcm_module.list_devices()
    ]


@router.delete("/devices/{device_token}", status_code=204)
async def unregister_device(device_token: str, _: str = Depends(require_write_auth)):
    devices = fcm_module.list_devices()
    filtered = [d for d in devices if d["device_token"] != device_token]
    if len(filtered) == len(devices):
        raise HTTPException(status_code=404, detail="Device token not found")
    fcm_module._save_devices(filtered)


@router.post("/push", response_model=PushResponse)
async def push_notification(req: PushRequest, _: str = Depends(require_write_auth)):
    result = fcm_module.send_fcm_all(req.title, req.body, req.data)
    return PushResponse(**result)
