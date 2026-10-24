#!/usr/bin/env python3
"""
Unit and Integration Tests for parse_enquiries.py
"""

import io
import json
import os
import sqlite3
import tempfile
import unittest

import pandas as pd
from pypdf import PageObject, PdfWriter

from parse_enquiries import (
    clean_html_body,
    extract_text_from_pdf,
    extract_text_from_excel,
    determine_processing_status,
    save_enquiry_to_database,
    ClaudeEnquiryParser,
    InboundEmail,
    AttachmentFile
)
from sync_erp_catalog import init_database


class TestEnquiryParser(unittest.TestCase):

    def test_clean_html_body(self):
        html = "<p>Dear <b>Sales</b>,<br>We need 10 MT of Solvent EA.&nbsp;</p>"
        cleaned = clean_html_body(html)
        self.assertIn("Dear Sales", cleaned)
        self.assertIn("10 MT of Solvent EA.", cleaned)
        self.assertNotIn("<p>", cleaned)
        self.assertNotIn("&nbsp;", cleaned)

    def test_pdf_extraction(self):
        writer = PdfWriter()
        page = PageObject.create_blank_page(width=612, height=792)
        writer.add_page(page)
        stream = io.BytesIO()
        writer.write(stream)
        pdf_bytes = stream.getvalue()

        extracted = extract_text_from_pdf(pdf_bytes, "test.pdf")
        self.assertIsInstance(extracted, str)

    def test_excel_extraction(self):
        df = pd.DataFrame([
            {"Chemical": "Ethyl Acetate", "CAS": "141-78-6", "Qty_kg": 5000},
            {"Chemical": "Acetone", "CAS": "67-64-1", "Qty_kg": 3000}
        ])
        stream = io.BytesIO()
        with pd.ExcelWriter(stream, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="RFQ")
        excel_bytes = stream.getvalue()

        extracted = extract_text_from_excel(excel_bytes, "rfq.xlsx")
        self.assertIn("Ethyl Acetate", extracted)
        self.assertIn("141-78-6", extracted)
        self.assertIn("5000", extracted)

    def test_determine_processing_status(self):
        # 1. Valid standard items -> PROCESSED
        valid_data = {
            "enquiry_intent": "Firm Order",
            "requested_items": [
                {"chemical_name": "ethyl acetate", "cas_number": "141-78-6", "quantity_kg": 1000.0}
            ]
        }
        self.assertEqual(determine_processing_status(valid_data), "PROCESSED")

        # 2. Missing CAS -> FLAGGED_FOR_RND
        rnd_missing_cas = {
            "enquiry_intent": "General Inquiry",
            "requested_items": [
                {"chemical_name": "experimental fluoro reagent", "cas_number": None, "quantity_kg": 5.0}
            ]
        }
        self.assertEqual(determine_processing_status(rnd_missing_cas), "FLAGGED_FOR_RND")

        # 3. None data -> FAILED
        self.assertEqual(determine_processing_status(None), "FAILED")

    def test_database_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_enquiries.db")
            init_database(db_path, "schema.sql")

            email = InboundEmail(
                id="TEST-001",
                subject="RFQ for Isopropanol",
                sender_email="buyer@testcorp.com",
                sender_name="Buyer One",
                received_at="2026-10-24T10:00:00Z",
                body_text="Please quote 5000 kg IPA"
            )

            extracted = {
                "customer_name": "Buyer One",
                "email_date": "2026-10-24T10:00:00Z",
                "enquiry_intent": "Quote Request",
                "requested_items": [
                    {"chemical_name": "isopropanol", "cas_number": "67-63-0", "quantity_kg": 5000.0}
                ]
            }

            log_id = save_enquiry_to_database(
                db_path=db_path,
                email=email,
                raw_full_text=email.body_text,
                extracted_data=extracted,
                status="PROCESSED"
            )
            self.assertGreater(log_id, 0)

            conn = sqlite3.connect(db_path)
            try:
                cur = conn.cursor()
                cur.execute("SELECT sender_email, processing_status, extracted_json FROM enquiry_logs WHERE id = ?", (log_id,))
                row = cur.fetchone()
                self.assertEqual(row[0], "buyer@testcorp.com")
                self.assertEqual(row[1], "PROCESSED")
                loaded = json.loads(row[2])
                self.assertEqual(loaded["enquiry_intent"], "Quote Request")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
