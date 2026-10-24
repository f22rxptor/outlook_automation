# Chemical Manufacturing & Trading ERP Integration & Catalog Database

Enterprise data engineering solution for synchronizing chemical catalog data from an ERP system into a local operational database (SQLite / PostgreSQL ANSI compliant), complete with schema migrations, chemical name normalization, and CAS Registry Number validation.

---

## 1. Architecture Overview

```
                      +-----------------------------+
                      |   ERP Database / CSV Export |
                      +--------------+--------------+
                                     |
                                     v
                        [ sync_erp_catalog.py ]
                     -----------------------------
                     - Lowercase & character strip
                     - Regex CAS pattern match
                     - Official CAS check-digit
                     - Category & schema validation
                                     |
                                     v (Atomic Upsert)
                   +---------------------------------+
                   |           chemicals.db          |
                   |---------------------------------|
                   | - master_chemical_catalog       |
                   | - enquiry_logs                  |
                   | - demand_tracker                |
                   | - rnd_queue                     |
                   +---------------------------------+
```

---

## 2. Database Schema (`schema.sql`)

The database consists of 4 core tables designed with relational integrity, check constraints, and performance indexes:

| Table | Description | Key Fields & Constraints |
| :--- | :--- | :--- |
| `master_chemical_catalog` | Central repository of catalogued chemicals, stock levels, and commercial aliases. | `id`, `chemical_name`, `cas_number` (UNIQUE), `trade_names` (JSON), `current_stock_kg`, `reorder_point_kg`, `category` (`Trading`, `Internal Product`, `Raw Material`), `created_at`, `updated_at`. |
| `enquiry_logs` | Inbound client / trader email inquiries ingested for downstream parsing. | `id`, `email_subject`, `sender_email`, `received_at`, `raw_text`, `extracted_json` (JSON), `processing_status` (`PENDING`, `PROCESSED`, `FAILED`, `FLAGGED_FOR_RND`, `DUPLICATE`). |
| `demand_tracker` | Trailing 7-day demand velocity and alert thresholds for restocking or market actions. | `id`, `cas_number` (FK), `chemical_name`, `enquiry_count_7d`, `threshold_flag` (BOOLEAN), `last_alert_sent_at`, `updated_at`. |
| `rnd_queue` | Inquiries for specialty grades, custom synthesis, or uncatalogued molecules. | `id`, `chemical_name`, `requested_cas`, `requested_grade`, `requested_qty_kg`, `sample_development_status` (`NEW_REQUEST`, `FEASIBILITY_REVIEW`, `LAB_SYNTHESIS`, `QUALITY_CHECK`, `SAMPLE_DISPATCHED`, `REJECTED`). |

---

## 3. Data Transformation & Validation

### Chemical Name Normalization
- Converts all names to lowercase.
- Strips punctuation, parentheses, brackets, and special symbols (e.g. `!`, `@`, `#`, `$`, `%`, `*`, `~`).
- Preserves alphanumeric tokens and single spaces.
- Example: `"Ethyl Acetate (Industrial Grade #99%)"` $\rightarrow$ `"ethyl acetate industrial grade 99"`.

### CAS Registry Number Strict Verification
1. **Format Validation**: Strict regex matching `^\d{2,7}-\d{2}-\d$` (2 to 7 digits, hyphen, 2 digits, hyphen, 1 check digit).
2. **Official CAS Check-Digit Algorithm**:
   For digits $d_n, d_{n-1}, \dots, d_1$ preceding the final check digit $C$:
   $$\text{Checksum} = \left(\sum_{i=1}^{n} i \times d_i \right) \pmod{10}$$
   The record is rejected if $\text{Checksum} \neq C$.

---

## 4. 10 Sample Chemical Records

The built-in mock dataset contains 10 verified industrial chemicals:

