import json
import os

def build_workflow():
    workflow_id = "companybook-partner-sync"
    workflow_name = "CompanyBook Partner Sync & Verify"

    code_normalize_eik = """
const query = $json.query || {};
const body = $json.body || {};
const raw = query.eik || query.uic || query.EIK || query.UIC ||
            body.eik || body.uic || body.EIK || body.UIC ||
            $json.eik || $json.uic || '';

let eikStr = String(raw).trim();
if (eikStr.toUpperCase().startsWith('BG')) {
  eikStr = eikStr.substring(2).trim();
}
const cleanEik = eikStr.replace(/[^0-9]/g, '');

if (!cleanEik || cleanEik.length < 9 || cleanEik.length > 13) {
  return [{
    json: {
      valid: false,
      error: 'INVALID_EIK_FORMAT',
      message: `Невалиден или липсващ ЕИК/БУЛСТАТ: '${raw}'. Очаква се 9- или 13-цифрен код (напр. BG123456789 или 123456789).`,
      raw_input: raw
    }
  }];
}

return [{
  json: {
    valid: true,
    uic: cleanEik,
    raw_input: raw
  }
}];
""".strip()

    code_transform_partner = """
const input = $json;
if (!input || input.error || !input.company) {
  let fallbackUic = '';
  try {
    fallbackUic = $('Normalize EIK & Check Input').first().json.uic;
  } catch(e) {}

  let errCode = 'COMPANY_NOT_FOUND';
  let errMsg = 'Фирма с посочения ЕИК не е намерена в Търговския регистър / CompanyBook.';
  if (input?.errorBG) {
    errMsg = input.errorBG;
  } else if (typeof input?.error === 'string') {
    errCode = input.error;
  } else if (input?.error?.message && input.error.message.includes('companyNotFound')) {
    errCode = 'COMPANY_NOT_FOUND';
    errMsg = 'Фирма с посочения ЕИК не е намерена в Търговския регистър.';
  }

  return [{
    json: {
      success: false,
      error: errCode,
      message: errMsg,
      uic: fallbackUic
    }
  }];
}

const c = input.company || {};
const s = c.seat || {};
const r = c.registerInfo || {};
const contacts = c.contacts || {};
const funds = c.depositedFunds || {};

function parseDate(dStr) {
  if (!dStr) return null;
  if (/^\\d{4}-\\d{2}-\\d{2}/.test(dStr)) return dStr.split('T')[0];
  const m = dStr.match(/^(\\d{2})\\.(\\d{2})\\.(\\d{4})$/);
  if (m) return m[3] + '-' + m[2] + '-' + m[1];
  return null;
}

function getLegalForm(name) {
  if (!name) return null;
  if (name.includes('Еднолично акционерно дружество')) return 'ЕАД';
  if (name.includes('Акционерно дружество')) return 'АД';
  if (name.includes('Еднолично дружество с ограничена отговорност')) return 'ЕООД';
  if (name.includes('Дружество с ограничена отговорност')) return 'ООД';
  if (name.includes('Командитно дружество с акции')) return 'КДА';
  if (name.includes('Командитно дружество')) return 'КД';
  if (name.includes('Събирателно дружество')) return 'СД';
  if (name.includes('Едноличен търговец')) return 'ЕТ';
  return name;
}

function getLegalStatus(st) {
  if (st === 'N') return 'ACTIVE';
  if (st === 'L') return 'LIQUIDATION';
  if (st === 'B') return 'BANKRUPTCY';
  if (st === 'D') return 'DEREGISTERED';
  return st || 'ACTIVE';
}

const addrParts = [];
if (s.settlement) addrParts.push(s.settlement);
if (s.postCode) addrParts.push(s.postCode);
if (s.area) addrParts.push(s.area);
const streetFull = [s.street, s.streetNumber].filter(Boolean).join(' ');
if (streetFull) addrParts.push(streetFull);
if (s.block) addrParts.push('бл. ' + s.block);
if (s.entrance) addrParts.push('вх. ' + s.entrance);
if (s.floor) addrParts.push('ет. ' + s.floor);
if (s.apartment) addrParts.push('ап. ' + s.apartment);
const formattedAddress = addrParts.join(', ');

const partnerPayload = {
  companybook_id: c.id || null,
  country_code: 'BG',
  eik: c.uic,
  vat_number: r.vat || ('BG' + c.uic),
  legal_name: c.companyName?.name || null,
  transliteration: c.companyNameTransliteration?.name || null,
  legal_form: getLegalForm(c.legalForm),
  legal_status: getLegalStatus(c.status),
  seat_country: s.country || 'БЪЛГАРИЯ',
  seat_region: s.district || null,
  seat_district: s.district || null,
  seat_municipality: s.municipality || null,
  seat_settlement: s.settlement || null,
  seat_area: s.area || null,
  seat_street: s.street || null,
  seat_street_number: s.streetNumber || null,
  seat_block: s.block || null,
  seat_entrance: s.entrance || null,
  seat_floor: s.floor || null,
  seat_apartment: s.apartment || null,
  seat_post_code: s.postCode || null,
  seat_district_id: s.settlementEKATTE ? parseInt(s.settlementEKATTE, 10) : null,
  address: formattedAddress,
  city: s.settlement ? s.settlement.replace(/^гр\\.\\s*/i, '').replace(/^с\\.\\s*/i, '').trim() : null,
  postal_code: s.postCode || null,
  correspondence_address: formattedAddress,
  correspondence_seat: s,
  email: contacts.email || null,
  phone: contacts.phone || null,
  fax: contacts.fax || null,
  website: contacts.url || null,
  contact_presence: {
    email: Boolean(contacts.email),
    phone: Boolean(contacts.phone),
    fax: Boolean(contacts.fax),
    website: Boolean(contacts.url)
  },
  subject_of_activity: c.subjectOfActivity || null,
  nkids: c.nkids || [],
  primary_nkid_code: c.nkids?.[0]?.code || null,
  mol_name: c.managers?.[0]?.name || null,
  managers: c.managers || [],
  representatives: c.managers || [],
  board_of_directors: [],
  capital_amount: funds.value ? parseFloat(funds.value) : null,
  capital_currency: funds.currency || 'BGN',
  capital_paid_amount: funds.value ? parseFloat(funds.value) : null,
  partners: c.soleCapitalOwner ? [c.soleCapitalOwner] : [],
  beneficial_owners: c.soleCapitalOwner ? [c.soleCapitalOwner] : [],
  latest_revenue_range: c.latestRevenue || null,
  vat_status: r.vat ? 'REGISTERED' : 'NOT_REGISTERED',
  vat_registration_date: parseDate(r.registrationDate),
  vat_deregistration_date: null,
  vat_legal_basis: r.registrationBasis || null,
  is_verified: true,
  verified_source: 'COMPANYBOOK_API',
  verified_at: new Date().toISOString(),
  ocr_aliases: [c.companyName?.name, c.companyNameTransliteration?.name].filter(Boolean),
  metadata: {
    source: 'COMPANYBOOK_API',
    companybook_id: c.id,
    last_updated: c.lastUpdated,
    last_vat_updated: c.lastVATUpdated,
    history: input.history || []
  },
  last_synced_at: new Date().toISOString()
};

return [{
  json: {
    success: true,
    payload: partnerPayload
  }
}];
""".strip()

    nodes = [
        {
            "id": "node-webhook-post",
            "webhookId": "cb-webhook-post-001",
            "name": "Webhook POST",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [200, 200],
            "parameters": {
                "httpMethod": "POST",
                "path": "companybook-sync-partner",
                "responseMode": "responseNode",
                "options": {}
            }
        },
        {
            "id": "node-webhook-get",
            "webhookId": "cb-webhook-get-001",
            "name": "Webhook GET",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [200, 380],
            "parameters": {
                "httpMethod": "GET",
                "path": "companybook-sync-partner",
                "responseMode": "responseNode",
                "options": {}
            }
        },
        {
            "id": "node-normalize-eik",
            "name": "Normalize EIK & Check Input",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [460, 290],
            "parameters": {
                "language": "javaScript",
                "jsCode": code_normalize_eik
            }
        },
        {
            "id": "node-if-valid-eik",
            "name": "IF Valid EIK",
            "type": "n8n-nodes-base.if",
            "typeVersion": 1,
            "position": [700, 290],
            "parameters": {
                "conditions": {
                    "boolean": [
                        {
                            "value1": "={{ $json.valid }}",
                            "value2": True
                        }
                    ]
                }
            }
        },
        {
            "id": "node-respond-invalid-eik",
            "name": "Respond Invalid EIK",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [950, 480],
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify({ success: false, error: $json.error, message: $json.message, input: $json.raw_input }) }}",
                "options": {
                    "responseCode": 400
                }
            }
        },
        {
            "id": "node-companybook-api",
            "name": "CompanyBook API Lookup",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.1,
            "position": [950, 200],
            "continueOnFail": True,
            "parameters": {
                "method": "GET",
                "url": "=https://api.companybook.bg/api/companies/{{ $json.uic }}?with_data=true",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "X-API-Key",
                            "value": "={{ $env.COMPANYBOOK_API_KEY || 'b48fe8cf0c10eedf78148fab73a2e406173caad77205271a940a74df4f7cf8a1' }}"
                        },
                        {
                            "name": "Accept",
                            "value": "application/json"
                        }
                    ]
                },
                "options": {}
            }
        },
        {
            "id": "node-transform-partner",
            "name": "Transform & Map Partner Data",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [1200, 200],
            "parameters": {
                "language": "javaScript",
                "jsCode": code_transform_partner
            }
        },
        {
            "id": "node-if-found",
            "name": "IF Company Found",
            "type": "n8n-nodes-base.if",
            "typeVersion": 1,
            "position": [1440, 200],
            "parameters": {
                "conditions": {
                    "boolean": [
                        {
                            "value1": "={{ $json.success }}",
                            "value2": True
                        }
                    ]
                }
            }
        },
        {
            "id": "node-respond-not-found",
            "name": "Respond Company Not Found",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [1700, 380],
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify({ success: false, error: $json.error, message: $json.message, uic: $json.uic }) }}",
                "options": {
                    "responseCode": 404
                }
            }
        },
        {
            "id": "node-supabase-upsert",
            "name": "Upsert to Supabase accounting.partners",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.1,
            "position": [1700, 120],
            "parameters": {
                "method": "POST",
                "url": "={{ ($env.SUPABASE_URL || 'http://host.docker.internal:8002') + '/rest/v1/partners?on_conflict=country_code,eik' }}",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "apikey",
                            "value": "={{ $env.SUPABASE_SERVICE_ROLE_KEY || 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIiwiaXNzIjoic3VwYWJhc2UiLCJpYXQiOjE3ODIyMjY3OTksImV4cCI6MTkzOTkwNjc5OX0.5DAqw9x0gC7ZH-0UPg4eEkP2LqcW_PRk6O0AEISJUG4' }}"
                        },
                        {
                            "name": "Authorization",
                            "value": "={{ 'Bearer ' + ($env.SUPABASE_SERVICE_ROLE_KEY || 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIiwiaXNzIjoic3VwYWJhc2UiLCJpYXQiOjE3ODIyMjY3OTksImV4cCI6MTkzOTkwNjc5OX0.5DAqw9x0gC7ZH-0UPg4eEkP2LqcW_PRk6O0AEISJUG4') }}"
                        },
                        {
                            "name": "Content-Type",
                            "value": "application/json"
                        },
                        {
                            "name": "Accept-Profile",
                            "value": "accounting"
                        },
                        {
                            "name": "Content-Profile",
                            "value": "accounting"
                        },
                        {
                            "name": "Prefer",
                            "value": "resolution=merge-duplicates,return=representation"
                        }
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.payload) }}",
                "options": {}
            }
        },
        {
            "id": "node-respond-success",
            "name": "Respond Success",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [1980, 120],
            "parameters": {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify({ success: true, message: 'Партньорът е успешно валидиран от CompanyBook и синхронизиран в accounting.partners', partner: Array.isArray($json) ? $json[0] : $json }) }}",
                "options": {
                    "responseCode": 200
                }
            }
        }
    ]

    connections = {
        "Webhook POST": {
            "main": [
                [
                    {
                        "node": "Normalize EIK & Check Input",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "Webhook GET": {
            "main": [
                [
                    {
                        "node": "Normalize EIK & Check Input",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "Normalize EIK & Check Input": {
            "main": [
                [
                    {
                        "node": "IF Valid EIK",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "IF Valid EIK": {
            "main": [
                [
                    {
                        "node": "CompanyBook API Lookup",
                        "type": "main",
                        "index": 0
                    }
                ],
                [
                    {
                        "node": "Respond Invalid EIK",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "CompanyBook API Lookup": {
            "main": [
                [
                    {
                        "node": "Transform & Map Partner Data",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "Transform & Map Partner Data": {
            "main": [
                [
                    {
                        "node": "IF Company Found",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "IF Company Found": {
            "main": [
                [
                    {
                        "node": "Upsert to Supabase accounting.partners",
                        "type": "main",
                        "index": 0
                    }
                ],
                [
                    {
                        "node": "Respond Company Not Found",
                        "type": "main",
                        "index": 0
                    }
                ]
            ]
        },
        "Upsert to Supabase accounting.partners": {
            "main": [
                [
                    {
                        "node": "Respond Success",
                        "type": "main",
                        "index": 0
                    }
                ]
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
            "callerPolicy": "workflowsFromSameOwner",
            "availableInMCP": True
        }
    }

    return workflow

if __name__ == "__main__":
    wf = build_workflow()
    out_path = "/tmp/companybook_partner_sync.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)
    print(f"Workflow saved to {out_path}")
