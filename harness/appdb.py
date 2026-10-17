"""`th db`: read and change the app's local database, its Core Data SQLite store, on the simulator.

    th db stores                                    # the SQLite files in the app's containers; the default is marked
    th db tables                                    # tables and row counts of the app's store
    th db schema ZBOLUSSTORED                       # a table's columns
    th db query "SELECT Z_PK, ZAMOUNT FROM ZBOLUSSTORED ORDER BY Z_PK DESC LIMIT 5"
    th db exec "UPDATE ZBOLUSSTORED SET ZWASPROGRAMMEDBYPUMPUI = 1 WHERE Z_PK = 3"

Stores (Core Data's default location, or LoopKit's PersistenceController):
  trio  data container  Library/Application Support/TrioCoreDataPersistentContainer.sqlite
  loop  app group       com.loopkit.LoopKit/Model.sqlite (found by name anywhere in the group container)
Core Data names tables Z<ENTITY> and columns Z<ATTRIBUTE> in capitals, keys Z_PK; dates are seconds since 2001-01-01.

`query` opens the store read-only. `exec` terminates the app first (Core Data caches rows, and would write stale ones
back over the change), copies the store into the run's db/ before the run's first change, runs the SQL in one
transaction, records it as a run step and launches the app again (--no-relaunch to leave it closed). Change existing
rows rather than inserting: Core Data keeps its own primary key counters (Z_PRIMARYKEY) and relationships.

Simulator only. The CI / web UI agent's OS sandbox lets the harness write to the app's own data and app group containers
and nothing else of the simulator's (sandbox_paths(), used by ci/claude-sandbox.py).
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .util import HarnessError

STORES = {"trio": "TrioCoreDataPersistentContainer.sqlite", "loop": "Model.sqlite"}
# SQLite files apps get from the system (URL cache, WebKit), not the app's data
SKIP_DIRS = {"Caches", "HTTPStorages", "WebKit", "SplashBoard", "Cookies"}
SIDECARS = ("", "-wal", "-shm")


def bundle_ids(app) -> list[str]:
    """Where to look for the app on the simulator: the last simulator build's bundle id, then the one a simulator build
    of the app gets (a stale or foreign last-build.json must not hide the installed app)."""
    from .build import BuildInfo
    from .config import CONFIG
    bi = BuildInfo.load(app)
    ids = [bi.bundle_id] if bi and bi.target == "sim" else []
    return list(dict.fromkeys([*ids, app.bundle_id(CONFIG.team_for("sim"))]))


def installed_bundle(drv, app) -> str:
    ids = bundle_ids(app)
    for b in ids:
        if drv.is_installed(b):
            return b
    raise HarnessError(f"{app.name} is not installed on the harness simulator (looked for {', '.join(ids)})")


def containers(drv, bundle_id: str) -> list[Path]:
    """The app's data container and its app group containers on the simulator."""
    if not drv.is_installed(bundle_id):
        raise HarnessError(f"{bundle_id} is not installed on the harness simulator")
    return [Path(drv.app_data_container(bundle_id)), *(Path(p) for p in drv.app_group_containers(bundle_id).values())]


def stores(roots: list[Path]) -> list[Path]:
    return sorted(p for root in roots for p in root.rglob("*.sqlite")
                  if not SKIP_DIRS & set(p.relative_to(root).parts))


def pick_store(roots: list[Path], app: str, name: str | None = None) -> Path:
    """The store called `name` (file name or a path ending in it), else the app's own store."""
    found = stores(roots)
    want = name or STORES[app]
    hits = [p for p in found if p.name == want or str(p).endswith("/" + want.lstrip("/"))]
    if len(hits) == 1:
        return hits[0]
    listing = "\n  ".join(str(p) for p in found) or "(none)"
    if not hits:
        hint = "" if name else " (has the app run once since it was installed?)"
        raise HarnessError(f"no store {want}{hint}; the app's SQLite files:\n  {listing}")
    raise HarnessError(f"several stores match {want}; pass a longer --store path:\n  {listing}")


