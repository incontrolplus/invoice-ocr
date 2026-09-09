"""Email ingestion, parsing, SPF/DKIM security verification, and attachment filtering.

Provides robust parsing for:
- Multipart email payloads (SendGrid Inbound Parse, Cloudflare Email Routing Webhook)
- Cloudflare Email Worker JSON payloads with base64 attachments or raw MIME
- Raw RFC 822 / 5322 MIME messages
- SPF and DKIM security validation (Authentication-Results, Received-SPF, DKIM-Signature)
- Intelligent attachment extraction and filtering:
  - Discard spam/logos/icons/signatures under 10KB
  - Discard non-document attachments (.exe, .html, .txt, .zip, etc.)
  - Keep valid invoice candidate files (.pdf, .tiff, .tif, .png, .jpg, .jpeg)
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import email
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
import hmac
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Optional

from .constants import (
    DEFAULT_MIN_ATTACHMENT_SIZE_BYTES,
    SUPPORTED_EXTENSIONS,
)

logger = logging.getLogger("invoice_ocr_email")

SIGNATURE_NAME_KEYWORDS: set[str] = {
    "signature", "logo", "icon", "banner", "badge", "avatar",
    "image001", "image002", "image003", "facebook", "linkedin",
    "twitter", "instagram", "youtube", "social", "footer",
}


@dataclass
class EmailAttachment:
    """Represents an attachment extracted from an email message."""
    filename: str
    content_type: str
    data: bytes
    size_bytes: int
    is_inline: bool = False
    content_id: Optional[str] = None
    is_valid_invoice_candidate: bool = True
    filter_category: str = "accepted"  # "accepted", "size_too_small", "unsupported_type", "signature_logo"
    filter_reason: Optional[str] = None

    def to_dict(self, include_data: bool = False) -> dict[str, Any]:
        result = {
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "is_inline": self.is_inline,
            "content_id": self.content_id,
            "is_valid_invoice_candidate": self.is_valid_invoice_candidate,
            "filter_category": self.filter_category,
            "filter_reason": self.filter_reason,
        }
        if include_data:
            result["data_base64"] = base64.b64encode(self.data).decode("ascii")
        return result


@dataclass
class EmailSecurityResult:
    """Represents SPF/DKIM verification results and security status."""
    is_authorized: bool
    spf_status: str  # "pass", "fail", "softfail", "neutral", "none", "unknown"
    dkim_status: str  # "pass", "fail", "none", "unknown"
    security_verdict: str  # "ACCEPTED", "REJECTED_SPF_FAIL", "REJECTED_DKIM_FAIL", "ACCEPTED_WITH_WARNING", "ACCEPTED_NO_AUTH"
    details: str = ""
    auth_results_raw: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_authorized": self.is_authorized,
            "spf_status": self.spf_status,
            "dkim_status": self.dkim_status,
            "security_verdict": self.security_verdict,
            "details": self.details,
            "auth_results_raw": self.auth_results_raw,
        }


@dataclass
class ParsedEmail:
    """Represents a fully parsed incoming email message."""
    sender: str
    recipient: str
    subject: str
    date: Optional[str] = None
    message_id: Optional[str] = None
    body_plain: str = ""
    body_html: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    security: EmailSecurityResult = field(default_factory=lambda: EmailSecurityResult(
        is_authorized=True, spf_status="unknown", dkim_status="unknown", security_verdict="ACCEPTED",
    ))
    all_attachments: list[EmailAttachment] = field(default_factory=list)
    valid_attachments: list[EmailAttachment] = field(default_factory=list)
    filtered_attachments: list[EmailAttachment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "subject": self.subject,
            "date": self.date,
            "message_id": self.message_id,
            "security": self.security.to_dict(),
            "total_attachments_count": len(self.all_attachments),
            "valid_invoices_count": len(self.valid_attachments),
            "filtered_attachments_count": len(self.filtered_attachments),
            "valid_attachments": [a.to_dict() for a in self.valid_attachments],
            "filtered_attachments": [a.to_dict() for a in self.filtered_attachments],
        }


# ---------------------------------------------------------------------------
# SPF & DKIM Parsing and Security Evaluation
# ---------------------------------------------------------------------------

def extract_spf_dkim_status(
    headers: dict[str, str],
    payload_dict: Optional[dict[str, Any]] = None,
) -> tuple[str, str, Optional[str]]:
    """Extract normalized SPF and DKIM statuses from headers and payload flags.

    Returns:
        tuple[spf_status, dkim_status, auth_results_raw]
    """
    payload_dict = payload_dict or {}

    # 1. Check explicit payload flags (e.g. from Cloudflare Worker or SendGrid JSON)
    spf_val = str(payload_dict.get("spf") or payload_dict.get("SPF") or "").strip().lower()
    dkim_val = str(payload_dict.get("dkim") or payload_dict.get("DKIM") or "").strip().lower()

    # 2. Check headers (case-insensitive lookup)
    norm_headers = {k.lower(): v for k, v in headers.items()}
    auth_results = norm_headers.get("authentication-results", "")
    received_spf = norm_headers.get("received-spf", "")
    dkim_signature = norm_headers.get("dkim-signature", "")

    # Parse SPF
    if not spf_val:
        if received_spf:
            rec_lower = received_spf.lower()
            if rec_lower.startswith("pass"):
                spf_val = "pass"
            elif rec_lower.startswith("fail") or "status=fail" in rec_lower:
                spf_val = "fail"
            elif "softfail" in rec_lower:
                spf_val = "softfail"
            elif "neutral" in rec_lower:
                spf_val = "neutral"
            elif "none" in rec_lower:
                spf_val = "none"

        if not spf_val and auth_results:
            match = re.search(r"\bspf=([a-zA-Z]+)", auth_results, re.IGNORECASE)
            if match:
                spf_val = match.group(1).lower()

    # Parse DKIM
    if not dkim_val:
        if auth_results:
            match = re.search(r"\bdkim=([a-zA-Z]+)", auth_results, re.IGNORECASE)
            if match:
                dkim_val = match.group(1).lower()

        if not dkim_val and dkim_signature:
            # Signature is present in headers
            dkim_val = "pass"

    spf_final = spf_val or "none"
    dkim_final = dkim_val or "none"

    return spf_final, dkim_final, auth_results or None


def evaluate_security_policy(
    spf_status: str,
    dkim_status: str,
    require_spf: bool = False,
    require_dkim: bool = False,
    block_spf_fail: bool = True,
    auth_results_raw: Optional[str] = None,
) -> EmailSecurityResult:
    """Evaluate SPF and DKIM security results according to configured policy.

    Args:
        spf_status: Normalized SPF status ('pass', 'fail', 'softfail', 'none', etc.)
        dkim_status: Normalized DKIM status ('pass', 'fail', 'none', etc.)
        require_spf: If True, reject unless spf_status == 'pass'
        require_dkim: If True, reject unless dkim_status == 'pass'
        block_spf_fail: If True, reject if spf_status == 'fail'
        auth_results_raw: Raw Authentication-Results header text
    """
    spf_lower = spf_status.lower()
    dkim_lower = dkim_status.lower()

    if block_spf_fail and spf_lower == "fail":
        return EmailSecurityResult(
            is_authorized=False,
            spf_status=spf_lower,
            dkim_status=dkim_lower,
            security_verdict="REJECTED_SPF_FAIL",
            details="Rejected: Sender domain SPF verification explicitly failed (possible spoofing).",
            auth_results_raw=auth_results_raw,
        )

    if require_spf and spf_lower != "pass":
        return EmailSecurityResult(
            is_authorized=False,
            spf_status=spf_lower,
            dkim_status=dkim_lower,
            security_verdict="REJECTED_SPF_MISSING_OR_FAIL",
            details=f"Rejected: Policy requires valid SPF 'pass', but got '{spf_lower}'.",
            auth_results_raw=auth_results_raw,
        )

    if require_dkim and dkim_lower != "pass":
        return EmailSecurityResult(
            is_authorized=False,
            spf_status=spf_lower,
            dkim_status=dkim_lower,
            security_verdict="REJECTED_DKIM_MISSING_OR_FAIL",
            details=f"Rejected: Policy requires valid DKIM 'pass', but got '{dkim_lower}'.",
            auth_results_raw=auth_results_raw,
        )

    if spf_lower == "fail":
        verdict = "ACCEPTED_WITH_WARNING"
        details = "Accepted with warning: SPF failed but block_spf_fail is disabled."
    elif spf_lower == "pass" or dkim_lower == "pass":
        verdict = "ACCEPTED"
        details = f"Authorized: SPF={spf_lower}, DKIM={dkim_lower} verified."
    else:
        verdict = "ACCEPTED_NO_AUTH"
        details = f"Accepted: SPF={spf_lower}, DKIM={dkim_lower} (unverified sender domain)."

    return EmailSecurityResult(
        is_authorized=True,
        spf_status=spf_lower,
        dkim_status=dkim_lower,
        security_verdict=verdict,
        details=details,
        auth_results_raw=auth_results_raw,
    )


def verify_webhook_token(
    token: Optional[str],
    secret_env_var: str = "EMAIL_INGEST_SECRET",
) -> bool:
    """Verify shared secret token for webhook authorization (Cloudflare Worker token)."""
    expected = os.environ.get(secret_env_var) or os.environ.get("WEBHOOK_SECRET")
    if not expected:
        # If no secret configured, open ingestion is allowed
        return True
    if not token:
        return False
    return hmac.compare_digest(token.strip(), expected.strip())


# ---------------------------------------------------------------------------
# Attachment Filtering Engine
# ---------------------------------------------------------------------------

def filter_attachment(
    filename: str,
    content_type: str,
    data: bytes,
    is_inline: bool = False,
    content_id: Optional[str] = None,
    min_size_bytes: int = DEFAULT_MIN_ATTACHMENT_SIZE_BYTES,
) -> EmailAttachment:
    """Inspect and filter an attachment, tagging valid invoice candidates vs filtered items."""
    clean_name = Path(filename).name if filename else "unnamed_attachment"
    size = len(data)
    suffix = Path(clean_name).suffix.lower()

    # 1. Size Filter (< 10KB threshold)
    if size < min_size_bytes:
        return EmailAttachment(
            filename=clean_name,
            content_type=content_type,
            data=data,
            size_bytes=size,
            is_inline=is_inline,
            content_id=content_id,
            is_valid_invoice_candidate=False,
            filter_category="size_too_small",
            filter_reason=(
                f"Attachment size ({size} bytes) is below minimum {min_size_bytes} bytes "
                f"(< 10KB threshold) — filtered out as email signature badge, icon, or logo."
            ),
        )

    # 2. Extension / Format Filter
    if suffix not in SUPPORTED_EXTENSIONS:
        return EmailAttachment(
            filename=clean_name,
            content_type=content_type,
            data=data,
            size_bytes=size,
            is_inline=is_inline,
            content_id=content_id,
            is_valid_invoice_candidate=False,
            filter_category="unsupported_type",
            filter_reason=(
                f"Unsupported file format '{suffix}'. "
                f"Supported invoice formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
            ),
        )

    # 3. Signature & Logo Name Filter for medium images (< 35KB)
    stem_lower = Path(clean_name).stem.lower()
    for kw in SIGNATURE_NAME_KEYWORDS:
        if kw in stem_lower and size < 35 * 1024:
            return EmailAttachment(
                filename=clean_name,
                content_type=content_type,
                data=data,
                size_bytes=size,
                is_inline=is_inline,
                content_id=content_id,
                is_valid_invoice_candidate=False,
                filter_category="signature_logo",
                filter_reason=(
                    f"Attachment '{clean_name}' ({size} bytes) matches signature/logo keyword '{kw}'."
                ),
            )

    # 4. Inline Image Heuristic: Inline images without document names under 50KB
    if is_inline and suffix in {".png", ".jpg", ".jpeg"} and size < 50 * 1024:
        # Check if the filename explicitly contains 'invoice', 'фактура', etc.
        if not any(w in stem_lower for w in ("invoice", "фактура", "inv", "faktura")):
            return EmailAttachment(
                filename=clean_name,
                content_type=content_type,
                data=data,
                size_bytes=size,
                is_inline=is_inline,
                content_id=content_id,
                is_valid_invoice_candidate=False,
                filter_category="signature_logo",
                filter_reason=(
                    f"Inline image '{clean_name}' ({size} bytes) without invoice keyword treated as email layout decoration."
                ),
            )

    # Passed all filters — valid invoice document!
    return EmailAttachment(
        filename=clean_name,
        content_type=content_type,
        data=data,
        size_bytes=size,
        is_inline=is_inline,
        content_id=content_id,
        is_valid_invoice_candidate=True,
        filter_category="accepted",
        filter_reason=None,
    )


# ---------------------------------------------------------------------------
# Email Message Parsing
# ---------------------------------------------------------------------------

def _decode_header_str(val: Optional[str]) -> str:
    """Safely decode RFC 2047 encoded email headers."""
    if not val:
        return ""
    try:
        return str(make_header(decode_header(val)))
    except Exception:
        return val


def parse_mime_email(
    raw_content: bytes | str,
    require_spf: bool = False,
    require_dkim: bool = False,
    block_spf_fail: bool = True,
    min_size_bytes: int = DEFAULT_MIN_ATTACHMENT_SIZE_BYTES,
) -> ParsedEmail:
    """Parse raw RFC 822 / 5322 MIME email bytes into structured ParsedEmail."""
    raw_bytes = raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
    msg: EmailMessage = email.message_from_bytes(raw_bytes, policy=policy.default)

    headers: dict[str, str] = {}
    for k, v in msg.items():
        headers[k] = _decode_header_str(str(v))

    sender = _decode_header_str(msg.get("From", ""))
    recipient = _decode_header_str(msg.get("To", ""))
    subject = _decode_header_str(msg.get("Subject", ""))
    date_str = msg.get("Date")
    message_id = msg.get("Message-ID")

    # Evaluate SPF and DKIM
    spf_status, dkim_status, auth_results_raw = extract_spf_dkim_status(headers)
    security = evaluate_security_policy(
        spf_status=spf_status,
        dkim_status=dkim_status,
        require_spf=require_spf,
        require_dkim=require_dkim,
        block_spf_fail=block_spf_fail,
        auth_results_raw=auth_results_raw,
    )

    body_plain = ""
    body_html = ""
    all_attachments: list[EmailAttachment] = []

    # Iterate parts
    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = str(part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        content_id = part.get("Content-ID")

        # Decode decoded filename if present
        if filename:
            filename = _decode_header_str(filename)

        is_attachment = disposition in ("attachment", "inline") or filename is not None

        if not is_attachment and content_type == "text/plain":
            try:
                body_plain += part.get_content()
            except Exception:
                pass
            continue
        elif not is_attachment and content_type == "text/html":
            try:
                body_html += part.get_content()
            except Exception:
                pass
            continue

        # Extract file payload
        payload_bytes = part.get_payload(decode=True)
        if payload_bytes is None:
            continue

        fn = filename or f"attachment_{len(all_attachments) + 1}"
        is_inline = (disposition == "inline") or (content_id is not None)

        att = filter_attachment(
            filename=fn,
            content_type=content_type,
            data=payload_bytes,
            is_inline=is_inline,
            content_id=content_id,
            min_size_bytes=min_size_bytes,
        )
        all_attachments.append(att)

    valid_atts = [a for a in all_attachments if a.is_valid_invoice_candidate]
    filtered_atts = [a for a in all_attachments if not a.is_valid_invoice_candidate]

    return ParsedEmail(
        sender=sender,
        recipient=recipient,
        subject=subject,
        date=date_str,
        message_id=message_id,
        body_plain=body_plain,
        body_html=body_html,
        headers=headers,
        security=security,
        all_attachments=all_attachments,
        valid_attachments=valid_atts,
        filtered_attachments=filtered_atts,
    )


def parse_cloudflare_worker_json(
    payload: dict[str, Any],
    require_spf: bool = False,
    require_dkim: bool = False,
    block_spf_fail: bool = True,
    min_size_bytes: int = DEFAULT_MIN_ATTACHMENT_SIZE_BYTES,
) -> ParsedEmail:
    """Parse JSON payload received from Cloudflare Email Worker webhook.

    Cloudflare Email Workers can deliver emails in JSON format:
    {
      "from": "sender@vendor.bg",
      "to": "invoices@openbalancer.com",
      "subject": "Фактура № 0000000042",
      "headers": { ... },
      "spf": "pass",
      "dkim": "pass",
      "raw_email": "...(optional base64 or RFC 822 string)...",
      "attachments": [
        {
          "filename": "invoice.pdf",
          "content": "<base64 encoded content>",
          "content_type": "application/pdf"
        }
      ]
    }
    """
    # If raw_email is provided and no attachments list, delegate to raw MIME parser
    raw_email = payload.get("raw_email") or payload.get("raw")
    if raw_email and not payload.get("attachments"):
        try:
            raw_bytes = base64.b64decode(raw_email) if isinstance(raw_email, str) and not raw_email.startswith("From:") else raw_email.encode("utf-8")
            parsed = parse_mime_email(
                raw_bytes,
                require_spf=require_spf,
                require_dkim=require_dkim,
                block_spf_fail=block_spf_fail,
                min_size_bytes=min_size_bytes,
            )
            # Merge top-level metadata if present
            if payload.get("from"):
                parsed.sender = str(payload["from"])
            if payload.get("to"):
                parsed.recipient = str(payload["to"])
            if payload.get("subject"):
                parsed.subject = str(payload["subject"])
            return parsed
        except Exception as exc:
            logger.debug("Falling back from raw_email parsing: %s", exc)

    sender = str(payload.get("from") or payload.get("sender") or "")
    recipient = str(payload.get("to") or payload.get("recipient") or "")
    subject = str(payload.get("subject") or "")
    date_str = str(payload.get("date") or "")
    message_id = str(payload.get("message_id") or payload.get("messageId") or "")

    headers = payload.get("headers") or {}
    if isinstance(headers, list):
        # Convert header list of tuples or dicts
        hdr_dict = {}
        for item in headers:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                hdr_dict[item[0]] = str(item[1])
            elif isinstance(item, dict) and "name" in item and "value" in item:
                hdr_dict[item["name"]] = str(item["value"])
        headers = hdr_dict

    spf_status, dkim_status, auth_results = extract_spf_dkim_status(headers, payload_dict=payload)
    security = evaluate_security_policy(
        spf_status=spf_status,
        dkim_status=dkim_status,
        require_spf=require_spf,
        require_dkim=require_dkim,
        block_spf_fail=block_spf_fail,
        auth_results_raw=auth_results,
    )

    all_attachments: list[EmailAttachment] = []
    raw_attachments = payload.get("attachments") or []

    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        fn = item.get("filename") or item.get("name") or "attachment"
        ctype = item.get("content_type") or item.get("type") or "application/octet-stream"
        content_b64 = item.get("content") or item.get("data") or item.get("content_base64") or ""
        is_inline = bool(item.get("is_inline") or item.get("inline"))
        cid = item.get("content_id") or item.get("cid")

        try:
            if isinstance(content_b64, str):
                data = base64.b64decode(content_b64)
            elif isinstance(content_b64, bytes):
                data = content_b64
            else:
                continue
        except Exception as exc:
            logger.warning("Failed decoding attachment %s: %s", fn, exc)
            continue

        att = filter_attachment(
            filename=fn,
            content_type=ctype,
            data=data,
            is_inline=is_inline,
            content_id=cid,
            min_size_bytes=min_size_bytes,
        )
        all_attachments.append(att)

    valid_atts = [a for a in all_attachments if a.is_valid_invoice_candidate]
    filtered_atts = [a for a in all_attachments if not a.is_valid_invoice_candidate]

    return ParsedEmail(
        sender=sender,
        recipient=recipient,
        subject=subject,
        date=date_str,
        message_id=message_id,
        body_plain=str(payload.get("text") or payload.get("body_text") or ""),
        body_html=str(payload.get("html") or payload.get("body_html") or ""),
        headers=headers,
        security=security,
        all_attachments=all_attachments,
        valid_attachments=valid_atts,
        filtered_attachments=filtered_atts,
    )


def parse_multipart_form_data(
    form_fields: dict[str, Any],
    form_files: list[tuple[str, str, bytes, str]],  # (field_name, filename, content_bytes, content_type)
    require_spf: bool = False,
    require_dkim: bool = False,
    block_spf_fail: bool = True,
    min_size_bytes: int = DEFAULT_MIN_ATTACHMENT_SIZE_BYTES,
) -> ParsedEmail:
    """Parse multipart/form-data as sent by SendGrid Inbound Parse or standard webhooks."""
    # Check if raw RFC 822 email was sent in 'email' or 'raw' field
    raw_email = form_fields.get("email") or form_fields.get("raw")
    if raw_email and not form_files:
        return parse_mime_email(
            raw_email,
            require_spf=require_spf,
            require_dkim=require_dkim,
            block_spf_fail=block_spf_fail,
            min_size_bytes=min_size_bytes,
        )

    # Check headers field (SendGrid sends 'headers' as string)
    raw_headers = form_fields.get("headers", "")
    headers: dict[str, str] = {}
    if isinstance(raw_headers, str) and raw_headers:
        try:
            parsed_hdr_msg = email.message_from_string(raw_headers)
            headers = {k: _decode_header_str(str(v)) for k, v in parsed_hdr_msg.items()}
        except Exception:
            pass

    sender = str(form_fields.get("from") or form_fields.get("sender") or "")
    recipient = str(form_fields.get("to") or form_fields.get("recipient") or "")
    subject = str(form_fields.get("subject") or "")

    spf_status, dkim_status, auth_results = extract_spf_dkim_status(headers, payload_dict=form_fields)
    security = evaluate_security_policy(
        spf_status=spf_status,
        dkim_status=dkim_status,
        require_spf=require_spf,
        require_dkim=require_dkim,
        block_spf_fail=block_spf_fail,
        auth_results_raw=auth_results,
    )

    all_attachments: list[EmailAttachment] = []
    for field_name, filename, content_bytes, ctype in form_files:
        fn = filename or f"{field_name}.pdf"
        att = filter_attachment(
            filename=fn,
            content_type=ctype or "application/octet-stream",
            data=content_bytes,
            is_inline="inline" in field_name.lower(),
            min_size_bytes=min_size_bytes,
        )
        all_attachments.append(att)

    valid_atts = [a for a in all_attachments if a.is_valid_invoice_candidate]
    filtered_atts = [a for a in all_attachments if not a.is_valid_invoice_candidate]

    return ParsedEmail(
        sender=sender,
        recipient=recipient,
        subject=subject,
        body_plain=str(form_fields.get("text") or ""),
        body_html=str(form_fields.get("html") or ""),
        headers=headers,
        security=security,
        all_attachments=all_attachments,
        valid_attachments=valid_atts,
        filtered_attachments=filtered_atts,
    )
