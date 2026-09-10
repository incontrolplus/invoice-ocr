# CompanyBook API Specification & Accounting Partners Schema Architecture

## 1. Резюме и контекст (Executive Summary)

За гарантиране на 100% отчетност и съответствие със законовите изисквания по чл. 114 от ЗДДС (относно задължителните реквизити на фактурата – седалище и адрес на управление) и чл. 26 от Наредба Н-18/2006 г. на МФ (относно фискалните бонове и търговските обекти), в self-hosted Supabase на сървъра `macmini-primary` бе създадена нова схема **`accounting`** с главна таблица **`accounting.partners`**, съдържаща пълните **63 колони**, покриващи на 100% спецификацията на **CompanyBook API**.

Разширението стъпва директно върху официалната спецификация на **CompanyBook API** (`https://companybook.bg/api-docs` и `llms-full.txt`) за ендпойнта за пълна проверка по ЕИК:
`GET /api/companies/:uic?with_data=true` и `GET /api/companies/:uic/financial?year=YYYY`.
Таблицата е налична в Supabase Studio и през PostgREST API с хедър `Accept-Profile: accounting`.

---

## 2. Идентифицирани полета от CompanyBook API и съответствие в Базата Данни

| Група данни | Поле от CompanyBook API (`JSON`) | Тип данни в PostgreSQL | Колона в `public.contractors` | Описание и нормативно предназначение |
|---|---|---|---|---|
| **Идентификация** | `uic` | `VARCHAR(32)` | `eik` | ЕИК / БУЛСТАТ на лицето |
| | `companyName.name` | `TEXT` | `legal_name` | Официално наименование по ТР (кирилица) |
| | `companyNameTransliteration.name`| `TEXT` | `transliteration` | Наименование на латиница за международни фактури |
| | `legalForm.name` | `VARCHAR(32)` | `legal_form` | Правна форма (ЕООД, ООД, АД, ЕТ) |
| | `status` (`N`, `L`, `B`, `D`) | `VARCHAR(32)` | `legal_status` | Правен статус (ACTIVE, LIQUIDATION, BANKRUPTCY) |
| | `companybook_id` | `VARCHAR(64)` | `companybook_id` | Уникален вътрешен идентификатор в CompanyBook |
| **Седалище по ТР (чл. 12 ТЗ)** | `seat.country` | `VARCHAR(64)` | `seat_country` | Държава по седалище (по подразбиране 'България') |
| | `seat.region` | `VARCHAR(128)` | `seat_region` | Област (напр. София (столица), Плевен) |
| | `seat.district` | `VARCHAR(128)` | `seat_district` | Областен район |
| | `seat.municipality` | `VARCHAR(128)` | `seat_municipality` | Община (Столична, Плевен и др.) |
| | `seat.settlement` | `VARCHAR(128)` | `seat_settlement` | Населено място (гр. София, с. Горна Митрополия) |
| | `seat.area` | `VARCHAR(128)` | `seat_area` | Район / ж.к. / квартал (р-н Красна поляна) |
| | `seat.street` | `VARCHAR(255)` | `seat_street` | Наименование на улица / булевард (ул. Суходолска) |
| | `seat.streetNumber` | `VARCHAR(32)` | `seat_street_number` | Номер на сграда (201) |
| | `seat.block` | `VARCHAR(32)` | `seat_block` | Блок (за жилищни комплекси) |
| | `seat.entrance` | `VARCHAR(16)` | `seat_entrance` | Вход |
| | `seat.floor` | `VARCHAR(16)` | `seat_floor` | Етаж |
| | `seat.apartment` | `VARCHAR(16)` | `seat_apartment` | Апартамент / офис |
| | `seat.postCode` | `VARCHAR(20)` | `seat_post_code` | Пощенски код (1373) |
| | `seat.districtid` | `INTEGER` | `seat_district_id` | ЕКАТТЕ код на населеното място (напр. 68134 за София) |
| **Адрес за кореспонденция**| `correspondenceSeat` (декомпозиран)| `JSONB` | `correspondence_seat` | Пълен структуриран адрес за връчване по ДОПК |
| | Текстов изглед на адреса | `TEXT` | `correspondence_address` | Форматиран адрес за съобщения и призовки |
| **Контакти и сигнали** | `contacts.email` | `VARCHAR(255)` | `email` | Официален корпоративен имейл |
| | `contacts.phone` | `VARCHAR(64)` | `phone` | Телефонен номер |
| | `contacts.fax` | `VARCHAR(64)` | `fax` | Факс |
| | `contacts.website` | `VARCHAR(255)` | `website` | Фирмен уебсайт (напр. `https://nargile.bg`) |
| | `contactPresence` | `JSONB` | `contact_presence` | Флагове за наличие на канали (`email`, `phone`, `web`) |
| **Икономическа дейност** | `subjectOfActivity` | `TEXT` | `subject_of_activity` | Предмет на дейност по Търговския регистър |
| | `nkids` (списък от кодове) | `JSONB` | `nkids` | Списък НКИД-2008 дейности (`code`, `description`, `primary`)|
| | `primary_nkid_code` | `VARCHAR(16)` | `primary_nkid_code` | Основен 4-значен НКИД код (напр. `47.78`) |
| **Управление и МОЛ** | `managers` | `JSONB` | `managers` | Списък управители (`name`, `indent`, `address`) |
| | `representatives` | `JSONB` | `representatives` | Представители и начин на представляване |
| | `boardOfDirectors` | `JSONB` | `board_of_directors` | Съвет на директорите (за АД) |
| | `mol_name` (първи управител) | `VARCHAR(255)` | `mol_name` | Канонично МОЛ лице за фактури (чл. 6 ЗСч) |
| **Капитал и собственост** | `capital.amount` | `NUMERIC(18,2)` | `capital_amount` | Размер на записания капитал |
| | `capital.currency` | `VARCHAR(3)` | `capital_currency` | Валута (BGN, EUR) |
| | `capital.paidAmount` | `NUMERIC(18,2)` | `capital_paid_amount` | Внесен капитал |
| | `partners` | `JSONB` | `partners` | Съдружници, дялове и вид отговорност |
| | `beneficial_owners` | `JSONB` | `beneficial_owners` | Действителни собственици по чл. 61 ЗМИП |
| **Търговски обекти (Н-18)** | `trade_outlets` | `JSONB` | `trade_outlets` | Физически магазини, складове, адреси на касови апарати |
| **Финанси и отчети** | `activeFinancialYear` | `INTEGER` | `active_financial_year` | Последна финансова година с публикуван ГФО в ТР |
| | `latestRevenue` | `VARCHAR(64)` | `latest_revenue_range` | Оборот / диапазон на приходите |
| | `financial_metrics` | `JSONB` | `financial_metrics` | Приходи, разходи, печалба, персонал, EBITDA, ROA |
| **Данъчен и ДДС статус** | `registerInfo.vat` | `VARCHAR(32)` | `vat_number` | ДДС номер по чл. 94 ЗДДС |
| | `registerInfo.vatRegistered` | `VARCHAR(32)` | `vat_status` | REGISTERED, NOT_REGISTERED, DEREGISTERED |
| | `registerInfo.vatRegistrationDate` | `DATE` | `vat_registration_date` | Дата на регистрация по ЗДДС |
| | `registerInfo.deregistrationDate` | `DATE` | `vat_deregistration_date`| Дата на дерегистрация по ЗДДС |
| | `registerInfo.vatLegalBasis` | `TEXT` | `vat_legal_basis` | Правно основание за регистрация (чл. 100, чл. 96) |
| **Синхронизация** | `last_synced_at` | `TIMESTAMPTZ` | `last_synced_at` | Времева марка на последна синхронизация с API/ТР |

