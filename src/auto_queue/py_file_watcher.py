#!/usr/bin/env python3
"""
py_file_watcher.py -- always-on background automation: intake/applications
watchers, materials export to your cloud drive, LM Studio warm-up, all in
one process. Config: config/file_watcher.json. See util/Register-FileWatcher.ps1.
"""
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Config lives at jobs/config/file_watcher.json, a different directory
# than this script, so its path is located explicitly.

SCRIPT_DIR = Path(__file__).resolve().parent
JOBS_ROOT = SCRIPT_DIR.parent.parent  # auto_queue -> src -> jobs
CONFIG_PATH = JOBS_ROOT / "config" / "file_watcher.json"


def resolve_path(value: str, base_dir: Path) -> Path:
    """Absolute paths pass through unchanged; anything else resolves
    relative to base_dir. Uses PureWindowsPath for the absoluteness
    check so 'C:/...' is still recognized as absolute on non-Windows hosts."""
    from pathlib import PureWindowsPath
    if PureWindowsPath(value).is_absolute():
        return Path(value)
    return (base_dir / value).resolve()


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
CONFIG_DIR = CONFIG_PATH.parent

SERVER_CFG = CONFIG["server"]
DB_PATH = resolve_path(SERVER_CFG["db_file"], CONFIG_DIR)

DEBOUNCE_SECONDS = CONFIG.get("watcher", {}).get("debounce_seconds", 0.75)

LOG_PATH = resolve_path(CONFIG["logging"]["log_file"], CONFIG_DIR)

INTAKE_CFG = CONFIG.get("intake")
if INTAKE_CFG:
    INTAKE_WATCH_DIR = resolve_path(INTAKE_CFG["watch_dir"], CONFIG_DIR)
    INTAKE_ACTION = INTAKE_CFG.get("action", "save_job_details")

CHROME_CFG = CONFIG.get("chrome")
if CHROME_CFG and CHROME_CFG.get("enabled", True):
    CHROME_PROFILE_DIRECTORY = CHROME_CFG.get("profile_directory", "Default")
    CHROME_EXE_CANDIDATES = [Path(os.path.expandvars(p)) for p in CHROME_CFG.get("exe_candidates", [])]

APPLICATIONS_CFG = CONFIG.get("applications")
if APPLICATIONS_CFG:
    APPLICATIONS_ROOT = resolve_path(APPLICATIONS_CFG["applications_root"], CONFIG_DIR)
    INPUT_FILENAME = APPLICATIONS_CFG["input_filename"]
    PYTHON_EXE = APPLICATIONS_CFG["python_exe"]  # always an absolute interpreter path
    PIPELINE_SCRIPT = resolve_path(APPLICATIONS_CFG["pipeline_script"], CONFIG_DIR)
    PIPELINE_ARGS = APPLICATIONS_CFG.get("pipeline_args", [])
    MAX_CONCURRENT_RUNS = int(APPLICATIONS_CFG.get("max_concurrent_runs", 1))

MATERIALS_CFG = CONFIG.get("materials_export")
if MATERIALS_CFG:
    MATERIALS_DIRNAME = MATERIALS_CFG["generated_materials_dirname"]
    MATERIALS_ROOT = resolve_path(MATERIALS_CFG["proton_materials_root"], CONFIG_DIR)

METRICS_CFG = CONFIG.get("metrics", {"enabled": True, "jd_url_field_candidates": ["url"]})

