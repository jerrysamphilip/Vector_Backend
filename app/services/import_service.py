# app/services/import_service.py
"""
CRM import (BR-CM-25..30; C-03..C-06, F-16): CSV / XLSX of contacts, companies, or both in one file.

Two ways in:
  * synchronous (``run_sync``): POST /imports with the file and a mapping, for files up to
    ``ImportLimits.sync_max_rows`` (1,000) rows; a larger file is stored and queued as a background job
    (202). Kept so the first import screen keeps working.
  * background (C-03): upload (the file is stored, tenant-scoped) -> mapping (suggested, or a saved
    mapping) -> dry run (counts, sample errors, nothing written) -> start (QUEUED) -> the worker's
    "imports" loop runs it in checkpointed chunks -> progress polling, cancel, resume.

The worker (``run_background``) claims a QUEUED job, or a RUNNING one whose heartbeat is older than
``STALE_AFTER_SECONDS`` (the process that ran it died), and processes chunks of ``CHUNK`` rows. Each
chunk commits together with the job's checkpoint (``processed_rows``), counters, errors and heartbeat,
so a resumed job starts at the first row that was not committed: nothing is imported twice. A cancel
request is honoured between chunks. Deadlocks between simultaneous imports roll the chunk back and
run it again.

``ImportService`` serves the routes; each public method is one unit of work. SQL lives in
app/repositories/imports.py, the row rules in app/services/import_rows.py, file reading and value
parsing in app/services/import_parsing.py.
"""

import csv
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.adapters.storage import FileStore, LocalFileStore
from app.core.clock import utcnow
from app.core.config import settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.imports import (
    COMPANY_TARGETS,
    CONTACT_TARGETS,
    ImportInputError,
    ImportLimits,
    ImportOptions,
    JobStatus,
    SavedMappingInput,
    parse_mapping,
    progress_percent,
    suggest_mapping,
)
from app.models.contact_field import FIELD_TYPES
from app.models.crm import ImportJob
from app.models.import_mapping import ImportMapping
from app.models.prospect_list import ProspectList
from app.models.user import User
from app.repositories.imports import ImportsUnitOfWork, Session, SqlAlchemyImportsUnitOfWork, imports_uow
from app.services import crm
from app.services.contact_service import NO_MANAGE_PERMISSION, can_manage_contacts, can_see_owner
from app.services.import_limits import FileRejected
from app.services.import_parsing import Table, data_error_reason, read_table, table_frame
from app.services.import_rows import (
    CONTACT_LIMITS,
    EngineState,
    ImportPlan,
    MappingError,
    RowEngine,
    RowError,
    build_plan,
    check_lengths,
)

__all__ = [
    "CHUNK", "CONTACT_LIMITS", "CONTACT_TARGETS", "ImportService", "RowError", "UploadedFile", "check_lengths",
    "crm", "data_error_reason", "job_dict", "limits_from_settings", "parse_frame", "run_background",
]

logger = logging.getLogger(__name__)

CHUNK = 500
DEADLOCK_RETRIES = 5
MAX_STORED_ERRORS = 10_000
STALE_AFTER_SECONDS = 120          # a RUNNING background job without a heartbeat this long is resumed
SYNC_INTERRUPTED_AFTER = timedelta(hours=1)
WORKER_BUDGET_SECONDS = 45.0       # one worker iteration processes chunks for at most this long
DRY_RUN_SAMPLE_ERRORS = 20
_ALLOWED_EXTENSIONS = (".csv", ".txt", ".tsv", ".xlsx", ".xlsm", ".xls")


def limits_from_settings() -> ImportLimits:
    """File limits (configurable: IMPORT_MAX_ROWS / IMPORT_MAX_COLUMNS / IMPORT_MAX_FILE_MB /
    IMPORT_SYNC_MAX_ROWS)."""
    return ImportLimits(
        max_rows=int(getattr(settings, "IMPORT_MAX_ROWS", 50_000) or 50_000),
        max_columns=int(getattr(settings, "IMPORT_MAX_COLUMNS", 200) or 200),
        max_bytes=int(getattr(settings, "IMPORT_MAX_FILE_MB", 15) or 15) * 1024 * 1024,
        sync_max_rows=int(getattr(settings, "IMPORT_SYNC_MAX_ROWS", 1_000) or 1_000),
    )


