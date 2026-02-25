# Smart PDF Analyzer with Azure Durable Functions

## CST8917 – Serverless Applications  
**Semester:** Winter 2026  
**Project:** Midterm Project – Smart PDF Analyzer  
**Group members:**
  - Muhire Rutayisire  
  - Mendoza Jediael
  - Boudouh Ahmed

## Demo
### [YouTube Demo](https://www.youtube.com/watch?v=DvWAJQN3mmE)

---

# Overview

This project implements a **serverless PDF analysis system** using **Azure Durable Functions**. The system automatically processes PDF files uploaded to Azure Blob Storage and performs multiple analyses in parallel using the **Fan-Out/Fan-In pattern**.

When a PDF is uploaded to the `pdfs` container, the system:

1. Automatically detects the new file (Blob Trigger)
2. Starts a Durable Function orchestration
3. Runs 4 analysis activities in parallel:
   - Extract text
   - Extract metadata
   - Analyze statistics
   - Detect sensitive data
4. Combines results into a report
5. Stores the report in Azure Table Storage
6. Allows retrieval via HTTP endpoint

This architecture demonstrates scalable, event-driven, and serverless document processing.

# Setup Instructions Local and Azure Portal

**Prerequisites:**
- Completed Week 4 Durable Functions Exercise
- VS Code with Azure Functions Extension installed
- Azure Functions Core Tools installed
- Python 3.11 or 3.12 installed
- Azure Storage Explorer (standalone app) — [Download here](https://azure.microsoft.com/features/storage-explorer/)
- **WSL/Linux only:** `libsecret-1-0` package (required for Azure Storage Explorer and AzCopy uploads — see Part 1 setup below)

---

## Part 1: Create the Project

### Step 1.1: Create a New Functions Project

1. Press `F1` to open the Command Palette
2. Select **Azure Functions: Create New Project...**
3. Choose a new empty folder (e.g., `smart-pdf-analyzer`)

| Prompt                          | Your Selection             |
| ------------------------------- | -------------------------- |
| **Select a language**           | `Python`                   |
| **Select a Python interpreter** | Choose Python 3.12 |
| **Select a template**           | `Skip for now`             |
| **Open project**                | `Open in current window`   |


### Step 1.2: Install Required Packages

Open `requirements.txt` and replace its contents with:

```
azure-functions
azure-functions-durable
azure-storage-blob
pypdf
azure-data-tables
```

| Package                   | Purpose                                           |
| ------------------------- | ------------------------------------------------- |
| `azure-functions`         | Azure Functions SDK            |
| `azure-functions-durable` | Durable Functions extension     |
| `azure-data-tables`       | Azure Table Storage SDK for storing results       |
| `azure-storage-blob`      | Python library for interacting with Azure Blob Storage      |
| `pypdf`                   | Python PDF library capable of splitting, merging, cropping, and transforming the pages of PDF files.|

Install the packages:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

> **Windows:** Use `.venv\Scripts\activate` instead.

### Step 1.3: Configure Local Settings

Open `local.settings.json` and replace its contents with:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
  },
  "Host": {
    "CORS": "*"
  }
}
```

| Setting                  | Purpose                                                |
| ------------------------ | ------------------------------------------------------ |
| `AzureWebJobsStorage`    | Durable Functions state storage     |
| `CORS`                   | Allows cross-origin requests to the function endpoints |

> **Note:** Connection string points to Azurite for local development. When you deploy to Azure, these will point to your real storage account.

---
## Part 2: Write the Code

Create `function_app.py` and paste the code below.

```python
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
            return func.HttpResponse(json.dumps(json.loads(entity.get("report", "{}")), indent=2), status_code=200, mimetype="application/json")
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
    return func.HttpResponse(json.dumps({"count": len(items), "results": items}, indent=2, ensure_ascii=False), status_code=200, mimetype="application/json")

@app.route(route="analyze", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="client")
async def PdfHttpStarter(req: func.HttpRequest, client: df.DurableOrchestrationClient):
    logging.info("HTTP starter triggered")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body. Expected {container, blob_name}."}),
            status_code=400,
            mimetype="application/json",
        )

    container = body.get("container") or "pdfs"
    blob_name = body.get("blob_name") or body.get("name")
    if not blob_name:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'blob_name' in request body."}),
            status_code=400,
            mimetype="application/json",
        )

    instance_id = await client.start_new(
        "PdfOrchestrator",
        None,
        {"container": container, "blob_name": blob_name},
    )

    return client.create_check_status_response(req, instance_id)
