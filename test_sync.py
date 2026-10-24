#!/usr/bin/env python3
"""
Unit and Integration Tests for Chemical Catalog ERP Synchronization
"""

import os
import sqlite3
import tempfile
import unittest

from sync_erp_catalog import (
    normalize_chemical_name,
    validate_cas_number,
    parse_and_validate_record,
    init_database,
    upsert_catalog_records,
    ChemicalRecord
)


class TestChemicalCatalogSync(unittest.TestCase):

    def test_normalize_chemical_name(self):
        # Lowercased and stripped of special characters
        self.assertEqual(normalize_chemical_name("Ethyl Acetate! (99%)"), "ethyl acetate 99")
        self.assertEqual(normalize_chemical_name("  Toluene @ Refined Solvent-Grade#1  "), "toluene refined solvent grade 1")
        self.assertEqual(normalize_chemical_name("Isopropanol / Isopropyl Alcohol*"), "isopropanol isopropyl alcohol")
        self.assertEqual(normalize_chemical_name("ACETONE"), "acetone")
        self.assertEqual(normalize_chemical_name(""), "")

    def test_cas_validation_valid(self):
        # Standard CAS numbers from major chemicals
        valid_cases = [
            "141-78-6",    # Ethyl acetate
            "67-64-1",     # Acetone
            "67-63-0",     # Isopropanol
            "67-56-1",     # Methanol
            "108-88-3",    # Toluene
            "7664-93-9",   # Sulfuric acid
            "1310-73-2",   # Sodium hydroxide
            "109-99-9",    # THF
            "75-05-8",     # Acetonitrile
            "75-09-2"      # DCM
        ]
        for cas in valid_cases:
            valid, msg = validate_cas_number(cas, verify_checksum=True)
            self.assertTrue(valid, f"Expected {cas} to be valid, got error: {msg}")

    def test_cas_validation_invalid(self):
        invalid_cases = [
            ("141-78-9", "Checksum mismatch"),
            ("141786", "Missing hyphens"),
            ("141-78", "Incomplete parts"),
            ("ABC-DE-F", "Non-numeric"),
            ("141-78-6-1", "Too many parts"),
            ("", "Empty string")
        ]
        for cas, reason in invalid_cases:
            valid, msg = validate_cas_number(cas, verify_checksum=True)
            self.assertFalse(valid, f"Expected {cas} ({reason}) to fail validation, but it passed.")

    def test_parse_and_validate_record(self):
        # Valid record
        valid_row = {
            "raw_chemical_name": "Acetone (Extra Pure!)",
            "cas_number": "67-64-1",
            "trade_names": ["2-Propanone", "Dimethyl Ketone"],
            "current_stock_kg": 500.0,
            "reorder_point_kg": 100.0,
            "category": "Trading"
        }
        rec = parse_and_validate_record(valid_row)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.chemical_name, "acetone extra pure")
        self.assertEqual(rec.cas_number, "67-64-1")
        self.assertEqual(rec.category, "Trading")

        # Invalid category record
        invalid_cat_row = dict(valid_row, category="UnknownCategory")
        self.assertIsNone(parse_and_validate_record(invalid_cat_row))

        # Negative stock record
        negative_stock_row = dict(valid_row, current_stock_kg=-10.0)
        self.assertIsNone(parse_and_validate_record(negative_stock_row))

    def test_database_init_and_upsert(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_chemicals.db")
            schema_path = "schema.sql"

            init_database(db_path, schema_path)

            records = [
                ChemicalRecord(
                    chemical_name="ethyl acetate",
                    cas_number="141-78-6",
                    trade_names=["Solvent EA"],
                    current_stock_kg=1000.0,
                    reorder_point_kg=200.0,
                    category="Trading"
                )
            ]

            # 1st run: Insert
            stats1 = upsert_catalog_records(db_path, records)
            self.assertEqual(stats1["inserted"], 1)
            self.assertEqual(stats1["updated"], 0)

            # Modify stock and 2nd run: Update
            records[0].current_stock_kg = 1500.0
            stats2 = upsert_catalog_records(db_path, records)
            self.assertEqual(stats2["inserted"], 0)
            self.assertEqual(stats2["updated"], 1)

            # Query database to verify updated stock
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT current_stock_kg FROM master_chemical_catalog WHERE cas_number = '141-78-6'")
                val = cursor.fetchone()[0]
                self.assertEqual(val, 1500.0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
