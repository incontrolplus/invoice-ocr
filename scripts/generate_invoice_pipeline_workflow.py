import json
from pathlib import Path

def build_invoice_pipeline_workflow():
    workflow_id = "invoice-ocr-deltapro-pipeline"
    workflow_name = "Enterprise Invoice OCR & Microinvest Delta Pro Pipeline"

    # Javascript for normalizing incoming input from either Webhook or IMAP
    js_normalize_input = """
const items = $input.all();
const results = [];

for (const item of items) {
  const json = item.json || {};
  const binary = item.binary || {};

  let sender = json.sender || json.from?.text || json.from || json.From || "inbound-email@openbalancer.com";
  let recipient = json.recipient || json.to?.text || json.to || json.To || "billing@openbalancer.com";
  let subject = json.subject || json.Subject || "Incoming Invoice Document";
  let received_at = json.received_at || json.date || new Date().toISOString();

  let attachments = [];

  // 1. If explicit attachments array is provided in body
  if (Array.isArray(json.attachments) && json.attachments.length > 0) {
    attachments = json.attachments.map(a => ({
      filename: a.filename || a.name || "invoice.pdf",
      mime_type: a.mime_type || a.type || a.content_type || "application/pdf",
      content: a.content || a.base64_data || a.data || "",
      base64_data: a.base64_data || a.content || a.data || ""
    }));
  } 
  // 2. If single file or base64 is in body
  else if (json.base64_data || json.file_base64 || json.content) {
    const b64 = json.base64_data || json.file_base64 || json.content;
    attachments.push({
      filename: json.filename || json.file_name || "invoice.pdf",
      mime_type: json.mime_type || json.content_type || "application/pdf",
      content: b64,
      base64_data: b64
    });
  }
  // 3. If binary files are attached (from IMAP or multipart webhook)
  else if (Object.keys(binary).length > 0) {
    for (const [key, bin] of Object.entries(binary)) {
      if (!bin || !bin.data) continue;
      const fn = bin.fileName || `${key}.pdf`;
      const mime = bin.mimeType || "application/pdf";
      // Filter invoice extensions
      if (/\\.(pdf|png|jpe?g|tiff?)$/i.test(fn) || mime.includes('pdf') || mime.includes('image')) {
        attachments.push({
          filename: fn,
          mime_type: mime,
          content: bin.data,
          base64_data: bin.data
        });
      }
    }
  }

  // If no attachments found, check if raw body contains forward email payload
  if (attachments.length === 0 && json.body) {
    const b = json.body;
    if (Array.isArray(b.attachments)) {
      attachments = b.attachments;
    }
  }

  if (attachments.length > 0) {
    results.push({
      json: {
        sender: String(sender),
        recipient: String(recipient),
        subject: String(subject),
        received_at: String(received_at),
        attachments: attachments,
        _source_trigger: json.path ? "webhook" : "imap"
      }
    });
  } else {
    // If no document attachment was detected, return structured error/skip info
    results.push({
      json: {
        _skip: true,
        reason: "No invoice attachment (PDF/image) detected in incoming payload.",
        sender: String(sender),
        subject: String(subject)
      }
    });
  }
}

return results;
""".strip()

    # Javascript for formatting Telegram notification message
    js_format_telegram = r"""

const raw = $input.first().json;
const res = (raw.results && raw.results.length > 0) ? raw.results[0] : raw;

if (res._skip || res.routing_action === 'error') {
  return [{
    json: {
      telegram_text: `⚠️ *Проблем при обработка на фактура:* ${res.error || res.reason || 'Не е открит документ'}\\n*Файл:* ${res.file_name || 'Неизвестен'}`,
      reply_markup: { inline_keyboard: [] },
      original_result: raw
    }
  }];
}

const docId = res.document_id || "";
const invNum = res.invoice_number || res.accounting_operation?.document_number || "Неизвестен";
const suppName = res.supplier_name || res.accounting_operation?.contractor_name || "Неизвестен доставчик";
const suppEik = res.accounting_operation?.contractor_eik || res.historical_match_report?.contractor_eik || "";
const recName = res.accounting_operation?.client_company || "БИЛДИНГ 11 ООД";
const recEik = res.historical_match_report?.client_company_eik || "206062202";
const total = (Number(res.total_amount) || Number(res.accounting_operation?.total_amount) || 0).toFixed(2);
const currency = res.accounting_operation?.currency || res.currency || "EUR";
const issueDate = res.accounting_operation?.document_date || res.issue_date || res.date || "";

const op = res.accounting_operation || {};
const taxBase = (Number(op.tax_base) || (Number(total) / 1.2)).toFixed(2);
const vatAmount = (Number(op.vat_amount) || (Number(total) - Number(taxBase))).toFixed(2);
const reason = op.reason || "м-ли";

const debits = op.debit_entries || [];
const expEntry = debits.find(d => d.account !== '4531') || { account: '601', account_name: 'Разходи за материали' };
const expAcc = expEntry.account || '601';
const expName = expEntry.account_name || 'Разходи за материали';
const vatAcc = '4531';
const credAcc = '401';

const hist = res.historical_match_report || {};
const isMatched = hist.matched || false;
const histCount = hist.total_past_transactions || 0;
const verdict = hist.comparison_verdict || (isMatched ? "EXACT_HISTORICAL_MATCH" : "NEW_CONTRACTOR");

const transferLogSize = res.delta_pro_export?.transfer_log_size || 65536;
const transferLdbSize = res.delta_pro_export?.transfer_ldb_size || 64;


let msg = `🧾 *НОВА ОБРАБОТЕНА ФАКТУРА — MICROINVEST READY*\\n\\n` +
  `🏢 *Доставчик:* ${suppName} (\`${suppEik}\`)\\n` +
  `🏢 *Получател:* ${recName} (\`${recEik}\`)\\n` +
  `📄 *Фактура №:* \`${invNum}\`\\n` +
  `📅 *Дата:* ${issueDate}\\n` +
  `💰 *Сума:* \`${total} ${currency}\` (Основа: \`${taxBase}\`, ДДС: \`${vatAmount}\`)\\n\\n` +
  `⚖️ *Контировка в Microinvest Делта Pro:*\\n` +
  `• Дт \`${expAcc}\` (${expName}): \`${taxBase} ${currency}\`\\n` +
  `• Дт \`${vatAcc}\` (ДДС покупки 20%): \`${vatAmount} ${currency}\`\\n` +
  `• Кт \`${credAcc}\` (Доставчици): \`${total} ${currency}\`\\n` +
  `📝 *Основание:* \`${reason}\`\\n\\n`;

if (isMatched) {
  msg += `🎯 *Архив:* Намерени ${histCount} предходни операции в базата данни\\n`;
}
if (res.enriched_from_accounting_partners) {
  msg += `🏛️ *Релационна формула:* Данните са синхронизирани с \`accounting.partners\`\\n`;
}

msg += `📦 *TRANSFER.LOG:* \`${transferLogSize} B\` | *ldb:* \`${transferLdbSize} B\`\\n` +
  `🆔 *Doc ID:* \`${docId}\``;

const inlineKeyboard = [
  [
    { text: "📥 Свали TRANSFER.LOG", url: `https://ocr.openbalancer.com/api/v1/accounting/transfer-log/${docId}` },
    { text: "📦 Свали ZIP Пакет", url: `https://ocr.openbalancer.com/api/v1/accounting/package/${docId}` }
  ],
  [
    { text: "🌐 Supabase Рекорд", url: `https://supabase.openbalancer.com` }
  ]
];

return [{
  json: {
    telegram_text: msg,
    reply_markup: { inline_keyboard: inlineKeyboard },
    chat_id: "8041248687",
    original_result: res
  }
}];
""".strip()

    nodes = [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "invoice-ingest",
                "responseMode": "responseNode",
                "options": {}
            },
            "id": "webhook-inbound-invoice",
            "name": "Webhook - Inbound Invoice",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [100, 200],
            "webhookId": "invoice-ingest-webhook-001"
        },
        {
            "parameters": {
                "mailbox": "INBOX",
                "postProcessAction": "nothing",
                "format": "resolved",
                "options": {
                    "downloadAttachments": True
                }
            },
            "id": "imap-inbound-invoices",
            "name": "Gmail IMAP - Inbound Invoices",
            "type": "n8n-nodes-base.emailReadImap",
            "typeVersion": 2,
            "position": [100, 420],
            "disabled": True,
            "credentials": {
                "imap": {
                    "id": "38gUB4RDJSyDDWUf",
                    "name": "Gmail IMAP - miropetrovski12"
                }
            }
        },
        {
            "parameters": {
                "mode": "runOnceForAllItems",
                "jsCode": js_normalize_input
            },
            "id": "code-normalize-input",
            "name": "Normalize Inbound Document",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [360, 300]
        },
        {
            "parameters": {
                "conditions": {
                    "boolean": [
                        {
                            "value1": "={{ $json._skip }}",
                            "value2": True
                        }
                    ]
                }
            },
            "id": "if-skip-check",
            "name": "Check If Valid Invoice",
            "type": "n8n-nodes-base.if",
            "typeVersion": 1,
            "position": [580, 300]
        },
        {
            "parameters": {
                "method": "POST",
                "url": "https://ocr.openbalancer.com/api/v1/ingest/docs-email?token=dev_webhook_secret",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json) }}",
                "options": {
                    "timeout": 90000
                }
            },
            "id": "http-process-invoice",
            "name": "Process Invoice (OCR & Delta Pro)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [820, 380]
        },
        {
            "parameters": {
                "mode": "runOnceForAllItems",
                "jsCode": js_format_telegram
            },
            "id": "code-format-telegram",
            "name": "Format Telegram Notification",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1060, 380]
        },
        {
            "parameters": {
                "method": "POST",
                "url": "https://api.telegram.org/bot8490162949:AAFTyaGugc_QbkwnCPfc-tZdnRBMF0aZ1KI/sendMessage",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ { chat_id: $json.chat_id, text: $json.telegram_text, parse_mode: 'Markdown', reply_markup: $json.reply_markup } }}",
                "options": {
                    "response": {
                        "response": {
                            "neverError": True
                        }
                    }
                }
            },
            "id": "http-send-telegram",
            "name": "Send Telegram Alert",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1280, 380]
        },
        {
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify($json.original_result || $json) }}",
                "options": {}
            },
            "id": "respond-to-webhook",
            "name": "Respond to Webhook",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.1,
            "position": [1500, 380]
        }
    ]

    connections = {
        "Webhook - Inbound Invoice": {
            "main": [
                [{"node": "Normalize Inbound Document", "type": "main", "index": 0}]
            ]
        },
        "Gmail IMAP - Inbound Invoices": {
            "main": [
                [{"node": "Normalize Inbound Document", "type": "main", "index": 0}]
            ]
        },
        "Normalize Inbound Document": {
            "main": [
                [{"node": "Check If Valid Invoice", "type": "main", "index": 0}]
            ]
        },
        "Check If Valid Invoice": {
            "main": [
                # False output (index 1 in n8n IF v1) -> process invoice
                [],
                [{"node": "Process Invoice (OCR & Delta Pro)", "type": "main", "index": 0}]
            ]
        },
        "Process Invoice (OCR & Delta Pro)": {
            "main": [
                [{"node": "Format Telegram Notification", "type": "main", "index": 0}]
            ]
        },
        "Format Telegram Notification": {
            "main": [
                [{"node": "Send Telegram Alert", "type": "main", "index": 0}]
            ]
        },
        "Send Telegram Alert": {
            "main": [
                [{"node": "Respond to Webhook", "type": "main", "index": 0}]
            ]
        }
    }

    workflow = {
        "id": workflow_id,
        "name": workflow_name,
        "active": True,
        "nodes": nodes,
        "connections": connections,
        "settings": {
            "executionOrder": "v1"
        }
    }

    return [workflow]

if __name__ == "__main__":
    wf_data = build_invoice_pipeline_workflow()
    out_path = Path("config/n8n_invoice_ocr_deltapro_pipeline.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(wf_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Generated n8n workflow file at: {out_path}")
