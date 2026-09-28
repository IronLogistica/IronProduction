"""
Conto lavoro — PR 1: schema e migrazione 0001.

Due livelli:
  - SEMPRE: i modelli si creano su SQLite in memoria (bind separato) e
    create_all di avvio non li tocca.
  - SOLO con CL_TEST_PG_URL impostata a un Postgres LOCALE usa e getta
    (localhost/127.0.0.1): prova il vero SQL — su/giù/su, trigger, vincoli.
    Il test rifiuta qualsiasi host non locale, per non toccare mai un
    database reale per errore.
"""
import io
import os
import unittest
from urllib.parse import urlsplit

from flask import Flask
from sqlalchemy import create_engine, inspect, text

from models import db
from blueprints.conto_lavoro import models as clm
from blueprints.conto_lavoro import migra

PG_URL = os.environ.get('CL_TEST_PG_URL', '')
PG_LOCALE = urlsplit(PG_URL).hostname in ('localhost', '127.0.0.1') if PG_URL else False


def _modelli_cl():
    return [m for m in vars(clm).values() if isinstance(m, type) and issubclass(m, db.Model)
            and m is not db.Model and getattr(m, '__bind_key__', None) == 'conto_lavoro']


class TestModelliSqlite(unittest.TestCase):
    def test_bind_separato_e_create_all_di_avvio(self):
        app = Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                          SQLALCHEMY_BINDS={'conto_lavoro': 'sqlite:///:memory:'},
                          SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        with app.app_context():
            db.create_all(bind_key=None)                        # come app.py
            self.assertFalse([t for t in inspect(db.engine).get_table_names() if t.startswith('cl_')])
            db.create_all(bind_key='conto_lavoro')              # solo per il test
            tabelle = set(inspect(db.engines['conto_lavoro']).get_table_names())
        self.assertEqual(tabelle, {m.__tablename__ for m in _modelli_cl()})
        self.assertEqual(len(tabelle), 12)