def import_store() -> FileStore:
    """Where uploaded import files are kept: IMPORT_STORAGE_DIR, else uploads/imports next to the
    template attachments. Keys are flat and tenant-scoped (``<tenant>_<import>.<ext>``)."""
    root = getattr(settings, "IMPORT_STORAGE_DIR", None) or str(Path(settings.ATTACHMENTS_DIR).parent / "imports")
    return LocalFileStore(root)


@dataclass(frozen=True)
class UploadedFile:
    name: str
    data: bytes


def _extension(name: str) -> str:
    lower = (name or "").lower()
    return next((ext for ext in _ALLOWED_EXTENSIONS if lower.endswith(ext)), ".csv")


def _file_key(job: ImportJob) -> str:
    return f"{job.tenant_id}_{job.import_id}{_extension(job.file_name)}"


def parse_frame(raw: bytes, name: str) -> Any:
    """The file as a pandas DataFrame of text cells (kept for callers of the first version)."""
    return table_frame(read_table(raw, name, limits_from_settings()))


# ── Job views ──────────────────────────────────────────────────────

def job_dict(job: ImportJob, names: Optional[Dict[str, str]] = None) -> dict:
    return {
        "import_id": job.import_id, "file_name": job.file_name, "status": job.status,
        "created_by": job.created_by, "created_by_name": (names or {}).get(job.created_by),
        "total_rows": job.total_rows, "contacts_created": job.contacts_created,
        "contacts_updated": job.contacts_updated, "companies_created": job.companies_created,
        "companies_updated": job.companies_updated, "error_count": job.error_count,
        "message": job.message, "options": job.options, "mapping": job.mapping,
        "started_at": job.started_at, "finished_at": job.finished_at,
        # background imports (C-03)
        "mode": job.mode, "skipped": job.skipped_count or 0, "processed_rows": job.processed_rows or 0,
        "progress": 100 if job.status == JobStatus.COMPLETED.value else progress_percent(job.processed_rows, job.total_rows),
        "background": job.file_path is not None, "cancel_requested": bool(job.cancel_requested),
        "queued_at": job.queued_at, "heartbeat_at": job.heartbeat_at, "sheet": job.sheet_name,
    }


def mapping_dict(m: ImportMapping) -> dict:
    return {"mapping_id": m.mapping_id, "name": m.name, "mapping": m.mapping or {}, "options": m.options or {},
            "created_by": m.created_by, "created_at": m.created_at, "updated_at": m.updated_at}


def _targets(custom: Dict[str, str]) -> dict:
    return {
        "contact": [{"value": f"contact.{k}", "label": v} for k, v in CONTACT_TARGETS.items()],
        "company": [{"value": f"company.{k}", "label": v} for k, v in COMPANY_TARGETS.items()],
        "custom": [{"value": f"custom.{k}", "label": v} for k, v in custom.items()],
        "new_types": list(FIELD_TYPES),
    }


def errors_csv(job: ImportJob) -> Tuple[str, bytes]:
    """The rejected rows with the reason, ready to fix and re-import (BR-CM-29); formula-guarded."""
    errors = job.errors or []
    columns = list(dict.fromkeys(k for e in errors for k in (e.get("values") or {})))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(crm.csv_safe_row(["Row", "Error"] + columns))
    for e in errors:
        writer.writerow(crm.csv_safe_row([e["row"], e["reason"]] + [(e.get("values") or {}).get(c) for c in columns]))
    name = re.sub(r"[^\w.-]+", "_", job.file_name.rsplit(".", 1)[0])
    return f"{name}-errors.csv", out.getvalue().encode("utf-8-sig")


# ── Chunk runner (sync and background) ─────────────────────────────

class _Cancelled(Exception):
    pass


def _store_progress(job: ImportJob, state: EngineState, processed: int) -> None:
    for k, v in state.tally.as_dict().items():
        if k == "skipped":
            job.skipped_count = v
        else:
            setattr(job, k, v)
    errors = sorted(state.errors, key=lambda e: e["row"])
    job.error_count = len(errors)
    job.errors = errors[:MAX_STORED_ERRORS]
    job.processed_rows = processed
    job.heartbeat_at = utcnow()


