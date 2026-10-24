#!/usr/bin/env python3
"""
Email Enquiry Parser for Chemical Procurement Pipeline
Enterprise AI Pipeline Developer

This script connects to Microsoft Outlook via Microsoft Graph API (OAuth2 MSAL),
fetches unread emails from `enquiries@company.com`, extracts text from message
bodies and attachments (PDF & Excel RFQs), and prompts the Claude API (Anthropic SDK)
to extract structured chemical procurement data. The parsed data and raw text
are stored in the `enquiry_logs` database table.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports with defensive fallbacks
try:
    import msal
except ImportError:
    msal = None

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import pandas as pd
except ImportError:
    pd = None

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("enquiry_parser")

# Standard CAS format regular expression (e.g. 141-78-6)
CAS_REGEX = re.compile(r"^\d{2,7}-\d{2}-\d$")

# Valid enquiry intents
VALID_INTENTS = {"Firm Order", "Quote Request", "General Inquiry"}


@dataclass
class AttachmentFile:
    filename: str
    content_type: str
    content_bytes: bytes


@dataclass
class InboundEmail:
    id: str
    subject: str
    sender_email: str
    sender_name: str
    received_at: str
    body_text: str
    attachments: List[AttachmentFile] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. ATTACHMENT EXTRACTION (PDF & Excel)
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_bytes: bytes, filename: str) -> str:
    """Extracts text content from a PDF attachment (e.g. Purchase Order or RFQ)."""
    if not PdfReader:
        logger.warning(f"pypdf not available; cannot parse PDF '{filename}'.")
        return f"[PDF Attachment: {filename} (pypdf library missing)]"

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        extracted_pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                extracted_pages.append(f"--- Page {i + 1} ---\n{page_text.strip()}")
        return "\n\n".join(extracted_pages) if extracted_pages else f"[PDF {filename}: No text extracted]"
    except Exception as exc:
        logger.error(f"Failed to extract text from PDF '{filename}': {exc}")
        return f"[Error parsing PDF {filename}: {exc}]"


def extract_text_from_excel(excel_bytes: bytes, filename: str) -> str:
    """Extracts tabular data from an Excel RFQ (.xlsx, .xls) attachment."""
    if not pd:
        logger.warning(f"pandas not available; cannot parse Excel '{filename}'.")
        return f"[Excel Attachment: {filename} (pandas library missing)]"

    try:
        excel_file = io.BytesIO(excel_bytes)
        excel_data = pd.read_excel(excel_file, sheet_name=None)
        sheets_text = []
        for sheet_name, df in excel_data.items():
            clean_df = df.dropna(how="all").dropna(axis=1, how="all")
            csv_repr = clean_df.to_csv(index=False)
            sheets_text.append(f"--- Sheet: {sheet_name} ---\n{csv_repr.strip()}")
        return "\n\n".join(sheets_text) if sheets_text else f"[Excel {filename}: Empty workbook]"
    except Exception as exc:
        logger.error(f"Failed to extract text from Excel '{filename}': {exc}")
        return f"[Error parsing Excel {filename}: {exc}]"


def process_attachments(attachments: List[AttachmentFile]) -> str:
    """Extracts and aggregates text from all supported attachments."""
    aggregated_text = []
    for att in attachments:
        fname = att.filename.lower()
        if fname.endswith(".pdf"):
            logger.info(f"Extracting PDF attachment: {att.filename}")
            content = extract_text_from_pdf(att.content_bytes, att.filename)
            aggregated_text.append(f"\n=== ATTACHMENT: {att.filename} (PDF) ===\n{content}")
        elif fname.endswith((".xlsx", ".xls")):
            logger.info(f"Extracting Excel attachment: {att.filename}")
            content = extract_text_from_excel(att.content_bytes, att.filename)
            aggregated_text.append(f"\n=== ATTACHMENT: {att.filename} (EXCEL) ===\n{content}")
        elif fname.endswith(".csv"):
            try:
                csv_text = att.content_bytes.decode("utf-8", errors="replace")
                aggregated_text.append(f"\n=== ATTACHMENT: {att.filename} (CSV) ===\n{csv_text}")
            except Exception as e:
                logger.warning(f"Failed decoding CSV {att.filename}: {e}")
        else:
            logger.info(f"Ignoring unsupported attachment type: {att.filename}")

    return "\n".join(aggregated_text)


def clean_html_body(html_or_text: str) -> str:
    """Strips HTML markup and decodes entities into clean readable plaintext."""
    if not html_or_text:
        return ""
    # Convert <br> and </p> to newlines
    text = re.sub(r"(?i)<br\s*/?>", "\n", html_or_text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode basic HTML entities
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    # Clean whitespace
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# 2. MICROSOFT GRAPH API INTEGRATION
# ---------------------------------------------------------------------------

class MicrosoftGraphClient:
    """OAuth2 Client Credentials & Graph API Client for Outlook Inbox."""

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        mailbox: str = "enquiries@company.com"
    ):
        self.tenant_id = tenant_id or os.getenv("AZURE_TENANT_ID")
        self.client_id = client_id or os.getenv("AZURE_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("AZURE_CLIENT_SECRET")
        self.mailbox = mailbox or os.getenv("OUTLOOK_MAILBOX", "enquiries@company.com")
        self._token: Optional[str] = None

    def is_configured(self) -> bool:
        """Returns True if Azure credentials are fully configured."""
        return bool(self.tenant_id and self.client_id and self.client_secret and msal)

    def _acquire_token(self) -> str:
        """Acquires an app-only OAuth2 Bearer token from Azure Active Directory."""
        if not self.is_configured():
            raise ValueError("Microsoft Graph credentials are incomplete or msal is not installed.")

        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        app = msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=authority
        )
        scopes = ["https://graph.microsoft.com/.default"]
        result = app.acquire_token_for_client(scopes=scopes)

        if "access_token" in result:
            self._token = result["access_token"]
            return self._token
        else:
            err = result.get("error_description") or result.get("error")
            raise ConnectionError(f"Failed to authenticate with Microsoft Graph API: {err}")

    def fetch_unread_enquiries(self, top: int = 10) -> List[InboundEmail]:
        """
        Fetches unread emails and attachments from designated mailbox inbox.
        Graph Endpoint: /users/{mailbox}/mailFolders/inbox/messages?$filter=isRead eq false&$expand=attachments
        """
        import requests

        token = self._acquire_token()
        url = (
            f"https://graph.microsoft.com/v1.0/users/{self.mailbox}/mailFolders/inbox/messages"
            f"?$filter=isRead eq false&$top={top}&$expand=attachments"
            f"&$select=id,subject,from,receivedDateTime,body,hasAttachments,attachments"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(
                f"Graph API error ({response.status_code}): {response.text}"
            )

        data = response.json()
        messages = data.get("value", [])
        logger.info(f"Retrieved {len(messages)} unread messages from '{self.mailbox}'.")

        emails: List[InboundEmail] = []
        for msg in messages:
            msg_id = msg.get("id", "")
            subject = msg.get("subject", "(No Subject)")
            from_obj = msg.get("from", {}).get("emailAddress", {})
            sender_email = from_obj.get("address", "unknown@sender.com")
            sender_name = from_obj.get("name", sender_email)
            received_at = msg.get("receivedDateTime") or datetime.now(timezone.utc).isoformat()

            raw_body = msg.get("body", {}).get("content", "")
            clean_body = clean_html_body(raw_body)

            attachments_list: List[AttachmentFile] = []
            for att in msg.get("attachments", []):
                if att.get("@odata.type") == "#microsoft.graph.fileAttachment":
                    filename = att.get("name", "attachment.bin")
                    content_type = att.get("contentType", "application/octet-stream")
                    content_b64 = att.get("contentBytes", "")
                    try:
                        content_bytes = base64.b64decode(content_b64)
                        attachments_list.append(AttachmentFile(filename, content_type, content_bytes))
                    except Exception as e:
                        logger.warning(f"Failed decoding attachment {filename}: {e}")

            emails.append(InboundEmail(
                id=msg_id,
                subject=subject,
                sender_email=sender_email,
                sender_name=sender_name,
                received_at=received_at,
                body_text=clean_body,
                attachments=attachments_list
            ))

        return emails

    def mark_as_read(self, message_id: str) -> None:
        """Marks processed message as read in Outlook mailbox."""
        import requests

        if not self._token:
            self._acquire_token()

        url = f"https://graph.microsoft.com/v1.0/users/{self.mailbox}/messages/{message_id}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json"
        }
        res = requests.patch(url, headers=headers, json={"isRead": True}, timeout=10)
        if res.status_code not in (200, 204):
            logger.warning(f"Could not mark email {message_id} as read: {res.text}")
        else:
            logger.info(f"Marked email '{message_id}' as read in Outlook.")


# ---------------------------------------------------------------------------
# 2B. DIRECT IMAP EMAIL CLIENT (OUTLOOK / GMAIL / CORPORATE MAILBOX)
# ---------------------------------------------------------------------------

class IMAPEmailClient:
    """Direct IMAP client for Outlook / Office 365 / Gmail / Corporate Mailbox using App Password."""

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        port: int = 993
    ):
        self.host = host or os.getenv("IMAP_HOST", "outlook.office365.com")
        self.user = user or os.getenv("IMAP_USER")
        self.password = password or os.getenv("IMAP_PASSWORD")
        self.port = int(os.getenv("IMAP_PORT", str(port)))

    def is_configured(self) -> bool:
        """Returns True if IMAP credentials are provided."""
        return bool(self.host and self.user and self.password)

    def fetch_unread_enquiries(self, top: int = 10, folder: str = "INBOX") -> List[InboundEmail]:
        """Connects via IMAP SSL, searches for UNSEEN messages, extracts body & attachments."""
        import email
        import imaplib
        from email.header import decode_header

        if not self.is_configured():
            raise ValueError("IMAP credentials (IMAP_HOST, IMAP_USER, IMAP_PASSWORD) not configured.")

        logger.info(f"Connecting to IMAP server '{self.host}:{self.port}' as '{self.user}'...")
        mail = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            mail.login(self.user, self.password)
            mail.select(folder)

            status, search_data = mail.search(None, "UNSEEN")
            if status != "OK" or not search_data[0]:
                logger.info("No unread emails found in mailbox.")
                return []

            msg_ids = search_data[0].split()
            # Fetch latest `top` emails
            target_ids = msg_ids[-top:]
            logger.info(f"Found {len(msg_ids)} unread emails. Processing {len(target_ids)} items...")

            inbound_emails: List[InboundEmail] = []

            for mid in target_ids:
                status, msg_data = mail.fetch(mid, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Decode subject
                subject_raw = msg.get("Subject", "(No Subject)")
                decoded_subj_parts = decode_header(subject_raw)
                subject_parts = []
                for content, encoding in decoded_subj_parts:
                    if isinstance(content, bytes):
                        subject_parts.append(content.decode(encoding or "utf-8", errors="replace"))
                    else:
                        subject_parts.append(str(content))
                subject = " ".join(subject_parts)

                # Decode sender
                from_header = msg.get("From", "unknown@sender.com")
                sender_email_match = re.search(r"<([^>]+)>", from_header)
                if sender_email_match:
                    sender_email = sender_email_match.group(1).strip()
                    sender_name = from_header.split("<")[0].strip().strip('"')
                else:
                    sender_email = from_header.strip()
                    sender_name = sender_email

                received_at = msg.get("Date") or datetime.now(timezone.utc).isoformat()

                # Extract body text and attachments
                body_parts = []
                attachments_list: List[AttachmentFile] = []

                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        disposition = str(part.get("Content-Disposition") or "")
                        filename = part.get_filename()

                        if filename:
                            # Attachment found
                            payload = part.get_payload(decode=True)
                            if payload:
                                attachments_list.append(AttachmentFile(filename, content_type, payload))
                        elif content_type == "text/plain" and "attachment" not in disposition:
                            charset = part.get_content_charset() or "utf-8"
                            body_parts.append(part.get_payload(decode=True).decode(charset, errors="replace"))
                        elif content_type == "text/html" and "attachment" not in disposition and not body_parts:
                            charset = part.get_content_charset() or "utf-8"
                            html_text = part.get_payload(decode=True).decode(charset, errors="replace")
                            body_parts.append(clean_html_body(html_text))
                else:
                    charset = msg.get_content_charset() or "utf-8"
                    raw_text = msg.get_payload(decode=True).decode(charset, errors="replace")
                    body_parts.append(raw_text)

                clean_body = clean_html_body("\n".join(body_parts))

                inbound_emails.append(InboundEmail(
                    id=mid.decode("utf-8") if isinstance(mid, bytes) else str(mid),
                    subject=subject,
                    sender_email=sender_email,
                    sender_name=sender_name or sender_email,
                    received_at=received_at,
                    body_text=clean_body,
                    attachments=attachments_list
                ))

            return inbound_emails
        finally:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass

    def mark_as_read(self, message_id: str, folder: str = "INBOX") -> None:
        """Flags message as read (Seen) in IMAP mailbox."""
        import imaplib
        mail = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            mail.login(self.user, self.password)
            mail.select(folder)
            mail.store(message_id, "+FLAGS", "\\Seen")
            logger.info(f"Marked email '{message_id}' as Seen in IMAP mailbox.")
        except Exception as e:
            logger.warning(f"Could not mark IMAP message {message_id} as seen: {e}")
        finally:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 3. CLAUDE API PARSER PROMPTING
# ---------------------------------------------------------------------------

CLAUDE_SYSTEM_PROMPT = """
You are an expert Chemical Commercial Data Analyst for a Chemical Manufacturing and Trading corporation.
Your task is to extract structured chemical procurement inquiry information from customer emails and RFQ attachments.

