#!/usr/bin/env python3
"""
ERP Chemical Catalog Synchronization Script
Enterprise Data Engineering Pipeline for Chemical Manufacturing & Trading

This script extracts chemical master data from an ERP source (or CSV/in-memory extract),
normalizes chemical names, strictly validates CAS Registry Numbers against the 
standard format and official CAS check-digit algorithm, and upserts the records
into the local `master_chemical_catalog` database table.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("erp_sync")

# Standard CAS format regex: 2 to 7 digits, hyphen, 2 digits, hyphen, 1 check digit
CAS_REGEX = re.compile(r"^\d{2,7}-\d{2}-\d$")

# Valid business categories
VALID_CATEGORIES = {"Trading", "Internal Product", "Raw Material"}


@dataclass
class ChemicalRecord:
    """Normalized and validated chemical record ready for catalog ingestion."""
    chemical_name: str
    cas_number: str
    trade_names: List[str]
    current_stock_kg: float
    reorder_point_kg: float
    category: str
    sku: Optional[str] = None


# 10 Common chemicals representing realistic ERP export data
SAMPLE_ERP_DATA: List[Dict[str, Any]] = [
    {
        "sku": "CHEM-001-EA",
        "raw_chemical_name": "Ethyl Acetate (Industrial Grade #99%)",
        "cas_number": "141-78-6",
        "trade_names": ["Ethyl Ethanoate", "Acetic Ester", "Solvent EA"],
        "current_stock_kg": 15400.50,
        "reorder_point_kg": 5000.00,
        "category": "Trading"
    },
    {
        "sku": "CHEM-002-AC",
        "raw_chemical_name": "Acetone [High Purity 99.5%]",
        "cas_number": "67-64-1",
        "trade_names": ["2-Propanone", "Dimethyl Ketone", "Propan-2-one"],
        "current_stock_kg": 8250.00,
        "reorder_point_kg": 3000.00,
        "category": "Trading"
    },
    {
        "sku": "CHEM-003-IPA",
        "raw_chemical_name": "Isopropanol / Isopropyl Alcohol*",
        "cas_number": "67-63-0",
        "trade_names": ["IPA", "2-Propanol", "Rubbing Alcohol"],
        "current_stock_kg": 24100.00,
        "reorder_point_kg": 8000.00,
        "category": "Internal Product"
    },
    {
        "sku": "CHEM-004-MEOH",
        "raw_chemical_name": "Methanol (Technical Grade!!)",
        "cas_number": "67-56-1",
        "trade_names": ["Methyl Alcohol", "Wood Spirit", "Carbinol"],
        "current_stock_kg": 31200.00,
        "reorder_point_kg": 10000.00,
        "category": "Raw Material"
    },
    {
        "sku": "CHEM-005-TOL",
        "raw_chemical_name": "Toluene @ Refined Solvent",
        "cas_number": "108-88-3",
        "trade_names": ["Methylbenzene", "Toluol", "Antisal 1a"],
        "current_stock_kg": 12750.75,
        "reorder_point_kg": 4000.00,
        "category": "Trading"
    },
    {
        "sku": "CHEM-006-SA",
        "raw_chemical_name": "Sulfuric Acid {98% Commercial}",
        "cas_number": "7664-93-9",
        "trade_names": ["Oil of Vitriol", "Hydrogen Sulfate", "Mattling Acid"],
        "current_stock_kg": 45000.00,
        "reorder_point_kg": 15000.00,
        "category": "Raw Material"
    },
    {
        "sku": "CHEM-007-SH",
        "raw_chemical_name": "Sodium Hydroxide (Caustic Flakes~)",
        "cas_number": "1310-73-2",
        "trade_names": ["Caustic Soda", "Lye", "Sodium Hydrate"],
        "current_stock_kg": 28300.00,
        "reorder_point_kg": 10000.00,
        "category": "Raw Material"
    },
    {
        "sku": "CHEM-008-THF",
        "raw_chemical_name": "Tetrahydrofuran [Stabilized BHT]",
        "cas_number": "109-99-9",
        "trade_names": ["THF", "Oxolane", "1,4-Epoxybutane"],
        "current_stock_kg": 6400.00,
        "reorder_point_kg": 2500.00,
        "category": "Internal Product"
    },
    {
        "sku": "CHEM-009-ACN",
        "raw_chemical_name": "Acetonitrile (HPLC Grade: 99.9%)",
        "cas_number": "75-05-8",
        "trade_names": ["Methyl Cyanide", "Cyanomethane", "ACN Solvent"],
        "current_stock_kg": 4200.25,
        "reorder_point_kg": 1500.00,
        "category": "Trading"
    },
    {
        "sku": "CHEM-010-DCM",
        "raw_chemical_name": "Dichloromethane - Premium Solvent$",
        "cas_number": "75-09-2",
        "trade_names": ["Methylene Chloride", "DCM", "Methylene Bichloride"],
        "current_stock_kg": 19800.00,
        "reorder_point_kg": 6000.00,
        "category": "Trading"
    }
]


def normalize_chemical_name(raw_name: str) -> str:
    """
    Normalizes chemical name:
    1. Converts to lowercase.
    2. Strips all special characters, symbols, and punctuation.
    3. Normalizes multiple spaces into a single space.
    4. Trims leading and trailing whitespace.
    """
    if not raw_name:
        return ""
    # Convert to lowercase
    normalized = raw_name.lower()
    # Strip special characters (preserve letters, digits, and whitespace)
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    # Collapse multiple consecutive whitespaces
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def validate_cas_number(cas_str: str, verify_checksum: bool = True) -> Tuple[bool, str]:
    """
    Validates a CAS Registry Number.
    Strict requirements:
    - Must match regex: 2-7 digits, hyphen, 2 digits, hyphen, 1 check digit (e.g. 141-78-6).
    - Optional: Official CAS check digit verification:
      If digits preceding check digit are d_n ... d_1,
      checksum = (sum(i * d_i for i in 1..n)) % 10 == check_digit.

    Returns:
        (is_valid: bool, error_message: str)
    """
    if not cas_str or not isinstance(cas_str, str):
        return False, "CAS number is empty or not a string"

    clean_cas = cas_str.strip()

    if not CAS_REGEX.match(clean_cas):
        return False, f"CAS '{clean_cas}' does not match standard pattern (e.g. 141-78-6)"

    if verify_checksum:
        parts = clean_cas.split("-")
        check_digit = int(parts[2])
        digits = parts[0] + parts[1]
        
        # Calculate checksum from right to left
        total = sum((idx + 1) * int(digit) for idx, digit in enumerate(reversed(digits)))
        expected_check = total % 10

        if expected_check != check_digit:
            return False, (
                f"CAS '{clean_cas}' failed check-digit verification: "
                f"expected {expected_check}, got {check_digit}"
            )

    return True, ""


def init_database(db_path: str, schema_path: str) -> None:
    """Initializes SQLite database with schema.sql if tables do not exist."""
    schema_file = Path(schema_path)
    if not schema_file.exists():
        raise FileNotFoundError(f"Schema file not found at: {schema_file.resolve()}")

    conn = sqlite3.connect(db_path)
    try:
        with open(schema_file, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Initialized database schema at '{db_path}' using '{schema_path}'.")


def parse_and_validate_record(raw_row: Dict[str, Any]) -> Optional[ChemicalRecord]:
    """Validates and normalizes raw input row from ERP or CSV export."""
    raw_name = raw_row.get("raw_chemical_name") or raw_row.get("chemical_name", "")
    raw_cas = raw_row.get("cas_number", "")
    raw_category = (raw_row.get("category") or "").strip()
    raw_trade_names = raw_row.get("trade_names", [])

    # 1. Normalize chemical name
    normalized_name = normalize_chemical_name(raw_name)
    if not normalized_name:
        logger.warning(f"Skipping record due to missing/empty chemical name: {raw_row}")
        return None

    # 2. Validate CAS number
    is_valid_cas, cas_err = validate_cas_number(raw_cas, verify_checksum=True)
    if not is_valid_cas:
        logger.warning(f"Skipping chemical '{normalized_name}': {cas_err}")
        return None
    normalized_cas = raw_cas.strip()

    # 3. Validate Category
    if raw_category not in VALID_CATEGORIES:
        logger.warning(
            f"Invalid category '{raw_category}' for '{normalized_name}'. "
            f"Must be one of {sorted(VALID_CATEGORIES)}. Skipping."
        )
        return None

    # 4. Handle trade names
    if isinstance(raw_trade_names, str):
        try:
            trade_names_list = json.loads(raw_trade_names)
            if not isinstance(trade_names_list, list):
                trade_names_list = [raw_trade_names]
        except json.JSONDecodeError:
            # Comma-separated or single string
            trade_names_list = [t.strip() for t in raw_trade_names.split(",") if t.strip()]
    elif isinstance(raw_trade_names, list):
        trade_names_list = [str(t).strip() for t in raw_trade_names if str(t).strip()]
    else:
        trade_names_list = []

    # 5. Numerical stock levels
    try:
        current_stock = float(raw_row.get("current_stock_kg", 0.0))
        reorder_point = float(raw_row.get("reorder_point_kg", 0.0))
        if current_stock < 0 or reorder_point < 0:
            raise ValueError("Stock or reorder point cannot be negative.")
    except (ValueError, TypeError) as err:
        logger.warning(f"Invalid stock quantities for '{normalized_name}': {err}. Skipping.")
        return None

    return ChemicalRecord(
        chemical_name=normalized_name,
        cas_number=normalized_cas,
        trade_names=trade_names_list,
        current_stock_kg=current_stock,
        reorder_point_kg=reorder_point,
        category=raw_category,
        sku=raw_row.get("sku")
    )


def upsert_catalog_records(
    db_path: str,
    records: List[ChemicalRecord],
    dry_run: bool = False
) -> Dict[str, int]:
    """
    Executes atomic upsert on `master_chemical_catalog` table using CAS number as unique key.
    Tracks inserts vs updates.
    """
    stats = {"processed": len(records), "inserted": 0, "updated": 0, "errors": 0}

    if dry_run:
        logger.info(f"[DRY-RUN] Validated {len(records)} records. Skipping database write.")
        return stats

    upsert_sql = """
    INSERT INTO master_chemical_catalog (
        chemical_name,
        cas_number,
        trade_names,
        current_stock_kg,
        reorder_point_kg,
        category,
        updated_at
    ) VALUES (
        :chemical_name,
        :cas_number,
        :trade_names,
        :current_stock_kg,
        :reorder_point_kg,
        :category,
        CURRENT_TIMESTAMP
    )
    ON CONFLICT(cas_number) DO UPDATE SET
        chemical_name = excluded.chemical_name,
        trade_names = excluded.trade_names,
        current_stock_kg = excluded.current_stock_kg,
        reorder_point_kg = excluded.reorder_point_kg,
        category = excluded.category,
        updated_at = CURRENT_TIMESTAMP;
    """

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        for rec in records:
            # Check if record already exists to accurately report metrics
            cursor.execute("SELECT id FROM master_chemical_catalog WHERE cas_number = ?", (rec.cas_number,))
            existing = cursor.fetchone()

            params = {
                "chemical_name": rec.chemical_name,
                "cas_number": rec.cas_number,
                "trade_names": json.dumps(rec.trade_names),
                "current_stock_kg": rec.current_stock_kg,
                "reorder_point_kg": rec.reorder_point_kg,
                "category": rec.category
            }

            try:
                cursor.execute(upsert_sql, params)
                if existing:
                    stats["updated"] += 1
                else:
                    stats["inserted"] += 1
            except Exception as e:
                logger.error(f"Failed to upsert CAS '{rec.cas_number}': {e}")
                stats["errors"] += 1

        conn.commit()
    finally:
        conn.close()

    return stats


def export_sample_csv(output_csv_path: str) -> None:
    """Exports the 10 sample mock chemicals to a CSV file to emulate ERP data extracts."""
    with open(output_csv_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "sku",
                "raw_chemical_name",
                "cas_number",
                "trade_names",
                "current_stock_kg",
                "reorder_point_kg",
                "category"
            ]
        )
        writer.writeheader()
        for row in SAMPLE_ERP_DATA:
            row_copy = dict(row)
            row_copy["trade_names"] = json.dumps(row_copy["trade_names"])
            writer.writerow(row_copy)
    logger.info(f"Sample ERP chemical export generated at '{output_csv_path}'.")


def load_from_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Loads ERP export records from a CSV file."""
    rows: List[Dict[str, Any]] = []
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def print_catalog_summary(db_path: str) -> None:
    """Prints a formatted summary table of all active catalog entries."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, chemical_name, cas_number, category, current_stock_kg, reorder_point_kg, trade_names
            FROM master_chemical_catalog
            ORDER BY id ASC;
            """
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    print("\n" + "=" * 115)
    print(f"{'CURRENT MASTER CHEMICAL CATALOG':^115}")
    print("=" * 115)
    header = f"{'ID':<4} | {'CAS NUMBER':<12} | {'NORMALIZED CHEMICAL NAME':<32} | {'CATEGORY':<16} | {'STOCK (KG)':<12} | {'REORDER':<10}"
    print(header)
    print("-" * 115)
    for r in rows:
        cid, name, cas, cat, stock, reorder, _ = r
        print(f"{cid:<4} | {cas:<12} | {name:<32} | {cat:<16} | {stock:<12.2f} | {reorder:<10.2f}")
    print("=" * 115 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enterprise Chemical Catalog Synchronization Script for ERP & SQLite"
    )
    parser.add_argument(
        "--db",
        default="chemicals.db",
        help="Path to SQLite database file (default: chemicals.db)"
    )
    parser.add_argument(
        "--schema",
        default="schema.sql",
        help="Path to schema SQL file (default: schema.sql)"
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Optional path to a CSV file containing ERP chemical catalog extract"
    )
    parser.add_argument(
        "--export-sample-csv",
        dest="export_sample",
        default=None,
        help="Optional filename to export the 10 mock ERP chemical records to CSV"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and normalize input data without modifying the database"
    )

    args = parser.parse_args()

    # Step 0: Export sample CSV if requested
    if args.export_sample:
        export_sample_csv(args.export_sample)
        if not args.csv:
            return

    # Step 1: Initialize Database & Schema
    if not args.dry_run:
        init_database(args.db, args.schema)

    # Step 2: Extract ERP Data (CSV or Embedded Mock Data)
    if args.csv:
        logger.info(f"Extracting chemical master list from CSV file: {args.csv}")
        raw_dataset = load_from_csv(args.csv)
    else:
        logger.info("Extracting chemical master list from sample ERP dataset (10 chemicals)...")
        raw_dataset = SAMPLE_ERP_DATA

    # Step 3: Transform & Validate (Normalization & CAS integrity)
    validated_records: List[ChemicalRecord] = []
    rejected_count = 0

    for raw_item in raw_dataset:
        record = parse_and_validate_record(raw_item)
        if record:
            validated_records.append(record)
        else:
            rejected_count += 1

    logger.info(
        f"Validation complete: {len(validated_records)} valid records, "
        f"{rejected_count} rejected records."
    )

    # Step 4: Load / Upsert into Database
    stats = upsert_catalog_records(args.db, validated_records, dry_run=args.dry_run)
    logger.info(
        f"Synchronization finished. Stats: {stats['inserted']} inserted, "
        f"{stats['updated']} updated, {stats['errors']} errors."
    )

    # Step 5: Display Verified Catalog
    if not args.dry_run and stats["inserted"] + stats["updated"] > 0:
        print_catalog_summary(args.db)


if __name__ == "__main__":
    main()
