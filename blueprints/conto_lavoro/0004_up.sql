-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0004 (UP)
--  Pagina Ordini, richiesta di Mauri (02/10/2026):
--    - cl_ordine_riga.quantita_evasa: era sempre 0.0 calcolato al volo (il
--      modulo DDT di uscita non esiste ancora) — ora è una colonna vera,
--      correggibile a mano dal capo finché quel modulo non la scrive da solo.
--    - cl_ordine_riga.quantita_prodotta_manuale: la "Prodotta" resta di
--      regola calcolata da OrdineProduzione.qta_buona (sola lettura, vedi
--      testata di routes.py) — questa colonna, se valorizzata, la
--      SOVRASCRIVE per quella riga (correzione manuale quando MasterWork
--      non ha ancora dichiarato o la dichiarazione non corrisponde).
--      NULL = nessuna correzione, resta il calcolo automatico.
--  Non tocca nessuna tabella esistente di IronProduction.
--  Rollback completo: 0004_down.sql
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra su --esegui --conferma 0004
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

ALTER TABLE cl_ordine_riga ADD COLUMN quantita_evasa NUMERIC(14,3) NOT NULL DEFAULT 0
    CHECK (quantita_evasa >= 0);
ALTER TABLE cl_ordine_riga ADD COLUMN quantita_prodotta_manuale NUMERIC(14,3)
    CHECK (quantita_prodotta_manuale IS NULL OR quantita_prodotta_manuale >= 0);

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (4, '0004: quantita_evasa reale + correzione manuale quantita_prodotta su cl_ordine_riga (pagina Ordini)');