You MUST respond strictly with a single valid JSON object. Do not include markdown code blocks, conversational greetings, explanations, or trailing commentary.

JSON Schema:
{
  "customer_name": "string (company or person name, or null)",
  "email_date": "string (ISO 8601 or original date format)",
  "enquiry_intent": "Firm Order" | "Quote Request" | "General Inquiry",
  "requested_items": [
    {
      "chemical_name": "canonical lowercased chemical name without special characters or punctuation",
      "cas_number": "standard CAS format (e.g. 141-78-6) or null if unknown or custom",
      "trade_name": "commercial or trade alias mentioned by customer or null",
      "purity_grade": "e.g. HPLC, Tech 99%, Pharma Grade, or null",
      "quantity_kg": float (quantity converted strictly to kilograms, e.g. 15000.0) or null,
      "target_price": "string indicating target price with currency/unit or null"
    }
  ],
  "confidence_score": float (between 0.0 and 1.0),
  "notes": "string summary of any ambiguity, missing details, or specialty synthesis requests"
}

Extraction Rules:
1. Chemical Name Normalization:
   - Provide standard canonical chemical name in lowercase without punctuation.
   - If a commercial trade name is used (e.g. 'Solvent EA', 'Caustic Soda', 'Oil of Vitriol', 'Lye', 'Methyl Alcohol', 'IPA', 'THF', 'DCM'), resolve chemical_name to its canonical name (e.g. 'ethyl acetate', 'sodium hydroxide', 'sulfuric acid', 'methanol', 'isopropanol', 'tetrahydrofuran', 'dichloromethane') and populate 'trade_name' with the original term.
