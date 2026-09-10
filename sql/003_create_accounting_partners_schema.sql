-- ===========================================================================
-- Migration: 003_create_accounting_partners_schema.sql
-- Description: Creates the `accounting` schema and `accounting.partners` table
--              fully structured according to the comprehensive CompanyBook API
--              specification for company verification by EIK/UIC.
-- Schema: accounting
-- Target: Supabase PostgreSQL
-- ===========================================================================

-- 1. Create schema 'accounting'
CREATE SCHEMA IF NOT EXISTS accounting;

-- 2. Grant permissions to Supabase roles so it appears in Supabase Studio Table Editor
GRANT USAGE ON SCHEMA accounting TO postgres, service_role, authenticated, anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA accounting GRANT ALL ON TABLES TO postgres, service_role, authenticated, anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA accounting GRANT ALL ON SEQUENCES TO postgres, service_role, authenticated, anon;

-- 3. Create table 'accounting.partners'
CREATE TABLE IF NOT EXISTS accounting.partners (
    -- Primary Key & System Identifiers
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    companybook_id VARCHAR(64),
    
    -- Identification & Legal Form
    country_code VARCHAR(2) NOT NULL DEFAULT 'BG',
    eik VARCHAR(32) NOT NULL,
    vat_number VARCHAR(32),
    legal_name TEXT NOT NULL,
    transliteration TEXT,
    trade_name TEXT,
    legal_form VARCHAR(32),
    legal_status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    
    -- Decomposed Registered Office (Седалище и адрес на управление - чл. 12 ТЗ)
    seat_country VARCHAR(64) DEFAULT 'България',
    seat_region VARCHAR(128),
    seat_district VARCHAR(128),
    seat_municipality VARCHAR(128),
    seat_settlement VARCHAR(128),
    seat_area VARCHAR(128),
    seat_street VARCHAR(255),
    seat_street_number VARCHAR(32),
    seat_block VARCHAR(32),
    seat_entrance VARCHAR(16),
    seat_floor VARCHAR(16),
    seat_apartment VARCHAR(16),
    seat_post_code VARCHAR(20),
    seat_district_id INTEGER,
    
    -- Correspondence Address (Адрес за кореспонденция по ДОПК)
    correspondence_address TEXT,
    correspondence_seat JSONB DEFAULT '{}'::jsonb,
    
    -- Contact Information & Signals
    email VARCHAR(255),
    phone VARCHAR(64),
    fax VARCHAR(64),
    website VARCHAR(255),
    contact_presence JSONB NOT NULL DEFAULT '{"email": false, "phone": false, "website": false}'::jsonb,
    
    -- Economic Activity (НКИД / NACE)
    subject_of_activity TEXT,
    nkids JSONB NOT NULL DEFAULT '[]'::jsonb,
    primary_nkid_code VARCHAR(16),
    
    -- Management & Representation (Управление и МОЛ)
    mol_name VARCHAR(255),
    managers JSONB NOT NULL DEFAULT '[]'::jsonb,
    representatives JSONB NOT NULL DEFAULT '[]'::jsonb,
    board_of_directors JSONB NOT NULL DEFAULT '[]'::jsonb,
    
    -- Capital & Ownership Details (Капитал и собственост)
    capital_amount NUMERIC(18, 2),
    capital_currency VARCHAR(3) DEFAULT 'BGN',
    capital_paid_amount NUMERIC(18, 2),
    partners JSONB NOT NULL DEFAULT '[]'::jsonb,
    beneficial_owners JSONB NOT NULL DEFAULT '[]'::jsonb,
    
    -- Physical Trade Outlets & Fiscal Memory Registers (Търговски обекти по Наредба Н-18)
    trade_outlets JSONB NOT NULL DEFAULT '[]'::jsonb,
    
    -- Financial Performance Indicators (Финансови отчети и ГФО)
    active_financial_year INTEGER,
    latest_revenue_range VARCHAR(64),
    financial_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    
    -- VAT and Tax Registration (Данъчен и ДДС статус)
    vat_status VARCHAR(32) NOT NULL DEFAULT 'NOT_REGISTERED',
    vat_registration_date DATE,
    vat_deregistration_date DATE,
    vat_legal_basis TEXT,
    
    -- Pipeline & Verification Metadata
    address TEXT,
    city VARCHAR(128),
    postal_code VARCHAR(20),
    is_verified BOOLEAN NOT NULL DEFAULT false,
    verified_source VARCHAR(64) DEFAULT 'COMPANYBOOK',
    verified_at TIMESTAMPTZ,
    ocr_aliases TEXT[] DEFAULT '{}'::text[],
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Constraints
    CONSTRAINT uq_accounting_partners_country_eik UNIQUE (country_code, eik)
);