```

## Part 3: Run and Test Locally

### Step 3.1: Start Azurite

Durable Functions and Table Storage both require Azurite locally.

1. Press `F1` to open the Command Palette
2. Select **Azurite: Start**
3. Verify the status bar shows Azurite is running

> **CLI alternative:** `azurite --silent --location .azurite --debug .azurite/debug.log`

### Step 3.2: Create the PDFs Container

Before uploading PDFs, you need to create the `pdfs` container in Azurite. The easiest way is using the **Azure Storage Explorer** extension in VS Code or the standalone Azure Storage Explorer app.

1. Open [Azure Storage Explorer](https://azure.microsoft.com/features/storage-explorer/)
2. Connect to **Local & Attached** > **Storage Accounts** > **(Emulator - Default Ports)**
3. Right-click **Blob Containers** > **Create Blob Container**
4. Name it `pdfs`


### Step 3.3: Start the Function App

1. Press `F5` or run `func start` in the terminal

2. Wait for the output to show the available endpoints:

   ```
    Functions:

        PdfHttpStarter: [POST] http://localhost:7071/api/analyze

        reports: [GET] http://localhost:7071/api/reports/{container}/{blob_name?}

        analyze_statistics: activityTrigger

        detect_sensitive_data: activityTrigger

        extract_metadata: activityTrigger

        extract_text: activityTrigger

        generate_report: activityTrigger

        PdfBlobTrigger: blobTrigger

        PdfOrchestrator: orchestrationTrigger

        store_report: activityTrigger
   ```

   Notice: Only `reports` has an HTTP URL. The blob trigger, orchestrator, and activities are all triggered internally.

### Step 3.4: Upload a Test PDF

Upload any PDF to the `pdfs` container to trigger the orchestration.

1. In Azure Storage Explorer, navigate to **Local Emulator** > **Blob Containers** > **pdfs**
2. Click **Upload** > **Upload Files**
3. Select any PDF file from your computer
4. Click **Upload**

### Step 3.5: Retrieve the Results

Use the HTTP endpoint to check the stored results:

**Get all results:**
```
http://localhost:7071/api/reports/pdfs
```

**Expected response:**
```json
{
  "count": 1,
  "results": [
        {
          "container": "pdfs",
          "blob_name": "sample.pdf",
          "generated_at_utc": "2026-02-10T14:30:00.500000",
      }
  ]
}

```

**Get a specific result (copy the ID from above):**
```
http://localhost:7071/api/reports/pdfs/sample.pdf
```

This returns the full analysis including all four analysis sections.

### Step 3.7: Test with Multiple Images

Upload 2-3 different PDF files to the `pdfs` container. Each upload triggers a new orchestration. Then query `/api/reports/pdfs` to see all stored analyses.

---

## Part 4: Deploy to Azure

### Step 4.1: Create Azure Resources

You need a Storage Account (for Blob Storage and Table Storage) and a Function App.

1. Press `F1` > **Azure Functions: Create Function App in Azure...(Advanced)**

| Prompt                                      | Your Action                                           |
| ------------------------------------------- | ----------------------------------------------------- |
| **Select subscription**                     | Choose your Azure subscription                        |
| **Enter a globally unique name**            | Enter a unique name (e.g., `smart-pdf-analyzer`) |
| **Select a runtime stack**                  | `Python 3.12`                                         |
| **Select an OS**                            | `Linux`                                               |
| **Select a resource group**                 | Create new (e.g., `midterm-rg`)               |
| **Select a location**                       | Choose a region near you (e.g., `Canada Central`)     |
| **Select a hosting plan**                   | `Consumption`                                         |
| **Select a storage account**                | Create new (e.g., `stpdfanalyzer`)                  |
| **Select an Application Insights resource** | `Skip for now`                                        |

### Step 4.2: Configure Application Settings

After the Function App is created, make sure the `AzureWebJobsStorage` variable is present:

1. In Azure Portal, navigate to your **Function App**
2. Go to **Settings** > **Environment variables** (or **Configuration** > **Application settings**)
3. Check `AzureWebJobsStorage`


### Step 4.3: Create the Images Container in Azure

1. In Azure Portal, navigate to your **Storage Account**
2. Go to **Data Storage** > **Containers**
3. Click **+ Container**
4. Name it `pdfs` and click **Create**

### Step 4.4: Deploy

1. Press `F1` > **Azure Functions: Deploy to Function App**
2. Select your function app
3. Confirm the deployment

### Step 4.5: Test in Azure

1. Upload an pdf to the `pdfs` container in Azure Portal (Storage Account > Containers > pdfs > Upload)
2. Wait a few seconds for the orchestration to complete
3. Navigate to `https://yourname-image-analyzer.azurewebsites.net/api/reports/{container}` to see the results

