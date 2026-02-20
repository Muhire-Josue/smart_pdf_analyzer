# Smart PDF Analyzer with Azure Durable Functions

## CST8917 – Serverless Applications  
**Semester:** Winter 2026  
**Project:** Midterm Project – Smart PDF Analyzer  
**Student:** Muhire Rutayisire  
**Instructor:** Ramy Mohamed  

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