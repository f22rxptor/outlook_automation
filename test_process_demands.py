#!/usr/bin/env python3
"""
Unit and Integration Tests for process_demands.py
"""

import json
import os
import sqlite3
import tempfile
import unittest

from process_demands import (
    normalize_text,
    match_chemical,
    load_master_catalog,
    handle_existing_product_demand,
    handle_untraded_compound,
    process_single_enquiry,
    AlertDispatcher,
    CatalogChemical
)
from sync_erp_catalog import init_database, ChemicalRecord, upsert_catalog_records


class TestProcessDemands(unittest.TestCase):

    def setUp(self):
        self.catalog = [
            CatalogChemical(
                id=1,
                chemical_name="ethyl acetate",
                cas_number="141-78-6",
                trade_names=["solvent ea", "acetic ester"],
                current_stock_kg=15000.0,
                reorder_point_kg=5000.0,
                category="Trading"
            ),
            CatalogChemical(
                id=2,
                chemical_name="acetone",
                cas_number="67-64-1",
                trade_names=["dimethyl ketone"],
                current_stock_kg=8000.0,
                reorder_point_kg=3000.0,
                category="Trading"
            )
        ]

    def test_normalize_text(self):
        self.assertEqual(normalize_text("Solvent EA! #1"), "solvent ea 1")
        self.assertEqual(normalize_text("  Ethyl   Acetate  "), "ethyl acetate")
        self.assertEqual(normalize_text(""), "")

    def test_match_chemical_cas_exact(self):
        item = {"chemical_name": "unknown alias", "cas_number": "141-78-6"}
        res = match_chemical(item, self.catalog)
        self.assertTrue(res.matched)
        self.assertEqual(res.match_strategy, "CAS_EXACT")
        self.assertEqual(res.catalog_chemical.chemical_name, "ethyl acetate")

    def test_match_chemical_name_exact(self):
        item = {"chemical_name": "Ethyl Acetate", "cas_number": None}
        res = match_chemical(item, self.catalog)
        self.assertTrue(res.matched)
        self.assertEqual(res.match_strategy, "NAME_EXACT")
        self.assertEqual(res.catalog_chemical.cas_number, "141-78-6")

    def test_match_chemical_trade_name_exact(self):
        item = {"chemical_name": "solvent ea", "cas_number": None}
        res = match_chemical(item, self.catalog)
        self.assertTrue(res.matched)
        self.assertEqual(res.match_strategy, "TRADE_NAME_EXACT")

    def test_match_chemical_fuzzy(self):
        # Slight typo: 'ethyl acetat'
        item = {"chemical_name": "ethyl acetat", "cas_number": None}
        res = match_chemical(item, self.catalog, fuzzy_threshold=0.80)
        self.assertTrue(res.matched)
        self.assertIn("FUZZY", res.match_strategy)
        self.assertEqual(res.catalog_chemical.cas_number, "141-78-6")

    def test_untraded_compound_routing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_rnd.db")
            init_database(db_path, "schema.sql")
            dispatcher = AlertDispatcher()

            # Case A: Small sample order (< 50 kg) -> Immediate LAB_SYNTHESIS
            small_sample = {
                "chemical_name": "Novel fluorinated pyridine",
                "cas_number": None,
                "quantity_kg": 10.0,
                "purity_grade": "98% HPLC"
            }
            res_a = handle_untraded_compound(db_path, small_sample, "Lab Client", dispatcher)
            self.assertEqual(res_a["sample_development_status"], "LAB_SYNTHESIS")
            self.assertTrue(res_a["is_lab_scale"])

            # Case B: Large commercial order (>= 50 kg) -> Standard NEW_REQUEST
            large_order = {
                "chemical_name": "Novel polymer resin",
                "cas_number": None,
                "quantity_kg": 500.0,
                "purity_grade": "Tech Grade"
            }
            res_b = handle_untraded_compound(db_path, large_order, "Bulk Buyer", dispatcher)
            self.assertEqual(res_b["sample_development_status"], "NEW_REQUEST")
            self.assertFalse(res_b["is_lab_scale"])

            # Verify in SQLite database
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.cursor()
                cur.execute("SELECT sample_development_status FROM rnd_queue ORDER BY id ASC")
                statuses = [r[0] for r in cur.fetchall()]
                self.assertEqual(statuses, ["LAB_SYNTHESIS", "NEW_REQUEST"])
            finally:
                conn.close()

    def test_existing_product_demand_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_demand.db")
            init_database(db_path, "schema.sql")

            # Seed catalog
            records = [
                ChemicalRecord(
                    chemical_name="ethyl acetate",
                    cas_number="141-78-6",
                    trade_names=["solvent ea"],
                    current_stock_kg=1000.0,
                    reorder_point_kg=200.0,
                    category="Trading"
                )
            ]
            upsert_catalog_records(db_path, records)

            catalog_chem = CatalogChemical(
                id=1,
                chemical_name="ethyl acetate",
                cas_number="141-78-6",
                trade_names=["solvent ea"],
                current_stock_kg=1000.0,
                reorder_point_kg=200.0,
                category="Trading"
            )
            dispatcher = AlertDispatcher()

            # Increment 9 times with threshold = 10
            for _ in range(9):
                stat = handle_existing_product_demand(db_path, catalog_chem, dispatcher, threshold=10)
                self.assertFalse(stat["threshold_flag"])
                self.assertFalse(stat["alert_triggered"])

            # 10th time -> Triggers threshold flag and alert
            stat_10 = handle_existing_product_demand(db_path, catalog_chem, dispatcher, threshold=10)
            self.assertEqual(stat_10["enquiry_count_7d"], 10)
            self.assertTrue(stat_10["threshold_flag"])
            self.assertTrue(stat_10["alert_triggered"])

            # Verify in SQLite
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.cursor()
                cur.execute("SELECT enquiry_count_7d, threshold_flag, last_alert_sent_at FROM demand_tracker WHERE cas_number = '141-78-6'")
                count, flag, alert_at = cur.fetchone()
                self.assertEqual(count, 10)
                self.assertEqual(flag, 1)
                self.assertIsNotNone(alert_at)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