---

# Architecture Diagram

![alt text](architecture-diagram.png)


# How the System Works

## Step 1 – Upload PDF
pdfs

---

## Step 2 – Blob Trigger Activates

The Blob Trigger function automatically detects the new PDF.

Function:

This function starts the Durable Orchestrator.

---

## Step 3 – Durable Orchestrator Starts

The orchestrator function:

This function coordinates the workflow.

It uses the **Fan-Out / Fan-In pattern** to run multiple analysis tasks in parallel.

---

## Step 4 – Parallel Activity Functions (Fan-Out)

Four activity functions run at the same time:

### 1. extract_text

Extracts all text from the PDF.

Output example:

```json
{
  "pages": [...],
  "full_text": "Extracted content"
}
```
### 2. extract_metadata

Extracts metadata such as:
- Title
- Author
- Producer
- Creation date

### 3. analyze_statistics

Analyzes document statistics:
- Page count
- Word count
- Average words per page
- Estimated reading time

### 4. detect_sensitive_data

Detects sensitive information using pattern matching:
- Emails
- Phone numbers
- URLs
- Dates

### Step 5 – Fan-In and Report Generation

The orchestrator waits for all activity functions to finish.

Then it calls:
generate_report

This function combines all analysis results into one report.

### Step 6 – Store Report in Azure Table Storage

The report is saved using:

store_report

Storage details:
- PartitionKey = container name
- RowKey = blob name
- Data stored as JSON

This allows fast retrieval.

### Step 7 – Retrieve Report using HTTP Endpoint

Users can retrieve reports using HTTP.

Get single report
```
GET /api/reports/{container}/{blob_name}
```

Example:
```
GET /api/reports/pdfs/test.pdf
```

List reports
```
GET /api/reports/{container}
```

Example:
```
GET /api/reports/pdfs
```

### Durable Functions Pattern Used

This project uses:

Fan-Out / Fan-In Pattern

Fan-Out:

The orchestrator runs 4 activity functions in parallel.

Fan-In:

The orchestrator waits for all functions to complete and combines results.

Benefits:
- Faster processing
- Parallel execution
- Scalable architecture

### Work Distribution

This project was designed as a distributed team project where different components of the system could be implemented independently. The work was logically divided based on system components and responsibilities.

Although the implementation was completed by one member, the responsibilities were structured as follows to reflect proper distributed system design

### Muhire Rutayisire

Primary responsibilities:
- Designed overall system architecture
- Implemented Azure Durable Functions orchestration workflow
- Implemented Fan-Out/Fan-In orchestration pattern
- Developed Blob Trigger function
- Developed HTTP starter function
- Implemented report generation logic
- Implemented report storage using Azure Table Storage
- Implemented report retrieval HTTP endpoint
- Configured Azure resources:
- Function App
- Storage Account
- Blob Container
- Table Storage
- Deployed application to Azure
- Tested and validated complete workflow
- Prepared documentation and README

### Mendoza Jediael

Assigned responsibilities (design level):
- Analysis logic design
- Text extraction strategy
- Metadata extraction design
- Statistics analysis design

These components were implemented as independent activity functions to support parallel execution.

### Boudouh Ahmed

Assigned responsibilities (design level):
- Sensitive data detection design
- Report structure design
- Data storage structure design
- HTTP retrieval interface design

These components were implemented as modular activity and HTTP functions.
