#!/usr/bin/env python3
"""
Chemical Procurement Demand Processing & R&D Routing Engine
Enterprise Backend Logic Developer

This module analyzes parsed customer enquiry JSON documents against the Master Chemical Catalog.
It implements:
1. Exact & Fuzzy matching against the Master Catalog by CAS Number (primary) and Chemical/Trade Names (secondary).
2. Existing Product Velocity Tracker: Increments 7-day trailing velocity in `demand_tracker` and triggers
   automated Procurement & Sales alerts when threshold (>= 10 inquiries) is reached.
3. New/Untraded Compound Router: Flags non-cataloged requests as `NEW_UNTRADED_COMPOUND`, queues them into
   `rnd_queue`, flags small-quantity orders (< 50 kg) for "Immediate Lab Sample Synthesis", and dispatches
   R&D alerts.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party requests for webhooks
try:
    import requests
except ImportError:
    requests = None

# Ensure UTF-8 output encoding across Windows / Linux consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("process_demands")

# Standard CAS regex format
CAS_REGEX = re.compile(r"^\d{2,7}-\d{2}-\d$")

# Threshold configuration defaults
HIGH_DEMAND_THRESHOLD = 10  # 10 enquiries in 7-day rolling window
LAB_SAMPLE_THRESHOLD_KG = 50.0  # < 50 kg triggers Immediate Lab Sample Synthesis
FUZZY_MATCH_THRESHOLD = 0.85  # Minimum similarity ratio (0.0 to 1.0)


@dataclass
class CatalogChemical:
    """Master chemical catalog entry."""
    id: int
    chemical_name: str
    cas_number: str
    trade_names: List[str]
    current_stock_kg: float
    reorder_point_kg: float
    category: str


@dataclass
class MatchResult:
    """Outcome of chemical catalog matching."""
    matched: bool
    catalog_chemical: Optional[CatalogChemical] = None
    match_strategy: Optional[str] = None  # "CAS_EXACT", "NAME_EXACT", "TRADE_NAME_EXACT", "NAME_FUZZY", "TRADE_NAME_FUZZY"
    similarity_score: float = 0.0


# ---------------------------------------------------------------------------
# 1. CATALOG MATCHING ENGINE
# ---------------------------------------------------------------------------

def normalize_text(text: Optional[str]) -> str:
    """Normalizes chemical strings for comparison."""
    if not text:
        return ""
    lowered = text.lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def load_master_catalog(db_path: str) -> List[CatalogChemical]:
    """Loads all active master catalog entries from SQLite."""
    chemicals: List[CatalogChemical] = []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, chemical_name, cas_number, trade_names, current_stock_kg, reorder_point_kg, category
            FROM master_chemical_catalog;
            """
        )
        for row in cur.fetchall():
            cid, name, cas, raw_trades, stock, reorder, cat = row
            try:
                trades_list = json.loads(raw_trades) if raw_trades else []
                if not isinstance(trades_list, list):
                    trades_list = [raw_trades]
            except Exception:
                trades_list = [raw_trades] if raw_trades else []

            chemicals.append(CatalogChemical(
                id=cid,
                chemical_name=name,
                cas_number=cas,
                trade_names=trades_list,
                current_stock_kg=float(stock),
                reorder_point_kg=float(reorder),
                category=cat
            ))
    finally:
        conn.close()
    return chemicals