def _run_chunks(uow: ImportsUnitOfWork, job_id: str, engine: RowEngine, rows: List[Dict[str, Any]], start: int,
                chunk_size: int, list_id: Optional[str], deadline: Optional[float] = None) -> bool:
    """Process rows[start:] chunk by chunk, committing each with the job's checkpoint. Returns True when
    every row is done, False when the deadline stopped it early. Raises _Cancelled on a cancel request."""
    for begin in range(start, len(rows), chunk_size):
        if deadline is not None and time.monotonic() > deadline:
            return False
        chunk = list(enumerate(rows[begin:begin + chunk_size], start=begin + 2))  # +2: header row, 1-based
        _run_one_chunk(uow, job_id, engine, chunk, list_id, begin + len(chunk))
        job = uow.imports.job_by_id(job_id)
        if job is not None and job.cancel_requested:
            raise _Cancelled()
    return True


def _run_one_chunk(uow: ImportsUnitOfWork, job_id: str, engine: RowEngine, chunk: list, list_id: Optional[str],
                   processed: int) -> None:
    for attempt in range(DEADLOCK_RETRIES + 1):
        saved = engine.state.mark()
        try:
            engine.process_chunk(chunk)
            if list_id:  # list membership commits with its chunk, not at the very end
                engine.add_list_members(list_id, saved[3])
            job = uow.imports.job_by_id(job_id)
            if job is not None:
                _store_progress(job, engine.state, processed)
            uow.commit()
            return
        except Exception as exc:
            # Simultaneous imports can deadlock on the unique keys; MySQL rolls one back.
            # Undo this chunk's bookkeeping and run it again (BR-CM-28).
            if not uow.is_retryable(exc) or attempt == DEADLOCK_RETRIES:
                raise
            uow.rollback()
            engine.state.restore(saved)
            time.sleep(random.uniform(0.1, 0.5) * (attempt + 1))


def _finish(uow: ImportsUnitOfWork, job_id: str, status: JobStatus, message: Optional[str] = None,
            state: Optional[EngineState] = None) -> Optional[ImportJob]:
    job = uow.imports.job_by_id(job_id)
    if job is None:
        return None
    if state is not None:
        _store_progress(job, state, job.processed_rows or 0)
    job.status = status.value
    if message is not None:
        job.message = message[:2000]
    job.finished_at = utcnow()
    uow.commit()
    return job


def _fail(uow: ImportsUnitOfWork, job_id: str, exc: BaseException, state: Optional[EngineState]) -> None:
    """Keep the history row truthful: what was committed stays, the job says it stopped."""
    uow.rollback()
    _finish(uow, job_id, JobStatus.FAILED, f"Import stopped: {exc}", None)
    if state is not None:
        logger.warning("Import %s stopped after %s error rows: %s", job_id, len(state.errors), exc)


# ── Service ────────────────────────────────────────────────────────

