from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

import azure.durable_functions as df
import azure.functions as func
from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient
from pypdf import PdfReader

app = func.FunctionApp()

# =============================================================================
# Helpers
# =============================================================================
def _get_storage_conn_str() -> str:
    """
    Uses AzureWebJobsStorage (Azurite locally: UseDevelopmentStorage=true).
    """
    conn = os.getenv("AzureWebJobsStorage")
    if not conn:
        raise RuntimeError("AzureWebJobsStorage is not set in environment/local.settings.json")
    return conn


def _download_pdf_bytes(container: str, blob_name: str) -> bytes:
    """
    Download PDF bytes from Blob Storage (works with Azurite and Azure).
    """
    service = BlobServiceClient.from_connection_string(_get_storage_conn_str())
    blob = service.get_blob_client(container=container, blob=blob_name)
    return blob.download_blob().readall()


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


class _BytesIO:
    def __init__(self, b: bytes):
        import io

        self._bio = io.BytesIO(b)

    def __getattr__(self, item):
        return getattr(self._bio, item)


def _get_table_client():
    """
    Azurite-friendly Table client.
    """
    conn_str = os.environ.get("TableStorageConnection") or os.environ["AzureWebJobsStorage"]
    table_name = os.environ.get("ReportTableName", "PdfReports")

    service = TableServiceClient.from_connection_string(conn_str)
    table = service.get_table_client(table_name)

    try:
        table.create_table()
    except Exception:
        pass

    return table


def _query_entities_compat(table, filter_str: str):
    """
    azure-data-tables has had slightly different signatures across versions.
    Support both:
      - query_entities(query_filter="...")
      - query_entities("...")
    """
    try:
        return table.query_entities(query_filter=filter_str)
    except TypeError:
        return table.query_entities(filter_str)


# =============================================================================
# 1) Client Function: Blob trigger (REQUIRED by midterm)
#    When a PDF is uploaded -> start the orchestrator automatically
# =============================================================================
@app.blob_trigger(arg_name="myblob", path="pdfs/{name}", connection="AzureWebJobsStorage")
@app.durable_client_input(client_name="client")
async def PdfBlobTrigger(myblob: func.InputStream, client: df.DurableOrchestrationClient):
    # myblob.name is like "pdfs/test.pdf" in Azurite
    blob_path = myblob.name or ""
    blob_name = blob_path.split("/", 1)[1] if "/" in blob_path else blob_path
    container = "pdfs"  # path watcher is pdfs/{name}

    logging.info(f"New PDF detected: {myblob.name} ({myblob.length} bytes)")

    instance_id = await client.start_new(
        "PdfOrchestrator",
        None,
        {"container": container, "blob_name": blob_name},
    )
    logging.info(f"Started orchestration {instance_id} for {container}/{blob_name}")


# =============================================================================
# 2) Orchestrator: Fan-Out/Fan-In (4 in parallel) + Chaining (report -> store)
# =============================================================================
@app.orchestration_trigger(context_name="context")
def PdfOrchestrator(context: df.DurableOrchestrationContext):
    payload = context.get_input() or {}
    container = payload.get("container") or "pdfs"
    blob_name = payload.get("blob_name")
    if not blob_name:
        return {"error": "Missing blob_name in orchestrator input."}

    base = {"container": container, "blob_name": blob_name}

    # Fan-out (4 independent analyses IN PARALLEL)
    tasks = [
        context.call_activity("extract_text", base),
        context.call_activity("extract_metadata", base),
        context.call_activity("analyze_statistics", base),
        context.call_activity("detect_sensitive_data", base),
    ]

    text_result, metadata_result, stats_result, sensitive_result = yield context.task_all(tasks)

    # Chaining: generate report then store it
    report = yield context.call_activity(
        "generate_report",
        {
            "container": container,
            "blob_name": blob_name,
            "extract_text": text_result,
            "extract_metadata": metadata_result,
            "analyze_statistics": stats_result,
            "detect_sensitive_data": sensitive_result,
        },
    )

    yield context.call_activity("store_report", report)
    return report


# =============================================================================
# 3) Activity: extract_text
# =============================================================================
@app.activity_trigger(input_name="payload")
def extract_text(payload: Dict[str, Any]):
    container = payload.get("container") or "pdfs"
    blob_name = payload.get("blob_name")
    if not blob_name:
        return {"pages": [], "full_text": ""}

    pdf_bytes = _download_pdf_bytes(container, blob_name)
    reader = PdfReader(_BytesIO(pdf_bytes))

    pages: List[Dict[str, Any]] = []
    full_text_parts: List[str] = []

    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append({"page": i, "text": text})
        if text:
            full_text_parts.append(text)

    return {"pages": pages, "full_text": "\n".join(full_text_parts)}


# =============================================================================
# 4) Activity: extract_metadata
# =============================================================================
@app.activity_trigger(input_name="payload")
def extract_metadata(payload: Dict[str, Any]):
    container = payload.get("container") or "pdfs"
    blob_name = payload.get("blob_name")
    if not blob_name:
        return {}

    pdf_bytes = _download_pdf_bytes(container, blob_name)
    reader = PdfReader(_BytesIO(pdf_bytes))

    md = reader.metadata or {}
    return {
        "title": _safe_str(md.get("/Title")),
        "author": _safe_str(md.get("/Author")),
        "subject": _safe_str(md.get("/Subject")),
        "creator": _safe_str(md.get("/Creator")),
        "producer": _safe_str(md.get("/Producer")),
        "creation_date": _safe_str(md.get("/CreationDate")),
        "mod_date": _safe_str(md.get("/ModDate")),
    }