def match_chemical(
    extracted_item: Dict[str, Any],
    catalog: List[CatalogChemical],
    fuzzy_threshold: float = FUZZY_MATCH_THRESHOLD
) -> MatchResult:
    """
    Attempts to match an extracted enquiry item against the Master Chemical Catalog.
    Order of precedence:
    1. CAS Number Exact Match
    2. Canonical Chemical Name Exact Match
    3. Trade Name Exact Match
    4. Fuzzy Chemical Name Match
    5. Fuzzy Trade Name Match
    """
    raw_cas = (extracted_item.get("cas_number") or "").strip()
    raw_name = extracted_item.get("chemical_name") or ""
    raw_trade = extracted_item.get("trade_name") or ""

    norm_name = normalize_text(raw_name)
    norm_trade = normalize_text(raw_trade)

    # 1. Primary Strategy: CAS Number Exact Match
    if raw_cas and CAS_REGEX.match(raw_cas):
        for chem in catalog:
            if chem.cas_number == raw_cas:
                return MatchResult(
                    matched=True,
                    catalog_chemical=chem,
                    match_strategy="CAS_EXACT",
                    similarity_score=1.0
                )

    # 2. Secondary Strategy: Chemical Name Exact Match
    if norm_name:
        for chem in catalog:
            if normalize_text(chem.chemical_name) == norm_name:
                return MatchResult(
                    matched=True,
                    catalog_chemical=chem,
                    match_strategy="NAME_EXACT",
                    similarity_score=1.0
                )

    # 3. Secondary Strategy: Trade Name Exact Match
    if norm_name or norm_trade:
        search_targets = [t for t in [norm_name, norm_trade] if t]
        for chem in catalog:
            for t_alias in chem.trade_names:
                norm_alias = normalize_text(t_alias)
                if norm_alias in search_targets:
                    return MatchResult(
                        matched=True,
                        catalog_chemical=chem,
                        match_strategy="TRADE_NAME_EXACT",
                        similarity_score=1.0
                    )

    # 4 & 5. Fuzzy Matching (Name & Trade Names)
    best_chem: Optional[CatalogChemical] = None
    best_score: float = 0.0
    best_strategy: Optional[str] = None

    if norm_name:
        for chem in catalog:
            score = difflib.SequenceMatcher(None, norm_name, normalize_text(chem.chemical_name)).ratio()
            if score > best_score:
                best_score = score
                best_chem = chem
                best_strategy = "NAME_FUZZY"

            # Check trade names
            for t_alias in chem.trade_names:
                t_score = difflib.SequenceMatcher(None, norm_name, normalize_text(t_alias)).ratio()
                if t_score > best_score:
                    best_score = t_score
                    best_chem = chem
                    best_strategy = "TRADE_NAME_FUZZY"

    if best_chem and best_score >= fuzzy_threshold:
        return MatchResult(
            matched=True,
            catalog_chemical=best_chem,
            match_strategy=best_strategy,
            similarity_score=best_score
        )

    # No match found
    return MatchResult(matched=False, similarity_score=best_score)


# ---------------------------------------------------------------------------
# 2. ALERT DISPATCHERS (Slack / Email / Webhook / Console)
# ---------------------------------------------------------------------------

