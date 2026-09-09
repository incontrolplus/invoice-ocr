/**
 * Cloudflare Email Worker for Zero-Touch Invoice Ingestion.
 * 
 * Intercepts incoming emails to invoice@incontrolplus.com (or invoice@openbalancer.com),
 * extracts the raw RFC 822 MIME stream, and securely POSTs to
 * https://ocr.openbalancer.com/api/v1/ingest/email.
 * 
 * Execution time in Cloudflare: ~2-5 ms.
 * Max email size supported: 25 MB.
 */

export default {
  async email(message, env, ctx) {
    const sender = message.from || "";
    const recipient = message.to || "";
    const subject = message.headers?.get("subject") || "";
    const date = message.headers?.get("date") || new Date().toISOString();
    const messageId = message.headers?.get("message-id") || "";

    // Support intelligent recipient-based routing between invoice and docs endpoints
    const isDocsRecipient = recipient.toLowerCase().includes("docs@");
    const targetUrl = isDocsRecipient
      ? ((env && env.DOCS_WEBHOOK_URL) || "https://ocr.openbalancer.com/api/v1/ingest/docs-email")
      : ((env && env.OCR_WEBHOOK_URL) || "https://ocr.openbalancer.com/api/v1/ingest/email");
    const webhookSecret = (env && (env.OCR_WEBHOOK_SECRET || env.DOCS_WEBHOOK_SECRET)) || "dev_webhook_secret";

    try {
      // Read the full raw email stream (RFC 822 MIME) into memory
      const rawEmailBytes = await new Response(message.raw).arrayBuffer();

      // Forward directly to the OCR Ingestion Engine
      const resp = await fetch(targetUrl, {
        method: "POST",
        headers: {
          "User-Agent": "Cloudflare-Email-Worker/1.0",
          "Content-Type": "message/rfc822",
          "X-Webhook-Secret": webhookSecret,
          "X-Ingest-Token": webhookSecret,
          "Authorization": `Bearer ${webhookSecret}`,
          "X-Sender": sender,
          "X-Recipient": recipient,
          "X-Subject": encodeURIComponent(subject),
          "X-Date": date,
          "X-Message-ID": messageId,
        },
        body: rawEmailBytes,
      });

      if (!resp.ok) {
        const errText = await resp.text();
        console.error(`[Invoice OCR Error] HTTP ${resp.status}: ${errText}`);
      } else {
        const data = await resp.json();
        console.log(`[Invoice OCR Success] Processed ${data.invoices_processed || 0} invoice(s) from ${sender}`);
      }
    } catch (err) {
      console.error(`[Invoice OCR Fatal Error] Failed delivering email to webhook: ${err.message}`);
    }
  },
};