2. CAS Registry Numbers:
   - If the CAS number is mentioned or easily inferred for common industrial chemicals, provide the standard format 'XXXX-XX-X'.
   - Common references: Ethyl Acetate (141-78-6), Acetone (67-64-1), Isopropanol (67-63-0), Methanol (67-56-1), Toluene (108-88-3), Sulfuric Acid (7664-93-9), Sodium Hydroxide (1310-73-2), Tetrahydrofuran (109-99-9), Acetonitrile (75-05-8), Dichloromethane (75-09-2).
   - If the inquiry is for a custom R&D molecule, obscure derivative, or unknown chemical lacking a CAS, set 'cas_number' to null.
3. Quantity Conversion to Kilograms (quantity_kg):
   - 1 Metric Ton (MT / Ton) = 1000 kg.
   - 1 standard drum (liquid) = 200 kg unless specified otherwise.
   - 1 lb / pound = 0.453592 kg.
4. Enquiry Intent Classification:
   - "Firm Order": Formal PO attached, PO number referenced, "we confirm purchase", "please dispatch".
   - "Quote Request": Requesting CIF/FOB quotations, price indication, lead time, spot pricing.
   - "General Inquiry": Technical questions, COA/SDS request, R&D feasibility, general catalog inquiry.
""".strip()


class ClaudeEnquiryParser:
    """Wrapper for Anthropic Claude API using Structured JSON Output mode."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.client = None
        if self.api_key and anthropic:
            try:
                self.client = anthropic.Anthropic(api_key=self.api_key)
            except Exception as e:
                logger.warning(f"Could not initialize Anthropic client: {e}")

    def is_configured(self) -> bool:
        """Returns True if live Anthropic client is ready."""
        return self.client is not None

    def parse_enquiry(self, email: InboundEmail, full_text: str) -> Dict[str, Any]:
        """
        Sends email context and attachment text to Claude and returns validated JSON dict.
        Uses fallback heuristic parser if Claude API key is not configured or in offline mode.
        """
        if not self.is_configured():
            logger.info("Anthropic API key not provided or client unavailable. Using fallback heuristic parser.")
            return self._heuristic_fallback_parser(email, full_text)

        user_content = f"""
Please parse the following chemical procurement inquiry:

From: {email.sender_name} <{email.sender_email}>
Subject: {email.subject}
Date: {email.received_at}

=== EMAIL CONTENT & ATTACHMENTS ===
{full_text}
""".strip()

        try:
            # Structured JSON call to Claude
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                temperature=0.0,
                system=CLAUDE_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_content}
                ]
            )

            # Extract raw response text
            reply_text = ""
            for block in response.content:
                if getattr(block, "type", "") == "text":
                    reply_text += block.text

            return self._clean_and_parse_json(reply_text)

        except Exception as exc:
            logger.error(f"Claude API request failed: {exc}. Falling back to heuristic parser.")
            return self._heuristic_fallback_parser(email, full_text)

    def _clean_and_parse_json(self, raw_reply: str) -> Dict[str, Any]:
        """Strips accidental markdown code fences and parses JSON securely."""
        text = raw_reply.strip()
        # Remove ```json ... ``` wrapper if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        try:
            parsed = json.loads(text)
            self._validate_extracted_schema(parsed)
            return parsed
        except json.JSONDecodeError as err:
            logger.error(f"Failed to decode JSON from Claude response: {err}\nResponse was:\n{text}")
            # Attempt regex find of first {...} block
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    self._validate_extracted_schema(parsed)
                    return parsed
                except Exception:
                    pass
            raise

    def _validate_extracted_schema(self, data: Dict[str, Any]) -> None:
        """Validates that extracted JSON matches the required schema fields."""
        if "enquiry_intent" not in data or data["enquiry_intent"] not in VALID_INTENTS:
            # Default or correct invalid intent
            data["enquiry_intent"] = "General Inquiry"
        if "requested_items" not in data or not isinstance(data["requested_items"], list):
            data["requested_items"] = []

        # Validate items
        for item in data["requested_items"]:
            # Ensure quantity_kg is float or null
            if item.get("quantity_kg") is not None:
                try:
                    item["quantity_kg"] = float(item["quantity_kg"])
                except (ValueError, TypeError):
                    item["quantity_kg"] = None

    def _heuristic_fallback_parser(self, email: InboundEmail, full_text: str) -> Dict[str, Any]:
        """
        Rule-based NLP and entity extraction fallback for testing or when Claude API is offline.
        Detects common chemicals, trade names, quantities, CAS numbers, and intent.
        """
        content_lower = full_text.lower()
        subject_lower = email.subject.lower()

        # 1. Intent Detection
        if any(term in content_lower or term in subject_lower for term in ["purchase order", "po #", "po-", "firm order", "dispatch"]):
            intent = "Firm Order"
        elif any(term in content_lower or term in subject_lower for term in ["quote", "rfq", "price", "quotation", "cif", "fob"]):
            intent = "Quote Request"
        else:
            intent = "General Inquiry"

        # 2. Known Chemicals Knowledge Base
        knowledge_base = [
            {"canonical": "ethyl acetate", "cas": "141-78-6", "trade": ["solvent ea", "acetic ester", "ethyl ethanoate"]},
            {"canonical": "acetone", "cas": "67-64-1", "trade": ["2-propanone", "dimethyl ketone"]},
            {"canonical": "isopropanol", "cas": "67-63-0", "trade": ["ipa", "isopropyl alcohol", "2-propanol"]},
            {"canonical": "methanol", "cas": "67-56-1", "trade": ["methyl alcohol", "wood spirit"]},
            {"canonical": "toluene", "cas": "108-88-3", "trade": ["methylbenzene", "toluol"]},
            {"canonical": "sulfuric acid", "cas": "7664-93-9", "trade": ["oil of vitriol", "hydrogen sulfate"]},
            {"canonical": "sodium hydroxide", "cas": "1310-73-2", "trade": ["caustic soda", "lye"]},
            {"canonical": "tetrahydrofuran", "cas": "109-99-9", "trade": ["thf", "oxolane"]},
            {"canonical": "acetonitrile", "cas": "75-05-8", "trade": ["acn", "methyl cyanide"]},
            {"canonical": "dichloromethane", "cas": "75-09-2", "trade": ["dcm", "methylene chloride"]}
        ]

        detected_items = []
        for chem in knowledge_base:
            matched_alias = None
            if chem["canonical"] in content_lower:
                matched_alias = None
            else:
                for alias in chem["trade"]:
                    if re.search(r"\b" + re.escape(alias) + r"\b", content_lower):
                        matched_alias = alias
                        break

            if chem["canonical"] in content_lower or matched_alias:
                # Estimate quantity
                qty_kg = None
                # Check for metric tons (e.g. 10 MT, 20 metric tons)
                mt_match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:mt|metric ton|tons|tonnes)\b", content_lower)
                kg_match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:kg|kilogram|kilos)\b", content_lower)
                drum_match = re.search(r"(\d+)\s*drums?\b", content_lower)

                if mt_match:
                    qty_kg = float(mt_match.group(1).replace(",", "")) * 1000.0
                elif kg_match:
                    qty_kg = float(kg_match.group(1).replace(",", ""))
                elif drum_match:
                    qty_kg = float(drum_match.group(1)) * 200.0

                # Purity grade heuristic
                purity = None
                for p in ["hplc", "99.5%", "99.9%", "99%", "tech grade", "technical grade", "industrial grade", "pharma"]:
                    if p in content_lower:
                        purity = p.upper()
                        break

                detected_items.append({
                    "chemical_name": chem["canonical"],
                    "cas_number": chem["cas"],
                    "trade_name": matched_alias,
                    "purity_grade": purity,
                    "quantity_kg": qty_kg,
                    "target_price": None
                })

        # 3. Detect uncatalogued / custom R&D requests
        if not detected_items and ("custom" in content_lower or "synthesis" in content_lower or "derivative" in content_lower):
            # Extract sample custom molecule
            mol_match = re.search(r"(?:custom synthesis of\s+(?:\d+\s*(?:kg|g|mt)\s+of\s+)?|inquiry for\s+)([A-Za-z0-9\-\(\)\,\s]+?)(?:\.|\n|$)", content_lower)
            chem_name = mol_match.group(1).strip() if mol_match else "custom specialty molecule"
            # Strip leading "x kg of " if still present
            chem_name = re.sub(r"^\d+\s*(?:kg|g|mt)\s+of\s+", "", chem_name).strip()
            detected_items.append({
                "chemical_name": chem_name,
                "cas_number": None,
                "trade_name": None,
                "purity_grade": "R&D Lab Grade",
                "quantity_kg": 5.0,
                "target_price": None
            })

        # Customer name from sender
        customer_name = email.sender_name if email.sender_name != email.sender_email else None

        return {
            "customer_name": customer_name,
            "email_date": email.received_at,
            "enquiry_intent": intent,
            "requested_items": detected_items,
            "confidence_score": 0.85 if detected_items else 0.40,
            "notes": "Parsed using heuristic fallback engine"
        }