class ImportService:
    """The import routes; each method is one unit of work (it commits)."""

    def __init__(self, uow: ImportsUnitOfWork, store: Optional[FileStore] = None,
                 limits: Optional[ImportLimits] = None) -> None:
        self.uow = uow
        self._store = store
        self.limits = limits or limits_from_settings()

    @classmethod
    def for_session(cls, db: Session) -> "ImportService":
        return cls(imports_uow(db))

    @property
    def store(self) -> FileStore:
        if self._store is None:
            self._store = import_store()
        return self._store

    # guards -------------------------------------------------------------
    @staticmethod
    def require_manager(user: User) -> None:
        if not can_manage_contacts(user):
            raise PermissionDenied(NO_MANAGE_PERMISSION)

    def _read(self, upload: UploadedFile, sheet: Optional[str] = None) -> Table:
        try:
            return read_table(upload.data, upload.name, self.limits, sheet)
        except FileRejected as exc:
            raise BadRequestError(str(exc)) from exc
        except Exception as exc:
            raise BadRequestError(f"Could not read the file: {exc}") from exc

    def _job(self, user: User, import_id: str) -> ImportJob:
        job = self.uow.imports.job(user.tenant_id, import_id)
        if not job or (not can_manage_contacts(user) and job.created_by != user.user_id):
            raise NotFoundError("Import not found")
        return job

    def _custom_labels(self, tenant_id: str) -> Dict[str, str]:
        return {d.field_key: d.label for d in self.uow.imports.field_definitions(tenant_id)}

    # preview / upload ----------------------------------------------------
    def _preview_body(self, user: User, name: str, table: Table) -> dict:
        custom = self._custom_labels(user.tenant_id)
        return {
            "file_name": name, "row_count": len(table.rows), "headers": table.headers,
            "sample": table.rows[:5], "suggested_mapping": suggest_mapping(table.headers, custom),
            "targets": _targets(custom), "sheet_names": table.sheet_names, "sheet": table.sheet,
        }

    def preview(self, user: User, upload: UploadedFile, sheet: Optional[str] = None) -> dict:
        self.require_manager(user)
        table = self._read(upload, sheet)
        with self.uow:
            return self._preview_body(user, upload.name, table)

    def upload(self, user: User, upload: UploadedFile, sheet: Optional[str] = None) -> dict:
        """Store the file for a background import (C-03); the job waits for its mapping (UPLOADED)."""
        self.require_manager(user)
        table = self._read(upload, sheet)
        with self.uow as uow:
            job = ImportJob(tenant_id=user.tenant_id, created_by=user.user_id, file_name=(upload.name or "import")[:255],
                            status=JobStatus.UPLOADED.value, total_rows=len(table.rows), file_size=len(upload.data),
                            sheet_name=table.sheet, processed_rows=0)
            uow.imports.add(job)
            uow.imports.flush()
            job.file_path = _file_key(job)
            self.store.put(job.file_path, upload.data)
            uow.commit()
            return {**self._preview_body(user, job.file_name, table), "import": job_dict(job)}

    # mapping + options ---------------------------------------------------
    def _options(self, user: User, raw: Any) -> ImportOptions:
        try:
            options = ImportOptions.from_raw(raw)
        except ImportInputError as exc:
            raise BadRequestError(str(exc)) from exc
        if options.owner_id and not self.uow.imports.user(user.tenant_id, options.owner_id):
            raise BadRequestError("Owner must be a user in your workspace")
        if options.owner_id and not can_see_owner(self.uow.session, user, options.owner_id):
            raise PermissionDenied("You can only assign records to yourself or your team")
        return options

    @staticmethod
    def _mapping(raw: Any) -> Dict[str, str]:
        try:
            return parse_mapping(raw)
        except ImportInputError as exc:
            raise BadRequestError(str(exc)) from exc

    def _plan(self, user: User, mapping: Dict[str, str], table: Table, create: bool) -> ImportPlan:
        try:
            return build_plan(self.uow, user.tenant_id, mapping, table.headers, table.rows, create)
        except MappingError as exc:
            raise BadRequestError(str(exc)) from exc

    def _target_list(self, user: User, plan: ImportPlan, options: ImportOptions) -> Optional[ProspectList]:
        if not plan.has_contacts or not (options.list_id or options.new_list_name):
            return None
        if options.new_list_name:
            target = ProspectList(tenant_id=user.tenant_id, source_type="IMPORT",
                                  list_name=self.uow.imports.free_list_name(user.tenant_id, options.new_list_name),
                                  list_type="STATIC", uploaded_by=user.user_id)
            self.uow.imports.add(target)
            self.uow.imports.flush()
            return target
        found = self.uow.imports.static_list(user.tenant_id, options.list_id or "")
        if not found:
            raise BadRequestError("Static list not found")
        return found

    def _stored_table(self, job: ImportJob) -> Table:
        try:
            raw = self.store.get(job.file_path or "")
        except OSError as exc:
            raise ConflictError("The uploaded file is no longer available; upload it again") from exc
        return self._read(UploadedFile(job.file_name, raw), job.sheet_name)

    # dry run -------------------------------------------------------------
    def dry_run(self, user: User, import_id: str, mapping_raw: Any, options_raw: Any) -> dict:
        """What the import would do: create / update / skip / fail counts and sample errors. Writes nothing."""
        self.require_manager(user)
        with self.uow as uow:
            job = self._job(user, import_id)
            if job.file_path is None:
                raise ConflictError("This import has no stored file")
            table = self._stored_table(job)
            options = self._options(user, options_raw)
            plan = self._plan(user, self._mapping(mapping_raw), table, create=False)
            engine = RowEngine(uow, user, plan, options, job.file_name)
            rows = table.rows
            for begin in range(0, len(rows), CHUNK):
                engine.evaluate_chunk(list(enumerate(rows[begin:begin + CHUNK], start=begin + 2)))
            uow.rollback()  # nothing is written; drop anything a legacy helper may have staged
            return _dry_run_body(len(rows), engine.state)

    # start / cancel / resume ---------------------------------------------
    def start(self, user: User, import_id: str, mapping_raw: Any, options_raw: Any) -> dict:
        """Queue the stored file for the worker with its mapping and options (new properties and a new
        list are created now, so the job runs with a fixed plan)."""
        self.require_manager(user)
        with self.uow as uow:
            job = self._job(user, import_id)
            if job.status != JobStatus.UPLOADED.value:
                raise ConflictError(f"This import is {job.status.lower()}; it can only be started once")
            table = self._stored_table(job)
            options = self._options(user, options_raw)
            plan = self._plan(user, self._mapping(mapping_raw), table, create=True)
            target = self._target_list(user, plan, options)
            if target is not None:
                options = options.with_list(target.list_id)
            opts = options.as_dict()
            if target is not None:
                opts["list_name"] = target.list_name
            job.mapping, job.options, job.mode = plan.resolved, opts, options.mode.value
            job.status, job.queued_at, job.total_rows = JobStatus.QUEUED.value, utcnow(), len(table.rows)
            uow.commit()
            return job_dict(job)

    def cancel(self, user: User, import_id: str) -> dict:
        self.require_manager(user)
        with self.uow as uow:
            job = self._job(user, import_id)
            if job.status in (JobStatus.UPLOADED.value, JobStatus.QUEUED.value):
                job.status, job.finished_at, job.message = JobStatus.CANCELLED.value, utcnow(), "Cancelled before it ran"
                self._drop_file(job)
            elif job.status == JobStatus.RUNNING.value:
                job.cancel_requested = True  # the worker stops after the chunk it is on
            else:
                raise ConflictError(f"This import is already {job.status.lower()}")
            uow.commit()
            return job_dict(job)

    def resume(self, user: User, import_id: str) -> dict:
        """Queue a FAILED background import again; it continues at its checkpoint."""
        self.require_manager(user)
        with self.uow as uow:
            job = self._job(user, import_id)
            if job.status != JobStatus.FAILED.value or job.file_path is None or not job.mapping:
                raise ConflictError("Only a failed background import can be resumed")
            if not self.store.exists(job.file_path):
                raise ConflictError("The uploaded file is no longer available; upload it again")
            job.status, job.queued_at, job.finished_at, job.message = JobStatus.QUEUED.value, utcnow(), None, None
            job.cancel_requested = False
            uow.commit()
            return job_dict(job)

    def _drop_file(self, job: ImportJob) -> None:
        if job.file_path:
            self.store.delete(job.file_path)

    # synchronous import --------------------------------------------------
    def run_sync(self, user: User, upload: UploadedFile, mapping_raw: str, options_raw: str,
                 chunk_size: int = CHUNK) -> Tuple[int, dict]:
        """POST /imports: small files now (201), larger ones as a queued background job (202)."""
        self.require_manager(user)
        try:
            mapping_in = json.loads(mapping_raw)
            opts_in = json.loads(options_raw or "{}")
        except json.JSONDecodeError:
            raise BadRequestError("mapping and options must be JSON") from None
        table = self._read(upload)
        if len(table.rows) > self.limits.sync_max_rows:
            return 202, self._queue_large(user, upload, table, mapping_in, opts_in)
        with self.uow as uow:
            options = self._options(user, opts_in)
            plan = self._plan(user, self._mapping(mapping_in), table, create=True)
            job = ImportJob(tenant_id=user.tenant_id, created_by=user.user_id, file_name=upload.name or "import",
                            mapping=plan.resolved, options=opts_in, total_rows=len(table.rows),
                            status=JobStatus.RUNNING.value, mode=options.mode.value, processed_rows=0)
            uow.imports.add(job)
            target = self._target_list(user, plan, options)
            if target is not None:
                job.options = {**(job.options or {}), "list_id": target.list_id, "list_name": target.list_name}
            uow.commit()
            job_id = job.import_id
            engine = RowEngine(uow, user, plan, options, job.file_name)
            try:
                _run_chunks(uow, job_id, engine, table.rows, 0, chunk_size, target.list_id if target else None)
            except Exception as exc:
                _fail(uow, job_id, exc, engine.state)
                raise
            done = _finish(uow, job_id, JobStatus.COMPLETED, state=engine.state)
            if done is None:
                raise NotFoundError("Import not found")
            return 201, {**job_dict(done), "errors": (done.errors or [])[:50]}

    def _queue_large(self, user: User, upload: UploadedFile, table: Table, mapping_in: Any, opts_in: Any) -> dict:
        uploaded = self.upload(user, upload, opts_in.get("sheet") if isinstance(opts_in, dict) else None)
        job = self.start(user, uploaded["import"]["import_id"], mapping_in, opts_in)
        return {**job, "errors": []}

    # history / detail ----------------------------------------------------
    def history(self, user: User, page: int, page_size: int) -> dict:
        self.require_manager(user)
        with self.uow as uow:
            rows, total = uow.imports.history(user.tenant_id, (page - 1) * page_size, page_size)
            names = uow.imports.user_names({r.created_by for r in rows})
            return {"items": [job_dict(r, names) for r in rows], "total": total, "page": page, "page_size": page_size}

    def detail(self, user: User, import_id: str) -> dict:
        with self.uow:
            job = self._job(user, import_id)
            return {**job_dict(job), "errors": (job.errors or [])[:200]}

    def error_report(self, user: User, import_id: str) -> Tuple[str, bytes]:
        with self.uow:
            return errors_csv(self._job(user, import_id))

    # saved mappings (C-04) -------------------------------------------------
    def mappings(self, user: User) -> List[dict]:
        self.require_manager(user)
        with self.uow as uow:
            return [mapping_dict(m) for m in uow.imports.mappings(user.tenant_id)]

    def save_mapping(self, user: User, name: Any, mapping: Any, options: Any) -> dict:
        """Create a named mapping, or replace the one with the same name."""
        self.require_manager(user)
        try:
            data = SavedMappingInput.from_raw(name, mapping, options)
        except ImportInputError as exc:
            raise BadRequestError(str(exc)) from exc
        with self.uow as uow:
            row = uow.imports.mapping_named(user.tenant_id, data.name)
            if row is None:
                row = uow.imports.add(ImportMapping(tenant_id=user.tenant_id, name=data.name, mapping=data.mapping,
                                                    options=data.options, created_by=user.user_id))
            else:
                row.mapping, row.options = data.mapping, data.options
            uow.imports.flush()
            uow.commit()
            return mapping_dict(row)

    def delete_mapping(self, user: User, mapping_id: str) -> dict:
        self.require_manager(user)
        with self.uow as uow:
            row = uow.imports.mapping(user.tenant_id, mapping_id)
            if row is None:
                raise NotFoundError("Saved mapping not found")
            uow.imports.delete(row)
            uow.commit()
            return {"status": "deleted", "mapping_id": mapping_id}