-- 4. High-Performance B-Tree & GIN Indexes
CREATE INDEX IF NOT EXISTS idx_acc_partners_eik ON accounting.partners (eik);
CREATE INDEX IF NOT EXISTS idx_acc_partners_vat_number ON accounting.partners (vat_number);
CREATE INDEX IF NOT EXISTS idx_acc_partners_settlement ON accounting.partners (seat_settlement);
CREATE INDEX IF NOT EXISTS idx_acc_partners_primary_nkid ON accounting.partners (primary_nkid_code);
CREATE INDEX IF NOT EXISTS idx_acc_partners_financial_year ON accounting.partners (active_financial_year);
CREATE INDEX IF NOT EXISTS idx_acc_partners_trade_outlets ON accounting.partners USING gin (trade_outlets);
CREATE INDEX IF NOT EXISTS idx_acc_partners_managers ON accounting.partners USING gin (managers);
CREATE INDEX IF NOT EXISTS idx_acc_partners_nkids ON accounting.partners USING gin (nkids);
CREATE INDEX IF NOT EXISTS idx_acc_partners_partners ON accounting.partners USING gin (partners);
CREATE INDEX IF NOT EXISTS idx_acc_partners_financial_metrics ON accounting.partners USING gin (financial_metrics);
CREATE INDEX IF NOT EXISTS idx_acc_partners_metadata ON accounting.partners USING gin (metadata);
CREATE INDEX IF NOT EXISTS idx_acc_partners_transliteration ON accounting.partners USING gin (to_tsvector('simple', COALESCE(transliteration, '')));

-- 5. Trigger for updated_at automatic maintenance
CREATE OR REPLACE FUNCTION accounting.trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_accounting_partners_updated_at ON accounting.partners;
CREATE TRIGGER set_accounting_partners_updated_at
BEFORE UPDATE ON accounting.partners
FOR EACH ROW
EXECUTE FUNCTION accounting.trigger_set_updated_at();

-- 6. Initial Migration from public.contractors into accounting.partners
INSERT INTO accounting.partners (
    id, country_code, eik, vat_number, legal_name, transliteration,
    trade_name, legal_form, legal_status, companybook_id,
    seat_country, seat_region, seat_district, seat_municipality,
    seat_settlement, seat_area, seat_street, seat_street_number,
    seat_block, seat_entrance, seat_floor, seat_apartment,
    seat_post_code, seat_district_id, correspondence_address,
    correspondence_seat, email, phone, fax, website,
    contact_presence, subject_of_activity, nkids, primary_nkid_code,
    mol_name, managers, representatives, board_of_directors,
    capital_amount, capital_currency, capital_paid_amount,
    partners, beneficial_owners, trade_outlets, active_financial_year,
    latest_revenue_range, financial_metrics, vat_status,
    vat_registration_date, vat_deregistration_date, vat_legal_basis,
    address, city, postal_code, is_verified, verified_source,
    verified_at, ocr_aliases, notes, metadata, last_synced_at,
    created_at, updated_at
)
SELECT
    id, country_code, eik, vat_number, legal_name, transliteration,
    trade_name, legal_form, legal_status, companybook_id,
    seat_country, seat_region, seat_district, seat_municipality,
    seat_settlement, seat_area, seat_street, seat_street_number,
    seat_block, seat_entrance, seat_floor, seat_apartment,
    seat_post_code, seat_district_id, correspondence_address,
    correspondence_seat, email, phone, fax, website,
    contact_presence, subject_of_activity, nkids, primary_nkid_code,
    mol_name, managers, representatives, board_of_directors,
    capital_amount, capital_currency, capital_paid_amount,
    partners, beneficial_owners, trade_outlets, active_financial_year,
    latest_revenue_range, financial_metrics, vat_status,
    vat_registration_date, vat_deregistration_date, vat_legal_basis,
    address, city, postal_code, is_verified, verified_source,
    verified_at, ocr_aliases, notes, metadata, last_synced_at,
    created_at, updated_at
