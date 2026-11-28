-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0008 (UP) — richiesta di Mauri, 28/11/2026
--  TRIPLE WATCH: Ordine cliente <-> DDT di vendita <-> Fattura, dai PDF
--  Zucchetti importati (a fine mese tutte le fatture del conto lavoro).
--   - cl_tw_documento:    un DDT di vendita o una fattura letti dal PDF
--   - cl_tw_riferimento:  i documenti richiamati (DDT -> ordine ORDCL,
--                         fattura -> DDT / ordine)
--   - cl_tw_riga:         righe articolo lette (codice, quantita')
--  L'abbinamento si calcola alla lettura: nessun legame rigido, cosi' un
--  documento caricato prima dell'altro si aggancia da solo quando arriva.
--  Eseguire con CL_MIGRA_AUTO=0008 oppure:
--     python -m blueprints.conto_lavoro.migra su --esegui --conferma 0008
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

CREATE TABLE cl_tw_documento (
    id              SERIAL PRIMARY KEY,
    tipo            VARCHAR(10) NOT NULL CHECK (tipo IN ('DDT','FATTURA')),
    numero          VARCHAR(30) NOT NULL,
    anno            INTEGER NOT NULL DEFAULT 0,
    data_documento  DATE,
    cliente_id      INTEGER REFERENCES cl_cliente(id),
    cliente_nome    VARCHAR(200) NOT NULL DEFAULT '',
    importo         NUMERIC(14, 2),
    filename        VARCHAR(255) NOT NULL DEFAULT '',
    testo_grezzo    TEXT NOT NULL DEFAULT '',
    caricato_il     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_cl_tw_documento UNIQUE (tipo, numero, anno)
);

CREATE TABLE cl_tw_riferimento (
    id              SERIAL PRIMARY KEY,
    documento_id    INTEGER NOT NULL REFERENCES cl_tw_documento(id) ON DELETE CASCADE,
    tipo_rif        VARCHAR(10) NOT NULL CHECK (tipo_rif IN ('ORDINE','DDT')),
    numero_rif      VARCHAR(30) NOT NULL
);
CREATE INDEX ix_cl_tw_riferimento ON cl_tw_riferimento (tipo_rif, numero_rif);

CREATE TABLE cl_tw_riga (
    id                  SERIAL PRIMARY KEY,
    documento_id        INTEGER NOT NULL REFERENCES cl_tw_documento(id) ON DELETE CASCADE,
    numero_ordine_rif   VARCHAR(30) NOT NULL DEFAULT '',
    codice              VARCHAR(100) NOT NULL DEFAULT '',
    descrizione         VARCHAR(300) NOT NULL DEFAULT '',
    quantita            NUMERIC(14, 3)
);

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (8, '0008: Triple Watch — DDT di vendita e fatture importati da PDF');
