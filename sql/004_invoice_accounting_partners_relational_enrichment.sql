-- =============================================================================
-- Migration 004: Relational Partner Enrichment Formula
-- Links public.invoices with accounting.partners to automatically populate
-- missing or corrupted OCR party fields with canonical master registry data.
-- =============================================================================

-- 1. Add Foreign Key columns linking invoices to accounting.partners
ALTER TABLE public.invoices
ADD COLUMN IF NOT EXISTS supplier_partner_id UUID REFERENCES accounting.partners(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS recipient_partner_id UUID REFERENCES accounting.partners(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_invoices_supplier_partner_id ON public.invoices(supplier_partner_id);
CREATE INDEX IF NOT EXISTS idx_invoices_recipient_partner_id ON public.invoices(recipient_partner_id);

-- 2. Trigger function to enforce relational formula and data enrichment
CREATE OR REPLACE FUNCTION public.handle_invoice_partner_enrichment()
RETURNS trigger AS $$
DECLARE
    p_supp RECORD;
    p_rec RECORD;
    supp_eik_clean TEXT;
    rec_eik_clean TEXT;
    is_building_11 BOOLEAN := FALSE;
    supp_full_name TEXT;
    rec_full_name TEXT;
BEGIN
    -- Extract clean digits for EIK lookup
    supp_eik_clean := regexp_replace(COALESCE(NEW.supplier_eik, ''), '\D', '', 'g');
    rec_eik_clean := regexp_replace(COALESCE(NEW.recipient_eik, ''), '\D', '', 'g');

    -- Check if invoice relates to Building 11 (EIK 206062202)
    IF rec_eik_clean = '206062202' 
       OR supp_eik_clean = '206062202'
       OR COALESCE(NEW.recipient_name, '') ILIKE '%билдинг 11%'
       OR COALESCE(NEW.supplier_name, '') ILIKE '%билдинг 11%'
    THEN
        is_building_11 := TRUE;
    END IF;

    -- Lookup Supplier in accounting.partners
    IF supp_eik_clean <> '' AND supp_eik_clean <> '000000000' THEN
        SELECT * INTO p_supp
        FROM accounting.partners
        WHERE country_code = 'BG' AND (eik = supp_eik_clean OR eik = NEW.supplier_eik)
        LIMIT 1;

        IF p_supp.id IS NOT NULL THEN
            NEW.supplier_partner_id := p_supp.id;

            -- Determine full legal name with legal form
            IF p_supp.legal_name IS NOT NULL AND p_supp.legal_name <> '' THEN
                IF p_supp.legal_form IS NOT NULL AND p_supp.legal_form <> '' 
                   AND p_supp.legal_name NOT ILIKE '%' || p_supp.legal_form || '%' 
                THEN
                    supp_full_name := p_supp.legal_name || ' ' || p_supp.legal_form;
                ELSE
                    supp_full_name := p_supp.legal_name;
                END IF;
            ELSE
                supp_full_name := p_supp.trade_name;
            END IF;

            -- If Building 11 invoice OR missing/partial OCR data, replace with canonical master data
            IF is_building_11 OR NEW.supplier_name IS NULL OR NEW.supplier_name = '' OR NEW.supplier_name ILIKE '%неизвестен%' THEN
                IF supp_full_name IS NOT NULL AND supp_full_name <> '' THEN
                    NEW.supplier_name := supp_full_name;
                END IF;
                IF p_supp.vat_number IS NOT NULL AND p_supp.vat_number <> '' THEN
                    NEW.supplier_vat_number := p_supp.vat_number;
                END IF;
                IF p_supp.address IS NOT NULL AND p_supp.address <> '' THEN
                    NEW.supplier_address := p_supp.address;
                END IF;
                IF COALESCE(p_supp.city, p_supp.seat_settlement) IS NOT NULL THEN
                    NEW.supplier_city := SUBSTRING(COALESCE(p_supp.city, p_supp.seat_settlement) FROM 1 FOR 128);
                END IF;
                NEW.supplier_country := 'BGR';
                IF p_supp.mol_name IS NOT NULL AND p_supp.mol_name <> '' THEN
                    NEW.supplier_mol := SUBSTRING(p_supp.mol_name FROM 1 FOR 255);
                END IF;
                IF p_supp.phone IS NOT NULL AND p_supp.phone <> '' THEN
                    NEW.supplier_phone := SUBSTRING(p_supp.phone FROM 1 FOR 64);
                END IF;
                IF p_supp.email IS NOT NULL AND p_supp.email <> '' THEN
                    NEW.supplier_email := SUBSTRING(p_supp.email FROM 1 FOR 255);
                END IF;
            ELSE
                -- Always fill in missing fields even if name was present
                IF (NEW.supplier_mol IS NULL OR NEW.supplier_mol = '') AND p_supp.mol_name IS NOT NULL THEN
                    NEW.supplier_mol := SUBSTRING(p_supp.mol_name FROM 1 FOR 255);
                END IF;
                IF (NEW.supplier_phone IS NULL OR NEW.supplier_phone = '') AND p_supp.phone IS NOT NULL THEN
                    NEW.supplier_phone := SUBSTRING(p_supp.phone FROM 1 FOR 64);
                END IF;
                IF (NEW.supplier_email IS NULL OR NEW.supplier_email = '') AND p_supp.email IS NOT NULL THEN
                    NEW.supplier_email := SUBSTRING(p_supp.email FROM 1 FOR 255);
                END IF;
                IF (NEW.supplier_city IS NULL OR NEW.supplier_city = '') AND COALESCE(p_supp.city, p_supp.seat_settlement) IS NOT NULL THEN
                    NEW.supplier_city := SUBSTRING(COALESCE(p_supp.city, p_supp.seat_settlement) FROM 1 FOR 128);
                END IF;
                IF (NEW.supplier_address IS NULL OR NEW.supplier_address = '') AND p_supp.address IS NOT NULL THEN
                    NEW.supplier_address := p_supp.address;
                END IF;
            END IF;
        END IF;
    END IF;

    -- Lookup Recipient in accounting.partners
    IF rec_eik_clean <> '' AND rec_eik_clean <> '000000000' THEN
        SELECT * INTO p_rec
        FROM accounting.partners
        WHERE country_code = 'BG' AND (eik = rec_eik_clean OR eik = NEW.recipient_eik)
        LIMIT 1;

        IF p_rec.id IS NOT NULL THEN
            NEW.recipient_partner_id := p_rec.id;

            -- Determine full legal name with legal form
            IF p_rec.legal_name IS NOT NULL AND p_rec.legal_name <> '' THEN
                IF p_rec.legal_form IS NOT NULL AND p_rec.legal_form <> '' 
                   AND p_rec.legal_name NOT ILIKE '%' || p_rec.legal_form || '%' 
                THEN
                    rec_full_name := p_rec.legal_name || ' ' || p_rec.legal_form;
                ELSE
                    rec_full_name := p_rec.legal_name;
                END IF;
            ELSE
                rec_full_name := p_rec.trade_name;
            END IF;

            -- If Building 11 invoice OR missing/partial OCR data, replace with canonical master data
            IF is_building_11 OR NEW.recipient_name IS NULL OR NEW.recipient_name = '' OR NEW.recipient_name ILIKE '%неизвестен%' THEN
                IF rec_full_name IS NOT NULL AND rec_full_name <> '' THEN
                    NEW.recipient_name := rec_full_name;
                END IF;
                IF p_rec.vat_number IS NOT NULL AND p_rec.vat_number <> '' THEN
                    NEW.recipient_vat_number := p_rec.vat_number;
                END IF;
                IF p_rec.address IS NOT NULL AND p_rec.address <> '' THEN
                    NEW.recipient_address := p_rec.address;
                END IF;
                IF COALESCE(p_rec.city, p_rec.seat_settlement) IS NOT NULL THEN
                    NEW.recipient_city := SUBSTRING(COALESCE(p_rec.city, p_rec.seat_settlement) FROM 1 FOR 128);
                END IF;
                NEW.recipient_country := 'BGR';
                IF p_rec.mol_name IS NOT NULL AND p_rec.mol_name <> '' THEN
                    NEW.recipient_mol := SUBSTRING(p_rec.mol_name FROM 1 FOR 255);
                END IF;
                IF p_rec.phone IS NOT NULL AND p_rec.phone <> '' THEN
                    NEW.recipient_phone := SUBSTRING(p_rec.phone FROM 1 FOR 64);
                END IF;
                IF p_rec.email IS NOT NULL AND p_rec.email <> '' THEN
                    NEW.recipient_email := SUBSTRING(p_rec.email FROM 1 FOR 255);
                END IF;
            ELSE
                -- Always fill in missing fields even if name was present
                IF (NEW.recipient_mol IS NULL OR NEW.recipient_mol = '') AND p_rec.mol_name IS NOT NULL THEN
                    NEW.recipient_mol := SUBSTRING(p_rec.mol_name FROM 1 FOR 255);
                END IF;
                IF (NEW.recipient_phone IS NULL OR NEW.recipient_phone = '') AND p_rec.phone IS NOT NULL THEN
                    NEW.recipient_phone := SUBSTRING(p_rec.phone FROM 1 FOR 64);
                END IF;
                IF (NEW.recipient_email IS NULL OR NEW.recipient_email = '') AND p_rec.email IS NOT NULL THEN
                    NEW.recipient_email := SUBSTRING(p_rec.email FROM 1 FOR 255);
                END IF;
                IF (NEW.recipient_city IS NULL OR NEW.recipient_city = '') AND COALESCE(p_rec.city, p_rec.seat_settlement) IS NOT NULL THEN
                    NEW.recipient_city := SUBSTRING(COALESCE(p_rec.city, p_rec.seat_settlement) FROM 1 FOR 128);
                END IF;
                IF (NEW.recipient_address IS NULL OR NEW.recipient_address = '') AND p_rec.address IS NOT NULL THEN
                    NEW.recipient_address := p_rec.address;
                END IF;
            END IF;
        END IF;
    END IF;

    -- Update ocr_structured_json if present
    IF NEW.ocr_structured_json IS NOT NULL THEN
        IF p_supp.id IS NOT NULL THEN
            NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{supplier,name}', to_jsonb(NEW.supplier_name));
            IF NEW.supplier_mol IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{supplier,mol}', to_jsonb(NEW.supplier_mol));
            END IF;
            IF NEW.supplier_address IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{supplier,address}', to_jsonb(NEW.supplier_address));
            END IF;
            IF NEW.supplier_city IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{supplier,city}', to_jsonb(NEW.supplier_city));
            END IF;
            IF NEW.supplier_phone IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{supplier,phone}', to_jsonb(NEW.supplier_phone));
            END IF;
            IF NEW.supplier_email IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{supplier,email}', to_jsonb(NEW.supplier_email));
            END IF;
        END IF;

        IF p_rec.id IS NOT NULL THEN
            NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{recipient,name}', to_jsonb(NEW.recipient_name));
            IF NEW.recipient_mol IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{recipient,mol}', to_jsonb(NEW.recipient_mol));
            END IF;
            IF NEW.recipient_address IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{recipient,address}', to_jsonb(NEW.recipient_address));
            END IF;
            IF NEW.recipient_city IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{recipient,city}', to_jsonb(NEW.recipient_city));
            END IF;
            IF NEW.recipient_phone IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{recipient,phone}', to_jsonb(NEW.recipient_phone));
            END IF;
            IF NEW.recipient_email IS NOT NULL THEN
                NEW.ocr_structured_json := jsonb_set(NEW.ocr_structured_json, '{recipient,email}', to_jsonb(NEW.recipient_email));
            END IF;
        END IF;
    END IF;

    -- Record enrichment audit in custom_metadata
    IF p_supp.id IS NOT NULL OR p_rec.id IS NOT NULL THEN
        NEW.custom_metadata := COALESCE(NEW.custom_metadata, '{}'::jsonb) || jsonb_build_object(
            'enriched_from_accounting_partners', true,
            'enriched_at', now(),
            'is_building_11', is_building_11,
            'supplier_partner_id', p_supp.id,
            'recipient_partner_id', p_rec.id
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_invoice_partner_enrichment ON public.invoices;
CREATE TRIGGER trg_invoice_partner_enrichment
BEFORE INSERT OR UPDATE ON public.invoices
FOR EACH ROW
EXECUTE FUNCTION public.handle_invoice_partner_enrichment();

-- 3. Create relational view exposing live accounting.partners links
CREATE OR REPLACE VIEW public.v_invoices_enriched AS
SELECT
  i.*,
  sp.legal_name AS sp_legal_name,
  sp.legal_form AS sp_legal_form,
  CASE
    WHEN sp.legal_form IS NOT NULL AND sp.legal_form <> '' AND sp.legal_name NOT ILIKE '%' || sp.legal_form || '%'
    THEN sp.legal_name || ' ' || sp.legal_form
    ELSE sp.legal_name
  END AS sp_canonical_name,
  sp.vat_number AS sp_vat_number,
  sp.address AS sp_address,
  COALESCE(sp.city, sp.seat_settlement) AS sp_city,
  sp.mol_name AS sp_mol,
  sp.phone AS sp_phone,
  sp.email AS sp_email,
  sp.is_verified AS sp_is_verified,
  rp.legal_name AS rp_legal_name,
  rp.legal_form AS rp_legal_form,
  CASE
    WHEN rp.legal_form IS NOT NULL AND rp.legal_form <> '' AND rp.legal_name NOT ILIKE '%' || rp.legal_form || '%'
    THEN rp.legal_name || ' ' || rp.legal_form
    ELSE rp.legal_name
  END AS rp_canonical_name,
  rp.vat_number AS rp_vat_number,
  rp.address AS rp_address,
  COALESCE(rp.city, rp.seat_settlement) AS rp_city,
  rp.mol_name AS rp_mol,
  rp.phone AS rp_phone,
  rp.email AS rp_email,
  rp.is_verified AS rp_is_verified
FROM public.invoices i
LEFT JOIN accounting.partners sp ON i.supplier_partner_id = sp.id OR (sp.country_code = 'BG' AND sp.eik = regexp_replace(i.supplier_eik, '\D', '', 'g'))
LEFT JOIN accounting.partners rp ON i.recipient_partner_id = rp.id OR (rp.country_code = 'BG' AND rp.eik = regexp_replace(i.recipient_eik, '\D', '', 'g'));

GRANT SELECT ON public.v_invoices_enriched TO anon, authenticated, service_role;