FROM public.contractors
ON CONFLICT (country_code, eik) DO UPDATE SET
    vat_number = EXCLUDED.vat_number,
    legal_name = EXCLUDED.legal_name,
    transliteration = EXCLUDED.transliteration,
    trade_name = EXCLUDED.trade_name,
    legal_form = EXCLUDED.legal_form,
    legal_status = EXCLUDED.legal_status,
    seat_country = EXCLUDED.seat_country,
    seat_settlement = EXCLUDED.seat_settlement,
    seat_area = EXCLUDED.seat_area,
    seat_street = EXCLUDED.seat_street,
    seat_street_number = EXCLUDED.seat_street_number,
    seat_post_code = EXCLUDED.seat_post_code,
    seat_district_id = EXCLUDED.seat_district_id,
    correspondence_address = EXCLUDED.correspondence_address,
    correspondence_seat = EXCLUDED.correspondence_seat,
    email = EXCLUDED.email,
    phone = EXCLUDED.phone,
    website = EXCLUDED.website,
    contact_presence = EXCLUDED.contact_presence,
    subject_of_activity = EXCLUDED.subject_of_activity,
    nkids = EXCLUDED.nkids,
    primary_nkid_code = EXCLUDED.primary_nkid_code,
    mol_name = EXCLUDED.mol_name,
    managers = EXCLUDED.managers,
    representatives = EXCLUDED.representatives,
    capital_amount = EXCLUDED.capital_amount,
    capital_currency = EXCLUDED.capital_currency,
    capital_paid_amount = EXCLUDED.capital_paid_amount,
    partners = EXCLUDED.partners,
    beneficial_owners = EXCLUDED.beneficial_owners,
    trade_outlets = EXCLUDED.trade_outlets,
    active_financial_year = EXCLUDED.active_financial_year,
    latest_revenue_range = EXCLUDED.latest_revenue_range,
    financial_metrics = EXCLUDED.financial_metrics,
    vat_status = EXCLUDED.vat_status,
    vat_registration_date = EXCLUDED.vat_registration_date,
    vat_deregistration_date = EXCLUDED.vat_deregistration_date,
    vat_legal_basis = EXCLUDED.vat_legal_basis,
    address = EXCLUDED.address,
    city = EXCLUDED.city,
    postal_code = EXCLUDED.postal_code,
    is_verified = EXCLUDED.is_verified,
    verified_source = EXCLUDED.verified_source,
    verified_at = EXCLUDED.verified_at,
    ocr_aliases = EXCLUDED.ocr_aliases,
    notes = EXCLUDED.notes,
    metadata = EXCLUDED.metadata,
    last_synced_at = EXCLUDED.last_synced_at,
    updated_at = now();

-- 7. Grant table and sequence permissions
GRANT ALL ON TABLE accounting.partners TO postgres, service_role, authenticated, anon;

-- 8. Notify PostgREST to reload schema cache
NOTIFY pgrst, 'reload schema';
