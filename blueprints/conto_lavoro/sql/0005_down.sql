-- CONTO LAVORO — migrazione 0005 (DOWN)
SET LOCAL search_path TO conto_lavoro;
DROP TABLE IF EXISTS cl_anomalia_risolta;
DROP TABLE IF EXISTS cl_riga_revisione;
UPDATE cl_ordine SET stato = 'CONFERMATO' WHERE stato = 'SOSPESO';
-- il vincolo sullo stato nella 0002 non ha un nome fisso: si toglie qualunque CHECK su "stato"
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'conto_lavoro.cl_ordine'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%stato%'
    LOOP
        EXECUTE format('ALTER TABLE conto_lavoro.cl_ordine DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE cl_ordine ADD CONSTRAINT ck_cl_ordine_stato CHECK (stato IN ('BOZZA','CONFERMATO','ANNULLATO'));
DELETE FROM cl_schema_version WHERE versione = 5;
