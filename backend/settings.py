"""Operator credentials that outlive a restart — Kindle delivery and Gemini.

Everything else this server reads from the environment is immutable for the life
of the process. These four values are not: they are the ones an operator
actually needs to change (a new Kindle address, a rotated app password, a Gemini
key that finally has quota), and restarting a container to change an email
address is a bad trade. They live in a single JSON file beside the book store.

Precedence is ``defaults <- environment <- stored file``, with one deliberate
twist: **an empty stored value does not win.** Blanking a field in the console
therefore falls back to whatever ``.env`` supplies rather than blanking the
server, which is what makes the console override reversible without shell
access. :func:`effective` reports which source each live value came from, so
the settings page can say so out loud instead of leaving the operator guessing.

**These are live credentials.** The file is written chmod 600, secrets are never
rendered back to the browser (only a mask), and never logged. Setting them in
the environment instead keeps them off disk entirely.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.RLock()

#: Beside the book workspaces, not inside ``books/`` — pruning must never be
#: able to take the mail configuration with it.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
PATH = DATA_DIR / "settings.json"

#: field -> environment variable it is seeded from.
FIELDS: dict[str, str] = {
    "sender_email": "SENDER_EMAIL",
    "gmail_app_pass": "GMAIL_APP_PASS",
    "recipient_email": "RECIPIENT_EMAIL",
    "gemini_api_key": "GEMINI_API_KEY",
}

#: Fields never rendered back to the browser in the clear.
SECRETS = frozenset({"gmail_app_pass", "gemini_api_key"})

DEFAULTS: dict[str, str] = {key: "" for key in FIELDS}

#: The bullet run a mask is drawn with. The console deliberately does NOT post
#: this back — a secret input is rendered empty and "unchanged" is expressed by
#: simply not passing the field to :func:`save` — because recognising a mask on
#: the way back in is one encoding mishap away from storing the mask *as* the
#: credential. It survives only as the shape :func:`save` refuses to write.
MASK_SENTINEL = "•" * 8


def _read_env() -> dict[str, str]:
    return {key: (os.getenv(env) or "").strip() for key, env in FIELDS.items()}


#: The environment layer, frozen at import. Deliberately not re-read: this
#: module *writes* to ``os.environ`` (see :func:`apply_runtime`), so a live read
#: would show the console's own override back as an environment value — and the
#: settings page would then tell the operator that ``.env`` holds a key it never
#: had. Frozen, "env" means what the container was started with, which is the
#: only thing that sentence can usefully mean.
ENV: dict[str, str] = _read_env()


def _stored() -> dict[str, str]:
    """The saved overrides, or {} when there is no readable file."""
    try:
        with PATH.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("settings file unreadable (%s); using the environment only", exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("settings file is not a JSON object; using the environment only")
        return {}
    # Empty is "unset", not "set to empty" — that is the fallback-to-env rule.
    return {k: str(raw[k]).strip() for k in FIELDS if str(raw.get(k) or "").strip()}


def load() -> dict[str, str]:
    """The live values: environment, overridden by any non-empty stored value."""
    with _lock:
        values = {**DEFAULTS, **ENV}
        values.update(_stored())
        return values


def effective() -> dict[str, dict[str, Any]]:
    """Per field: the live value, where it came from, and whether it is set.

    The source matters more than it looks. With a value in both ``.env`` and the
    settings file, an operator who edits ``.env`` and restarts will see nothing
    change — the page has to be able to explain that.
    """
    with _lock:
        stored = _stored()
        out: dict[str, dict[str, Any]] = {}
        for key in FIELDS:
            value = stored.get(key) or ENV.get(key) or ""
            out[key] = {
                "value": value,
                "set": bool(value),
                "source": "console" if stored.get(key) else ("env" if ENV.get(key) else ""),
                "env_set": bool(ENV.get(key)),
                "overridden": bool(stored.get(key) and ENV.get(key)),
            }
        return out


def save(**changes: str) -> dict[str, str]:
    """Merge ``changes`` into the stored overrides and persist them atomically.

    A field passed empty is *cleared*, which reverts it to the environment. A
    field not passed at all is left exactly as it was — that is how the console
    expresses "the operator did not retype this secret".
    """
    with _lock:
        current = _stored()
        for key, value in changes.items():
            if key not in FIELDS:
                continue
            value = str(value or "").strip()
            # Defence in depth behind the console's empty-means-unchanged rule:
            # no real credential is a run of bullets, so a mask that reaches
            # here by any route is a bug upstream and must not be written. The
            # symptom it prevents is an SMTP 535 days later, long after the page
            # that caused it.
            if value and set(value) == {"•"}:
                logger.warning("refusing to store a mask as %s; keeping the stored value", key)
                continue
            if value:
                current[key] = value
            else:
                current.pop(key, None)

        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".json.tmp")
        # Created 0600 rather than chmod'ed afterwards: on a bind-mounted data
        # folder, chmod-after-write leaves a window in which the file is
        # world-readable at whatever the umask allows, and it holds a Gmail
        # app password.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(PATH)

        logger.info(
            "settings saved (overrides: %s)",
            ", ".join(sorted(current)) or "none",
        )
        apply_runtime()
        return load()


def mask(key: str, value: str) -> str:
    """A recognisable stand-in for a secret — never enough to use it.

    An API key keeps its first four and last four characters: that is how you
    tell two Gemini keys apart in a screenshot without handing one over. A Gmail
    app password gets nothing at all — it is sixteen characters of entropy with
    no structure, so any prefix or suffix is a real fraction of the secret.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if key == "gemini_api_key" and len(value) > 12:
        return f"{value[:4]}{'•' * 6}{value[-4:]}"
    if key in SECRETS:
        return "•" * 8
    return value


