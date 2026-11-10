"""Subscription usage as the workers last read it; only their cached responses, never a request."""

import json
import math
import time
from datetime import datetime
from pathlib import Path

from .store import data_dir

SESSION, WEEK = 5 * 3600.0, 7 * 86400.0


def providers(config):
    """Providers whose usage windows the profile's workers follow; none under API billing."""
    agent = config.get("agent") or "claude"
    names = []
    if agent != "codex" and config.get("claude_billing") == "subscription":
        names.append("claude")
    if agent != "claude":
        names.append("codex")
    return names


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
    login = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return [
        *login.glob(".tauceti-quota/usage/quota-claude-*.json"),
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


def cached(path, now):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    fetched_at = number(value.get("fetched_at")) if isinstance(value, dict) else None
    if fetched_at is None or fetched_at > now or not isinstance(value.get("payload"), dict):
        return None
    return fetched_at, value["payload"]


def readings(config, now=None):
    """The newest cached reading per provider the profile uses, as plain data.

    {provider: None before any reading, or {fetched_at, windows: {session | weekly:
    {seconds, used, left, resets_at}}}}. Times are epoch seconds, used and left are
    percentages; both are None when the response gave no usable figure or the
    window has reset since.
    """
    now = time.time() if now is None else now
    result = {}
    for provider in providers(config):
        found = [entry for entry in (cached(path, now) for path in reading_files(config, provider)) if entry]
        if not found:
            result[provider] = None
            continue
        fetched_at, payload = max(found, key=lambda entry: entry[0])
        windows = claude_windows(payload, now) if provider == "claude" else codex_windows(payload, fetched_at, now)
        result[provider] = dict(fetched_at=fetched_at, windows=windows)
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


def display(value, now=None):
    now = time.time() if now is None else now
    for provider, reading in value.items():
        label = provider.capitalize() + " usage: "
        if not reading:
            print(label + "no reading yet")
            continue
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
        print(label + ("; ".join(parts) or "unreadable response") + f" (read {span(now - reading['fetched_at'])} ago)")
