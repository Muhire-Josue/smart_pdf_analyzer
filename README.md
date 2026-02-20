# Smart PDF Analyzer with Azure Durable Functions

## CST8917 – Serverless Applications  
**Semester:** Winter 2026  
**Project:** Midterm Project – Smart PDF Analyzer  
**Group members:**
  - Muhire Rutayisire  
  - Mendoza Jediael
  - Boudouh Ahmed

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

This project was designed as a distributed team project where different components of the system could be implemented independently. The work was logically divided based on system components and responsibilities.

Although the implementation was completed by one member, the responsibilities were structured as follows to reflect proper distributed system design.

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


### Demo

[YouTube Demo](https://youtu.be/JLoHyAkFJ-I)