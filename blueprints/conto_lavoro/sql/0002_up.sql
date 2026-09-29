-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0002 (UP)
--  Ordini cliente (import PDF) + predisposizione Triple Watch:
--    - cl_ordine / cl_ordine_riga: l'ordine del cliente è l'origine del
--      "codice di magazzino" (cl_articolo_cliente) — se un codice dell'ordine
--      non esiste ancora per quel cliente, viene creato automaticamente.
--    - cl_fattura, cl_impostazione, e i collegamenti cl_ddt.ordine_id /
--      cl_ddt.fattura_id: predisposti ORA per il meccanismo Triple Watch
--      (Ordine <-> DDT di uscita <-> Fattura), ma NON attivi finché
--      cl_impostazione.triple_watch_attivo non viene impostata a 'on'
--      dall'interruttore in interfaccia.
--  Non tocca nessuna tabella esistente di IronProduction.
--  Rollback completo: 0002_down.sql
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra su --esegui --conferma 0002
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

-- ── Ordini cliente (import PDF) ─────────────────────────────────────────────
CREATE TABLE cl_ordine (
    id                SERIAL PRIMARY KEY,
    cliente_id        INTEGER      NOT NULL REFERENCES cl_cliente(id),
    numero_ordine     VARCHAR(50)  NOT NULL,   -- numero documento del cliente, es. "299/IS"
    rif_cliente       VARCHAR(50)  NOT NULL DEFAULT '',  -- "Rif. n. Cliente", es. "622/DDT"
    data_documento    DATE,
    stato             VARCHAR(20)  NOT NULL DEFAULT 'BOZZA' CHECK (stato IN ('BOZZA','CONFERMATO','ANNULLATO')),
    filename          VARCHAR(255) NOT NULL DEFAULT '',
    testo_grezzo_pdf  TEXT         NOT NULL DEFAULT '',
    note              TEXT         NOT NULL DEFAULT '',
    creato_il         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    confermato_il     TIMESTAMPTZ,
    CONSTRAINT uq_cl_ordine_cliente_numero UNIQUE (cliente_id, numero_ordine)
);

CREATE TABLE cl_ordine_riga (
    id               SERIAL PRIMARY KEY,
    ordine_id        INTEGER       NOT NULL REFERENCES cl_ordine(id) ON DELETE CASCADE,
    n_riga           INTEGER       NOT NULL CHECK (n_riga > 0),
    articolo_id      INTEGER       NOT NULL REFERENCES cl_articolo_cliente(id),
    descrizione      VARCHAR(300)  NOT NULL DEFAULT '',
    quantita         NUMERIC(14,3) NOT NULL CHECK (quantita >= 0),
    prezzo_unitario  NUMERIC(14,4),
    CONSTRAINT uq_cl_ordine_riga UNIQUE (ordine_id, n_riga)
);

-- ── Predisposizione Triple Watch (fatture + interruttore) ───────────────────
CREATE TABLE cl_fattura (
    id              SERIAL PRIMARY KEY,
    cliente_id      INTEGER      NOT NULL REFERENCES cl_cliente(id),
    numero          VARCHAR(50)  NOT NULL,
    data_documento  DATE,
    importo         NUMERIC(14,2),
    filename        VARCHAR(255) NOT NULL DEFAULT '',
    creato_il       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_cl_fattura_cliente_numero UNIQUE (cliente_id, numero)
);

CREATE TABLE cl_impostazione (
    chiave        VARCHAR(60)  PRIMARY KEY,
    valore        VARCHAR(200) NOT NULL DEFAULT '',
    aggiornato_il TIMESTAMPTZ  NOT NULL DEFAULT now()
);
INSERT INTO cl_impostazione (chiave, valore) VALUES ('triple_watch_attivo', 'off');

ALTER TABLE cl_ddt ADD COLUMN ordine_id  INTEGER REFERENCES cl_ordine(id);
ALTER TABLE cl_ddt ADD COLUMN fattura_id INTEGER REFERENCES cl_fattura(id);

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (2, '0002: ordini cliente (import PDF) + predisposizione Triple Watch (fatture, impostazioni, collegamento DDT->ordine/fattura)');
