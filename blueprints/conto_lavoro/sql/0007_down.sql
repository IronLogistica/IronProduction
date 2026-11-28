-- CONTO LAVORO — migrazione 0007 (DOWN): la colonna tipo resta (creata anche dalla 0006 nuova)
SET LOCAL search_path TO conto_lavoro;
DELETE FROM cl_schema_version WHERE versione = 7;