@contextmanager
def _connect(store: Path, *, readonly: bool):
    # autocommit (isolation_level=None): transactions are only the ones written out below
    with closing(sqlite3.connect(f"file:{store}?mode={'ro' if readonly else 'rw'}", uri=True, timeout=10,
                                 isolation_level=None)) as con:
        if readonly:
            con.execute("PRAGMA query_only = ON")
        yield con


def tables(store: Path) -> list[tuple[str, int]]:
    with _connect(store, readonly=True) as con:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return [(n, con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in names]


def schema(store: Path, table: str) -> list[tuple[str, str]]:
    with _connect(store, readonly=True) as con:
        cols = [(r[1], r[2]) for r in con.execute(f'PRAGMA table_info("{table}")')]
    if not cols:
        raise HarnessError(f"no table {table} (`th db tables` lists them)")
    return cols


def query(store: Path, sql: str) -> tuple[list[str], list[tuple]]:
    with _connect(store, readonly=True) as con:
        try:
            cur = con.execute(sql)
        except sqlite3.Error as e:
            raise HarnessError(f"sqlite: {e}") from None
        return [d[0] for d in cur.description or []], cur.fetchall()


def backup(store: Path, dest: Path) -> Path | None:
    """Copy the store and its WAL files into dest/<store name without .sqlite>-before/, once: the state before the
    first change."""
    out = dest / f"{store.stem}-before"
    if out.exists():
        return None
    out.mkdir(parents=True)
    for suffix in SIDECARS:
        src = store.with_name(store.name + suffix)
        if src.exists():
            shutil.copy2(src, out / src.name)
    return out


def execute(store: Path, sql: str) -> int:
    """Run `sql` (one or more statements) in one transaction; returns the rows changed."""
    with _connect(store, readonly=False) as con:
        before = con.total_changes
        try:
            con.executescript(f"BEGIN IMMEDIATE;\n{sql};\nCOMMIT;")
        except sqlite3.Error as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise HarnessError(f"sqlite: {e} (nothing was changed)") from None
        return con.total_changes - before


def wait_closed(drv, bundle_id: str, store: Path, timeout: float = 10) -> None:
    """Terminate the app and wait until its write lock on the store is gone."""
    drv.terminate(bundle_id)
    deadline = time.time() + timeout
    while True:
        try:
            with _connect(store, readonly=False) as con:
                con.execute("BEGIN IMMEDIATE"); con.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            if time.time() > deadline:
                raise HarnessError(f"{store.name} is still locked {timeout:.0f}s after terminating {bundle_id}")
            time.sleep(0.5)


def sandbox_paths(app: str | None = None) -> list[str]:
    """Directories the agent's OS sandbox may write so `th db` works: the installed app's data container and app group
    containers on the harness simulator, nothing more. Empty when the simulator or the app is not there. Runs outside
    the sandbox, before the agent starts (ci/claude-sandbox.py)."""
    from . import apps, xcode
    from .config import CONFIG
    a = apps.get(app) if app else apps.current()
    name, rt = CONFIG.get("TH_SIM_NAME"), xcode.pick_runtime(CONFIG.get("TH_SIM_RUNTIME"))["identifier"]
    udid = next((d["udid"] for d in xcode.list_devices() if d["name"] == name and d["runtimeIdentifier"] == rt), None)
    if not udid:
        return []
    out = []
    for bundle_id in bundle_ids(a):
        for kind in ("data", "groups"):
            cp = xcode.xcrun(["simctl", "get_app_container", udid, bundle_id, kind], quiet=True, check=False)
            if cp.returncode == 0:
                # data: one path; groups: "<group id>\t<path>" per line
                out += [line.split(None, 1)[-1].strip() for line in cp.stdout.splitlines() if line.strip()]
        if out:
            break
    # never more than this simulator's per-app containers (anything else would mean simctl answered oddly)
    return [p for p in out if f"/{udid}/data/Containers/" in p]