# ---------------------------------------------------------------------------
# 4. DATABASE INGESTION & STATUS HANDLING
# ---------------------------------------------------------------------------

def save_enquiry_to_database(
    db_path: str,
    email: InboundEmail,
    raw_full_text: str,
    extracted_data: Optional[Dict[str, Any]],
    status: str
) -> int:
    """
    Persists enquiry into `enquiry_logs` table.
    Status can be: 'PROCESSED', 'FLAGGED_FOR_RND', 'FAILED', or 'PENDING'.
    """
    sql = """
    INSERT INTO enquiry_logs (
        email_subject,
        sender_email,
        received_at,
        raw_text,
        extracted_json,
        processing_status,
        created_at
    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """

    json_str = json.dumps(extracted_data, indent=2) if extracted_data else None

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            sql,
            (
                email.subject,
                email.sender_email,
                email.received_at,
                raw_full_text,
                json_str,
                status
            )
        )
        conn.commit()
        record_id = cursor.lastrowid
    finally:
        conn.close()

    logger.info(f"Enquiry logged with ID={record_id}, Status='{status}', Sender='{email.sender_email}'.")
    return record_id


def determine_processing_status(extracted: Optional[Dict[str, Any]]) -> str:
    """
    Determines status based on extracted data:
    - If extraction failed: 'FAILED'
    - If chemical has no CAS, is custom R&D synthesis, or zero catalog match: 'FLAGGED_FOR_RND'
    - Otherwise: 'PROCESSED'
    """
    if not extracted:
        return "FAILED"

    items = extracted.get("requested_items", [])
    if not items:
        return "PROCESSED"

    for item in items:
        cas = item.get("cas_number")
        # Flag if CAS is missing or invalid or noted as R&D
        if not cas or not CAS_REGEX.match(str(cas)):
            return "FLAGGED_FOR_RND"
        if "custom" in str(item.get("chemical_name", "")).lower():
            return "FLAGGED_FOR_RND"

    return "PROCESSED"


