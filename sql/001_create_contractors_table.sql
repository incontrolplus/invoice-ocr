-- ===========================================================================
-- Migration: 001_create_contractors_table.sql
-- Description: Centralized registry for verified contractors, companies,
--              VAT status, legal form, and OCR aliases in Supabase.
-- Schema: public
-- ===========================================================================

-- 1. Create Contractors Table
CREATE TABLE IF NOT EXISTS public.contractors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    country_code VARCHAR(2) NOT NULL DEFAULT 'BG',
    eik VARCHAR(20) NOT NULL,
    vat_number VARCHAR(20),
    legal_name TEXT NOT NULL,
    trade_name TEXT,
    legal_form VARCHAR(50),
    mol_name VARCHAR(255),
    address TEXT,
    city VARCHAR(128),
    postal_code VARCHAR(20),
    legal_status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
    vat_status VARCHAR(50) NOT NULL DEFAULT 'REGISTERED',
    vat_registration_date DATE,
    vat_deregistration_date DATE,
    vat_legal_basis TEXT,
    is_verified BOOLEAN NOT NULL DEFAULT true,
    verified_source VARCHAR(50) NOT NULL DEFAULT 'COMMERCIAL_REGISTER',
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ocr_aliases TEXT[] NOT NULL DEFAULT '{}'::text[],
    notes TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_contractors_country_eik UNIQUE (country_code, eik)
);

-- 2. Indexes for High Performance Search & Linking
CREATE INDEX IF NOT EXISTS idx_contractors_eik ON public.contractors (eik);
CREATE INDEX IF NOT EXISTS idx_contractors_vat_number ON public.contractors (vat_number);
CREATE INDEX IF NOT EXISTS idx_contractors_legal_name ON public.contractors USING gin (to_tsvector('simple', legal_name));
CREATE INDEX IF NOT EXISTS idx_contractors_ocr_aliases ON public.contractors USING gin (ocr_aliases);
CREATE INDEX IF NOT EXISTS idx_contractors_vat_status ON public.contractors (vat_status);
CREATE INDEX IF NOT EXISTS idx_contractors_legal_status ON public.contractors (legal_status);

-- 3. Automatic updated_at Trigger
DROP TRIGGER IF EXISTS trg_contractors_updated_at ON public.contractors;
CREATE TRIGGER trg_contractors_updated_at
    BEFORE UPDATE ON public.contractors
    FOR EACH ROW
    EXECUTE FUNCTION public.set_updated_at();

-- 4. Row Level Security & Permissions
ALTER TABLE public.contractors ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow read access to all" ON public.contractors;
CREATE POLICY "Allow read access to all"
    ON public.contractors FOR SELECT
    TO anon, authenticated, service_role
    USING (true);

DROP POLICY IF EXISTS "Allow write access to service_role" ON public.contractors;
CREATE POLICY "Allow write access to service_role"
    ON public.contractors FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS "Allow insert update for authenticated" ON public.contractors;
CREATE POLICY "Allow insert update for authenticated"
    ON public.contractors FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

GRANT ALL ON public.contractors TO postgres, service_role;
GRANT SELECT, INSERT, UPDATE ON public.contractors TO anon, authenticated;

-- 5. Realtime Subscription Support
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        BEGIN
            ALTER PUBLICATION supabase_realtime ADD TABLE public.contractors;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END;
    END IF;
END $$;

-- 6. Reload PostgREST schema cache
NOTIFY pgrst, 'reload schema';