LM_STUDIO_CFG = CONFIG.get("lm_studio")
ensure_lm_studio_ready = None
if LM_STUDIO_CFG and LM_STUDIO_CFG.get("enabled", True):
    LMSTUDIO_MODEL_ID = LM_STUDIO_CFG["model_id"]
    LMSTUDIO_BASE_URL = LM_STUDIO_CFG.get("base_url", "http://localhost:1234/v1")
    LMSTUDIO_EXE_CANDIDATES = [Path(os.path.expandvars(p)) for p in LM_STUDIO_CFG.get("exe_candidates", [])]
    LMSTUDIO_APP_LAUNCH_TIMEOUT = int(LM_STUDIO_CFG.get("app_launch_timeout_seconds", 90))
    LMSTUDIO_MODEL_LOAD_TIMEOUT = int(LM_STUDIO_CFG.get("model_load_timeout_seconds", 600))
    LMSTUDIO_POLL_INTERVAL = int(LM_STUDIO_CFG.get("poll_interval_seconds", 2))

    # Explicit sys.path insert since ensure_lm_studio_ready.py's sibling
    # location only lands on sys.path[0] automatically when run as __main__.
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from ensure_lm_studio_ready import ensure_lm_studio_ready
    except ImportError:
        ensure_lm_studio_ready = None  # logged at first use, once logger exists

# Logging: JSON Lines, one JSON object per line, for easy jq/Notion ingestion.


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


logger = logging.getLogger("file_watcher")
logger.setLevel(logging.INFO)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(JsonLineFormatter())
logger.addHandler(_file_handler)
logger.addHandler(logging.StreamHandler(sys.stdout))  # plain text if run in a console

