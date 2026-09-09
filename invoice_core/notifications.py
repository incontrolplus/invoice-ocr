"""Reverse Notification Engine for Zero-Touch Ingestion.

Generates and dispatches real-time notifications to accountants and clients upon invoice receipt:
1. Automated Email Confirmation to Sender (Bulgarian HTML card + Plain text fallback)
   with Supplier, Amount, VAT, Status, and direct HITL dashboard link.
2. Telegram Bot Notifications (rich Markdown formatted card with status badges).
3. Slack Notifications (Slack Block Kit layout with interactive button).
4. ERP Outbound Webhook (with ready double-entry journal entries and NAP ledger record).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import email.mime.multipart
import email.mime.text
import json
import logging
import os
import smtplib
from typing import Any, Optional

import httpx

from .constants import DEFAULT_HITL_BASE_URL

logger = logging.getLogger("invoice_ocr_notifications")

DEFAULT_SMTP_HOST = os.environ.get("SMTP_HOST", "")
DEFAULT_SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
DEFAULT_SMTP_USER = os.environ.get("SMTP_USER", "")
DEFAULT_SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
DEFAULT_SMTP_FROM = os.environ.get("SMTP_FROM", "invoices@openbalancer.com")
DEFAULT_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DEFAULT_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DEFAULT_SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


@dataclass
class InvoiceNotificationSummary:
    """Summary of processed invoice for reverse notification delivery."""
    document_id: str
    file_name: str
    invoice_number: Optional[str]
    date_issued: Optional[str]
    supplier_name: Optional[str]
    supplier_eik: Optional[str]
    supplier_vat: Optional[str]
    recipient_name: Optional[str]
    tax_base: float
    vat_amount: float
    total_amount: float
    currency: str
    status: str  # "completed", "needs_review", "approved", "failed"
    is_valid: bool
    error_count: int = 0
    warning_count: int = 0
    error_messages: list[str] = field(default_factory=list)
    warning_messages: list[str] = field(default_factory=list)
    hitl_url: str = ""
    processing_time_sec: float = 0.0

    @classmethod
    def from_document_dict(
        cls,
        doc_dict: dict[str, Any],
        hitl_base_url: Optional[str] = None,
    ) -> InvoiceNotificationSummary:
        """Construct notification summary from standard document dictionary."""
        base_url = (hitl_base_url or DEFAULT_HITL_BASE_URL).rstrip("/")
        doc_id = str(doc_dict.get("id") or doc_dict.get("document_id") or "unknown")
        hitl_link = f"{base_url}/dashboard?doc_id={doc_id}"

        # Extract normalized data
        norm_data = doc_dict.get("normalized_data") or doc_dict.get("data", {}).get("normalized_data", {})
        meta = norm_data.get("invoice_metadata", {})
        supp = norm_data.get("supplier", {})
        recip = norm_data.get("recipient", {})
        fin = norm_data.get("financial_summary", {})
        val = doc_dict.get("validation_results") or doc_dict.get("validation") or doc_dict.get("data", {}).get("validation_results", {})

        def _money_to_float(v: Any) -> float:
            if isinstance(v, dict):
                v = v.get("amount")
            if v is None:
                return 0.0
            try:
                return float(v)
            except (ValueError, TypeError):
                return 0.0

        tax_base = doc_dict.get("tax_base") or _money_to_float(fin.get("tax_base"))
        vat_amount = doc_dict.get("vat_amount") or _money_to_float(fin.get("vat_amount"))
        total_amount = doc_dict.get("total_amount") or _money_to_float(fin.get("total_amount_due"))
        currency = doc_dict.get("currency") or meta.get("currency") or "BGN"

        errors = [e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in val.get("errors", [])]
        warnings = [w.get("message", str(w)) if isinstance(w, dict) else str(w) for w in val.get("warnings", [])]

        return cls(
            document_id=doc_id,
            file_name=doc_dict.get("file_name", "invoice.pdf"),
            invoice_number=doc_dict.get("invoice_number") or meta.get("invoice_number") or "Не е разпознат",
            date_issued=doc_dict.get("date_issued") or meta.get("date_issued") or "N/A",
            supplier_name=doc_dict.get("supplier_name") or supp.get("name") or "Неизвестен доставчик",
            supplier_eik=doc_dict.get("supplier_eik") or supp.get("eik") or "N/A",
            supplier_vat=supp.get("vat_number"),
            recipient_name=doc_dict.get("recipient_name") or recip.get("name"),
            tax_base=round(float(tax_base), 2),
            vat_amount=round(float(vat_amount), 2),
            total_amount=round(float(total_amount), 2),
            currency=str(currency),
            status=doc_dict.get("status", "completed"),
            is_valid=bool(doc_dict.get("is_valid", True)),
            error_count=len(errors),
            warning_count=len(warnings),
            error_messages=errors,
            warning_messages=warnings,
            hitl_url=hitl_link,
            processing_time_sec=float(doc_dict.get("processing_time_sec", 0.0)),
        )


# ---------------------------------------------------------------------------
# Template Generators (HTML, Text, Telegram, Slack)
# ---------------------------------------------------------------------------

def build_email_confirmation_html(summary: InvoiceNotificationSummary) -> str:
    """Generate professional Bulgarian HTML email confirmation with status badge and HITL button."""
    status_color = "#10b981" if summary.is_valid else "#f59e0b"
    status_text = "Успешно валидирана" if summary.is_valid else "Изисква преглед (HITL)"
    status_bg = "#ecfdf5" if summary.is_valid else "#fffbeb"

    issues_html = ""
    if summary.error_messages or summary.warning_messages:
        issue_items = "".join(f"<li style='color:#dc2626;'><b>Грешка:</b> {msg}</li>" for msg in summary.error_messages)
        issue_items += "".join(f"<li style='color:#d97706;'><b>Предупреждение:</b> {msg}</li>" for msg in summary.warning_messages)
        issues_html = f"""
        <div style="margin-top:20px; padding:12px; background:#fef2f2; border:1px solid #fecaca; border-radius:6px;">
            <h4 style="margin:0 0 8px 0; color:#991b1b; font-size:14px;">Открити несъответствия:</h4>
            <ul style="margin:0; padding-left:20px; font-size:13px;">
                {issue_items}
            </ul>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="bg">
