-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0001 (UP)
--  Crea SOLO lo schema dedicato "conto_lavoro" e le sue tabelle.
--  Non tocca nessuna tabella esistente di IronProduction.
--  Rollback completo: 0001_down.sql  (DROP SCHEMA conto_lavoro CASCADE)
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra su --esegui --conferma 0001
-- ════════════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS conto_lavoro;
SET LOCAL search_path TO conto_lavoro;

CREATE TABLE cl_schema_version (
    versione     INTEGER PRIMARY KEY,
    descrizione  VARCHAR(200) NOT NULL,
    applicata_il TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Utenti e ruoli (login del modulo, PR 2) ────────────────────────────────
CREATE TABLE cl_utente (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(60)  NOT NULL UNIQUE,
    nome          VARCHAR(120) NOT NULL DEFAULT '',
    password_hash VARCHAR(255) NOT NULL,
    attivo        BOOLEAN      NOT NULL DEFAULT TRUE,
    creato_il     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE cl_utente_ruolo (
    utente_id INTEGER     NOT NULL REFERENCES cl_utente(id) ON DELETE CASCADE,
    ruolo     VARCHAR(30) NOT NULL CHECK (ruolo IN ('MAGAZZINO','CAPO_REPARTO','AMMINISTRAZIONE','DIREZIONE','LETTURA')),
    PRIMARY KEY (utente_id, ruolo)
);

-- ── Anagrafiche ────────────────────────────────────────────────────────────
CREATE TABLE cl_cliente (
    id              SERIAL PRIMARY KEY,
    ragione_sociale VARCHAR(200) NOT NULL,
    piva            VARCHAR(20),
    codice_fiscale  VARCHAR(20),
    indirizzo       VARCHAR(200) NOT NULL DEFAULT '',
    cap             VARCHAR(10)  NOT NULL DEFAULT '',
    citta           VARCHAR(100) NOT NULL DEFAULT '',
    provincia       VARCHAR(5)   NOT NULL DEFAULT '',
    nazione         VARCHAR(2)   NOT NULL DEFAULT 'IT',
    attivo          BOOLEAN      NOT NULL DEFAULT TRUE,
    creato_il       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_cl_cliente_piva ON cl_cliente (piva) WHERE piva IS NOT NULL AND piva <> '';

CREATE TABLE cl_articolo_cliente (
    id           SERIAL PRIMARY KEY,
    cliente_id   INTEGER      NOT NULL REFERENCES cl_cliente(id),
    codice       VARCHAR(100) NOT NULL,
    descrizione  VARCHAR(300) NOT NULL DEFAULT '',
    udm          VARCHAR(10)  NOT NULL CHECK (udm IN ('PZ','KG','M','KIT')),
    tracciamento VARCHAR(10)  NOT NULL DEFAULT 'LOTTO' CHECK (tracciamento IN ('LOTTO','MATRICOLA')),
    attivo       BOOLEAN      NOT NULL DEFAULT TRUE,
    creato_il    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_cl_articolo_cliente_codice UNIQUE (cliente_id, codice),
    -- serve alla FK composita di cl_lotto: il lotto non può cambiare cliente
    CONSTRAINT uq_cl_articolo_cliente_id_cliente UNIQUE (id, cliente_id)
);

-- ── Documenti di trasporto (ingresso dal cliente / uscita verso il cliente) ─
CREATE TABLE cl_ddt (
    id                  SERIAL PRIMARY KEY,
    direzione           VARCHAR(3)   NOT NULL CHECK (direzione IN ('IN','OUT')),
    cliente_id          INTEGER      NOT NULL REFERENCES cl_cliente(id),
    stato               VARCHAR(20)  NOT NULL DEFAULT 'BOZZA',
    causale             VARCHAR(100) NOT NULL DEFAULT '',
    numero_cliente      VARCHAR(50),          -- IN: numero scritto sul DDT del cliente
    data_documento      DATE,
    anno                INTEGER,              -- OUT: numerazione nostra, assegnata all'emissione
    numero              INTEGER,
    note                TEXT         NOT NULL DEFAULT '',
    creato_da           INTEGER      REFERENCES cl_utente(id),
    creato_il           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    confermato_da       INTEGER      REFERENCES cl_utente(id),
    confermato_il       TIMESTAMPTZ,
    annullato_da        INTEGER      REFERENCES cl_utente(id),
    annullato_il        TIMESTAMPTZ,
    motivo_annullamento TEXT,
    CONSTRAINT ck_cl_ddt_stato CHECK (
        (direzione = 'IN'  AND stato IN ('BOZZA','VERIFICATO','CONFERMATO','ANNULLATO')) OR
        (direzione = 'OUT' AND stato IN ('BOZZA','EMESSO','ANNULLATO'))),
    CONSTRAINT ck_cl_ddt_numero_out CHECK (direzione = 'IN' OR stato = 'BOZZA' OR (anno IS NOT NULL AND numero IS NOT NULL))
);
CREATE UNIQUE INDEX ux_cl_ddt_in  ON cl_ddt (cliente_id, numero_cliente, data_documento) WHERE direzione = 'IN' AND stato <> 'ANNULLATO';
CREATE UNIQUE INDEX ux_cl_ddt_out ON cl_ddt (anno, numero) WHERE direzione = 'OUT' AND numero IS NOT NULL;

CREATE TABLE cl_ddt_riga (
    id             SERIAL PRIMARY KEY,
    ddt_id         INTEGER        NOT NULL REFERENCES cl_ddt(id) ON DELETE CASCADE,
    n_riga         INTEGER        NOT NULL CHECK (n_riga > 0),
    articolo_id    INTEGER        NOT NULL REFERENCES cl_articolo_cliente(id),
    descrizione    VARCHAR(300)   NOT NULL DEFAULT '',
    qta_dichiarata NUMERIC(14,3)  NOT NULL CHECK (qta_dichiarata >= 0),
    qta_verificata NUMERIC(14,3)  CHECK (qta_verificata >= 0),
    cassone        VARCHAR(50)    NOT NULL DEFAULT '',
    matricola      VARCHAR(100),
    lotto_id       INTEGER,       -- FK aggiunta dopo cl_lotto
    CONSTRAINT uq_cl_ddt_riga UNIQUE (ddt_id, n_riga)
);

-- ── Lotti: l'unità di tracciabilità del bene del cliente ───────────────────
CREATE TABLE cl_lotto (
    id                  SERIAL PRIMARY KEY,
    codice              VARCHAR(30)  NOT NULL UNIQUE,     -- es. L-2026-000001
    cliente_id          INTEGER      NOT NULL,
    articolo_id         INTEGER      NOT NULL,
    ddt_riga_origine_id INTEGER      REFERENCES cl_ddt_riga(id),
    cassone             VARCHAR(50)  NOT NULL DEFAULT '',
    matricola           VARCHAR(100),
    creato_il           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT fk_cl_lotto_articolo_stesso_cliente
        FOREIGN KEY (articolo_id, cliente_id) REFERENCES cl_articolo_cliente(id, cliente_id)
);
CREATE UNIQUE INDEX ux_cl_lotto_matricola ON cl_lotto (articolo_id, matricola) WHERE matricola IS NOT NULL;
ALTER TABLE cl_ddt_riga ADD CONSTRAINT fk_cl_ddt_riga_lotto FOREIGN KEY (lotto_id) REFERENCES cl_lotto(id);

-- ── Ledger dei movimenti: SOLO INSERT ──────────────────────────────────────
CREATE TABLE cl_movimento (
    id                 BIGSERIAL PRIMARY KEY,
    lotto_id           INTEGER        NOT NULL REFERENCES cl_lotto(id),
    causale            VARCHAR(20)    NOT NULL,
    quantita           NUMERIC(14,3)  NOT NULL CHECK (quantita <> 0),
    ddt_riga_id        INTEGER        REFERENCES cl_ddt_riga(id),
    riferimento        VARCHAR(100)   NOT NULL DEFAULT '',
    chiave_idempotenza VARCHAR(120)   NOT NULL UNIQUE,
    utente_id          INTEGER        REFERENCES cl_utente(id),
    note               VARCHAR(300)   NOT NULL DEFAULT '',
    creato_il          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT ck_cl_movimento_segno CHECK (
        (causale IN ('CARICO_DDT','PRODUZIONE','SCARTO') AND quantita > 0) OR
        (causale IN ('CONSUMO','RESO') AND quantita < 0) OR
        (causale IN ('STORNO','RETTIFICA')))
);
CREATE INDEX ix_cl_movimento_lotto ON cl_movimento (lotto_id);

-- ── Audit: SOLO INSERT ─────────────────────────────────────────────────────
CREATE TABLE cl_audit (
    id        BIGSERIAL PRIMARY KEY,
    creato_il TIMESTAMPTZ NOT NULL DEFAULT now(),
    utente_id INTEGER     REFERENCES cl_utente(id),
    azione    VARCHAR(60) NOT NULL,
    entita    VARCHAR(40) NOT NULL,
    entita_id VARCHAR(40) NOT NULL DEFAULT '',
    prima     JSONB,
    dopo      JSONB,
    ip        VARCHAR(45) NOT NULL DEFAULT ''
);

-- ── Allegati (PDF DDT, foto): nel database, non su disco ───────────────────
CREATE TABLE cl_allegato (
    id         SERIAL PRIMARY KEY,
    entita     VARCHAR(40)  NOT NULL,
    entita_id  VARCHAR(40)  NOT NULL,
    nome_file  VARCHAR(255) NOT NULL,
    mime       VARCHAR(100) NOT NULL,
    dimensione INTEGER      NOT NULL CHECK (dimensione > 0 AND dimensione <= 20971520),
    sha256     CHAR(64)     NOT NULL,
    contenuto  BYTEA        NOT NULL,
    creato_da  INTEGER      REFERENCES cl_utente(id),
    creato_il  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_cl_allegato UNIQUE (entita, entita_id, sha256)
);

-- ── Contatori (numeri lotto, numeri DDT di uscita) ─────────────────────────
CREATE TABLE cl_sequenza (
    nome   VARCHAR(30) NOT NULL,
    anno   INTEGER     NOT NULL,
    ultimo INTEGER     NOT NULL DEFAULT 0 CHECK (ultimo >= 0),
    PRIMARY KEY (nome, anno)
);

-- ── Protezioni nel database ────────────────────────────────────────────────
-- 1) ledger e audit: nessuna UPDATE/DELETE, nemmeno da SQL diretto
CREATE FUNCTION cl_vieta_modifica() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'conto_lavoro: % su % non consentito (tabella solo in inserimento)', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'check_violation';
END $$;
CREATE TRIGGER tr_cl_movimento_solo_insert BEFORE UPDATE OR DELETE ON cl_movimento
    FOR EACH ROW EXECUTE FUNCTION cl_vieta_modifica();
CREATE TRIGGER tr_cl_movimento_no_truncate BEFORE TRUNCATE ON cl_movimento
    FOR EACH STATEMENT EXECUTE FUNCTION cl_vieta_modifica();
CREATE TRIGGER tr_cl_audit_solo_insert BEFORE UPDATE OR DELETE ON cl_audit
    FOR EACH ROW EXECUTE FUNCTION cl_vieta_modifica();
CREATE TRIGGER tr_cl_audit_no_truncate BEFORE TRUNCATE ON cl_audit
    FOR EACH STATEMENT EXECUTE FUNCTION cl_vieta_modifica();

-- 2) il saldo di un lotto non può mai diventare negativo
CREATE FUNCTION cl_controlla_saldo() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE saldo NUMERIC;
BEGIN
    SELECT COALESCE(SUM(quantita), 0) INTO saldo FROM conto_lavoro.cl_movimento WHERE lotto_id = NEW.lotto_id;
    IF saldo < 0 THEN
        RAISE EXCEPTION 'conto_lavoro: saldo negativo (%) per il lotto %', saldo, NEW.lotto_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER tr_cl_movimento_saldo AFTER INSERT ON cl_movimento
    DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION cl_controlla_saldo();

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (1, '0001: anagrafiche, DDT, lotti, ledger movimenti, audit, utenti, allegati, contatori');