def _dry_run_body(total: int, state: EngineState) -> dict:
    tally = state.tally
    errors = sorted(state.errors, key=lambda e: e["row"])
    return {
        "total_rows": total, "create": tally.contacts_created, "update": tally.contacts_updated,
        "skip": tally.skipped, "fail": len(errors),
        "companies_create": tally.companies_created, "companies_update": tally.companies_updated,
        "errors": errors[:DRY_RUN_SAMPLE_ERRORS],
    }


# ── Worker ─────────────────────────────────────────────────────────

def run_background(session_factory: Callable[[], Session], worker: str = "worker",
                   budget_seconds: float = WORKER_BUDGET_SECONDS, store: Optional[FileStore] = None,
                   chunk_size: int = CHUNK) -> Dict[str, int]:
    """One pass of the "imports" loop (runs under the job's named lock): claim queued or stale jobs and
    process them until the time budget is used. Returns counts for the log."""
    deadline = time.monotonic() + budget_seconds
    stats = {"claimed": 0, "completed": 0, "failed": 0, "cancelled": 0, "pending": 0}
    with SqlAlchemyImportsUnitOfWork(session_factory) as uow:
        uow.imports.fail_interrupted(utcnow() - SYNC_INTERRUPTED_AFTER, utcnow(),
                                     "Import was interrupted (the server restarted); run it again")
        uow.commit()
    while time.monotonic() < deadline:
        with SqlAlchemyImportsUnitOfWork(session_factory) as uow:
            job_id = uow.imports.claim_next(utcnow() - timedelta(seconds=STALE_AFTER_SECONDS), utcnow(), worker)
            uow.commit()
            if job_id is None:
                break
            stats["claimed"] += 1
            outcome = _run_job(uow, job_id, store or import_store(), deadline, chunk_size)
            stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def _engine_for(uow: ImportsUnitOfWork, job: ImportJob, store: FileStore) -> Tuple[RowEngine, Table, Optional[str]]:
    user = uow.imports.user(job.tenant_id, job.created_by)
    if user is None:
        raise RowError("The user who started the import no longer exists")
    raw = store.get(job.file_path or "")
    table = read_table(raw, job.file_name, limits_from_settings(), job.sheet_name)
    options = ImportOptions.from_raw(job.options)
    plan = build_plan(uow, job.tenant_id, dict(job.mapping or {}), table.headers, table.rows, create_properties=False)
    state = _resumed_state(job)
    engine = RowEngine(uow, user, plan, options, job.file_name, state)
    if plan.has_contacts and job.processed_rows:
        engine.remember_emails(list(enumerate(table.rows[:job.processed_rows], start=2)))
    return engine, table, (job.options or {}).get("list_id")


