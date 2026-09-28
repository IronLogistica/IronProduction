"""
Migrazioni del modulo CONTO LAVORO — eseguite SOLO a mano, mai all'avvio.

Non importa app.py (che all'import esegue il DDL di IronProduction): apre
una connessione propria al database indicato.

Uso (dalla cartella del progetto, es. shell Railway del servizio IronProduction):

    python -m blueprints.conto_lavoro.migra stato
    python -m blueprints.conto_lavoro.migra su                       # PROVA: mostra cosa farebbe, non esegue
    python -m blueprints.conto_lavoro.migra su  --esegui --conferma 0001
    python -m blueprints.conto_lavoro.migra giu                      # PROVA
    python -m blueprints.conto_lavoro.migra giu --esegui --conferma ELIMINA-0001

Database: --url, altrimenti CL_DATABASE_URL, altrimenti DATABASE_URL.
Ogni migrazione gira in UNA transazione: se qualcosa fallisce non resta nulla a metà.
"""
import argparse
import os
import sys
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

CARTELLA_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sql')
MIGRAZIONI = [1]  # versioni disponibili, in ordine
SCHEMA = 'conto_lavoro'


def _url(arg_url=None):
    url = arg_url or os.environ.get('CL_DATABASE_URL') or os.environ.get('DATABASE_URL') or ''
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url


def descrivi_url(url):
    """Host e database, MAI la password."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.username or ''}@{p.hostname or ''}{':' + str(p.port) if p.port else ''}{p.path}"


def leggi_sql(versione, verso):
    with open(os.path.join(CARTELLA_SQL, f'{versione:04d}_{verso}.sql'), encoding='utf-8') as f:
        return f.read()


def _esegui_script(engine, sql):
    """Esegue lo script SQL così com'è, in UNA transazione, senza interpretare
    i '%' come segnaposto (servono nei messaggi RAISE dei trigger)."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)          # nessun parametro: il driver non tocca il testo
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def versione_attuale(conn):
    esiste = conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = 'cl_schema_version'"),
        {'s': SCHEMA}).first()
    if not esiste:
        return 0
    return conn.execute(text(f'SELECT COALESCE(MAX(versione), 0) FROM {SCHEMA}.cl_schema_version')).scalar() or 0


def esegui(comando, url, esegui_davvero=False, conferma='', out=print):
    if not url:
        out('ERRORE: nessun database (usa --url, CL_DATABASE_URL o DATABASE_URL).')
        return 2
    if not url.startswith('postgresql'):
        out('ERRORE: il modulo conto lavoro richiede PostgreSQL.')
        return 2
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            attuale = versione_attuale(conn)
        out(f'Database: {descrivi_url(url)}')
        out(f'Versione schema conto_lavoro: {attuale}')
        if comando == 'stato':
            return 0

        if comando == 'su':
            da_fare = [v for v in MIGRAZIONI if v > attuale]
            if not da_fare:
                out('Niente da fare: schema già aggiornato.')
                return 0
            for v in da_fare:
                sql = leggi_sql(v, 'up')
                if not esegui_davvero:
                    out(f'--- PROVA: verrebbe eseguita la migrazione {v:04d} (non eseguo nulla) ---')
                    out(sql)
                    continue
                if conferma != f'{v:04d}':
                    out(f'ERRORE: per eseguire la migrazione {v:04d} serve --conferma {v:04d}')
                    return 3
                _esegui_script(engine, sql)
                out(f'OK: migrazione {v:04d} applicata.')
            return 0

        if comando == 'giu':
            if attuale == 0:
                out('Niente da fare: schema conto_lavoro assente.')
                return 0
            sql = leggi_sql(attuale, 'down')
            if not esegui_davvero:
                out(f'--- PROVA: verrebbe eseguito il rollback della {attuale:04d} (non eseguo nulla) ---')
                out(sql)
                return 0
            if conferma != f'ELIMINA-{attuale:04d}':
                out(f'ERRORE: il rollback cancella TUTTI i dati del conto lavoro. Serve --conferma ELIMINA-{attuale:04d}')
                return 3
            _esegui_script(engine, sql)
            out(f'OK: rollback {attuale:04d} eseguito, schema conto_lavoro eliminato.')
            return 0
        out(f'Comando sconosciuto: {comando}')
        return 2
    finally:
        engine.dispose()


def main(argv=None):
    p = argparse.ArgumentParser(description='Migrazioni del modulo Conto lavoro (solo manuali).')
    p.add_argument('comando', choices=['stato', 'su', 'giu'])
    p.add_argument('--url', default=None)
    p.add_argument('--esegui', action='store_true', help='esegue davvero (senza: solo prova)')
    p.add_argument('--conferma', default='', help='0001 per "su", ELIMINA-0001 per "giu"')
    a = p.parse_args(argv)
    return esegui(a.comando, _url(a.url), a.esegui, a.conferma)


if __name__ == '__main__':
    sys.exit(main())
