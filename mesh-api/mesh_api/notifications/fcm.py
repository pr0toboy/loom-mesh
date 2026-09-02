"""Firebase Cloud Messaging helpers for mesh-api push notifications.

Initialisation is lazy and fail-safe: if the credentials file is absent or
firebase-admin is not installed, all functions log a warning and return
gracefully without raising.  The rest of the API continues to work normally.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MESH_HOME = Path(os.environ.get("MESH_HOME", os.path.expanduser("~/mesh")))
FIREBASE_CREDS_PATH = _MESH_HOME / "firebase-server-key.json"
FCM_DEVICES_PATH = _MESH_HOME / "fcm-devices.json"

_app = None          # firebase_admin.App instance, None until init_fcm() succeeds
_fcm_ready = False


# ── Init ───────────────────────────────────────────────────────────────────────

def init_fcm() -> bool:
    """Initialise Firebase Admin SDK.  Returns True on success, False otherwise."""
    global _app, _fcm_ready

    if _fcm_ready:
        return True

    if not FIREBASE_CREDS_PATH.exists():
        log.warning("FCM: credentials file not found at %s — push disabled", FIREBASE_CREDS_PATH)
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            cred = credentials.Certificate(str(FIREBASE_CREDS_PATH))
            _app = firebase_admin.initialize_app(cred)
        else:
            _app = firebase_admin.get_app()

        _fcm_ready = True
        log.info("FCM: initialised (project=%s)", _app.project_id)
        return True
    except Exception as e:
        log.warning("FCM: init failed (%s) — push disabled", e)
        return False


# ── Device registry ────────────────────────────────────────────────────────────

def _load_devices() -> list[dict]:
    if not FCM_DEVICES_PATH.exists():
        return []
    try:
        return json.loads(FCM_DEVICES_PATH.read_text())
    except Exception:
        return []


def _save_devices(devices: list[dict]) -> None:
    FCM_DEVICES_PATH.write_text(json.dumps(devices, indent=2, ensure_ascii=False))
    os.chmod(FCM_DEVICES_PATH, 0o600)


def register_device(device_token: str, name: str, platform: str) -> dict:
    """Register or update a FCM device token.  Returns the device record."""
    devices = _load_devices()
    now = datetime.now(timezone.utc).isoformat()

    for dev in devices:
        if dev["device_token"] == device_token:
            dev["name"] = name
            dev["platform"] = platform
            dev["updated_at"] = now
            _save_devices(devices)
            return dev

    record = {
        "device_token": device_token,
        "name": name,
        "platform": platform,
        "registered_at": now,
    }
    devices.append(record)
    _save_devices(devices)
    log.info("FCM: registered device %s (%s / %s)", name, platform, device_token[:12] + "…")
    return record


def list_devices() -> list[dict]:
    return _load_devices()


# ── Send ───────────────────────────────────────────────────────────────────────

def send_fcm(device_token: str, title: str, body: str, data: dict[str, Any] | None = None) -> bool:
    """Send a FCM notification to a single device token.  Returns True on success."""
    if not init_fcm():
        return False

    try:
        from firebase_admin import messaging

        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            token=device_token,
        )
        response = messaging.send(msg)
        log.info("FCM: sent to %s… → %s", device_token[:12], response)
        return True
    except Exception as e:
        log.warning("FCM: send failed to %s…: %s", device_token[:12], e)
        return False


def send_fcm_all(title: str, body: str, data: dict[str, Any] | None = None) -> dict:
    """Send FCM notification to all registered devices in parallel.

    Returns {"sent": int, "failed": int, "total": int}.
    """
    devices = _load_devices()
    if not devices:
        log.debug("FCM: no devices registered, skipping push")
        return {"sent": 0, "failed": 0, "total": 0}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[bool] = []
    with ThreadPoolExecutor(max_workers=min(len(devices), 8)) as pool:
        futures = {pool.submit(send_fcm, dev["device_token"], title, body, data): dev
                   for dev in devices}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                results.append(False)

    sent = sum(1 for r in results if r)
    failed = len(results) - sent
    log.info("FCM: broadcast — sent=%d failed=%d total=%d", sent, failed, len(devices))
    return {"sent": sent, "failed": failed, "total": len(devices)}


# ── Ticket completion helper ───────────────────────────────────────────────────

def notify_ticket_done(ticket_id: str, agent: str, tldr: str, failed: bool = False) -> None:
    """Best-effort FCM push when a ticket is completed or failed."""
    if not _load_devices():
        return  # no devices → skip init overhead

    status = "failed" if failed else "done"
    title = f"Ticket {ticket_id} {status} [{agent}]"
    body = tldr or "(no summary)"
    data = {"ticket_id": ticket_id, "agent": agent, "status": "failed" if failed else "done"}
    send_fcm_all(title, body, data)
