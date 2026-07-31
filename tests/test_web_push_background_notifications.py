"""Regression coverage for server-originated Web Push notifications."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from api import web_push


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


def _chrome_subscription(endpoint_suffix: str = "device-1") -> dict:
    return {
        "endpoint": f"https://fcm.googleapis.com/fcm/send/{endpoint_suffix}",
        "keys": {
            "p256dh": "B" + ("A" * 86),
            "auth": "A" * 22,
        },
    }


def test_vapid_config_and_subscription_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with (
            patch.object(
                web_push,
                "SUBSCRIPTIONS_FILE",
                tmp_path / "subscriptions.json",
            ),
            patch.object(
                web_push,
                "VAPID_PRIVATE_KEY_FILE",
                tmp_path / "vapid-private.pem",
            ),
        ):
            config = web_push.public_config()
            assert config["supported"] is True
            assert config["public_key"]
            assert "=" not in config["public_key"]
            assert web_push.VAPID_PRIVATE_KEY_FILE.stat().st_mode & 0o077 == 0

            assert web_push.register_subscription(_chrome_subscription()) == {
                "ok": True
            }
            saved = json.loads(
                web_push.SUBSCRIPTIONS_FILE.read_text(encoding="utf-8")
            )
            assert len(saved["subscriptions"]) == 1
            assert saved["subscriptions"][0]["endpoint"].startswith(
                "https://fcm.googleapis.com/"
            )
            assert web_push.SUBSCRIPTIONS_FILE.stat().st_mode & 0o077 == 0

            assert web_push.remove_subscription(
                _chrome_subscription()["endpoint"]
            ) == {"ok": True}
            assert json.loads(
                web_push.SUBSCRIPTIONS_FILE.read_text(encoding="utf-8")
            )["subscriptions"] == []


def test_subscription_endpoint_is_fail_closed():
    invalid_endpoints = [
        "http://fcm.googleapis.com/fcm/send/device",
        "https://localhost/push",
        "https://127.0.0.1/push",
        "https://example.com/push",
    ]
    for endpoint in invalid_endpoints:
        payload = _chrome_subscription()
        payload["endpoint"] = endpoint
        try:
            web_push.validate_subscription(payload)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe push endpoint: {endpoint}")


def test_terminal_event_payloads_are_private_and_deep_linked():
    done = web_push.notification_for_stream_event(
        "done",
        {
            "session": {
                "title": "Sensitive project",
                "messages": [{"content": "secret"}],
            }
        },
        "session-123",
    )
    assert done == {
        "title": "Hermes",
        "body": "Task finished. Tap to view the result.",
        "url": "session/session-123",
        "tag": "hermes-session-123",
        "kind": "done",
    }
    assert "secret" not in json.dumps(done)
    assert web_push.notification_for_stream_event(
        "approval", {"description": "rm private-file"}, "session-123"
    )["body"] == "Approval required. Tap to review."
    assert web_push.notification_for_stream_event(
        "clarify", {"question": "Sensitive question"}, "session-123"
    )["body"] == "Your input is required. Tap to respond."
    assert web_push.notification_for_stream_event(
        "apperror", {"message": "secret provider error"}, "session-123"
    )["body"] == "The task encountered an error. Tap to review."
    assert (
        web_push.notification_for_stream_event("token", {}, "session-123")
        is None
    )


def test_enabled_terminal_event_is_queued_without_blocking_worker():
    with (
        patch.object(
            web_push,
            "load_settings",
            return_value={"notifications_enabled": True},
        ),
        patch.object(web_push, "_queue_payload", return_value=1) as queued,
    ):
        web_push.notify_stream_event(
            "done",
            {"session": {"messages": [{"content": "private result"}]}},
            "session-123",
        )
    payload = queued.call_args.args[0]
    assert payload["kind"] == "done"
    assert payload["url"] == "session/session-123"
    assert "private result" not in json.dumps(payload)


def test_disabled_or_ephemeral_events_are_not_queued():
    with (
        patch.object(
            web_push,
            "load_settings",
            return_value={"notifications_enabled": False},
        ),
        patch.object(web_push, "_queue_payload") as queued,
    ):
        web_push.notify_stream_event("done", {}, "session-123")
        web_push.notify_stream_event(
            "done",
            {},
            "session-123",
            ephemeral=True,
        )
    queued.assert_not_called()


def test_browser_subscription_and_service_worker_push_handlers_are_wired():
    assert "function ensureWebPushSubscription()" in MESSAGES_JS
    assert "registration.pushManager.subscribe" in MESSAGES_JS
    assert "api('/api/push/subscribe'" in MESSAGES_JS
    assert "function sendWebPushTestNotification()" in MESSAGES_JS

    assert "self.addEventListener('push'" in SW_JS
    assert "event.data.json()" in SW_JS
    assert "client.visibilityState === 'visible'" in SW_JS
    assert "self.registration.showNotification" in SW_JS

    assert 'parsed.path == "/api/push/config"' in ROUTES_PY
    assert 'parsed.path == "/api/push/subscribe"' in ROUTES_PY
    assert 'parsed.path == "/api/push/unsubscribe"' in ROUTES_PY
    assert 'parsed.path == "/api/push/test"' in ROUTES_PY
    assert "notify_stream_event" in STREAMING_PY
