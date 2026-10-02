-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0004 (DOWN)
--  Rimuove SOLO quanto aggiunto dalla 0004: quantita_evasa e
--  quantita_prodotta_manuale su cl_ordine_riga. Le tabelle delle migrazioni
--  precedenti restano intatte.
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra giu --esegui --conferma ELIMINA-0004
-- ════════════════════════════════════════════════════════════════════════════
SET LOCAL search_path TO conto_lavoro;

ALTER TABLE cl_ordine_riga DROP COLUMN IF EXISTS quantita_prodotta_manuale;
ALTER TABLE cl_ordine_riga DROP COLUMN IF EXISTS quantita_evasa;

DELETE FROM cl_schema_version WHERE versione = 4;
