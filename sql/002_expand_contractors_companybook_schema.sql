-- ===========================================================================
-- Migration: 002_expand_contractors_companybook_schema.sql
-- Description: Expands public.contractors schema based on the full
--              CompanyBook API specifications (company, seat, correspondence,
--              contacts, NKID, management, capital, partners, financials,
--              and trade outlets).
-- Schema: public
-- ===========================================================================

-- 1. Add Transliteration and Alternative Identifiers
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS transliteration TEXT,
    ADD COLUMN IF NOT EXISTS companybook_id VARCHAR(64);

-- 2. Add Structured Decomposed Seat Address (Седалище и адрес на управление по ТР)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS seat_country VARCHAR(64) DEFAULT 'България',
    ADD COLUMN IF NOT EXISTS seat_region VARCHAR(128),
    ADD COLUMN IF NOT EXISTS seat_district VARCHAR(128),
    ADD COLUMN IF NOT EXISTS seat_municipality VARCHAR(128),
    ADD COLUMN IF NOT EXISTS seat_settlement VARCHAR(128),
    ADD COLUMN IF NOT EXISTS seat_area VARCHAR(128),
    ADD COLUMN IF NOT EXISTS seat_street VARCHAR(255),
    ADD COLUMN IF NOT EXISTS seat_street_number VARCHAR(32),
    ADD COLUMN IF NOT EXISTS seat_block VARCHAR(32),
    ADD COLUMN IF NOT EXISTS seat_entrance VARCHAR(16),
    ADD COLUMN IF NOT EXISTS seat_floor VARCHAR(16),
    ADD COLUMN IF NOT EXISTS seat_apartment VARCHAR(16),
    ADD COLUMN IF NOT EXISTS seat_post_code VARCHAR(20),
    ADD COLUMN IF NOT EXISTS seat_district_id INTEGER;

-- 3. Add Correspondence Address (Адрес за кореспонденция с НАП по ДОПК)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS correspondence_address TEXT,
    ADD COLUMN IF NOT EXISTS correspondence_seat JSONB DEFAULT '{}'::jsonb;

-- 4. Add Contact Presence & Direct Contact Channels
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS email VARCHAR(255),
    ADD COLUMN IF NOT EXISTS phone VARCHAR(64),
    ADD COLUMN IF NOT EXISTS fax VARCHAR(64),
    ADD COLUMN IF NOT EXISTS website VARCHAR(255),
    ADD COLUMN IF NOT EXISTS contact_presence JSONB NOT NULL DEFAULT '{"email": false, "phone": false, "website": false}'::jsonb;

-- 5. Add Economic Activity Classifiers (Предмет на дейност, НКИД / NACE)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS subject_of_activity TEXT,
    ADD COLUMN IF NOT EXISTS nkids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS primary_nkid_code VARCHAR(16);

-- 6. Add Governance, Management & Representation (Управление и представителство)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS managers JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS representatives JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS board_of_directors JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 7. Add Capital & Ownership Details (Капитал, съдружници, действителни собственици)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS capital_amount NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS capital_currency VARCHAR(3) DEFAULT 'BGN',
    ADD COLUMN IF NOT EXISTS capital_paid_amount NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS partners JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS beneficial_owners JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 8. Add Physical Trade Outlets & Store Branches (Търговски обекти по Наредба Н-18)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS trade_outlets JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 9. Add Financial Performance Indicators (Годишни финансови отчети)
ALTER TABLE public.contractors
    ADD COLUMN IF NOT EXISTS active_financial_year INTEGER,
    ADD COLUMN IF NOT EXISTS latest_revenue_range VARCHAR(64),
    ADD COLUMN IF NOT EXISTS financial_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;

-- 10. Indexes for High-Performance Search on New Columns
CREATE INDEX IF NOT EXISTS idx_contractors_transliteration ON public.contractors USING gin (to_tsvector('simple', COALESCE(transliteration, '')));
CREATE INDEX IF NOT EXISTS idx_contractors_seat_settlement ON public.contractors (seat_settlement);
CREATE INDEX IF NOT EXISTS idx_contractors_primary_nkid ON public.contractors (primary_nkid_code);
CREATE INDEX IF NOT EXISTS idx_contractors_trade_outlets ON public.contractors USING gin (trade_outlets);
CREATE INDEX IF NOT EXISTS idx_contractors_managers ON public.contractors USING gin (managers);
CREATE INDEX IF NOT EXISTS idx_contractors_financial_year ON public.contractors (active_financial_year);

-- 11. Reload PostgREST schema cache
NOTIFY pgrst, 'reload schema';
