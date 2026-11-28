-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0005 (UP) — richiesta di Mauri, 28/11/2026
--  Gestione delle anomalie dell'ordine conto lavoro:
--   - cl_ordine.stato: nuovo stato SOSPESO (ordine rilasciato ma fermato).
--   - cl_riga_revisione: codici dell'ordine che Angelo segnala DA REVISIONARE
--     (nuovi tempi, nuove fasi), con nota. Tabella a parte: cl_ordine_riga
--     non cambia, cosi' il codice gia' in uso funziona anche prima della 0005.
--   - cl_anomalia_risolta: anomalie segnalate dagli operai da MasterWork
--     (tasto "Segnala anomalia" sul cellulare) marcate "E' stato risolto",
--     con nota. L'anomalia resta in MasterWork; qui solo la risoluzione.
--  Rollback: 0005_down.sql
--  Eseguire con CL_MIGRA_AUTO=0005 oppure:
--     python -m blueprints.conto_lavoro.migra su --esegui --conferma 0005
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

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
ALTER TABLE cl_ordine ADD CONSTRAINT ck_cl_ordine_stato
    CHECK (stato IN ('BOZZA','CONFERMATO','SOSPESO','ANNULLATO'));

CREATE TABLE cl_riga_revisione (
    riga_id       INTEGER PRIMARY KEY REFERENCES cl_ordine_riga(id) ON DELETE CASCADE,
    nota          TEXT NOT NULL DEFAULT '',
    segnalata_da  VARCHAR(80) NOT NULL DEFAULT '',
    segnalata_il  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cl_anomalia_risolta (
    id                  SERIAL PRIMARY KEY,
    mw_id               INTEGER NOT NULL UNIQUE,
    operaio             VARCHAR(120) NOT NULL DEFAULT '',
    codice_interno      VARCHAR(80) NOT NULL DEFAULT '',
    motivo              TEXT NOT NULL DEFAULT '',
    segnalata_il        TIMESTAMPTZ,
    nota_risoluzione    TEXT NOT NULL DEFAULT '',
    risolta_da          VARCHAR(80) NOT NULL DEFAULT '',
    risolta_il          TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (5, '0005: ordine SOSPESO, righe da revisionare, anomalie MasterWork risolte');
