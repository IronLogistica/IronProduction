-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0002 (DOWN)
--  Rimuove SOLO quanto aggiunto dalla 0002: ordini cliente e predisposizione
--  Triple Watch (fatture, impostazioni, collegamenti su cl_ddt). Le tabelle
--  della 0001 (clienti, articoli, DDT, lotti, movimenti...) restano intatte.
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra giu --esegui --conferma ELIMINA-0002
-- ════════════════════════════════════════════════════════════════════════════
SET LOCAL search_path TO conto_lavoro;

ALTER TABLE cl_ddt DROP COLUMN IF EXISTS fattura_id;
ALTER TABLE cl_ddt DROP COLUMN IF EXISTS ordine_id;

DROP TABLE IF EXISTS cl_impostazione;
DROP TABLE IF EXISTS cl_fattura;
DROP TABLE IF EXISTS cl_ordine_riga;
DROP TABLE IF EXISTS cl_ordine;

DELETE FROM cl_schema_version WHERE versione = 2;