| SKU | Raw Chemical Name | Normalized Name | CAS Number | Category | Stock (kg) | Reorder (kg) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `CHEM-001-EA` | Ethyl Acetate (Industrial Grade #99%) | `ethyl acetate industrial grade 99` | `141-78-6` | Trading | 15,400.50 | 5,000.00 |
| `CHEM-002-AC` | Acetone [High Purity 99.5%] | `acetone high purity 99 5` | `67-64-1` | Trading | 8,250.00 | 3,000.00 |
| `CHEM-003-IPA` | Isopropanol / Isopropyl Alcohol* | `isopropanol isopropyl alcohol` | `67-63-0` | Internal Product | 24,100.00 | 8,000.00 |
| `CHEM-004-MEOH`| Methanol (Technical Grade!!) | `methanol technical grade` | `67-56-1` | Raw Material | 31,200.00 | 10,000.00 |
| `CHEM-005-TOL` | Toluene @ Refined Solvent | `toluene refined solvent` | `108-88-3` | Trading | 12,750.75 | 4,000.00 |
| `CHEM-006-SA`  | Sulfuric Acid {98% Commercial} | `sulfuric acid 98 commercial` | `7664-93-9` | Raw Material | 45,000.00 | 15,000.00 |
| `CHEM-007-SH`  | Sodium Hydroxide (Caustic Flakes~) | `sodium hydroxide caustic flakes` | `1310-73-2` | Raw Material | 28,300.00 | 10,000.00 |
| `CHEM-008-THF` | Tetrahydrofuran [Stabilized BHT] | `tetrahydrofuran stabilized bht` | `109-99-9` | Internal Product | 6,400.00 | 2,500.00 |
| `CHEM-009-ACN` | Acetonitrile (HPLC Grade: 99.9%) | `acetonitrile hplc grade 99 9` | `75-05-8` | Trading | 4,200.25 | 1,500.00 |
| `CHEM-010-DCM` | Dichloromethane - Premium Solvent$ | `dichloromethane premium solvent` | `75-09-2` | Trading | 19,800.00 | 6,000.00 |

---

## 5. Email Ingestion & Claude API Parser (`parse_enquiries.py`)

A production AI pipeline that integrates with Microsoft Outlook via Microsoft Graph API and extracts chemical purchase enquiry details using Claude API (Anthropic SDK).

### Core Pipeline Capabilities
1. **Microsoft Graph API Authentication**:
   - Uses `MSAL` (Microsoft Authentication Library) with OAuth 2.0 Client Credentials flow.
   - Queries `/users/{mailbox}/mailFolders/inbox/messages?$filter=isRead eq false&$expand=attachments`.
   - Automatically marks processed emails as read via `PATCH`.
2. **Multi-Format Attachment Parsing**:
   - **PDF Purchase Orders / RFQs**: Extracted page-by-page using `pypdf`.
   - **Excel Workbooks (`.xlsx`, `.xls`)**: Tabular data extracted per sheet into structured markdown/CSV format using `pandas` and `openpyxl`.
   - **CSV files**: Decoded directly.
3. **Claude API Prompting (Structured JSON Output)**:
   - Enforces strict JSON schema via system instructions:
     * `customer_name`: Identified company or individual.
     * `email_date`: ISO 8601 or original date string.
     * `requested_items`: Array of objects containing:
       - `chemical_name`: Canonical lowercased chemical name.
       - `cas_number`: Standard CAS format (`XXXX-XX-X`) or `null`.
       - `trade_name`: Commercial/trade alias mentioned by buyer.
       - `purity_grade`: Extracted grade (e.g., `HPLC`, `Technical 99%`).
       - `quantity_kg`: Normalized quantity strictly converted to kilograms.
       - `target_price`: Quoted or target price with unit.
     * `enquiry_intent`: Classified strictly into `"Firm Order"`, `"Quote Request"`, or `"General Inquiry"`.
4. **Resilience & Fallback Handling**:
   - Gracefully resolves ambiguous trade names (e.g., *Solvent EA*, *Caustic Soda*, *Oil of Vitriol*, *Wood Spirit*).
   - Inquiries for custom synthesis, experimental molecules, or uncatalogued chemicals lacking CAS numbers are assigned `cas_number: null` and flagged in the database with status `'FLAGGED_FOR_RND'`.
   - When external credentials are not set, an intelligent heuristic fallback parser executes automatically, allowing testing without cloud credentials.

### Environment Configuration (`.env` or OS Environment)
```env
# Microsoft Graph API
AZURE_TENANT_ID=your-tenant-id-guid
AZURE_CLIENT_ID=your-client-id-guid
AZURE_CLIENT_SECRET=your-azure-app-client-secret
OUTLOOK_MAILBOX=enquiries@company.com

# Anthropic Claude API
ANTHROPIC_API_KEY=sk-ant-api03-...
ANTHROPIC_MODEL=claude-3-5-sonnet-latest
```

---

## 6. Demand Processing & R&D Routing Engine (`process_demands.py`)

A business logic module that cross-references extracted chemical purchase inquiries against the `master_chemical_catalog` to drive sales, procurement, and R&D actions.

### 1. Catalog Matching Engine Hierarchy
For every item extracted from customer RFQs and emails, the engine applies multi-tier resolution:
1. **CAS Registry Number Exact Match** (Primary): Validates against standard CAS format.
2. **Canonical Chemical Name Exact Match**: Normalized lowercase matching.
3. **Trade Name Exact Match**: Cross-references aliases and commercial brand names in the catalog's JSON array.
4. **Fuzzy String Matching** (Secondary): Uses Gestalt pattern matching (`difflib.SequenceMatcher`) across canonical and trade names with configurable similarity threshold (default $\ge 0.85$).

### 2. Existing Product Logic (Procurement & Sales Trigger)
- **Velocity Tracker**: Automatically increments `enquiry_count_7d` for the chemical in `demand_tracker`.
- **High Demand Alert Threshold**:
  - When `enquiry_count_7d >= 10` within a rolling 7-day period:
    * Updates `threshold_flag = 1`.
    * Dispatches an automated High Demand Alert to Procurement & Sales teams via Slack webhook / email / logs:
      `"High Demand Alert: [Chemical Name] hit 10+ enquiries this week. Prepare stock/supplier quotes."`
    * Records `last_alert_sent_at` timestamp.

### 3. New/Untraded Compound Logic (R&D Trigger)
- When no match exists in `master_chemical_catalog`:
  * Flags molecule as `NEW_UNTRADED_COMPOUND`.
  * Inserts the record into `rnd_queue`.
  * **Sample Scale Routing**: If requested quantity is small ($< 50\text{ kg}$ / sample scale):
    - Sets `sample_development_status = 'LAB_SYNTHESIS'` (*Immediate Lab Sample Synthesis* priority).
    - If $\ge 50\text{ kg}$, sets status to `'NEW_REQUEST'` (*Standard Feasibility Review*).
  * Dispatches an R&D ticket alert to the R&D chemistry team:
    `"New Market Compound Detected: [Chemical Name / CAS]. Customer requested [Qty] kg."`

## 7. Executive Streamlit Dashboard (`app_md_dashboard.py`)

A real-time executive decision dashboard designed for the Managing Director of Chemical Manufacturing & Trading operations.

### Key Executive Capabilities
1. **Top KPI Metric Cards**:
   - **Total Inbound Enquiries**: 7-Day active volume vs 30-Day trailing baseline.
   - **High-Demand Products**: Real-time counter of catalog items crossing the $\ge 10$ inquiries threshold.
   - **New Untraded R&D Items**: Active count of commercial inquiries for custom synthesis compounds not currently in the catalog.
   - **Unmet Demand Risk**: Catalog products experiencing customer inquiries while inventory stock is below safety reorder points.

2. **Executive Analytics Visualizations**:
   - **Demand Velocity Line Chart**: Multi-series volume trend over time for top requested chemicals.
   - **Stock Readiness vs Inbound Demand**: Scatter matrix comparing stock levels against 7-day inquiry velocity, sized by safety reorder point and colored by trade category (*Trading*, *Internal Product*, *Raw Material*).
   - **Inventory vs Demand Heatmap**: Interactive heatmap matrix contrasting current stock, safety reorder limits, and estimated 7-day demand pressure.

3. **Actionable Commercial & R&D Controls**:
   - **Approve R&D Batch**: Directly authorizes lab synthesis production for new untraded market compounds with real-time database state persistence.
   - **Mark Dispatched**: Tracks sample dispatch workflows to prospective clients.
   - **Trade Category Filtering**: Instant multi-select filtering across *Trading*, *Internal Product*, and *Raw Material*.
   - **Master Catalog Drill-Down**: Expandable data grid with live stock, reorder levels, and velocity metrics.

---

## 8. Usage & CLI Commands

### 1. Ingest Embedded Mock ERP Data into Catalog
```bash
python sync_erp_catalog.py
```

### 2. Export Mock ERP Data to CSV
```bash
python sync_erp_catalog.py --export-sample-csv sample_erp_export.csv
```

### 3. Sync from an External CSV / ERP Export
```bash
python sync_erp_catalog.py --csv sample_erp_export.csv
```

### 4. Run Outlook Email & Claude Parser Pipeline (Live or Mock Mode)
```bash
# Run with mock RFQ & PO emails (including PDF & Excel attachments):
python parse_enquiries.py --mock

# Run live against configured Outlook inbox (enquiries@company.com):
python parse_enquiries.py --mailbox enquiries@company.com --top 20
```

### 5. Run Demand Analysis & R&D Routing Engine
```bash
# Process all logged enquiries against master catalog (default threshold 10):
python process_demands.py

# Run with custom threshold and Slack webhook integration:
python process_demands.py --threshold 5 --slack-webhook https://hooks.slack.com/services/...
```

### 6. Launch Executive Streamlit Dashboard
```bash
streamlit run app_md_dashboard.py
```

### 7. Run Complete Test Suite
```bash
# Run all 22 tests across catalog sync, email parsing, demand routing, and dashboard:
python -m unittest test_sync.py test_parse_enquiries.py test_process_demands.py test_dashboard.py
```
