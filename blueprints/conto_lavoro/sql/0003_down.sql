-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0003 (DOWN)
--  Rimuove SOLO quanto aggiunto dalla 0003 (foto di riferimento articolo).
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra giu --esegui --conferma ELIMINA-0003
-- ════════════════════════════════════════════════════════════════════════════
SET LOCAL search_path TO conto_lavoro;

DROP TABLE IF EXISTS cl_foto_articolo;

DELETE FROM cl_schema_version WHERE versione = 3;
