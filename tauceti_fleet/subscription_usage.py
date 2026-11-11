"""Subscription usage as the workers last read it; only their cached responses, never a request."""

import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path

from .store import data_dir

SESSION, WEEK = 5 * 3600.0, 7 * 86400.0
# How long a worker keeps serving its cached reading for the credential it was fetched under.
SERVED = dict(claude=3600.0, codex=600.0)


def providers(config):
    """Providers whose usage windows the profile's workers follow; none under API billing."""
    agent = config.get("agent") or "claude"
    names = []
    if agent != "codex" and config.get("claude_billing") == "subscription":
        names.append("claude")
    if agent != "claude":
        names.append("codex")
    return names


def claude_login(config):
    """The Claude login directory the profile's workers copy; a profile's env may not move it."""
    return Path.home() / ".claude"


def reading_files(config, provider):
    """Every place the worker's pacer may have cached this provider's last usage response.

    Codex readings are kept per worker. Claude readings are shared beside the
    login the workers copy, or under the worker state when that is read-only.
    """
    env = config.get("env") or {}
    # Worker state stays inside the runtime that created it, so an earlier runtime can hold the newest reading.
    roots = list((data_dir() / "worker-runtimes").glob("*/lib/python*/site-packages/tauceti_worker"))
    if env.get("TAUCETI_LEGACY_DATA_ROOT"):
        roots.append(Path(env["TAUCETI_LEGACY_DATA_ROOT"]))
    if provider == "codex":
        return [path for root in roots for path in root.glob("state/*/cache/quota-codex.json")]
    return [
        *claude_login(config).glob(".tauceti-quota/usage/quota-claude-*.json"),
        *(path for root in roots for path in root.glob("state/.quota-usage/*/quota-claude-*.json")),
    ]


def number(value):
    try:
        value = float(value) if type(value) in (int, float) else math.nan
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def percent(value):
    value = number(value)
    return value if value is not None and value >= 0 else None


def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def window(seconds, used, resets_at, now):
    # Past its reset, a figure describes the window that ended.
    if resets_at is not None and resets_at <= now:
        used = None
    return dict(seconds=seconds, used=used, left=None if used is None else max(0.0, 100 - used), resets_at=resets_at)


def claude_window(record, seconds, now):
    if record is None:
        return window(seconds, 0.0, None, now)
    if not isinstance(record, dict):
        return window(seconds, None, None, now)
    raw = record.get("percent", record.get("utilization"))
    used, resets_at = percent(raw), timestamp(record.get("resets_at"))
    # An unopened window has a null usage or reset time and no value for the other.
    if raw is None and record.get("resets_at") is None and record.keys() & {"percent", "utilization", "resets_at"}:
        used = 0.0
    return window(seconds, used, resets_at, now)


def claude_windows(payload, now):
    limits = payload.get("limits")
    limits = [r for r in limits if isinstance(r, dict)] if isinstance(limits, list) else []
    groups = [(r, str(r.get("group") or r.get("kind") or "")) for r in limits]
    # Per-model weekly limits carry a scope and do not gate the worker.
    listed = dict(
        session=[r for r, group in groups if group == "session"],
        weekly=[r for r, group in groups if group.startswith("weekly") and not r.get("scope")],
    )
    result = {}
    for name, key, seconds in (("session", "five_hour", SESSION), ("weekly", "seven_day", WEEK)):
        if listed[name]:
            result[name] = claude_window(listed[name][0], seconds, now)
        elif key in payload:
            result[name] = claude_window(payload[key], seconds, now)
    return result


def codex_windows(payload, fetched_at, now):
    limits = payload.get("rate_limit")
    result = {}
    for key in ("primary_window", "secondary_window"):
        record = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(record, dict):
            continue
        seconds, after = number(record.get("limit_window_seconds")), number(record.get("reset_after_seconds"))
        # Codex has moved its windows between the two slots; only the length identifies one.
        name = "session" if seconds is not None and seconds <= 86400 else "weekly"
        result[name] = window(seconds, percent(record.get("used_percent")), None if after is None else fetched_at + after, now)
    return result


def credential(fp):
    """A short label for the credential a reading was fetched under; for Claude, the start of the cache file's suffix."""
    return hashlib.sha256(fp.encode()).hexdigest()[:8] if isinstance(fp, str) and fp else None


def cached(path, now):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    fetched_at = number(value.get("fetched_at")) if isinstance(value, dict) else None
    if fetched_at is None or fetched_at > now or not isinstance(value.get("payload"), dict):
        return None
    valid_until = number(value.get("valid_until"))
    return dict(fetched_at=fetched_at, payload=value["payload"], credential=credential(value.get("fp")),
                valid_until=valid_until)