def _resumed_state(job: ImportJob) -> EngineState:
    state = EngineState()
    t = state.tally
    t.contacts_created, t.contacts_updated = job.contacts_created or 0, job.contacts_updated or 0
    t.companies_created, t.companies_updated = job.companies_created or 0, job.companies_updated or 0
    t.skipped = job.skipped_count or 0
    state.errors = list(job.errors or [])
    return state


def _run_job(uow: ImportsUnitOfWork, job_id: str, store: FileStore, deadline: float, chunk_size: int) -> str:
    job = uow.imports.job_by_id(job_id)
    if job is None:
        return "failed"
    if job.cancel_requested:
        _finish(uow, job_id, JobStatus.CANCELLED, "Cancelled")
        store.delete(job.file_path or "")
        return "cancelled"
    engine: Optional[RowEngine] = None
    try:
        engine, table, list_id = _engine_for(uow, job, store)
        done = _run_chunks(uow, job_id, engine, table.rows, job.processed_rows or 0, chunk_size, list_id, deadline)
    except _Cancelled:
        _finish(uow, job_id, JobStatus.CANCELLED, "Cancelled; the rows imported before stay")
        store.delete(job.file_path or "")
        return "cancelled"
    except Exception as exc:  # the job records why; the loop goes on with the next one
        logger.error("Import %s failed: %s", job_id, exc, exc_info=True)
        _fail(uow, job_id, exc, engine.state if engine else None)
        return "failed"
    if not done:
        _requeue(uow, job_id)  # out of time: the next pass continues at the checkpoint
        return "pending"
    _finish(uow, job_id, JobStatus.COMPLETED, state=engine.state)
    store.delete(job.file_path or "")
    return "completed"


def _requeue(uow: ImportsUnitOfWork, job_id: str) -> None:
    job = uow.imports.job_by_id(job_id)
    if job is not None and job.status == JobStatus.RUNNING.value:
        job.status, job.claimed_by = JobStatus.QUEUED.value, None
        uow.commit()