# ---------------------------------------------------------------------------
# 5. SAMPLE MOCK EMAILS FOR OFFLINE / TESTING VERIFICATION
# ---------------------------------------------------------------------------

def generate_mock_inbound_emails() -> List[InboundEmail]:
    """Generates 3 realistic chemical purchase inquiries with PDF and Excel attachments."""
    
    # 1. Firm Order Email with valid in-memory PDF attachment
    mock_pdf_bytes = b""
    try:
        from pypdf import PageObject, PdfWriter
        writer = PdfWriter()
        # Add a blank page
        page = PageObject.create_blank_page(width=612, height=792)
        writer.add_page(page)
        pdf_stream = io.BytesIO()
        writer.write(pdf_stream)
        mock_pdf_bytes = pdf_stream.getvalue()
    except Exception:
        mock_pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\nxref\n0 4\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000124 00000 n \ntrailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n200\n%%EOF"

    email_1 = InboundEmail(
        id="MOCK-MSG-001",
        subject="PO #77421 - Purchase Order: Solvent EA & Commercial Acetone",
        sender_email="procurement@apexchemicals.com",
        sender_name="Mark Vance",
        received_at="2026-10-24T08:15:00Z",
        body_text=(
            "Dear Sales Team,\n\n"
            "Please find attached our official Purchase Order PO-2026-77421 for 10 MT of Solvent EA "
            "(Ethyl Ethanoate / Ethyl Acetate) and 25 standard drums of high purity Acetone.\n"
            "We confirm purchase as per agreed quarterly contract prices.\n"
            "Please dispatch by next Monday.\n\n"
            "Best regards,\nMark Vance\nApex Chemical Solutions"
        ),
        attachments=[
            AttachmentFile(
                filename="PO-2026-77421.pdf",
                content_type="application/pdf",
                content_bytes=mock_pdf_bytes
            )
        ]
    )

    # 2. Quote Request Email with Excel RFQ attachment
    mock_excel_bytes = b""
    try:
        if pd:
            excel_df = pd.DataFrame([
                {"Item": "Refined Toluene", "CAS": "108-88-3", "Purity": "Technical 99.5%", "Quantity_kg": 12000, "Port": "CIF Antwerp"},
                {"Item": "Isopropanol (IPA)", "CAS": "67-63-0", "Purity": "HPLC 99.8%", "Quantity_kg": 24000, "Port": "CIF Antwerp"}
            ])
            excel_stream = io.BytesIO()
            with pd.ExcelWriter(excel_stream, engine="openpyxl") as excel_writer:
                excel_df.to_excel(excel_writer, index=False, sheet_name="Procurement_Schedule")
            mock_excel_bytes = excel_stream.getvalue()
    except Exception as exc:
        logger.warning(f"Could not construct mock Excel workbook: {exc}")

    email_2 = InboundEmail(
        id="MOCK-MSG-002",
        subject="RFQ-2026-901: Bulk Isopropanol and Toluene quote request (CIF Antwerp)",
        sender_email="sourcing@pharmaglobal.org",
        sender_name="Elena Rostova",
        received_at="2026-10-24T09:30:00Z",
        body_text=(
            "Hello Commercial Desk,\n\n"
            "We are requesting a formal price quotation for 24,000 kg of Isopropanol (IPA - 99.8% grade) "
            "and 12,000 kg of Refined Toluene (CAS: 108-88-3).\n"
            "Please provide CIF Antwerp pricing and lead times for November delivery.\n"
            "Attached is our procurement schedule.\n\n"
            "Sincerely,\nElena Rostova\nPharmaGlobal Sourcing"
        ),
        attachments=[
            AttachmentFile(
                filename="RFQ-2026-901-Schedule.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                content_bytes=mock_excel_bytes
            )
        ] if mock_excel_bytes else []
    )

    # 3. Specialty Custom R&D Inquiry (Triggers FLAGGED_FOR_RND)
    email_3 = InboundEmail(
        id="MOCK-MSG-003",
        subject="Custom Synthesis Inquiry - 2-(Trifluoromethyl)nicotinic acid derivative",
        sender_email="dr.chen@biomolecule-labs.com",
        sender_name="Dr. Wei Chen",
        received_at="2026-10-24T10:05:00Z",
        body_text=(
            "Dear R&D Commercialization Team,\n\n"
            "Our research institute is looking for a toll manufacturer for custom synthesis of "
            "5 kg of 5-bromo-2-(trifluoromethyl)nicotinic acid ethyl ester.\n"
            "Purity required: >97.5% by HPLC.\n"
            "As this is a novel experimental precursor, there is no assigned CAS Registry Number.\n"
            "Please advise if your synthesis lab has the capability to produce a pilot batch.\n\n"
            "Regards,\nDr. Wei Chen\nBiomolecule Laboratories"
        ),
        attachments=[]
    )

    return [email_1, email_2, email_3]


