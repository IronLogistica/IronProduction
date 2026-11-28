-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0007 (UP) — richiesta di Mauri, 28/11/2026
--  cl_anomalia.tipo: TEMPO (problema di produzione: compare nel LIVE, per plant
--  manager e operai) o PREZZO (tempi gia' ottimizzati: si parla solo col
--  cliente, NON compare nel LIVE). Idempotente: se la 0006 ha gia' creato la
--  colonna, non fa nulla.
--  Eseguire con CL_MIGRA_AUTO=0007 oppure:
--     python -m blueprints.conto_lavoro.migra su --esegui --conferma 0007
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

ALTER TABLE cl_anomalia ADD COLUMN IF NOT EXISTS tipo VARCHAR(10) NOT NULL DEFAULT 'TEMPO';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cl_anomalia_tipo'
                   AND conrelid = 'conto_lavoro.cl_anomalia'::regclass) THEN
        ALTER TABLE conto_lavoro.cl_anomalia ADD CONSTRAINT ck_cl_anomalia_tipo CHECK (tipo IN ('TEMPO','PREZZO'));
    END IF;
END $$;

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (7, '0007: cl_anomalia.tipo (TEMPO / PREZZO)');
