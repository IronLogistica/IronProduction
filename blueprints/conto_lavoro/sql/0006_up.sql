-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0006 (UP) — richiesta di Mauri, 28/11/2026
--  cl_anomalia: anomalie segnalate da Angelo dal pulsante su ogni codice
--  (Magazzino, Riepilogo ordini). Finiscono nella pagina Anomalie insieme a
--  quelle degli operai da MasterWork; si chiudono con "E' stato risolto" + nota.
--  Rollback: 0006_down.sql
--  Eseguire con CL_MIGRA_AUTO=0006 oppure:
--     python -m blueprints.conto_lavoro.migra su --esegui --conferma 0006
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

CREATE TABLE cl_anomalia (
    id                  SERIAL PRIMARY KEY,
    articolo_id         INTEGER NOT NULL REFERENCES cl_articolo_cliente(id),
    ordine_id           INTEGER REFERENCES cl_ordine(id) ON DELETE SET NULL,
    tipo                VARCHAR(10) NOT NULL DEFAULT 'TEMPO',
    descrizione         TEXT NOT NULL,
    segnalata_da        VARCHAR(80) NOT NULL DEFAULT '',
    segnalata_il        TIMESTAMPTZ NOT NULL DEFAULT now(),
    risolta_il          TIMESTAMPTZ,
    nota_risoluzione    TEXT NOT NULL DEFAULT '',
    risolta_da          VARCHAR(80) NOT NULL DEFAULT '',
    CONSTRAINT ck_cl_anomalia_descrizione CHECK (length(trim(descrizione)) > 0),
    CONSTRAINT ck_cl_anomalia_tipo CHECK (tipo IN ('TEMPO','PREZZO'))
);
CREATE INDEX ix_cl_anomalia_aperte ON cl_anomalia (articolo_id) WHERE risolta_il IS NULL;

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (6, '0006: cl_anomalia (anomalie segnalate da Angelo per codice)');
