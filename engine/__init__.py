"""FTMO autonomous trading operator — deterministic enforcement engine."""
import sentry_sdk
from . import config

_dsn = config._env("SENTRY_DSN", "")
if _dsn:
    sentry_sdk.init(
        dsn=_dsn,
        environment="live" if config.is_armed() else "dry",
        traces_sample_rate=0.0,   # no performance tracing — errors only
        before_send=lambda event, hint: _scrub(event),
    )


def _scrub(event: dict) -> dict:
    """Strip any accidental credential leaks from error payloads before they leave the machine."""
    for frame in (event.get("exception") or {}).get("values") or []:
        for f in (frame.get("stacktrace") or {}).get("frames") or []:
            f.pop("vars", None)
    return event