class AlertDispatcher:
    """Dispatches Procurement, Sales, and R&D alerts via Webhooks and Logs."""

    def __init__(self, slack_webhook_url: Optional[str] = None):
        self.slack_webhook_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")

    def send_high_demand_alert(
        self,
        chemical_name: str,
        cas_number: str,
        enquiry_count_7d: int,
        current_stock_kg: float,
        reorder_point_kg: float
    ) -> None:
        """Dispatches High Demand Alert to Procurement and Sales teams."""
        message = (
            f"[HIGH DEMAND ALERT] Chemical '{chemical_name.upper()}' (CAS: {cas_number}) "
            f"hit {enquiry_count_7d} enquiries in the last 7 days!\n"
            f"  Stock: {current_stock_kg:,.2f} kg | Reorder Point: {reorder_point_kg:,.2f} kg\n"
            f"  ACTION: Prepare stock/supplier quotes."
        )
        logger.warning(message)
        self._post_webhook("HIGH_DEMAND_ALERT", message)

    def send_rnd_ticket_alert(
        self,
        chemical_name: str,
        cas_number: Optional[str],
        requested_qty_kg: Optional[float],
        requested_grade: Optional[str],
        customer_name: Optional[str],
        is_lab_scale: bool
    ) -> None:
        """Dispatches R&D ticket alert when new/untraded compound is detected."""
        qty_str = f"{requested_qty_kg:,.2f} kg" if requested_qty_kg is not None else "Unspecified"
        cas_str = cas_number if cas_number else "N/A"
        routing = "Immediate Lab Sample Synthesis" if is_lab_scale else "Standard Feasibility Review"

        message = (
            f"[R&D TICKET ALERT] New Market Compound Detected: '{chemical_name}' (CAS: {cas_str})\n"
            f"  Customer requested: {qty_str} (Grade: {requested_grade or 'Standard'})\n"
            f"  Customer: {customer_name or 'Anonymous'}\n"
            f"  Routing: {routing}"
        )
        logger.info(message)
        self._post_webhook("RND_NEW_COMPOUND", message)

    def _post_webhook(self, event_type: str, text: str) -> None:
        """Posts payload to Slack or webhook if configured."""
        if not self.slack_webhook_url or not requests:
            return
        payload = {
            "text": text,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        try:
            res = requests.post(self.slack_webhook_url, json=payload, timeout=5)
            if res.status_code != 200:
                logger.warning(f"Slack webhook returned non-200 code: {res.status_code}")
        except Exception as err:
            logger.warning(f"Failed to deliver webhook alert: {err}")


# ---------------------------------------------------------------------------
# 3. EXISTING PRODUCT LOGIC (DEMAND TRACKER)
# ---------------------------------------------------------------------------

def handle_existing_product_demand(
    db_path: str,
    catalog_chem: CatalogChemical,
    dispatcher: AlertDispatcher,
    threshold: int = HIGH_DEMAND_THRESHOLD
) -> Dict[str, Any]:
    """
    Increments 7-day trailing inquiry count for matched catalog chemical.
    If enquiry_count_7d >= threshold:
    - Sets threshold_flag = 1
    - Triggers automated Procurement & Sales alert
    - Records last_alert_sent_at
    """
    conn = sqlite3.connect(db_path)
    alert_triggered = False
    new_count = 1

    try:
        cur = conn.cursor()
        # Ensure UNIQUE index on cas_number in demand_tracker
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_demand_cas_unique 
            ON demand_tracker(cas_number);
            """
        )

        # Check existing row
        cur.execute(
            """
            SELECT id, enquiry_count_7d, threshold_flag, last_alert_sent_at
            FROM demand_tracker
            WHERE cas_number = ?;
            """,
            (catalog_chem.cas_number,)
        )
        existing = cur.fetchone()

        now_iso = datetime.now(timezone.utc).isoformat()

        if existing:
            track_id, count, current_flag, last_alert = existing
            new_count = count + 1
            threshold_met = 1 if new_count >= threshold else 0

            # Determine whether to send alert (send if threshold reached and not alerted recently)
            should_alert = (new_count >= threshold) and (current_flag == 0 or last_alert is None)

            cur.execute(
                """
                UPDATE demand_tracker
                SET enquiry_count_7d = ?,
                    threshold_flag = ?,
                    last_alert_sent_at = CASE WHEN ? = 1 THEN ? ELSE last_alert_sent_at END,
                    updated_at = ?
                WHERE id = ?;
                """,
                (
                    new_count,
                    threshold_met,
                    1 if should_alert else 0,
                    now_iso if should_alert else last_alert,
                    now_iso,
                    track_id
                )
            )

            if should_alert:
                alert_triggered = True
                dispatcher.send_high_demand_alert(
                    chemical_name=catalog_chem.chemical_name,
                    cas_number=catalog_chem.cas_number,
                    enquiry_count_7d=new_count,
                    current_stock_kg=catalog_chem.current_stock_kg,
                    reorder_point_kg=catalog_chem.reorder_point_kg
                )

        else:
            # First time tracked
            threshold_met = 1 if new_count >= threshold else 0
            cur.execute(
                """
                INSERT INTO demand_tracker (
                    cas_number,
                    chemical_name,
                    enquiry_count_7d,
                    threshold_flag,
                    last_alert_sent_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    catalog_chem.cas_number,
                    catalog_chem.chemical_name,
                    new_count,
                    threshold_met,
                    now_iso if threshold_met else None,
                    now_iso
                )
            )
            if threshold_met:
                alert_triggered = True
                dispatcher.send_high_demand_alert(
                    chemical_name=catalog_chem.chemical_name,
                    cas_number=catalog_chem.cas_number,
                    enquiry_count_7d=new_count,
                    current_stock_kg=catalog_chem.current_stock_kg,
                    reorder_point_kg=catalog_chem.reorder_point_kg
                )

        conn.commit()
    finally:
        conn.close()

    return {
        "cas_number": catalog_chem.cas_number,
        "chemical_name": catalog_chem.chemical_name,
        "enquiry_count_7d": new_count,
        "threshold_flag": new_count >= threshold,
        "alert_triggered": alert_triggered
    }


# ---------------------------------------------------------------------------
# 4. NEW / UNTRADED PRODUCT LOGIC (R&D QUEUE)
# ---------------------------------------------------------------------------