# =============================================================================
# 5) Activity: analyze_statistics  (NOW INDEPENDENT so it can run in parallel)
# =============================================================================
@app.activity_trigger(input_name="payload")
def analyze_statistics(payload: Dict[str, Any]):
    container = payload.get("container") or "pdfs"
    blob_name = payload.get("blob_name")
    if not blob_name:
        return {
            "page_count": 0,
            "word_count": 0,
            "avg_words_per_page": 0.0,
            "estimated_reading_time_minutes": 0.0,
        }

    pdf_bytes = _download_pdf_bytes(container, blob_name)
    reader = PdfReader(_BytesIO(pdf_bytes))

    page_count = len(reader.pages)
    all_text_parts: List[str] = []
    for page in reader.pages:
        all_text_parts.append(page.extract_text() or "")
    full_text = "\n".join(all_text_parts)

    words = re.findall(r"\b\w+\b", full_text)
    word_count = len(words)
    avg_words_per_page = (word_count / page_count) if page_count else 0.0

    wpm = 200
    estimated_minutes = (word_count / wpm) if wpm else 0.0

    return {
        "page_count": page_count,
        "word_count": word_count,
        "avg_words_per_page": avg_words_per_page,
        "estimated_reading_time_minutes": estimated_minutes,
    }


# =============================================================================
# 6) Activity: detect_sensitive_data
# =============================================================================
@app.activity_trigger(input_name="payload")
def detect_sensitive_data(payload: Dict[str, Any]):
    container = payload.get("container") or "pdfs"
    blob_name = payload.get("blob_name")
    if not blob_name:
        return {"emails": [], "phones": [], "urls": [], "dates": []}

    pdf_bytes = _download_pdf_bytes(container, blob_name)
    reader = PdfReader(_BytesIO(pdf_bytes))

    full_text_parts: List[str] = []
    for page in reader.pages:
        full_text_parts.append(page.extract_text() or "")
    text = "\n".join(full_text_parts)

    email_re = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    url_re = re.compile(r"\bhttps?://[^\s)]+|\bwww\.[^\s)]+")
    phone_re = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
    date_re = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")

    return {
        "emails": sorted(set(email_re.findall(text))),
        "phones": sorted(set(phone_re.findall(text))),
        "urls": sorted(set(url_re.findall(text))),
        "dates": sorted(set(date_re.findall(text))),
    }


# =============================================================================
# 7) Activity: generate_report  (matches Lab 2 structure)
# =============================================================================
@app.activity_trigger(input_name="payload")
def generate_report(payload: Dict[str, Any]):
    return {
        "container": payload.get("container", "pdfs"),
        "blob_name": payload.get("blob_name", ""),
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "extract_text": payload.get("extract_text") or {"pages": [], "full_text": ""},
        "extract_metadata": payload.get("extract_metadata") or {},
        "analyze_statistics": payload.get("analyze_statistics") or {},
        "detect_sensitive_data": payload.get("detect_sensitive_data") or {"emails": [], "phones": [], "urls": [], "dates": []},
    }


# =============================================================================
# 8) Activity: store_report (Table Storage)
# =============================================================================
@app.activity_trigger(input_name="payload")
def store_report(payload: Dict[str, Any]):
    container = payload.get("container", "pdfs")
    blob_name = payload.get("blob_name")
    if not blob_name:
        raise ValueError("store_report requires 'blob_name' in payload")

    table = _get_table_client()

    entity = {
        "PartitionKey": container,
        "RowKey": blob_name,
        "generated_at_utc": payload.get("generated_at_utc")
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "report": json.dumps(payload, ensure_ascii=False),
    }

    table.upsert_entity(mode="merge", entity=entity)
    return {"partition_key": container, "row_key": blob_name, "table": table.table_name}


# =============================================================================
# 9) HTTP Function: Get report OR list reports (Lab 2 style endpoint)
#    GET /api/reports/{container}            -> list (optionally ?top=50)
#    GET /api/reports/{container}/{blob_name} -> get one
# =============================================================================
@app.route(route="reports/{container}/{blob_name?}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def reports(req: func.HttpRequest) -> func.HttpResponse:
    container = req.route_params.get("container")
    blob_name = req.route_params.get("blob_name")

    if not container:
        return func.HttpResponse(
            json.dumps({"error": "Missing container in route."}),
            status_code=400,
            mimetype="application/json",
        )

    table = _get_table_client()

    # --- GET ONE ---
    if blob_name:
        try:
            entity = table.get_entity(partition_key=container, row_key=blob_name)
            return func.HttpResponse(entity.get("report", "{}"), status_code=200, mimetype="application/json")
        except Exception as e:
            return func.HttpResponse(
                json.dumps({"error": "Report not found", "details": str(e)}),
                status_code=404,
                mimetype="application/json",
            )

    # --- LIST ---
    try:
        top = int(req.params.get("top", "50"))
    except ValueError:
        top = 50
    top = max(1, min(top, 200))

    filter_str = f"PartitionKey eq '{container}'"

    items: List[Dict[str, Any]] = []
    try:
        entities = _query_entities_compat(table, filter_str)
        for e in entities:
            items.append(
                {
                    "container": e.get("PartitionKey"),
                    "blob_name": e.get("RowKey"),
                    "generated_at_utc": e.get("generated_at_utc"),
                }
            )
            if len(items) >= top:
                break
    except Exception as ex:
        logging.exception("Failed to query reports from Table Storage")
        return func.HttpResponse(
            json.dumps({"error": "Failed to query reports.", "details": str(ex)}),
            status_code=500,
            mimetype="application/json",
        )

    items.sort(key=lambda x: (x.get("generated_at_utc") or ""), reverse=True)
    return func.HttpResponse(json.dumps(items, ensure_ascii=False), status_code=200, mimetype="application/json")