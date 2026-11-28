-- CONTO LAVORO — migrazione 0008 (DOWN)
SET LOCAL search_path TO conto_lavoro;
DROP TABLE IF EXISTS cl_tw_riga;
DROP TABLE IF EXISTS cl_tw_riferimento;
DROP TABLE IF EXISTS cl_tw_documento;
DELETE FROM cl_schema_version WHERE versione = 8;
