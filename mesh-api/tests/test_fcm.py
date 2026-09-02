"""Tests for FCM notifications module — all Firebase calls are mocked."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

# Push notifications are an optional feature: firebase-admin is not in the base
# requirements, and the API disables push cleanly without it. These four tests
# drive the SDK itself, so they are skipped rather than failed where it is
# absent — a red CI for a feature nobody installed teaches the wrong lesson.
requires_firebase = pytest.mark.skipif(
    importlib.util.find_spec("firebase_admin") is None,
    reason="firebase-admin not installed (push notifications are optional)",
)

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture()
def fcm_env(tmp_path, monkeypatch):
    """Redirect FCM paths to tmp and provide a stub credentials file."""
    import mesh_api.notifications.fcm as fcm

    # Reset module-level state between tests
    monkeypatch.setattr(fcm, "_fcm_ready", False)
    monkeypatch.setattr(fcm, "_app", None)

    creds_file = tmp_path / "firebase-server-key.json"
    creds_file.write_text(json.dumps({
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key123",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "client_id": "123",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))
    devices_file = tmp_path / "fcm-devices.json"

    monkeypatch.setattr(fcm, "FIREBASE_CREDS_PATH", creds_file)
    monkeypatch.setattr(fcm, "FCM_DEVICES_PATH", devices_file)
    return tmp_path


# ── Init ───────────────────────────────────────────────────────────────────────

def test_init_fcm_missing_creds(tmp_path, monkeypatch):
    import mesh_api.notifications.fcm as fcm
    monkeypatch.setattr(fcm, "_fcm_ready", False)
    monkeypatch.setattr(fcm, "FIREBASE_CREDS_PATH", tmp_path / "nonexistent.json")
    assert fcm.init_fcm() is False


@requires_firebase
def test_init_fcm_success(fcm_env):
    import mesh_api.notifications.fcm as fcm

    mock_app = mock.MagicMock()
    mock_app.project_id = "test-project"

    with mock.patch("firebase_admin._apps", {}), \
         mock.patch("firebase_admin.initialize_app", return_value=mock_app), \
         mock.patch("firebase_admin.credentials.Certificate"):
        result = fcm.init_fcm()

    assert result is True
    assert fcm._fcm_ready is True


@requires_firebase
def test_init_fcm_idempotent(fcm_env, monkeypatch):
    import mesh_api.notifications.fcm as fcm
    monkeypatch.setattr(fcm, "_fcm_ready", True)
    # Should return True immediately without trying to initialise again
    with mock.patch("firebase_admin.initialize_app") as m:
        result = fcm.init_fcm()
    assert result is True
    m.assert_not_called()


# ── Device registry ────────────────────────────────────────────────────────────

def test_register_device_creates_file(fcm_env):
    import mesh_api.notifications.fcm as fcm

    record = fcm.register_device("token-abc", "Operator Phone", "android")

    assert record["device_token"] == "token-abc"
    assert record["name"] == "Operator Phone"
    assert record["platform"] == "android"
    assert "registered_at" in record

    # File should exist and be mode 0600
    assert fcm.FCM_DEVICES_PATH.exists()
    mode = fcm.FCM_DEVICES_PATH.stat().st_mode & 0o777
    assert mode == 0o600


def test_register_device_updates_existing(fcm_env):
    import mesh_api.notifications.fcm as fcm

    fcm.register_device("token-xyz", "Old Name", "android")
    record = fcm.register_device("token-xyz", "New Name", "ios")

    assert record["name"] == "New Name"
    assert record["platform"] == "ios"
    assert "updated_at" in record

    devices = fcm.list_devices()
    assert len(devices) == 1  # no duplicate


def test_list_devices_empty(fcm_env):
    import mesh_api.notifications.fcm as fcm
    assert fcm.list_devices() == []


# ── Send ───────────────────────────────────────────────────────────────────────

@requires_firebase
def test_send_fcm_single(fcm_env, monkeypatch):
    import mesh_api.notifications.fcm as fcm
    monkeypatch.setattr(fcm, "_fcm_ready", True)

    with mock.patch("firebase_admin.messaging.send", return_value="msg-id-123") as m_send, \
         mock.patch("firebase_admin.messaging.Message") as m_msg, \
         mock.patch("firebase_admin.messaging.Notification"):
        result = fcm.send_fcm("device-token", "Title", "Body", {"key": "val"})

    assert result is True
    m_send.assert_called_once()


@requires_firebase
def test_send_fcm_failure_returns_false(fcm_env, monkeypatch):
    import mesh_api.notifications.fcm as fcm
    monkeypatch.setattr(fcm, "_fcm_ready", True)

    with mock.patch("firebase_admin.messaging.send", side_effect=Exception("FCM error")), \
         mock.patch("firebase_admin.messaging.Message"), \
         mock.patch("firebase_admin.messaging.Notification"):
        result = fcm.send_fcm("bad-token", "Title", "Body")

    assert result is False


def test_send_fcm_all_no_devices(fcm_env):
    import mesh_api.notifications.fcm as fcm
    result = fcm.send_fcm_all("Title", "Body")
    assert result == {"sent": 0, "failed": 0, "total": 0}


def test_send_fcm_all_multiple_devices(fcm_env, monkeypatch):
    import mesh_api.notifications.fcm as fcm
    fcm.register_device("tok1", "Dev1", "android")
    fcm.register_device("tok2", "Dev2", "android")
    monkeypatch.setattr(fcm, "_fcm_ready", True)

    with mock.patch.object(fcm, "send_fcm", side_effect=[True, False]) as m:
        result = fcm.send_fcm_all("Title", "Body")

    assert result["sent"] == 1
    assert result["failed"] == 1
    assert result["total"] == 2
    assert m.call_count == 2


# ── Ticket completion helper ───────────────────────────────────────────────────

def test_notify_ticket_done_skipped_when_no_devices(fcm_env):
    import mesh_api.notifications.fcm as fcm
    # No devices registered → send_fcm_all should not be called
    with mock.patch.object(fcm, "send_fcm_all") as m:
        fcm.notify_ticket_done("tk-abc", "alice", "Done!", failed=False)
    m.assert_not_called()


def test_notify_ticket_done_sends_when_devices(fcm_env, monkeypatch):
    import mesh_api.notifications.fcm as fcm
    fcm.register_device("tok1", "Dev1", "android")
    monkeypatch.setattr(fcm, "_fcm_ready", True)

    with mock.patch.object(fcm, "send_fcm_all", return_value={"sent": 1, "failed": 0, "total": 1}) as m:
        fcm.notify_ticket_done("tk-xyz", "bob", "Task done.", failed=False)

    m.assert_called_once()
    call_args = m.call_args[0]
    assert "tk-xyz" in call_args[0]   # title
    assert "Task done." in call_args[1]  # body
