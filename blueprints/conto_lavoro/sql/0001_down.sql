-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0001 (DOWN)
--  Elimina TUTTO lo schema conto_lavoro (tabelle, dati, trigger, funzioni).
--  Non tocca nessuna tabella di IronProduction fuori da questo schema.
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra giu --esegui --conferma ELIMINA-0001
-- ════════════════════════════════════════════════════════════════════════════
DROP SCHEMA IF EXISTS conto_lavoro CASCADE;