def handle_untraded_compound(
    db_path: str,
    extracted_item: Dict[str, Any],
    customer_name: Optional[str],
    dispatcher: AlertDispatcher
) -> Dict[str, Any]:
    """
    Handles non-cataloged chemical inquiries:
    - Flags as NEW_UNTRADED_COMPOUND.
    - Pushes record to `rnd_queue`.
    - If requested quantity < 50 kg, sets sample_development_status = 'LAB_SYNTHESIS'
      (Immediate Lab Sample Synthesis).
    - Otherwise sets status = 'NEW_REQUEST'.
    - Sends an R&D ticket alert.
    """
    chem_name = (extracted_item.get("chemical_name") or extracted_item.get("trade_name") or "unknown compound").strip()
    req_cas = (extracted_item.get("cas_number") or "").strip() or None
    grade = extracted_item.get("purity_grade")
    qty_kg = extracted_item.get("quantity_kg")

    is_lab_scale = (qty_kg is not None and qty_kg < LAB_SAMPLE_THRESHOLD_KG)
    status = "LAB_SYNTHESIS" if is_lab_scale else "NEW_REQUEST"

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO rnd_queue (
                chemical_name,
                requested_cas,
                requested_grade,
                requested_qty_kg,
                sample_development_status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
            """,
            (chem_name, req_cas, grade, qty_kg, status)
        )
        conn.commit()
        queue_id = cur.lastrowid
    finally:
        conn.close()

    # Send R&D alert
    dispatcher.send_rnd_ticket_alert(
        chemical_name=chem_name,
        cas_number=req_cas,
        requested_qty_kg=qty_kg,
        requested_grade=grade,
        customer_name=customer_name,
        is_lab_scale=is_lab_scale
    )

    return {
        "queue_id": queue_id,
        "chemical_name": chem_name,
        "requested_cas": req_cas,
        "requested_qty_kg": qty_kg,
        "sample_development_status": status,
        "is_lab_scale": is_lab_scale,
        "routing": "Immediate Lab Sample Synthesis" if is_lab_scale else "Standard Feasibility Review"
    }


# ---------------------------------------------------------------------------
# 5. PIPELINE BATCH PROCESSOR
# ---------------------------------------------------------------------------

def process_single_enquiry(
    db_path: str,
    enquiry_log_id: int,
    extracted_json_str: str,
    dispatcher: AlertDispatcher,
    threshold: int = HIGH_DEMAND_THRESHOLD,
    fuzzy_threshold: float = FUZZY_MATCH_THRESHOLD
) -> Dict[str, Any]:
    """Processes a single parsed enquiry's items against the master catalog."""
    try:
        data = json.loads(extracted_json_str)
    except Exception as e:
        logger.error(f"Failed to parse JSON for enquiry_log id={enquiry_log_id}: {e}")
        return {"enquiry_id": enquiry_log_id, "error": str(e), "items": []}

    catalog = load_master_catalog(db_path)
    customer_name = data.get("customer_name")
    items = data.get("requested_items", [])

    item_results = []
    for item in items:
        match_res = match_chemical(item, catalog, fuzzy_threshold=fuzzy_threshold)

        if match_res.matched and match_res.catalog_chemical:
            # Existing Product Logic
            demand_stat = handle_existing_product_demand(
                db_path=db_path,
                catalog_chem=match_res.catalog_chemical,
                dispatcher=dispatcher,
                threshold=threshold
            )
            item_results.append({
                "action": "DEMAND_TRACKER_INCREMENTED",
                "match_strategy": match_res.match_strategy,
                "similarity_score": match_res.similarity_score,
                "demand_stat": demand_stat
            })
        else:
            # Untraded Compound / R&D Logic
            rnd_stat = handle_untraded_compound(
                db_path=db_path,
                extracted_item=item,
                customer_name=customer_name,
                dispatcher=dispatcher
            )
            item_results.append({
                "action": "NEW_UNTRADED_COMPOUND_QUEUED",
                "rnd_stat": rnd_stat
            })

    return {
        "enquiry_id": enquiry_log_id,
        "customer_name": customer_name,
        "processed_items_count": len(item_results),
        "results": item_results
    }


def process_all_pending_enquiries(
    db_path: str = "chemicals.db",
    threshold: int = HIGH_DEMAND_THRESHOLD,
    fuzzy_threshold: float = FUZZY_MATCH_THRESHOLD,
    slack_webhook_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Scans `enquiry_logs` for inquiries with extracted_json and processes them.
    """
    dispatcher = AlertDispatcher(slack_webhook_url=slack_webhook_url)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, email_subject, sender_email, extracted_json
            FROM enquiry_logs
            WHERE extracted_json IS NOT NULL AND extracted_json != ''
            ORDER BY id ASC;
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    logger.info(f"Loaded {len(rows)} enquiries from 'enquiry_logs' for catalog matching and demand analysis.")
    batch_summary = []

    for row in rows:
        eid, subj, sender, ext_json = row
        logger.info(f"Analyzing enquiry ID={eid}: '{subj}' from {sender}...")
        res = process_single_enquiry(
            db_path=db_path,
            enquiry_log_id=eid,
            extracted_json_str=ext_json,
            dispatcher=dispatcher,
            threshold=threshold,
            fuzzy_threshold=fuzzy_threshold
        )
        batch_summary.append(res)

    return batch_summary


def print_demand_and_rnd_summary(db_path: str) -> None:
    """Prints status tables of `demand_tracker` and `rnd_queue`."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        
        # 1. Demand Tracker Table
        cur.execute(
            """
            SELECT id, cas_number, chemical_name, enquiry_count_7d, threshold_flag, last_alert_sent_at
            FROM demand_tracker
            ORDER BY enquiry_count_7d DESC;
            """
        )
        demands = cur.fetchall()

        print("\n" + "=" * 115)
        print(f"{'CURRENT CHEMICAL DEMAND TRACKER (7-DAY TRAILING)':^115}")
        print("=" * 115)
        print(f"{'ID':<4} | {'CAS NUMBER':<12} | {'CHEMICAL NAME':<32} | {'7D ENQUIRIES':<14} | {'THRESHOLD':<12} | {'LAST ALERT'}")
        print("-" * 115)
        for d in demands:
            did, cas, name, count, flag, alert_at = d
            flag_str = "ACTIVE (>=10)" if flag == 1 else "Normal"
            alert_str = str(alert_at)[:19] if alert_at else "None"
            print(f"{did:<4} | {cas:<12} | {name:<32} | {count:<14} | {flag_str:<12} | {alert_str}")
        print("=" * 115)

        # 2. R&D Queue Table
        cur.execute(
            """
            SELECT id, chemical_name, requested_cas, requested_qty_kg, requested_grade, sample_development_status
            FROM rnd_queue
            ORDER BY id DESC;
            """
        )
        rnd_rows = cur.fetchall()

        print("\n" + "=" * 115)
        print(f"{'R&D QUEUE (NEW / UNTRADED MARKET COMPOUNDS)':^115}")
        print("=" * 115)
        print(f"{'ID':<4} | {'CHEMICAL NAME':<40} | {'CAS':<14} | {'QTY (KG)':<12} | {'STATUS':<25}")
        print("-" * 115)
        for r in rnd_rows:
            rid, rname, rcas, rqty, rgrade, rstat = r
            cas_display = rcas if rcas else "N/A"
            qty_display = f"{rqty:.2f}" if rqty is not None else "N/A"
            print(f"{rid:<4} | {rname[:38]:<40} | {cas_display:<14} | {qty_display:<12} | {rstat:<25}")
        print("=" * 115 + "\n")

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process Chemical Enquiries against Master Catalog for Demand & R&D Routing"
    )
    parser.add_argument(
        "--db",
        default="chemicals.db",
        help="Path to SQLite database file (default: chemicals.db)"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=HIGH_DEMAND_THRESHOLD,
        help=f"7-day enquiry alert threshold (default: {HIGH_DEMAND_THRESHOLD})"
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=FUZZY_MATCH_THRESHOLD,
        help=f"Minimum ratio for fuzzy chemical matching (default: {FUZZY_MATCH_THRESHOLD})"
    )
    parser.add_argument(
        "--slack-webhook",
        default=None,
        help="Optional Slack webhook URL for notifications"
    )

    args = parser.parse_args()

    results = process_all_pending_enquiries(
        db_path=args.db,
        threshold=args.threshold,
        fuzzy_threshold=args.fuzzy_threshold,
        slack_webhook_url=args.slack_webhook
    )

    print_demand_and_rnd_summary(args.db)


if __name__ == "__main__":
    main()