@unittest.skipUnless(PG_LOCALE, 'serve CL_TEST_PG_URL verso un Postgres LOCALE usa e getta')
class TestMigrazionePostgres(unittest.TestCase):
    def setUp(self):
        self.eng = create_engine(PG_URL)
        with self.eng.begin() as c:
            c.exec_driver_sql('DROP SCHEMA IF EXISTS conto_lavoro CASCADE')

    def tearDown(self):
        with self.eng.begin() as c:
            c.exec_driver_sql('DROP SCHEMA IF EXISTS conto_lavoro CASCADE')
        self.eng.dispose()

    def _migra(self, *args):
        righe = []
        rc = migra.esegui(args[0], PG_URL, *args[1:], out=righe.append)
        return rc, '\n'.join(righe)

    def _schema_esiste(self):
        with self.eng.connect() as c:
            return bool(c.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name='conto_lavoro'")).first())

    def _su(self):
        rc, out = self._migra('su', True, '0001')
        self.assertEqual(rc, 0, out)

    def test_prova_e_conferma_obbligatoria(self):
        rc, out = self._migra('su')
        self.assertEqual(rc, 0)
        self.assertIn('PROVA', out)
        self.assertNotIn(':prova@', out)  # la password non compare mai nell'output
        self.assertFalse(self._schema_esiste())
        rc, _ = self._migra('su', True, 'sbagliata')
        self.assertEqual(rc, 3)
        self.assertFalse(self._schema_esiste())

    def test_su_giu_su_e_idempotenza(self):
        self._su()
        rc, out = self._migra('stato')
        self.assertIn('Versione schema conto_lavoro: 1', out)
        rc, out = self._migra('su', True, '0001')
        self.assertIn('Niente da fare', out)
        rc, _ = self._migra('giu', True, '0001')
        self.assertEqual(rc, 3)
        self.assertTrue(self._schema_esiste())
        rc, _ = self._migra('giu', True, 'ELIMINA-0001')
        self.assertEqual(rc, 0)
        self.assertFalse(self._schema_esiste())
        self._su()

    def _avvio(self, valore):
        vecchi = {k: os.environ.get(k) for k in ('CL_MIGRA_AUTO', 'CL_DATABASE_URL')}
        try:
            os.environ['CL_DATABASE_URL'] = PG_URL
            if valore is None:
                os.environ.pop('CL_MIGRA_AUTO', None)
            else:
                os.environ['CL_MIGRA_AUTO'] = valore
            return migra.applica_all_avvio(out=lambda *_: None)
        finally:
            for k, v in vecchi.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_avvio_senza_consenso_non_fa_nulla(self):
        self.assertIsNone(self._avvio(None))
        self.assertIsNone(self._avvio(''))
        self.assertIsNone(self._avvio('9999'))
        self.assertIsNone(self._avvio('si'))
        self.assertFalse(self._schema_esiste())

    def test_avvio_con_consenso_e_due_worker_in_parallelo(self):
        import threading
        esiti = []
        t = [threading.Thread(target=lambda: esiti.append(self._avvio('0001'))) for _ in range(2)]
        # _avvio tocca os.environ: le due chiamate condividono lo stesso valore, va bene
        for x in t: x.start()
        for x in t: x.join()
        self.assertEqual(esiti, [1, 1])
        with self.eng.connect() as c:
            self.assertEqual(c.execute(text('SELECT COUNT(*) FROM conto_lavoro.cl_schema_version')).scalar(), 1)
        self.assertEqual(self._avvio('0001'), 1)   # al riavvio successivo: niente da fare

    def test_nessuna_tabella_fuori_dallo_schema(self):
        with self.eng.connect() as c:
            prima = set(inspect(c).get_table_names(schema='public'))
        self._su()
        with self.eng.connect() as c:
            dopo = set(inspect(c).get_table_names(schema='public'))
        self.assertEqual(prima, dopo)

    def test_sql_e_modelli_coincidono(self):
        self._su()
        ins = inspect(self.eng)
        for m in _modelli_cl():
            colonne_db = {c['name'] for c in ins.get_columns(m.__tablename__, schema='conto_lavoro')}
            self.assertEqual(colonne_db, set(m.__table__.columns.keys()), m.__tablename__)

    def _dati_base(self, c):
        c.exec_driver_sql("SET search_path TO conto_lavoro")
        cli = c.execute(text("INSERT INTO cl_cliente (ragione_sociale, piva) VALUES ('FAZA', '01') RETURNING id")).scalar()
        cli2 = c.execute(text("INSERT INTO cl_cliente (ragione_sociale, piva) VALUES ('ALTRO', '02') RETURNING id")).scalar()
        art = c.execute(text("INSERT INTO cl_articolo_cliente (cliente_id, codice, udm) VALUES (:c, 'TUBO', 'PZ') RETURNING id"), {'c': cli}).scalar()
        lotto = c.execute(text("INSERT INTO cl_lotto (codice, cliente_id, articolo_id) VALUES ('L-1', :c, :a) RETURNING id"), {'c': cli, 'a': art}).scalar()
        return cli, cli2, art, lotto

    def _deve_fallire(self, sql, params=None):
        with self.assertRaises(Exception):
            with self.eng.begin() as c:
                c.exec_driver_sql("SET search_path TO conto_lavoro")
                c.execute(text(sql), params or {})

    def test_vincoli_e_trigger(self):
        self._su()
        with self.eng.begin() as c:
            cli, cli2, art, lotto = self._dati_base(c)
            c.execute(text("INSERT INTO cl_movimento (lotto_id, causale, quantita, chiave_idempotenza) VALUES (:l, 'CARICO_DDT', 10, 'k1')"), {'l': lotto})
            c.execute(text("INSERT INTO cl_audit (azione, entita) VALUES ('prova', 'x')"))
        # ledger e audit: niente UPDATE / DELETE / TRUNCATE
        self._deve_fallire("UPDATE cl_movimento SET note = 'x'")
        self._deve_fallire("DELETE FROM cl_movimento")
        self._deve_fallire("TRUNCATE cl_movimento")
        self._deve_fallire("UPDATE cl_audit SET azione = 'x'")
        self._deve_fallire("DELETE FROM cl_audit")
        # saldo mai negativo (10 - 11)
        self._deve_fallire("INSERT INTO cl_movimento (lotto_id, causale, quantita, chiave_idempotenza) VALUES (:l, 'CONSUMO', -11, 'k2')", {'l': lotto})
        # segno coerente con la causale
        self._deve_fallire("INSERT INTO cl_movimento (lotto_id, causale, quantita, chiave_idempotenza) VALUES (:l, 'CARICO_DDT', -1, 'k3')", {'l': lotto})
        # stessa chiave di idempotenza = rifiutata
        self._deve_fallire("INSERT INTO cl_movimento (lotto_id, causale, quantita, chiave_idempotenza) VALUES (:l, 'CARICO_DDT', 1, 'k1')", {'l': lotto})
        # un lotto non può appartenere a un cliente diverso da quello del suo articolo
        self._deve_fallire("INSERT INTO cl_lotto (codice, cliente_id, articolo_id) VALUES ('L-2', :c, :a)", {'c': cli2, 'a': art})
        # stato DDT incoerente con la direzione / DDT uscita emesso senza numero
        self._deve_fallire("INSERT INTO cl_ddt (direzione, cliente_id, stato) VALUES ('IN', :c, 'EMESSO')", {'c': cli})
        self._deve_fallire("INSERT INTO cl_ddt (direzione, cliente_id, stato) VALUES ('OUT', :c, 'EMESSO')", {'c': cli})
        # consumo lecito (10 - 4 = 6) passa
        with self.eng.begin() as c:
            c.exec_driver_sql("SET search_path TO conto_lavoro")
            c.execute(text("INSERT INTO cl_movimento (lotto_id, causale, quantita, chiave_idempotenza) VALUES (:l, 'CONSUMO', -4, 'k4')"), {'l': lotto})
            saldo = c.execute(text("SELECT SUM(quantita) FROM cl_movimento WHERE lotto_id = :l"), {'l': lotto}).scalar()
        self.assertEqual(float(saldo), 6.0)

    def test_orm_scrive_nello_schema_tramite_bind(self):
        self._su()
        app = Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SQLALCHEMY_TRACK_MODIFICATIONS=False,
                          SQLALCHEMY_BINDS={'conto_lavoro': {'url': PG_URL, 'execution_options':
                                            {'schema_translate_map': {None: 'conto_lavoro'}}}})
        db.init_app(app)
        with app.app_context():
            db.session.add(clm.ClCliente(ragione_sociale='FAZA'))
            db.session.commit()
            self.assertEqual(clm.ClCliente.query.count(), 1)
            db.session.remove()
            for e in db.engines.values():
                e.dispose()
        with self.eng.connect() as c:
            self.assertEqual(c.execute(text('SELECT COUNT(*) FROM conto_lavoro.cl_cliente')).scalar(), 1)


if __name__ == '__main__':
    unittest.main()
