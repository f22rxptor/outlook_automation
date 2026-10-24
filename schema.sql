-- Chemical Manufacturing & Trading ERP Schema
-- Database: SQLite 3.x / PostgreSQL compatible ANSI SQL

PRAGMA foreign_keys = ON;

-- 1. Master Chemical Catalog
-- Stores centralized specifications, stock levels, trade names, and operational categories.
CREATE TABLE IF NOT EXISTS master_chemical_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chemical_name TEXT NOT NULL,
    cas_number TEXT NOT NULL UNIQUE,
    trade_names TEXT NOT NULL DEFAULT '[]', -- JSON array of commercial aliases
    current_stock_kg REAL NOT NULL DEFAULT 0.0 CHECK (current_stock_kg >= 0),
    reorder_point_kg REAL NOT NULL DEFAULT 0.0 CHECK (reorder_point_kg >= 0),
    category TEXT NOT NULL CHECK (category IN ('Trading', 'Internal Product', 'Raw Material')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chemical_catalog_cas ON master_chemical_catalog(cas_number);
CREATE INDEX IF NOT EXISTS idx_chemical_catalog_name ON master_chemical_catalog(chemical_name);
CREATE INDEX IF NOT EXISTS idx_chemical_catalog_category ON master_chemical_catalog(category);

-- 2. Enquiry Logs
-- Ingests inbound sales/sourcing email inquiries from clients and traders.
CREATE TABLE IF NOT EXISTS enquiry_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_subject TEXT,
    sender_email TEXT NOT NULL,
    received_at TIMESTAMP NOT NULL,
    raw_text TEXT NOT NULL,
    extracted_json TEXT, -- JSON document of parsed chemicals, quantities, grades
    processing_status TEXT NOT NULL DEFAULT 'PENDING' 
        CHECK (processing_status IN ('PENDING', 'PROCESSED', 'FAILED', 'FLAGGED_FOR_RND', 'DUPLICATE')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_enquiry_sender ON enquiry_logs(sender_email);
CREATE INDEX IF NOT EXISTS idx_enquiry_received ON enquiry_logs(received_at);
CREATE INDEX IF NOT EXISTS idx_enquiry_status ON enquiry_logs(processing_status);

-- 3. Demand Tracker
-- Monitors 7-day trailing velocity of chemical requests and triggers restocking / trading alerts.
CREATE TABLE IF NOT EXISTS demand_tracker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_number TEXT NOT NULL,
    chemical_name TEXT NOT NULL,
    enquiry_count_7d INTEGER NOT NULL DEFAULT 0 CHECK (enquiry_count_7d >= 0),
    threshold_flag INTEGER NOT NULL DEFAULT 0 CHECK (threshold_flag IN (0, 1)), -- SQLite BOOLEAN (0/1)
    last_alert_sent_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cas_number) REFERENCES master_chemical_catalog(cas_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_demand_cas ON demand_tracker(cas_number);
CREATE INDEX IF NOT EXISTS idx_demand_threshold ON demand_tracker(threshold_flag);

-- 4. R&D Queue
-- Queues non-catalog inquiries, specialty grades, or custom syntheses for laboratory feasibility review.
CREATE TABLE IF NOT EXISTS rnd_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chemical_name TEXT NOT NULL,
    requested_cas TEXT,
    requested_grade TEXT,
    requested_qty_kg REAL CHECK (requested_qty_kg IS NULL OR requested_qty_kg >= 0),
    sample_development_status TEXT NOT NULL DEFAULT 'NEW_REQUEST' 
        CHECK (sample_development_status IN ('NEW_REQUEST', 'FEASIBILITY_REVIEW', 'LAB_SYNTHESIS', 'APPROVED', 'QUALITY_CHECK', 'SAMPLE_DISPATCHED', 'REJECTED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rnd_cas ON rnd_queue(requested_cas);
CREATE INDEX IF NOT EXISTS idx_rnd_status ON rnd_queue(sample_development_status);