<head>
    <meta charset="UTF-8">
    <title>Потвърждение за получена фактура</title>
</head>
<body style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background:#f3f4f6; margin:0; padding:24px; color:#1f2937;">
    <div style="max-width:600px; margin:0 auto; background:#ffffff; border-radius:8px; border:1px solid #e5e7eb; overflow:hidden; box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);">
        <div style="background:#1e3a8a; padding:20px 24px; color:#ffffff;">
            <h2 style="margin:0; font-size:20px; font-weight:600;">OpenBalancer &mdash; Zero-Touch Ingestion</h2>
            <p style="margin:4px 0 0 0; font-size:13px; opacity:0.9;">Автоматично постъпване и разпознаване на входяща фактура</p>
        </div>
        
        <div style="padding:24px;">
            <div style="display:inline-block; padding:6px 14px; background:{status_bg}; color:{status_color}; border:1px solid {status_color}; border-radius:20px; font-size:13px; font-weight:600; margin-bottom:16px;">
                {status_text}
            </div>

            <p style="margin:0 0 16px 0; font-size:15px; line-height:1.5;">
                Здравейте,<br>
                Файлът <b>{summary.file_name}</b> беше успешно приет и обработен в рамките на <b>{summary.processing_time_sec:.2f}s</b>.
            </p>

            <table style="width:100%; border-collapse:collapse; margin-bottom:20px; font-size:14px;">
                <tr style="border-bottom:1px solid #f3f4f6;">
                    <td style="padding:8px 0; color:#6b7280;">Доставчик:</td>
                    <td style="padding:8px 0; font-weight:600; text-align:right;">{summary.supplier_name}</td>
                </tr>
                <tr style="border-bottom:1px solid #f3f4f6;">
                    <td style="padding:8px 0; color:#6b7280;">ЕИК:</td>
                    <td style="padding:8px 0; font-weight:600; text-align:right;">{summary.supplier_eik}</td>
                </tr>
                <tr style="border-bottom:1px solid #f3f4f6;">
                    <td style="padding:8px 0; color:#6b7280;">Фактура №:</td>
                    <td style="padding:8px 0; font-weight:600; text-align:right;">{summary.invoice_number}</td>
                </tr>
                <tr style="border-bottom:1px solid #f3f4f6;">
                    <td style="padding:8px 0; color:#6b7280;">Дата на издаване:</td>
                    <td style="padding:8px 0; font-weight:600; text-align:right;">{summary.date_issued}</td>
                </tr>
                <tr style="border-bottom:1px solid #f3f4f6;">
                    <td style="padding:8px 0; color:#6b7280;">Данъчна основа:</td>
                    <td style="padding:8px 0; font-weight:600; text-align:right;">{summary.tax_base:.2f} {summary.currency}</td>
                </tr>
                <tr style="border-bottom:1px solid #f3f4f6;">
                    <td style="padding:8px 0; color:#6b7280;">ДДС:</td>
                    <td style="padding:8px 0; font-weight:600; text-align:right;">{summary.vat_amount:.2f} {summary.currency}</td>
                </tr>
                <tr style="border-bottom:2px solid #e5e7eb; background:#f9fafb;">
                    <td style="padding:10px 8px; font-weight:700; color:#111827; font-size:15px;">ОБЩА СУМА:</td>
                    <td style="padding:10px 8px; font-weight:700; color:#1e3a8a; text-align:right; font-size:16px;">{summary.total_amount:.2f} {summary.currency}</td>
                </tr>
            </table>

            {issues_html}

            <div style="text-align:center; margin-top:28px; margin-bottom:12px;">
                <a href="{summary.hitl_url}" style="display:inline-block; padding:12px 28px; background:#2563eb; color:#ffffff; text-decoration:none; border-radius:6px; font-weight:600; font-size:14px; box-shadow:0 2px 4px rgba(37,99,235,0.3);">
                    Отвори в HITL таблото за счетоводен преглед &rarr;
                </a>
            </div>
        </div>

        <div style="background:#f9fafb; padding:16px 24px; border-top:1px solid #e5e7eb; font-size:12px; color:#9ca3af; text-align:center;">
            Документ ID: {summary.document_id} &bull; OpenBalancer Ingestion Engine &bull; Време: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
        </div>
    </div>
