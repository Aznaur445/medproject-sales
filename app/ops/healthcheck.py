"""Docker healthchecks without extra OS packages: python -m app.ops.healthcheck <component>."""

import sys
import time
import urllib.request

from app.core.redis import get_sync_redis

MAX_AGE = 180


def main(component: str) -> int:
    if component == "api":
        with urllib.request.urlopen("http://127.0.0.1:8000/health/live", timeout=5) as resp:  # noqa: S310
            return 0 if resp.status == 200 else 1
    raw = get_sync_redis().get(f"heartbeat:{component}")
    return 0 if raw is not None and time.time() - float(raw) < MAX_AGE else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:  # noqa: BLE001
        print(f"unhealthy: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