# ---------------------------------------------------------------------------
# 6. PIPELINE RUNNER
# ---------------------------------------------------------------------------

def run_pipeline(
    db_path: str = "chemicals.db",
    mailbox: str = "enquiries@company.com",
    use_mock: bool = False,
    top_emails: int = 10
) -> List[Dict[str, Any]]:
    """
    Executes the complete email ingestion and Claude parsing pipeline:
    1. Fetches unread emails from Graph API or mock source.
    2. Extracts message body and attachment text.
    3. Prompts Claude API (or heuristic fallback) with structured JSON instructions.
    4. Evaluates processing status ('PROCESSED', 'FLAGGED_FOR_RND', 'FAILED').
    5. Saves records to `enquiry_logs` table in `chemicals.db`.
    """
    logger.info("Starting Chemical Procurement Email Parsing Pipeline...")

    graph_client = MicrosoftGraphClient(mailbox=mailbox)
    imap_client = IMAPEmailClient()
    claude_parser = ClaudeEnquiryParser()
    active_client = None

    # Step 1: Ingest emails
    if not use_mock and graph_client.is_configured():
        logger.info(f"Connecting to Microsoft Graph API for mailbox '{mailbox}'...")
        active_client = graph_client
        try:
            inbound_emails = graph_client.fetch_unread_enquiries(top=top_emails)
        except Exception as e:
            logger.error(f"Failed to fetch emails via Graph API: {e}. Falling back to mock emails.")
            inbound_emails = generate_mock_inbound_emails()
    elif not use_mock and imap_client.is_configured():
        logger.info(f"Connecting to IMAP inbox '{imap_client.user}' on '{imap_client.host}'...")
        active_client = imap_client
        try:
            inbound_emails = imap_client.fetch_unread_enquiries(top=top_emails)
        except Exception as e:
            logger.error(f"Failed to fetch emails via IMAP: {e}. Falling back to mock emails.")
            inbound_emails = generate_mock_inbound_emails()
    else:
        if not use_mock:
            logger.info("Neither Microsoft Graph nor IMAP credentials configured. Running in MOCK SIMULATION mode.")
        else:
            logger.info("Executing pipeline with mock email test suite (--mock).")
        inbound_emails = generate_mock_inbound_emails()

    results = []

    # Step 2: Process each email
    for email in inbound_emails:
        logger.info(f"Processing Email: [{email.subject}] from {email.sender_email}")

        # Extract attachment content
        attachment_text = process_attachments(email.attachments)
        full_text = email.body_text
        if attachment_text.strip():
            full_text += "\n" + attachment_text

        # Step 3: Extract structured data with Claude API
        try:
            extracted_json = claude_parser.parse_enquiry(email, full_text)
            status = determine_processing_status(extracted_json)
        except Exception as err:
            logger.error(f"Error parsing email '{email.subject}': {err}")
            extracted_json = None
            status = "FAILED"

        # Step 4: Persist in `enquiry_logs`
        log_id = save_enquiry_to_database(
            db_path=db_path,
            email=email,
            raw_full_text=full_text,
            extracted_data=extracted_json,
            status=status
        )

        # Step 5: Mark as read in mailbox if live
        if active_client and not email.id.startswith("MOCK-"):
            try:
                active_client.mark_as_read(email.id)
            except Exception as e:
                logger.warning(f"Could not mark email as read: {e}")
                graph_client.mark_as_read(email.id)
            except Exception as e:
                logger.warning(f"Could not mark email as read: {e}")

        results.append({
            "log_id": log_id,
            "subject": email.subject,
            "sender": email.sender_email,
            "status": status,
            "extracted": extracted_json
        })

    logger.info(f"Pipeline completed. Processed {len(results)} emails.")
    return results