</body>
</html>
"""


def build_email_confirmation_text(summary: InvoiceNotificationSummary) -> str:
    """Generate plain text confirmation in Bulgarian."""
    status_label = "Успешно валидирана" if summary.is_valid else "Изисква преглед (HITL)"
    lines = [
        "============================================================",
        "OpenBalancer - Zero-Touch Ingestion (Потвърждение за фактура)",
        "============================================================",
        f"Статус:            {status_label}",
        f"Файл:              {summary.file_name}",
        f"Време за OCR:      {summary.processing_time_sec:.2f}s",
        "------------------------------------------------------------",
        f"Доставчик:         {summary.supplier_name}",
        f"ЕИК:               {summary.supplier_eik}",
        f"Фактура №:         {summary.invoice_number}",
        f"Дата на издаване:  {summary.date_issued}",
        "------------------------------------------------------------",
        f"Данъчна основа:    {summary.tax_base:.2f} {summary.currency}",
        f"ДДС:               {summary.vat_amount:.2f} {summary.currency}",
        f"ОБЩА СУМА:         {summary.total_amount:.2f} {summary.currency}",
        "------------------------------------------------------------",
    ]
    if summary.error_messages:
        lines.append("Грешки при валидация:")
        for err in summary.error_messages:
            lines.append(f"  - {err}")
    if summary.warning_messages:
        lines.append("Предупреждения:")
        for warn in summary.warning_messages:
            lines.append(f"  - {warn}")

    lines.extend([
        "------------------------------------------------------------",
        f"Линк за счетоводен преглед (HITL): {summary.hitl_url}",
        "============================================================",
    ])
    return "\n".join(lines)


def build_telegram_message(summary: InvoiceNotificationSummary) -> str:
    """Format rich Telegram message using Markdown."""
    icon = "🟢" if summary.is_valid else "🟡"
    status_text = "Валидна" if summary.is_valid else "Изисква преглед"

    msg = (
        f"🧾 *Нова получена фактура (Zero-Touch Ingestion)*\n"
        f"────────────────────────────\n"
        f"🏢 *Доставчик:* {summary.supplier_name}\n"
        f"🆔 *ЕИК:* `{summary.supplier_eik}`\n"
        f"📄 *Фактура №:* `{summary.invoice_number}`\n"
        f"📅 *Дата:* {summary.date_issued}\n"
        f"────────────────────────────\n"
        f"💵 *Данъчна основа:* {summary.tax_base:.2f} {summary.currency}\n"
        f"🏷 *ДДС:* {summary.vat_amount:.2f} {summary.currency}\n"
        f"💰 *ОБЩА СУМА:* *{summary.total_amount:.2f} {summary.currency}*\n"
        f"🚦 *Статус:* {icon} {status_text} (OCR: {summary.processing_time_sec:.2f}s)\n"
    )

    if summary.error_messages:
        msg += f"⚠️ *Грешки ({len(summary.error_messages)}):* {summary.error_messages[0]}\n"

    msg += (
        f"────────────────────────────\n"
        f"🔗 [Преглед в HITL табло]({summary.hitl_url})\n"
    )
    return msg


def build_slack_blocks(summary: InvoiceNotificationSummary) -> dict[str, Any]:
    """Format Slack Block Kit message with structured fields and action button."""
    status_emoji = ":white_check_mark:" if summary.is_valid else ":warning:"
    status_label = "Валидна" if summary.is_valid else "Изисква преглед (HITL)"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🧾 Нова входяща фактура (Zero-Touch Ingestion)",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Доставчик:*\n{summary.supplier_name} (`{summary.supplier_eik}`)"},
                {"type": "mrkdwn", "text": f"*Фактура №:*\n`{summary.invoice_number}` от {summary.date_issued}"},
                {"type": "mrkdwn", "text": f"*Сума за плащане:*\n*{summary.total_amount:.2f} {summary.currency}* (ДДС: {summary.vat_amount:.2f})"},
                {"type": "mrkdwn", "text": f"*Статус:*\n{status_emoji} {status_label} ({summary.processing_time_sec:.2f}s)"},
            ],
        },
    ]

    if summary.error_messages:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":bangbang: *Открити несъответствия:* {summary.error_messages[0]}",
            },
        })

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Преглед в HITL табло",
                    "emoji": True,
                },
                "url": summary.hitl_url,
                "style": "primary" if summary.is_valid else "danger",
            }
        ],
    })

    return {
        "text": f"Нова фактура № {summary.invoice_number} от {summary.supplier_name} на стойност {summary.total_amount:.2f} {summary.currency}",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Asynchronous Dispatchers
# ---------------------------------------------------------------------------

async def send_email_confirmation_async(
    recipient_email: str,
    summary: InvoiceNotificationSummary,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_password: Optional[str] = None,
    from_email: Optional[str] = None,
) -> dict[str, Any]:
    """Send reverse email confirmation asynchronously to the sender."""
    host = smtp_host or DEFAULT_SMTP_HOST
    port = smtp_port or DEFAULT_SMTP_PORT
    user = smtp_user or DEFAULT_SMTP_USER
    passwd = smtp_password or DEFAULT_SMTP_PASSWORD
    sender = from_email or DEFAULT_SMTP_FROM

    html_content = build_email_confirmation_html(summary)
    text_content = build_email_confirmation_text(summary)
    subject = f"[OpenBalancer] Фактура № {summary.invoice_number} от {summary.supplier_name} ({summary.status})"

    if not host:
        logger.info(
            "SMTP host not configured. Email confirmation to %s recorded in log.",
            recipient_email,
        )
        return {
            "status": "simulated",
            "recipient": recipient_email,
            "subject": subject,
            "message": "SMTP not configured; confirmation simulated successfully.",
        }

    try:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = recipient_email
        msg.attach(email.mime.text.MIMEText(text_content, "plain", "utf-8"))
        msg.attach(email.mime.text.MIMEText(html_content, "html", "utf-8"))

        def _send():
            with smtplib.SMTP(host, port, timeout=10) as s:
                if port == 587:
                    s.starttls()
                if user and passwd:
                    s.login(user, passwd)
                s.send_message(msg)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send)
        logger.info("Email confirmation sent to %s for doc %s", recipient_email, summary.document_id)
        return {"status": "sent", "recipient": recipient_email, "subject": subject}
    except Exception as exc:
        logger.error("Failed to send email confirmation to %s: %s", recipient_email, exc)
        return {"status": "failed", "recipient": recipient_email, "error": str(exc)}


async def send_telegram_notification_async(
    summary: InvoiceNotificationSummary,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> dict[str, Any]:
    """Send Telegram notification via Telegram Bot API."""
    token = bot_token or DEFAULT_TELEGRAM_BOT_TOKEN
    cid = chat_id or DEFAULT_TELEGRAM_CHAT_ID

    text = build_telegram_message(summary)

    if not token or not cid:
        return {
            "status": "skipped",
            "message": "Telegram bot_token or chat_id not configured.",
            "text": text,
        }

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram notification delivered for doc %s", summary.document_id)
                return {"status": "sent", "chat_id": cid}
            else:
                logger.warning("Telegram API error (%d): %s", resp.status_code, resp.text)
                return {"status": "failed", "status_code": resp.status_code, "error": resp.text}
    except Exception as exc:
        logger.error("Error sending Telegram message: %s", exc)
        return {"status": "failed", "error": str(exc)}


async def send_slack_notification_async(
    summary: InvoiceNotificationSummary,
    webhook_url: Optional[str] = None,
) -> dict[str, Any]:
    """Send Slack notification via incoming webhook."""
    url = webhook_url or DEFAULT_SLACK_WEBHOOK_URL
    payload = build_slack_blocks(summary)

    if not url:
        return {
            "status": "skipped",
            "message": "Slack webhook URL not configured.",
            "payload": payload,
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Slack notification delivered for doc %s", summary.document_id)
                return {"status": "sent"}
            else:
                logger.warning("Slack webhook error (%d): %s", resp.status_code, resp.text)
                return {"status": "failed", "status_code": resp.status_code, "error": resp.text}
    except Exception as exc:
        logger.error("Error sending Slack notification: %s", exc)
        return {"status": "failed", "error": str(exc)}


async def dispatch_reverse_notifications_bundle(
    doc_dict: dict[str, Any],
    sender_email: Optional[str] = None,
    erp_webhook_url: Optional[str] = None,
    telegram_bot_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    slack_webhook_url: Optional[str] = None,
    hitl_base_url: Optional[str] = None,
) -> dict[str, Any]:
    """Dispatch all configured reverse notifications concurrently."""
    summary = InvoiceNotificationSummary.from_document_dict(doc_dict, hitl_base_url=hitl_base_url)

    tasks = {}

    if sender_email:
        tasks["email_confirmation"] = send_email_confirmation_async(
            recipient_email=sender_email,
            summary=summary,
        )

    tasks["telegram"] = send_telegram_notification_async(
        summary=summary,
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
    )

    tasks["slack"] = send_slack_notification_async(
        summary=summary,
        webhook_url=slack_webhook_url,
    )

    if erp_webhook_url:
        from webhooks import prepare_webhook_payload, send_webhook_async
        erp_payload = prepare_webhook_payload(doc_dict, event_type="invoice.processed")
        tasks["erp_webhook"] = send_webhook_async(
            target_url=erp_webhook_url,
            payload=erp_payload,
            document_id=summary.document_id,
        )

    results: dict[str, Any] = {"summary": {
        "invoice_number": summary.invoice_number,
        "supplier_name": summary.supplier_name,
        "supplier_eik": summary.supplier_eik,
        "tax_base": summary.tax_base,
        "vat_amount": summary.vat_amount,
        "total_amount": summary.total_amount,
        "currency": summary.currency,
        "status": summary.status,
        "is_valid": summary.is_valid,
        "hitl_url": summary.hitl_url,
    }}

    executed = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for key, res in zip(tasks.keys(), executed):
        if isinstance(res, Exception):
            results[key] = {"status": "failed", "error": str(res)}
        else:
            results[key] = res

    return results
