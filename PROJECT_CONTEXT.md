# RSCP Project Context & Architecture

## Project Overview
**RSCP (Receive, Scan, Check, Process)** is a self-hosted WMS (Warehouse Management System) for tracking inbound IT equipment and shipments. It runs on a local Linux server (Proxmox container) using Python/Flask.

## Tech Stack
- **Backend:** Python 3, Flask, Pandas.
- **Frontend:** HTML, Bootstrap 5 (Dark/Light mode), JavaScript (vanilla).
- **Data:** CSV/JSON flat files (No SQL database).
    - `manifest.csv`: The master list of expected inbound items.
    - `history.csv`: The immutable log of scanned/received items.
    - `package_db.json`: The active state database (status, dates, priority).
- **Hardware:** Designed for Panasonic FZ-N1 Android Barcode Scanners.

## Core Architecture: "The Unified Manifest"
We recently migrated from separate Amazon/eBay processing to a **Unified Manifest** system.
1. **Ingest:** The system accepts `.csv` (Amazon) or `.xlsx` (eBay) exports via the Admin Panel.
2. **Normalization:** It standardizes columns to `TrackingNumber`, `ItemName`, `Date`, and `Quantity`.
3. **Smart Merge:** If multiple rows share a Tracking Number, it concatenates item names (e.g., "GPU + PSU") so the scanner shows all contents for a single box.
4. **Strict Sync:** The upload process uses "Strict Mirroring"—if an item is in the DB but not in the new manifest, it is deleted (pruned).

## Current State: V14.5 (Stable)
**Current Version:** V14.5
**Last Major Action:** Rolled back from V14.6 (PWA) due to UI issues on scanners.

### Key Logic Rules (Do Not Break)
1. **Column Detection:** The ingest logic MUST ignore columns named `ItemID`, `Item Number`, or `Category` when guessing Product Names (to avoid importing eBay Item IDs as names).
2. **Date Safety:** If an upload contains "Order Date" but no "Delivery Date", the date is set to "Pending" to avoid false "Past Due" alarms.
3. **Tracking Numbers:** Must be treated as strings to preserve long tracking numbers (prevent Scientific Notation conversion).
4. **Auto-Trim:** There is a feature to auto-delete history older than 60 days, controlled via the Admin Panel UI.

## Known Issues / Roadmap
- **PWA Status:** We attempted a Progressive Web App (V14.6) implementation to hide the Android navigation bar, but it caused issues. Currently running as a standard web page.
- **Return Mode:** Uses fuzzy search against the local `package_db.json`.
- **Admin Panel:** Features a compact "Auto-Trim" toggle in the table header.

## Instructions for AI Agent
If asked to modify RSCP, prioritize **stability** and **speed**. This tool is used in a high-volume receiving environment. Always ensure `app.py` is generated as a full file (no snippets) to prevent corruption.