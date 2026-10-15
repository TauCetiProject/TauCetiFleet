"""Private local configuration, persistent host identity and reporting events."""

import contextlib
import fcntl
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path


class FleetError(Exception):
    pass


def config_dir():
    return Path(os.environ.get("TAUCETI_FLEET_CONFIG", Path.home() / ".config/tauceti-fleet")).expanduser()


def state_dir():
    return Path(os.environ.get("TAUCETI_FLEET_STATE", Path.home() / ".local/state/tauceti-fleet")).expanduser()


def data_dir():
    return Path(os.environ.get("TAUCETI_FLEET_DATA", Path.home() / ".local/share/tauceti-fleet")).expanduser()


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise FleetError(f"Cannot read {path}: {type(exc).__name__}; preserve it and repair the file") from None


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".fleet-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def host_config():
    path = config_dir() / "host.json"
    with locked(path.with_suffix(".lock")):
        value = read(path)
        if value is None:
            value = dict(
                version=1,
                machine_id=uuid.uuid4().hex,
                created_at=time.time(),
                ci_enabled=False,
                workers_enabled=False,
                ci_allowed=True,
                profile="ai-for-science",
                max_workers=8,
                ci_vcpus=32,
                ci_memory_gib=64,
                ci_disk_gib=100,
                readiness_ttl=300,
            )
            atomic(path, value)
        return value


def update_host(**changes):
    host_config()
    path = config_dir() / "host.json"
    with locked(path.with_suffix(".lock")):
        value = read(path)
        value.update(changes)
        atomic(path, value)
        return value


@contextlib.contextmanager
def database():
    state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir() / "events.sqlite"
    db = sqlite3.connect(path, timeout=15)
    os.chmod(path, 0o600)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS prs (repository TEXT, number INTEGER, data TEXT NOT NULL, PRIMARY KEY(repository,number))"
    )
    db.commit()
    try:
        with db:
            yield db
    finally:
        db.close()


def event(kind, *, identity=None, at=None, **fields):
    identity = identity or uuid.uuid4().hex
    at = time.time() if at is None else at
    with database() as db:
        db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)", (identity, at, kind, json.dumps(fields)))


def import_worker_events():
    path = state_dir() / "worker-events.jsonl"
    if not path.exists():
        return
    with database() as db:
        stat = path.stat()
        name = "worker-log"
        prior = db.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        cursor = json.loads(prior[0]) if prior else {}
        offset = cursor.get("offset", 0) if cursor.get("inode") == stat.st_ino else 0
        if offset > stat.st_size:
            offset = 0
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                before = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    stream.seek(before)
                    break
                try:
                    value = json.loads(line)
                    if value.get("version") != 1 or not isinstance(value.get("pr"), int):
                        raise ValueError("invalid event")
                    db.execute(
                        "INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                        (value["id"], value["at"], value["kind"], json.dumps(value)),
                    )
                except (ValueError, KeyError, TypeError):
                    raise FleetError(
                        f"Invalid reporting record at byte {before} in {path}; preserve and repair it"
                    ) from None
            cursor = dict(inode=stat.st_ino, offset=stream.tell())
        db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (name, json.dumps(cursor)))


def export_events():
    import_worker_events()
    with database() as db:
        return [
            {**json.loads(row[3]), "id": row[0], "at": row[1], "kind": row[2]}
            for row in db.execute("SELECT id,at,kind,data FROM events ORDER BY at,id")
        ]