---

## 3. Пример за реално съответствие: „Джентълмен Груп“ ЕООД

```json
{
  "eik": "203818240",
  "vat_number": "BG203818240",
  "legal_name": "„ДЖЕНТЪЛМЕН ГРУП“ ЕООД",
  "transliteration": "DZENTELMEN GRUP",
  "trade_name": "Джентълмен Груп / Nargile.bg",
  "legal_form": "ЕООД",
  "mol_name": "Георги Ангелов Георгиев",
  "address": "гр. София 1373, р-н Красна поляна, ул. Суходолска 201",
  "seat_settlement": "гр. София",
  "seat_area": "р-н Красна поляна",
  "seat_street": "ул. Суходолска",
  "seat_street_number": "201",
  "seat_post_code": "1373",
  "seat_district_id": 68134,
  "trade_outlets": [
    {
      "name": "Магазин Nargile.bg",
      "address": "гр. София, бул. Патриарх Евтимий 77",
      "type": "МАГАЗИН",
      "brand": "Nargile.bg"
    }
  ],
  "website": "https://nargile.bg",
  "contact_presence": {
    "email": true,
    "phone": true,
    "website": true
  },
  "primary_nkid_code": "47.78",
  "managers": [
    {
      "name": "Георги Ангелов Георгиев",
      "role": "Управител"
    }
  ],
  "capital_amount": 2.00,
  "capital_currency": "BGN",
  "vat_status": "REGISTERED",
  "vat_registration_date": "2016-12-08",
  "vat_legal_basis": "чл. 100, ал. 1 ЗДДС",
  "is_verified": true,
  "verified_source": "COMMERCIAL_REGISTER"
}
```

---

## 4. Архитектурна превенция на разминаванията в адресите

1. **Разпознаване по Наредба Н-18 (`invoice_core/extraction.py`)**:
   - При сканиране на фактури с прикрепен касов бон или шапка по Н-18, линиите се анализират последователно.
   - Адресът преди маркерите `МАГАЗИН:`, `ОБЕКТ:`, `ФИЛИАЛ:` се идентифицира като седалище.
   - Адресът след маркера за търговски обект се класифицира като търговски обект/място на сделката.

2. **Канонично съгласуване на ниво пайплайн (`invoice_core/pipeline.py`)**:
   - Чрез функцията `reconcile_party_with_contractor_master`, при откриване на ЕИК във фактурата:
     - `party.address` **винаги задължително** се установява на официалното седалище (`seat_address` / `address` по чл. 114 ЗДДС).
     - Ако OCR е разчел физически магазин (напр. `бул. Патриарх Евтимий 77`), този адрес автоматично се пренасочва към `metadata.place_issued` и `raw_ocr_evidence["trade_outlet_address"]`.
     - `party.mol` и `party.name` се допълват и коригират автоматично при непълноти в сканирания документ.