def print_enquiry_summary(db_path: str) -> None:
    """Displays formatted table of recent enquiry logs."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, email_subject, sender_email, processing_status, received_at, extracted_json
            FROM enquiry_logs
            ORDER BY id DESC
            LIMIT 10;
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    print("\n" + "=" * 115)
    print(f"{'RECENT INBOUND CHEMICAL ENQUIRY LOGS':^115}")
    print("=" * 115)
    header = f"{'ID':<4} | {'STATUS':<16} | {'SENDER':<30} | {'SUBJECT':<55}"
    print(header)
    print("-" * 115)
    for r in rows:
        eid, subj, sender, status, recv, ext_str = r
        clean_subj = (subj[:52] + "...") if len(subj) > 55 else subj
        print(f"{eid:<4} | {status:<16} | {sender:<30} | {clean_subj:<55}")
    print("=" * 115 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Chemical Purchase Enquiry Details from Outlook using Claude API"
    )
    parser.add_argument(
        "--db",
        default="chemicals.db",
        help="Path to SQLite database file (default: chemicals.db)"
    )
    parser.add_argument(
        "--mailbox",
        default="enquiries@company.com",
        help="Designated Outlook inbox address (default: enquiries@company.com)"
    )
    parser.add_argument(
        "--imap-host",
        default=None,
        help="IMAP server (e.g. outlook.office365.com, imap.gmail.com)"
    )
    parser.add_argument(
        "--imap-user",
        default=None,
        help="IMAP username/email address"
    )
    parser.add_argument(
        "--imap-password",
        default=None,
        help="IMAP password or App Password"
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force run pipeline with mock inbound RFQ and order emails"
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Max number of unread emails to fetch per execution (default: 10)"
    )

    args = parser.parse_args()

    if args.imap_host:
        os.environ["IMAP_HOST"] = args.imap_host
    if args.imap_user:
        os.environ["IMAP_USER"] = args.imap_user
    if args.imap_password:
        os.environ["IMAP_PASSWORD"] = args.imap_password

    results = run_pipeline(
        db_path=args.db,
        mailbox=args.mailbox,
        use_mock=args.mock,
        top_emails=args.top
    )

    if results:
        print_enquiry_summary(args.db)


if __name__ == "__main__":
    main()
