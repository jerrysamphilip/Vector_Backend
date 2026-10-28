# app/routers/import_router.py
"""
CRM import (BR-CM-25..30, C-03..C-06): CSV / XLSX of contacts, companies, or both in one file.

1. POST /imports/preview  — headers, sample rows and a suggested column mapping
2. POST /imports          — run it with a mapping: {column: target}
     targets: "contact.<field>", "company.<field>", "custom.<key>",
              "new:<TYPE>:<Label>" (creates a custom property), or "skip".
     Files over 1,000 rows are queued as a background import instead (202 with the job).
3. GET  /imports, /imports/{id}, /imports/{id}/errors.csv — history and rejected rows

Background imports (C-03, F-16):
  POST /imports/uploads                 store the file; returns the preview and the job (UPLOADED)
  POST /imports/{id}/dry-run            {mapping, options}: create / update / skip / fail counts, nothing written
  POST /imports/{id}/start              {mapping, options}: queue it for the worker (QUEUED -> RUNNING)
  POST /imports/{id}/cancel             stop it (between chunks); rows already imported stay
  POST /imports/{id}/resume             queue a failed import again from its checkpoint
  GET  /imports/{id}                    progress: processed_rows, progress (%), status, heartbeat
Saved column mappings (C-04): GET / POST /imports/mappings, DELETE /imports/mappings/{id}.
Options: mode CREATE / UPDATE_ONLY / CREATE_AND_UPDATE, update_existing, owner_id, list_id or
new_list_name, default_source, date_order MDY / DMY, sheet.

Contacts are matched by email (unique per workspace) and companies by domain, then name.
New contacts and companies are inserted under the database's unique keys, so several
users importing at once can't create duplicates: a collision becomes an update.

Layered (ADR 0009): handlers make one call to ImportService (app/services/import_service.py).
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.auth import require_role
from app.core.database import get_db
from app.services import import_service
from app.services.import_service import (  # noqa: F401  (re-exported for callers of the first version)
    CONTACT_LIMITS,
    CONTACT_TARGETS,
    ImportService,
    RowError,
    UploadedFile,
    check_lengths,
    crm,
    data_error_reason,
    parse_frame,
)
from app.utils.http_files import content_disposition

router = APIRouter(prefix="/imports", tags=["Imports"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

CHUNK = import_service.CHUNK  # rows per transaction of a synchronous import (tests lower it)
_UPLOAD_SLACK = 1024


def imports_service(db=Depends(get_db)) -> ImportService:
    return ImportService.for_session(db)


Imports = Depends(imports_service)

# The first version's helpers, kept for existing callers and tests
_parse = parse_frame
_check_lengths = check_lengths


def _data_error_reason(exc: BaseException) -> str:
    return data_error_reason(exc, {**import_service.CONTACT_TARGETS, **import_service.COMPANY_TARGETS})


def _upload(file: UploadFile, service: ImportService) -> UploadedFile:
    """The uploaded bytes; reading stops just past the size limit (the service refuses it)."""
    return UploadedFile(file.filename or "import", file.file.read(service.limits.max_bytes + _UPLOAD_SLACK))


@router.post("/preview", response_model=None)
def preview(file: UploadFile = File(...), sheet: Optional[str] = Form(None, max_length=255),
            service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    service.require_manager(current_user)
    return service.preview(current_user, _upload(file, service), sheet)


@router.post("", status_code=201, response_model=None)
def run_import(
    file: UploadFile = File(...),
    mapping: str = Form(..., description="JSON {column: target}"),
    options: str = Form("{}", description='JSON {"owner_id", "list_id", "new_list_name", "update_existing"}'),
    service: ImportService = Imports,
    current_user: Any = Depends(tenant_user),
) -> Any:
    service.require_manager(current_user)
    status, body = service.run_sync(current_user, _upload(file, service), mapping, options, chunk_size=CHUNK)
    return body if status == 201 else JSONResponse(status_code=status, content=jsonable_encoder(body))


@router.post("/uploads", status_code=201, response_model=None)
def upload(file: UploadFile = File(...), sheet: Optional[str] = Form(None, max_length=255),
           service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    """Store the file for a background import; returns the preview and the job."""
    service.require_manager(current_user)
    return service.upload(current_user, _upload(file, service), sheet)


@router.get("", response_model=None)
def import_history(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.history(current_user, page, page_size)


@router.get("/mappings", response_model=None)
def list_mappings(service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Any:
    return service.mappings(current_user)


@router.post("/mappings", status_code=201, response_model=None)
def save_mapping(payload: Dict[str, Any] = Body(..., description='{"name", "mapping": {column: target}, "options"}'),
                 service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.save_mapping(current_user, payload.get("name"), payload.get("mapping"), payload.get("options"))


@router.delete("/mappings/{mapping_id}", response_model=None)
def delete_mapping(mapping_id: str, service: ImportService = Imports,
                   current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.delete_mapping(current_user, mapping_id)


@router.get("/{import_id}", response_model=None)
def import_detail(import_id: str, service: ImportService = Imports,
                  current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.detail(current_user, import_id)


@router.post("/{import_id}/dry-run", response_model=None)
def dry_run(import_id: str, payload: Dict[str, Any] = Body(..., description='{"mapping", "options"}'),
            service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.dry_run(current_user, import_id, payload.get("mapping"), payload.get("options"))


@router.post("/{import_id}/start", status_code=202, response_model=None)
def start(import_id: str, payload: Dict[str, Any] = Body(..., description='{"mapping", "options"}'),
          service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.start(current_user, import_id, payload.get("mapping"), payload.get("options"))


@router.post("/{import_id}/cancel", response_model=None)
def cancel(import_id: str, service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.cancel(current_user, import_id)


@router.post("/{import_id}/resume", status_code=202, response_model=None)
def resume(import_id: str, service: ImportService = Imports, current_user: Any = Depends(tenant_user)) -> Dict[str, Any]:
    return service.resume(current_user, import_id)


@router.get("/{import_id}/errors.csv", response_model=None)
def import_errors(import_id: str, service: ImportService = Imports,
                  current_user: Any = Depends(tenant_user)) -> StreamingResponse:
    """The rejected rows with the reason, ready to fix and re-import (BR-CM-29)."""
    name, data = service.error_report(current_user, import_id)
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": content_disposition(name)})