def limit_reached(payload):
    """Codex's own verdict that the account is blocked; the worker treats every window as spent while it is set."""
    limits = payload.get("rate_limit")
    return isinstance(limits, dict) and bool(limits.get("limit_reached"))


def exhausted(payload, windows, now):
    """Whether the reading shows a spent window that has not reset, which a worker keeps acting on after the reading expires."""
    reached = limit_reached(payload)
    return any(item["resets_at"] is not None and item["resets_at"] > now and (reached or (item["used"] or 0) >= 100)
               for item in windows.values())


def readings(config, now=None):
    """The cached readings per provider the profile uses, as plain data.

    {provider: None before any reading, or {credential, other_credentials, fetched_at,
    windows: {session | weekly: {seconds, used, left, resets_at}}}}, and for Codex
    limit_reached as the response gave it. Times are epoch
    seconds, used and left are percentages; both are None when the response gave no
    usable figure or the window has reset since. A reading is current while a worker
    would still serve it or while a spent window in it has not reset. While more than
    one credential has a current reading, the provider's value is {credentials:
    [reading, ...]}, newest first, since the active one cannot be told from the caches.
    """
    now = time.time() if now is None else now
    result = {}
    for provider in providers(config):
        newest = {}
        for entry in filter(None, (cached(path, now) for path in reading_files(config, provider))):
            if entry["fetched_at"] > newest.get(entry["credential"], {}).get("fetched_at", -math.inf):
                newest[entry["credential"]] = entry
        found = []
        for entry in sorted(newest.values(), key=lambda entry: entry["fetched_at"], reverse=True):
            payload, fetched_at = entry["payload"], entry["fetched_at"]
            windows = claude_windows(payload, now) if provider == "claude" else codex_windows(payload, fetched_at, now)
            served = now - fetched_at <= SERVED[provider] and (entry["valid_until"] is None or now < entry["valid_until"])
            reading = dict(credential=entry["credential"], fetched_at=fetched_at, windows=windows)
            if provider == "codex":
                reading["limit_reached"] = limit_reached(payload)
            found.append((served or exhausted(payload, windows, now), reading))
        current = [reading for is_current, reading in found if is_current]
        if len(current) > 1:
            result[provider] = dict(credentials=current)
        elif found:
            result[provider] = dict((current or [found[0][1]])[0], other_credentials=len(found) - 1)
        else:
            result[provider] = None
    return result


def status(config):
    value = readings(config)
    return dict(subscription_usage=value) if value else {}


def span(seconds):
    minutes = max(0, int(seconds)) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02}m" if hours < 48 else f"{hours // 24}d{hours % 24:02}h"


def windows_text(reading, now):
    parts = []
    for name, item in sorted(reading["windows"].items(), key=lambda pair: pair[1]["seconds"] or 0):
        seconds, resets_at = item["seconds"], item["resets_at"]
        length = name if not seconds else f"{seconds / 3600:g}h" if seconds <= 86400 else f"{seconds / 86400:g}d"
        if resets_at is not None and resets_at <= now:
            parts.append(f"{length} reset since this reading")
        elif item["left"] is None:
            parts.append(f"{length} unreadable")
        else:
            reset = f", resets in {span(resets_at - now)}" if resets_at is not None else "" if item["used"] else ", no window open"
            parts.append(f"{length} {item['left']:.0f}% left{reset}")
    if reading.get("limit_reached") and any(item["resets_at"] is None or item["resets_at"] > now for item in reading["windows"].values()):
        parts.insert(0, "limit reached")
    return ("; ".join(parts) or "unreadable response") + f" (read {span(now - reading['fetched_at'])} ago)"


def display(value, now=None):
    now = time.time() if now is None else now
    for provider, reading in value.items():
        label = provider.capitalize() + " usage: "
        if not reading:
            print(label + "no reading yet")
        elif "credentials" in reading:
            print(label + f"{len(reading['credentials'])} credentials have current readings; the workers use the active one")
            for each in reading["credentials"]:
                print(f"  credential {each['credential'] or 'unknown'}: " + windows_text(each, now))
        else:
            others = reading.get("other_credentials")
            if others:
                label = f"{provider.capitalize()} usage, credential {reading['credential'] or 'unknown'} ({others} other credential{'s have' if others > 1 else ' has'} an older reading): "
            print(label + windows_text(reading, now))
