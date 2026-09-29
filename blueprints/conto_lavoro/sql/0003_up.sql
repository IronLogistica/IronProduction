-- ════════════════════════════════════════════════════════════════════════════
--  CONTO LAVORO — migrazione 0003 (UP)
--  Foto di riferimento per codice articolo cliente (stesso pattern di
--  FotoArticolo in IronProduction: base64 nel DB, niente filesystem separato
--  da gestire/perdere ai redeploy) — una sola foto corrente per articolo,
--  usata dal monitor LIVE Conto lavoro (riquadro immagine accanto al codice).
--  Non tocca nessuna tabella esistente, né qui né in IronProduction.
--  Rollback completo: 0003_down.sql
--  Eseguire SOLO con:  python -m blueprints.conto_lavoro.migra su --esegui --conferma 0003
-- ════════════════════════════════════════════════════════════════════════════

SET LOCAL search_path TO conto_lavoro;

CREATE TABLE cl_foto_articolo (
    id                SERIAL PRIMARY KEY,
    articolo_id       INTEGER      NOT NULL REFERENCES cl_articolo_cliente(id) ON DELETE CASCADE,
    nome_file         VARCHAR(255) NOT NULL,
    tipo_mime         VARCHAR(100) NOT NULL DEFAULT 'application/octet-stream',
    contenuto_base64  TEXT         NOT NULL,
    caricato_il       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_cl_foto_articolo_articolo UNIQUE (articolo_id)
);

INSERT INTO cl_schema_version (versione, descrizione)
VALUES (3, '0003: foto di riferimento per articolo cliente (monitor LIVE Conto lavoro)');