def public_view() -> dict[str, Any]:
    """Settings shaped for display: secrets replaced by their masks."""
    view: dict[str, Any] = {}
    for key, info in effective().items():
        view[key] = {
            **info,
            "display": mask(key, info["value"]) if key in SECRETS else info["value"],
        }
    view["delivery_ready"], view["delivery_reason"] = delivery_ready()
    return view


# --------------------------------------------------------------------------
# consumers
# --------------------------------------------------------------------------

def delivery() -> tuple[str, str, str]:
    """``(sender_email, gmail_app_pass, recipient_email)`` as currently configured."""
    values = load()
    return (
        values["sender_email"],
        values["gmail_app_pass"],
        values["recipient_email"],
    )


def delivery_ready() -> tuple[bool, str]:
    """Can a book actually be mailed right now?"""
    sender, password, recipient = delivery()
    missing = [
        name
        for name, value in (
            ("a sender Gmail address", sender),
            ("a Gmail app password", password),
            ("a Send-to-Kindle address", recipient),
        )
        if not value
    ]
    if missing:
        return False, f"missing {', '.join(missing)}"
    return True, ""


def apply_runtime() -> None:
    """Push stored values the rest of the server reads from ``os.environ``.

    ``imagegen`` asks ``os.environ`` for GEMINI_API_KEY and then caches a client
    built from it, so a key saved in the console reaches image generation only
    if the environment is updated *and* that cached client is dropped. Doing
    both here is what makes the settings page take effect without a restart.
    """
    with _lock:
        # Cleared in the console means "fall back to the environment as it was
        # at boot" — which ENV still holds precisely because it was frozen.
        wanted = _stored().get("gemini_api_key") or ENV.get("gemini_api_key", "")
        if os.environ.get("GEMINI_API_KEY", "") == wanted:
            return
        if wanted:
            os.environ["GEMINI_API_KEY"] = wanted
        else:
            os.environ.pop("GEMINI_API_KEY", None)
        _reset_imagegen()


def _reset_imagegen() -> None:
    """Drop imagegen's cached client so the next call rebuilds it with the new key."""
    try:
        import imagegen
    except ImportError:  # pragma: no cover - imagegen is always importable here
        return
    # The client is dropped, not closed: google-genai's client holds a pooled
    # httpx session, and a generation already in flight on a worker thread is
    # still using it. Letting it fall off the last reference is the only safe
    # order.
    with getattr(imagegen, "_client_lock", threading.Lock()):
        imagegen._cached_client = None
    logger.info("gemini client reset after a settings change")
