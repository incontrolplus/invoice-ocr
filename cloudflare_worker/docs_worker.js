/**
 * Cloudflare Email Worker for Statutory Document Classification & Routing.
 * 
 * Intercepts incoming emails to docs@incontrolplus.com,
 * extracts the raw RFC 822 MIME stream, and securely POSTs to
 * https://ocr.openbalancer.com/api/v1/ingest/docs-email.
 * 
 * Statutory Categories:
 * 1. Фактури (INVOICE)
 * 2. Кредитни известия (CREDIT_NOTE)
 * 3. Стокови разписки (STOCK_RECEIPT)
 * 4. Фискални бонове (FISCAL_RECEIPT)
 * 5. Пощенски парични преводи (POSTAL_MONEY_TRANSFER)
 * 6. Платежни документи (PAYMENT_DOCUMENT)
 * 7. Некласифицирани (UNCLASSIFIED)
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

    // Default target webhook and secret token
    const targetUrl = (env && env.DOCS_WEBHOOK_URL) || "https://ocr.openbalancer.com/api/v1/ingest/docs-email?token=dev_webhook_secret";
    const webhookSecret = (env && env.DOCS_WEBHOOK_SECRET) || "dev_webhook_secret";

    try {
      // Read the full raw email stream (RFC 822 MIME) into memory
      const rawEmailBytes = await new Response(message.raw).arrayBuffer();

      // Forward directly to the Statutory Document Classification Engine
      const resp = await fetch(targetUrl, {
        method: "POST",
        headers: {
          "User-Agent": "Cloudflare-Docs-Email-Worker/1.0",
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
        console.error(`[Docs Classifier Error] HTTP ${resp.status}: ${errText}`);
      } else {
        const data = await resp.json();
        console.log(`[Docs Classifier Success] Processed & classified ${data.documents_processed || 0} document(s) from ${sender}`);
      }
    } catch (err) {
      console.error(`[Docs Classifier Fatal Error] Failed delivering email to webhook: ${err.message}`);
    }
  },
};
