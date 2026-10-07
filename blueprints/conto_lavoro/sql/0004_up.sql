-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0004 (UP)
--  BUG REALE CORRETTO (segnalato da Mauri, 07/10/2026 — "Tabelle degli
--  Ordini non ancora alla versione richiesta: imposta CL_MIGRA_AUTO = 0004"
--  comparso su Railway, ma i file SQL 0004 non esistevano affatto nel
--  repo: MIGRAZIONI in migra.py elencava già [1,2,3,4] e models.py aveva
--  già le due colonne qui sotto (richieste da Mauri il 02/10/2026 per le
--  quantità modificabili dell'ordine conto lavoro — vedi VERSIONE_SCHEMA_
--  RIGHE_MODIFICABILI in routes.py), ma la migrazione 0004 vera e propria
--  non era mai stata scritta: ogni Railway restava bloccato in eterno su
--  quel messaggio, perché CL_MIGRA_AUTO=0004 non trovava nessun file da
--  applicare.
--  Aggiunge a cl_ordine_riga le due colonne già usate dal codice:
--   - quantita_evasa: "Evasa" diventa una colonna vera (prima sempre 0.0
--     calcolata al volo, il modulo DDT di uscita non esiste ancora),
--     correggibile a mano dal capo nel frattempo.
--   - quantita_prodotta_manuale: correzione manuale di "Prodotta" per la
--     singola riga quando MasterWork non ha ancora dichiarato o la
--     dichiarazione non corrisponde — NULL = nessuna correzione, resta il
--     calcolo automatico da OrdineProduzione.qta_buona.
--  Non tocca nessun'altra tabella, né qui né in IronProduction.
--  Rollback completo: 0004_down.sql
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra su --esegui --conferma 0004
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

ALTER TABLE cl_ordine_riga
    ADD COLUMN quantita_evasa NUMERIC(14, 3) NOT NULL DEFAULT 0,
    ADD COLUMN quantita_prodotta_manuale NUMERIC(14, 3);

ALTER TABLE cl_ordine_riga
    ADD CONSTRAINT ck_cl_ordine_riga_evasa CHECK (quantita_evasa >= 0),
    ADD CONSTRAINT ck_cl_ordine_riga_prodotta_manuale
        CHECK (quantita_prodotta_manuale IS NULL OR quantita_prodotta_manuale >= 0);

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (4, '0004: cl_ordine_riga.quantita_evasa e quantita_prodotta_manuale (quantità modificabili)');
