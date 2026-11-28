-- CONTO LAVORO — migrazione 0006 (DOWN)
SET LOCAL search_path TO conto_lavoro;
DROP TABLE IF EXISTS cl_anomalia;
DELETE FROM cl_schema_version WHERE versione = 6;