# ---------------------------------------------------------------------------
# SQLite: trigger queue (intake -> extension) + run/export tracking
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                url TEXT,
                meta TEXT,
                source_file TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS application_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id TEXT NOT NULL,
                job_dir TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                started_at TEXT,
                finished_at TEXT,
                returncode INTEGER,
                log_file TEXT,
                UNIQUE(application_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS materials_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                source_mtime REAL NOT NULL,
                copied_at TEXT NOT NULL,
                UNIQUE(application_id, filename)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_metrics (
                application_id TEXT PRIMARY KEY,
                matched_trigger_id INTEGER,
                intake_enqueued_at TEXT,
                jd_landed_at TEXT,
                materials_landed_at TEXT,
                intake_to_jd_seconds REAL,
                jd_to_materials_seconds REAL,
                updated_at TEXT NOT NULL
            )
            """
        )


def enqueue_trigger(action: str, url: str | None, meta: dict, source_file: str) -> int:
    with _db_lock, db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO triggers (action, url, meta, source_file, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (action, url, json.dumps(meta), source_file, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get_pending_triggers() -> list[dict]:
    with _db_lock, db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def ack_trigger(trigger_id: int) -> bool:
    with _db_lock, db_conn() as conn:
        cur = conn.execute(
            "UPDATE triggers SET status = 'done' WHERE id = ? AND status = 'pending'",
            (trigger_id,),
        )
        return cur.rowcount > 0


def try_claim_run(application_id: str, job_dir: str) -> bool:
    """UNIQUE constraint is the dedupe mechanism — persists across restarts.
    To force a re-run, delete that application_id's row from application_runs."""
    with _db_lock, db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO application_runs (application_id, job_dir, status) VALUES (?, ?, 'queued')",
                (application_id, job_dir),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def mark_run_started(application_id: str, log_file: str):
    with _db_lock, db_conn() as conn:
        conn.execute(
            "UPDATE application_runs SET status='running', started_at=?, log_file=? WHERE application_id=?",
            (datetime.now(timezone.utc).isoformat(), log_file, application_id),
        )


def mark_run_finished(application_id: str, returncode: int):
    with _db_lock, db_conn() as conn:
        conn.execute(
            "UPDATE application_runs SET status=?, finished_at=?, returncode=? WHERE application_id=?",
            (
                "done" if returncode == 0 else "failed",
                datetime.now(timezone.utc).isoformat(),
                returncode,
                application_id,
            ),
        )


def should_export(application_id: str, filename: str, mtime: float) -> bool:
    with _db_lock, db_conn() as conn:
        row = conn.execute(
            "SELECT source_mtime FROM materials_exports WHERE application_id=? AND filename=?",
            (application_id, filename),
        ).fetchone()
        if row is None:
            return True
        return abs(row["source_mtime"] - mtime) > 0.5  # fs mtime precision varies across sync


def record_export(application_id: str, filename: str, mtime: float):
    with _db_lock, db_conn() as conn:
        conn.execute(
            "INSERT INTO materials_exports (application_id, filename, source_mtime, copied_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(application_id, filename) DO UPDATE SET "
            "source_mtime=excluded.source_mtime, copied_at=excluded.copied_at",
            (application_id, filename, mtime, datetime.now(timezone.utc).isoformat()),
        )


# Timing metrics: A = intake trigger enqueued -> jd_input.json landing.
# B = jd_input.json landing -> last materials export. Read from
# jd_input.json's own fields (metrics.intake_timestamp_field/_trigger_id_field).


def extract_intake_metrics_fields(
    job_dir: Path, input_filename: str, timestamp_field: str, trigger_id_field: str
) -> tuple[str | None, int | None]:
    try:
        data = json.loads((job_dir / input_filename).read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    ts = data.get(timestamp_field)
    ts = ts if isinstance(ts, str) and ts else None
    trigger_id = data.get(trigger_id_field)
    trigger_id = trigger_id if isinstance(trigger_id, int) else None
    return ts, trigger_id


def record_jd_landed(
    application_id: str,
    jd_landed_at: datetime,
    matched_trigger_id: int | None,
    intake_enqueued_at: str | None,
):
    intake_to_jd_seconds = None
    if intake_enqueued_at:
        try:
            enqueued = datetime.fromisoformat(intake_enqueued_at)
            intake_to_jd_seconds = (jd_landed_at - enqueued).total_seconds()
        except ValueError:
            logger.warning(
                "jd_input.json for %s had an unparseable intake timestamp %r — metric A skipped for this app.",
                application_id, intake_enqueued_at,
            )
            intake_enqueued_at = None
    with _db_lock, db_conn() as conn:
        conn.execute(
            "INSERT INTO pipeline_metrics "
            "(application_id, matched_trigger_id, intake_enqueued_at, jd_landed_at, intake_to_jd_seconds, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(application_id) DO UPDATE SET "
            "matched_trigger_id=excluded.matched_trigger_id, "
            "intake_enqueued_at=excluded.intake_enqueued_at, "
            "jd_landed_at=excluded.jd_landed_at, "
            "intake_to_jd_seconds=excluded.intake_to_jd_seconds, "
            "updated_at=excluded.updated_at",
            (
                application_id, matched_trigger_id, intake_enqueued_at,
                jd_landed_at.isoformat(), intake_to_jd_seconds, jd_landed_at.isoformat(),
            ),
        )


def record_materials_landed(application_id: str, exported_at: datetime):
    """Always advances materials_landed_at forward to the LATEST export
    seen for this application — represents 'the full output set is done',
    not just the first file to appear."""
    with _db_lock, db_conn() as conn:
        row = conn.execute(
            "SELECT jd_landed_at, materials_landed_at FROM pipeline_metrics WHERE application_id=?",
            (application_id,),
        ).fetchone()

        jd_landed_at = datetime.fromisoformat(row["jd_landed_at"]) if row and row["jd_landed_at"] else None
        prev_materials_landed_at = (
            datetime.fromisoformat(row["materials_landed_at"]) if row and row["materials_landed_at"] else None
        )
        if prev_materials_landed_at and prev_materials_landed_at >= exported_at:
            return  # already have a later (or equal) timestamp recorded, nothing to do

        jd_to_materials_seconds = (exported_at - jd_landed_at).total_seconds() if jd_landed_at else None
        conn.execute(
            "INSERT INTO pipeline_metrics "
            "(application_id, materials_landed_at, jd_to_materials_seconds, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(application_id) DO UPDATE SET "
            "materials_landed_at=excluded.materials_landed_at, "
            "jd_to_materials_seconds=excluded.jd_to_materials_seconds, "
            "updated_at=excluded.updated_at",
            (application_id, exported_at.isoformat(), jd_to_materials_seconds, exported_at.isoformat()),
        )


# URL extraction (intake). Format-agnostic; also strips trailing markdown
# emphasis wrapping a URL (iPhone Notes: **bold**, _italic_, `code`).

URL_RE = re.compile(r"https?://[^\s'\"<>\)\]]+")


def extract_urls(text: str) -> list[str]:
    raw = URL_RE.findall(text)
    cleaned = [u.rstrip(".,;:!?)]}\"'*_~`") for u in raw]
    return list(dict.fromkeys(cleaned))  # dedupe, preserve order


def wait_for_stable_file(path: Path) -> bool:
    """Debounce: block until the file's size stops changing (sync writes in
    chunks). Returns False if the file vanished before settling."""
    last_size = -1
    for _ in range(20):
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == last_size:
            return True
        last_size = size
        time.sleep(DEBOUNCE_SECONDS)
    return True


# ---------------------------------------------------------------------------
# Chrome auto-launch: the extension can only poll for triggers while Chrome
# is actually running, and nothing used to start it for you.
# ---------------------------------------------------------------------------

_chrome_launch_lock = threading.Lock()


def is_chrome_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return "chrome.exe" in result.stdout.lower()
    except Exception as e:
        logger.warning("Could not check for a running chrome.exe: %s", e)
        return True  # assume running rather than risk spawning a duplicate


def ensure_chrome_running():
    if not (CHROME_CFG and CHROME_CFG.get("enabled", True)):
        return
    with _chrome_launch_lock:
        if is_chrome_running():
            return
        exe = next((p for p in CHROME_EXE_CANDIDATES if p.exists()), None)
        if exe is None:
            logger.error("Chrome auto-launch enabled but no chrome.exe found in configured exe_candidates.")
            return
        try:
            # --profile-directory skips the profile picker Chrome otherwise
            # shows on a cold launch with multiple profiles configured.
            subprocess.Popen(
                [str(exe), f"--profile-directory={CHROME_PROFILE_DIRECTORY}"],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            logger.info("Launched Chrome (profile-directory=%s) — was not running.", CHROME_PROFILE_DIRECTORY)
        except Exception as e:
            logger.error("Failed to launch Chrome: %s", e)


# ---------------------------------------------------------------------------
# Intake: URL-drop file -> N triggers, one per URL, each an independent tab
# ---------------------------------------------------------------------------


class IntakeHandler(FileSystemEventHandler):
    def __init__(self, action: str, move_to: Path):
        self.action = action
        self.move_to = move_to
        self.move_to.mkdir(parents=True, exist_ok=True)

    def on_created(self, event):
        if event.is_directory:
            return
        threading.Thread(target=self._handle_file, args=(Path(event.src_path),), daemon=True).start()

    def _handle_file(self, path: Path):
        if not wait_for_stable_file(path):
            return
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Could not read %s: %s", path, e)
            return

        urls = extract_urls(text)
        if not urls:
            logger.warning("No URLs found in %s — leaving file in place for inspection.", path)
            return

        for url in urls:
            trigger_id = enqueue_trigger(self.action, url, {}, str(path))
            logger.info("Enqueued intake trigger #%d action=%s url=%s", trigger_id, self.action, url)

        ensure_chrome_running()

        dest = self.move_to / path.name
        try:
            path.rename(dest)
        except Exception as e:
            logger.error("Could not move %s -> %s: %s", path, dest, e)


# ---------------------------------------------------------------------------
# Applications: jd_input.json -> claimed run -> queued pipeline execution
# ---------------------------------------------------------------------------

_pipeline_queue: "queue.Queue[tuple[str, Path]]" = queue.Queue()
_lm_studio_lock = threading.Lock()  # serialize warm-up across concurrent pipeline workers


class ApplicationIntakeHandler(FileSystemEventHandler):
    """Watches applications_root recursively; when <app_id>/jd_input.json
    appears, claims the run (DB UNIQUE constraint dedupes) and enqueues it."""

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.name != INPUT_FILENAME:
            return
        threading.Thread(target=self._handle_file, args=(path,), daemon=True).start()

    def _handle_file(self, path: Path):
        if not wait_for_stable_file(path):
            return

        job_dir = path.parent
        application_id = job_dir.name

        if not try_claim_run(application_id, str(job_dir)):
            logger.info(
                "Skipping %s — a run for application_id=%s is already queued/done. "
                "Delete its row in application_runs to force a re-run.",
                path, application_id,
            )
            return

        jd_landed_at = datetime.now(timezone.utc)
        if METRICS_CFG.get("enabled", True):
            intake_enqueued_at, matched_trigger_id = extract_intake_metrics_fields(
                job_dir, INPUT_FILENAME,
                METRICS_CFG.get("intake_timestamp_field", "intake_enqueued_at"),
                METRICS_CFG.get("intake_trigger_id_field", "intake_trigger_id"),
            )
            record_jd_landed(application_id, jd_landed_at, matched_trigger_id, intake_enqueued_at)

        logger.info("Claimed pipeline run for application_id=%s (%s)", application_id, path)
        _pipeline_queue.put((application_id, job_dir))


def pipeline_worker_loop():
    while True:
        application_id, job_dir = _pipeline_queue.get()
        try:
            _run_pipeline(application_id, job_dir)
        except Exception:
            logger.exception("Unhandled error running pipeline for %s", application_id)
        finally:
            _pipeline_queue.task_done()


def _run_pipeline(application_id: str, job_dir: Path):
    if LM_STUDIO_CFG and LM_STUDIO_CFG.get("enabled", True):
        if ensure_lm_studio_ready is None:
            logger.error(
                "lm_studio is enabled in config but ensure_lm_studio_ready.py wasn't importable "
                "from %s — proceeding without a warm-up check.", JOBS_ROOT / "src" / "core",
            )
        else:
            # Locked so with max_concurrent_runs > 1 only the first worker
            # drives the launch/load sequence; the rest find it already warm.
            with _lm_studio_lock:
                logger.info("Ensuring LM Studio is serving %s before starting pipeline for %s...",
                            LMSTUDIO_MODEL_ID, application_id)
                ready = ensure_lm_studio_ready(
                    model_id=LMSTUDIO_MODEL_ID,
                    base_url=LMSTUDIO_BASE_URL,
                    exe_candidates=LMSTUDIO_EXE_CANDIDATES or None,
                    app_launch_timeout=LMSTUDIO_APP_LAUNCH_TIMEOUT,
                    model_load_timeout=LMSTUDIO_MODEL_LOAD_TIMEOUT,
                    poll_interval=LMSTUDIO_POLL_INTERVAL,
                )
            if not ready:
                logger.error("LM Studio not ready (model=%s) — aborting pipeline run for %s.",
                              LMSTUDIO_MODEL_ID, application_id)
                mark_run_finished(application_id, returncode=-1)
                return

    args = [arg.format(job_dir=str(job_dir), application_id=application_id) for arg in PIPELINE_ARGS]
    cmd = [PYTHON_EXE, str(PIPELINE_SCRIPT), *args]

    env = os.environ.copy()
    if LM_STUDIO_CFG and LM_STUDIO_CFG.get("enabled", True):
        env["LMSTUDIO_MODEL"] = LMSTUDIO_MODEL_ID  # loading the model doesn't make the pipeline use it

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_file = JOBS_ROOT / "log" / f"pipeline_{application_id}_{timestamp}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    mark_run_started(application_id, str(log_file))

    logger.info("Starting pipeline for %s: %s", application_id, " ".join(cmd))
    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(PIPELINE_SCRIPT.parent), env=env)

    mark_run_finished(application_id, proc.returncode)
    level = logging.INFO if proc.returncode == 0 else logging.ERROR
    logger.log(level, "Pipeline for %s finished with code %s (log: %s)", application_id, proc.returncode, log_file)


def start_pipeline_workers():
    for i in range(MAX_CONCURRENT_RUNS):
        threading.Thread(target=pipeline_worker_loop, daemon=True, name=f"pipeline-worker-{i}").start()
    logger.info("Started %d pipeline worker thread(s)", MAX_CONCURRENT_RUNS)


# Materials export: decoupled from how the run started, reacts only to
# files appearing under generated_materials/.


class MaterialsExportHandler(FileSystemEventHandler):
    def on_created(self, event):
        self._maybe_export(event)

    def on_modified(self, event):
        self._maybe_export(event)

    def _maybe_export(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.parent.name != MATERIALS_DIRNAME:
            return
        threading.Thread(target=self._export_file, args=(path,), daemon=True).start()

    def _export_file(self, path: Path):
        if not wait_for_stable_file(path):
            return

        application_id = path.parent.parent.name  # <app_id>/generated_materials/<file>
        mtime = path.stat().st_mtime

        if not should_export(application_id, path.name, mtime):
            return

        dest_dir = MATERIALS_ROOT / application_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            shutil.copy2(path, dest)
        except Exception as e:
            logger.error("Failed to export %s -> %s: %s", path, dest, e)
            return

        record_export(application_id, path.name, mtime)
        logger.info("Exported material %s -> %s", path, dest)

        if METRICS_CFG.get("enabled", True):
            record_materials_landed(application_id, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def start_watchers() -> Observer:
    observer = Observer()

    if INTAKE_CFG:
        INTAKE_WATCH_DIR.mkdir(parents=True, exist_ok=True)
        intake_move_to = INTAKE_WATCH_DIR / "processed"
        observer.schedule(IntakeHandler(INTAKE_ACTION, intake_move_to), str(INTAKE_WATCH_DIR), recursive=False)
        logger.info("Watching %s for URL-drop files -> action=%s", INTAKE_WATCH_DIR, INTAKE_ACTION)

    if APPLICATIONS_CFG:
        APPLICATIONS_ROOT.mkdir(parents=True, exist_ok=True)
        observer.schedule(ApplicationIntakeHandler(), str(APPLICATIONS_ROOT), recursive=True)
        logger.info("Watching %s (recursive) for %s -> pipeline auto-run", APPLICATIONS_ROOT, INPUT_FILENAME)

        if MATERIALS_CFG:
            MATERIALS_ROOT.mkdir(parents=True, exist_ok=True)
            observer.schedule(MaterialsExportHandler(), str(APPLICATIONS_ROOT), recursive=True)
            logger.info(
                "Watching %s (recursive) for %s/ output -> exporting to %s",
                APPLICATIONS_ROOT, MATERIALS_DIRNAME, MATERIALS_ROOT,
            )

    observer.start()
    return observer


app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "intake": bool(INTAKE_CFG), "applications": bool(APPLICATIONS_CFG)})


@app.get("/poll")
def poll():
    return jsonify({"pending": get_pending_triggers()})


@app.post("/ack/<int:trigger_id>")
def ack(trigger_id: int):
    ok = ack_trigger(trigger_id)
    return jsonify({"acked": ok}), (200 if ok else 404)


@app.get("/runs")
def runs():
    with _db_lock, db_conn() as conn:
        rows = conn.execute("SELECT * FROM application_runs ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify({"runs": [dict(r) for r in rows]})


@app.get("/metrics")
def metrics():
    with _db_lock, db_conn() as conn:
        rows = conn.execute("SELECT * FROM pipeline_metrics ORDER BY updated_at DESC LIMIT 50").fetchall()
        return jsonify({"metrics": [dict(r) for r in rows]})


def run_server():
    host, port = SERVER_CFG["host"], SERVER_CFG["port"]
    logger.info("Trigger server listening on http://%s:%s", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    init_db()
    if APPLICATIONS_CFG:
        start_pipeline_workers()
    observer = start_watchers()
    try:
        run_server()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()