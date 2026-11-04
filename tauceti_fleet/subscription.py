"""Prompt-free subscription observation, executed with the pinned worker Python."""

import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace


def observe(request):
    from tauceti_worker.paths import ensure_ssl_cert_file
    from tauceti_worker.quota import Quota

    ensure_ssl_cert_file()

    os.environ["CODEX_HOME"] = request["codex_home"]
    os.environ.pop("OPENAI_API_KEY", None)
    cache = Path(request["cache"])
    quota = Quota(SimpleNamespace(home=Path.home(), quota_cache=cache, state=cache, wid="fleet-subscription"))
    account = quota.codex_account()
    if not account or account.conflict or not account.matches(request["account"]):
        return dict(at=time.time(), error="Codex credential does not match the pool subscription account", windows=[])
    provider = quota.codex(refresh=True)
    return dict(
        at=time.time(),
        account_id=account.account_id,
        email=account.email,
        error=provider.error,
        rate_limited=provider.rate_limited,
        windows=[dataclasses.asdict(w) for w in provider.windows],
    )


def main():
    try:
        result = observe(json.load(sys.stdin))
    except Exception as exc:
        # Exception messages from provider libraries can contain request details.
        result = dict(at=time.time(), error=f"Subscription observation failed ({type(exc).__name__})", windows=[])
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
