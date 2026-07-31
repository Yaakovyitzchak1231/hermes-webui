"""Server-originated Web Push for installed Hermes PWAs.

The browser owns subscription creation. The server stores only the endpoint and
public encryption material, then emits privacy-preserving notifications from
the agent worker after durable turn state has been written.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from api.config import STATE_DIR, load_settings


logger = logging.getLogger(__name__)

SUBSCRIPTIONS_FILE = STATE_DIR / "webpush-subscriptions.json"
VAPID_PRIVATE_KEY_FILE = Path(
    os.getenv(
        "HERMES_WEBUI_VAPID_PRIVATE_KEY_FILE",
        str(STATE_DIR / "webpush-vapid-private.pem"),
    )
).expanduser()
VAPID_SUBJECT = os.getenv(
    "HERMES_WEBUI_VAPID_SUBJECT",
    "mailto:hermes-webui@localhost",
).strip()

_STATE_LOCK = threading.RLock()
_MAX_SUBSCRIPTIONS = 16
_ALLOWED_PUSH_HOSTS = {"fcm.googleapis.com"}


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        logger.debug("Could not tighten permissions on %s", path, exc_info=True)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _chmod_private(path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _load_or_create_vapid_key():
    with _STATE_LOCK:
        if VAPID_PRIVATE_KEY_FILE.exists():
            private_key = serialization.load_pem_private_key(
                VAPID_PRIVATE_KEY_FILE.read_bytes(),
                password=None,
            )
            if not isinstance(private_key, ec.EllipticCurvePrivateKey):
                raise ValueError("Web Push VAPID key is not an EC private key")
            _chmod_private(VAPID_PRIVATE_KEY_FILE)
            return private_key

        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _atomic_write(VAPID_PRIVATE_KEY_FILE, pem)
        return private_key


def _public_key_string(private_key) -> str:
    numbers = private_key.public_key().public_numbers()
    raw = (
        b"\x04"
        + numbers.x.to_bytes(32, "big")
        + numbers.y.to_bytes(32, "big")
    )
    return _urlsafe_encode(raw)


def _dependency_available() -> bool:
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False
    return True


def public_config() -> dict:
    """Return only the public application-server key used by PushManager."""
    if not _dependency_available():
        return {
            "supported": False,
            "public_key": None,
            "reason": "pywebpush is not installed",
        }
    private_key = _load_or_create_vapid_key()
    return {
        "supported": True,
        "public_key": _public_key_string(private_key),
    }


def validate_subscription(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("subscription must be an object")
    endpoint = str(payload.get("endpoint") or "").strip()
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("unsupported Web Push endpoint") from exc
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.hostname not in _ALLOWED_PUSH_HOSTS
        or len(endpoint) > 4096
    ):
        raise ValueError("unsupported Web Push endpoint")

    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("subscription keys are required")
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    try:
        decoded_p256dh = _urlsafe_decode(p256dh)
        decoded_auth = _urlsafe_decode(auth)
    except Exception as exc:
        raise ValueError("subscription keys are invalid") from exc
    if len(decoded_p256dh) != 65 or len(decoded_auth) < 16:
        raise ValueError("subscription keys are invalid")

    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": p256dh,
            "auth": auth,
        },
    }


def _read_subscriptions() -> list[dict]:
    with _STATE_LOCK:
        try:
            raw = json.loads(SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            logger.warning("Ignoring unreadable Web Push subscription state")
            return []
        rows = raw.get("subscriptions") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return []
        valid = []
        for row in rows:
            try:
                valid.append(validate_subscription(row))
            except ValueError:
                continue
        return valid


def _write_subscriptions(subscriptions: list[dict]) -> None:
    payload = json.dumps(
        {"version": 1, "subscriptions": subscriptions},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    with _STATE_LOCK:
        _atomic_write(SUBSCRIPTIONS_FILE, payload)


def register_subscription(payload: dict) -> dict:
    subscription = validate_subscription(payload)
    with _STATE_LOCK:
        subscriptions = _read_subscriptions()
        subscriptions = [
            row
            for row in subscriptions
            if row["endpoint"] != subscription["endpoint"]
        ]
        subscriptions.append(subscription)
        _write_subscriptions(subscriptions[-_MAX_SUBSCRIPTIONS:])
    return {"ok": True}


def remove_subscription(endpoint: str) -> dict:
    endpoint = str(endpoint or "").strip()
    with _STATE_LOCK:
        subscriptions = _read_subscriptions()
        remaining = [
            row for row in subscriptions if row.get("endpoint") != endpoint
        ]
        _write_subscriptions(remaining)
    return {"ok": True}


def notification_for_stream_event(
    event: str,
    data: dict | None,
    session_id: str,
) -> dict | None:
    """Map worker events to non-sensitive lock-screen notifications."""
    event = str(event or "")
    sid = str(session_id or "").strip()
    if not sid:
        return None
    bodies = {
        "done": "Task finished. Tap to view the result.",
        "apperror": "The task encountered an error. Tap to review.",
        "approval": "Approval required. Tap to review.",
        "clarify": "Your input is required. Tap to respond.",
    }
    body = bodies.get(event)
    if body is None:
        return None
    safe_sid = quote(sid, safe="")
    return {
        "title": "Hermes",
        "body": body,
        "url": f"session/{safe_sid}",
        "tag": f"hermes-{safe_sid}",
        "kind": event,
    }


def _send_payload(payload: dict) -> int:
    from pywebpush import WebPushException, webpush

    subscriptions = _read_subscriptions()
    if not subscriptions:
        return 0
    expired_endpoints = set()
    sent = 0
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=subscription,
                data=encoded,
                vapid_private_key=str(VAPID_PRIVATE_KEY_FILE),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=3600,
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                expired_endpoints.add(subscription["endpoint"])
            else:
                logger.warning("Web Push delivery failed (status=%s)", status)
        except Exception:
            logger.exception("Unexpected Web Push delivery failure")
    if expired_endpoints:
        _write_subscriptions(
            [
                row
                for row in subscriptions
                if row["endpoint"] not in expired_endpoints
            ]
        )
    return sent


def _queue_payload(payload: dict) -> int:
    subscription_count = len(_read_subscriptions())
    if subscription_count:
        threading.Thread(
            target=_send_payload,
            args=(dict(payload),),
            name="hermes-web-push",
            daemon=True,
        ).start()
    return subscription_count


def send_test_notification() -> dict:
    queued = _queue_payload(
        {
            "title": "Hermes",
            "body": "Background notifications are ready.",
            "url": "./",
            "tag": "hermes-web-push-test",
            "kind": "test",
            "force": True,
            "timestamp": int(time.time() * 1000),
        }
    )
    return {"ok": queued > 0, "queued": queued}


def notify_stream_event(
    event: str,
    data: dict | None,
    session_id: str,
    *,
    ephemeral: bool = False,
) -> None:
    if ephemeral:
        return
    payload = notification_for_stream_event(event, data, session_id)
    if payload is None:
        return
    try:
        if not load_settings().get("notifications_enabled", False):
            return
        payload["timestamp"] = int(time.time() * 1000)
        _queue_payload(payload)
    except Exception:
        logger.exception("Could not queue Web Push notification")
