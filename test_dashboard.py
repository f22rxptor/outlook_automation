#!/usr/bin/env python3
"""
Unit and Integration Tests for app_md_dashboard.py
"""

import os
import sqlite3
import tempfile
import unittest

from app_md_dashboard import (
    fetch_dashboard_kpis,
    fetch_catalog_inventory,
    fetch_rnd_queue,
    fetch_historical_demand_trend,
    update_rnd_status
)
from sync_erp_catalog import init_database, ChemicalRecord, upsert_catalog_records
from process_demands import handle_untraded_compound, AlertDispatcher


class TestMDDashboard(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_dashboard.db")
        init_database(self.db_path, "schema.sql")

        # Seed catalog
        records = [
            ChemicalRecord(
                chemical_name="ethyl acetate",
                cas_number="141-78-6",
                trade_names=["solvent ea"],
                current_stock_kg=1000.0,
                reorder_point_kg=2000.0,  # Below reorder point (unmet demand risk)
                category="Trading"
            ),
            ChemicalRecord(
                chemical_name="acetone",
                cas_number="67-64-1",
                trade_names=["dimethyl ketone"],
                current_stock_kg=8000.0,
                reorder_point_kg=3000.0,
                category="Trading"
            )
        ]
        upsert_catalog_records(self.db_path, records)

        # Seed Demand Tracker
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO demand_tracker (cas_number, chemical_name, enquiry_count_7d, threshold_flag)
            VALUES ('141-78-6', 'ethyl acetate', 12, 1);
            """
        )
        conn.commit()
        conn.close()

        # Seed R&D Queue
        dispatcher = AlertDispatcher()
        handle_untraded_compound(
            self.db_path,
            {"chemical_name": "Novel fluorinated pyridine", "cas_number": None, "quantity_kg": 5.0},
            "Customer X",
            dispatcher
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_fetch_dashboard_kpis(self):
        kpis = fetch_dashboard_kpis(self.db_path)
        self.assertIn("enquiries_7d", kpis)
        self.assertIn("enquiries_30d", kpis)
        self.assertIn("high_demand_count", kpis)
        self.assertIn("rnd_queue_count", kpis)
        self.assertIn("unmet_demand_count", kpis)

        # High demand products count should be 1
        self.assertEqual(kpis["high_demand_count"], 1)
        # R&D queue count should be 1
        self.assertEqual(kpis["rnd_queue_count"], 1)
        # Ethyl Acetate stock is 1000 <= 2000 reorder point with enquiries > 0 -> Unmet demand = 1
        self.assertEqual(kpis["unmet_demand_count"], 1)

    def test_fetch_catalog_inventory_filtering(self):
        # All categories
        df_all = fetch_catalog_inventory(self.db_path)
        self.assertEqual(len(df_all), 2)

        # Specific category
        df_trading = fetch_catalog_inventory(self.db_path, categories=["Trading"])
        self.assertEqual(len(df_trading), 2)

        # Empty category
        df_raw = fetch_catalog_inventory(self.db_path, categories=["Raw Material"])
        self.assertEqual(len(df_raw), 0)

    def test_fetch_rnd_queue(self):
        df_rnd = fetch_rnd_queue(self.db_path)
        self.assertEqual(len(df_rnd), 1)
        self.assertEqual(df_rnd.iloc[0]["sample_development_status"], "LAB_SYNTHESIS")

    def test_update_rnd_status(self):
        df_rnd = fetch_rnd_queue(self.db_path)
        qid = int(df_rnd.iloc[0]["id"])

        success = update_rnd_status(qid, "SAMPLE_DISPATCHED", self.db_path)
        self.assertTrue(success)

        # Re-fetch and verify
        df_updated = fetch_rnd_queue(self.db_path)
        self.assertEqual(df_updated.iloc[0]["sample_development_status"], "SAMPLE_DISPATCHED")

    def test_historical_demand_trend(self):
        df_trend = fetch_historical_demand_trend(self.db_path)
        self.assertFalse(df_trend.empty)
        self.assertIn("Chemical", df_trend.columns)
        self.assertIn("Quantity_kg", df_trend.columns)


if __name__ == "__main__":
    unittest.main()
