"""Conto Lavoro 0005 (Mauri, 28/11/2026): ordine SOSPESO, righe DA REVISIONARE,
anomalie MasterWork risolte con nota. Serve un Postgres locale (CL_TEST_PG_URL)."""
import os
import unittest
from unittest import mock
from urllib.parse import urlsplit

from flask import Flask
from sqlalchemy import create_engine

from models import db
from blueprints.conto_lavoro import migra
from blueprints.conto_lavoro import models as clm
from blueprints.conto_lavoro.routes import cl_bp
from blueprints.produzione_pp import routes as pp

PG_URL = os.environ.get('CL_TEST_PG_URL', '')
PG_LOCALE = urlsplit(PG_URL).hostname in ('localhost', '127.0.0.1') if PG_URL else False
RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(PG_LOCALE, 'serve CL_TEST_PG_URL verso un Postgres LOCALE usa e getta')
class TestAnomalieContoLavoro(unittest.TestCase):
    def setUp(self):
        eng = create_engine(PG_URL)
        with eng.begin() as c:
            c.exec_driver_sql('DROP SCHEMA IF EXISTS conto_lavoro CASCADE')
        eng.dispose()
        migra.applica_su_con_lock(PG_URL, migra.MIGRAZIONI[-1], out=lambda *_: None)
        self.app = Flask(__name__, template_folder=os.path.join(RADICE, 'templates'))
        self.app.config.update(TESTING=True, CL_ENABLED=True, SECRET_KEY='test', MASTERWORK_URL='https://mw.example',
                               SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SQLALCHEMY_TRACK_MODIFICATIONS=False,
                               SQLALCHEMY_BINDS={'conto_lavoro': {'url': PG_URL, 'execution_options':
                                                 {'schema_translate_map': {None: 'conto_lavoro'}}}})
        db.init_app(self.app)
        self.app.register_blueprint(cl_bp)
        self.app.register_blueprint(pp.pp_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            c = clm.ClCliente(ragione_sociale='FAZA Srl')
            db.session.add(c); db.session.flush()
            a = clm.ClArticoloCliente(cliente_id=c.id, codice='AS_001693', descrizione='TELAIO', udm='PZ')
            db.session.add(a); db.session.flush()
            o = clm.ClOrdine(cliente_id=c.id, numero_ordine='293/IS', stato='CONFERMATO')
            db.session.add(o); db.session.flush()
            r = clm.ClOrdineRiga(ordine_id=o.id, n_riga=1, articolo_id=a.id, descrizione='TELAIO', quantita=3)
            db.session.add(r); db.session.commit()
            self.oid, self.rid = o.id, r.id
        self.c = self.app.test_client()
        pp._FOTO_MW_CACHE.clear()
        pp._ANOMALIE_MW_CACHE.update(t=0, dati=None)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            for e in db.engines.values():
                e.dispose()
        eng = create_engine(PG_URL)
        with eng.begin() as c:
            c.exec_driver_sql('DROP SCHEMA IF EXISTS conto_lavoro CASCADE')
        eng.dispose()

    def test_sospendi_e_riattiva(self):
        r = self.c.put(f'/conto-lavoro/api/ordini/{self.oid}/sospendi', json={'sospeso': True, 'motivo': 'cliente fermo'})
        self.assertTrue(r.json['ok'])
        self.assertEqual(r.json['ordine']['stato'], 'SOSPESO')
        live = self.c.get('/conto-lavoro/api/live').json
        self.assertTrue(live[0]['ordini'][0]['sospeso'])          # resta nel LIVE, marcato sospeso
        r = self.c.put(f'/conto-lavoro/api/ordini/{self.oid}/sospendi', json={'sospeso': False})
        self.assertEqual(r.json['ordine']['stato'], 'CONFERMATO')

    def test_riga_da_revisionare(self):
        r = self.c.put(f'/conto-lavoro/api/ordini/{self.oid}/righe/{self.rid}/revisione',
                       json={'da_revisionare': True, 'nota': 'nuovi tempi'})
        self.assertEqual(r.json['ordine']['righe'][0]['revisione']['nota'], 'nuovi tempi')
        live = self.c.get('/conto-lavoro/api/live').json
        self.assertEqual(live[0]['ordini'][0]['righe'][0]['revisione']['nota'], 'nuovi tempi')
        r = self.c.put(f'/conto-lavoro/api/ordini/{self.oid}/righe/{self.rid}/revisione', json={'da_revisionare': False})
        self.assertIsNone(r.json['ordine']['righe'][0]['revisione'])

    def test_anomalie_da_masterwork_e_risoluzione(self):
        def finto_get(url, params=None, timeout=None):
            m = mock.Mock(ok=True)
            if 'foto_articolo' in url:
                m.json.return_value = {'foto_url': '', 'articoli': [{'codice_interno': 'FZ240'}]}
            else:
                m.json.return_value = [
                    {'id': 7, 'operaio': 'ROSSI MARIO', 'codice_interno': 'FZ240', 'motivo': 'manca un foro',
                     'creato_il': '2026-11-28T09:00:00', 'azione': 'SEGNALAZIONE'},
                    {'id': 8, 'operaio': 'BIANCHI', 'codice_interno': 'S1-SX', 'motivo': 'altro', 'creato_il': None}]
            return m
        with mock.patch.object(pp.requests, 'get', side_effect=finto_get):
            r = self.c.post('/api/masterwork/anomalie', json={'codici': ['AS_001693']})
        self.assertEqual([a['id'] for a in r.json['anomalie']], [7])        # solo quelle dei codici CL
        self.assertEqual(r.json['anomalie'][0]['codici'], ['AS_001693'])
        self.assertEqual(self.c.post('/conto-lavoro/api/anomalie-risolte', json={'mw_id': 7}).status_code, 400)
        r = self.c.post('/conto-lavoro/api/anomalie-risolte',
                        json={'mw_id': 7, 'nota': 'rifatto il foro', 'operaio': 'ROSSI MARIO', 'motivo': 'manca un foro',
                              'codice_interno': 'FZ240', 'segnalata_il': '2026-11-28T09:00:00'})
        self.assertTrue(r.json['ok'])
        risolte = self.c.get('/conto-lavoro/api/anomalie-risolte').json
        self.assertEqual((risolte[0]['mw_id'], risolte[0]['nota_risoluzione']), (7, 'rifatto il foro'))
        self.assertEqual(self.c.post('/conto-lavoro/api/anomalie-risolte',
                                     json={'mw_id': 7, 'nota': 'x'}).status_code, 409)

    def test_anomalia_segnalata_da_angelo(self):
        with self.app.app_context():
            aid = clm.ClArticoloCliente.query.first().id
        self.assertEqual(self.c.post('/conto-lavoro/api/anomalie-angelo',
                                     json={'articolo_id': aid, 'tipo': 'TEMPO', 'descrizione': ' '}).status_code, 400)
        self.assertEqual(self.c.post('/conto-lavoro/api/anomalie-angelo',            # tipo obbligatorio
                                     json={'articolo_id': aid, 'descrizione': 'x'}).status_code, 400)
        r = self.c.post('/conto-lavoro/api/anomalie-angelo',
                        json={'articolo_id': aid, 'tipo': 'PREZZO', 'descrizione': 'rinegoziare'})
        self.assertEqual(r.json['anomalia']['tipo'], 'PREZZO')
        self.c.post(f"/conto-lavoro/api/anomalie-angelo/{r.json['anomalia']['id']}/risolvi", json={'nota': 'ok'})
        r = self.c.post('/conto-lavoro/api/anomalie-angelo',
                        json={'articolo_id': aid, 'ordine_id': self.oid, 'tipo': 'TEMPO', 'descrizione': 'tempi da rivedere'})
        self.assertTrue(r.json['ok'])
        an = r.json['anomalia']
        self.assertEqual((an['codice'], an['numero_ordine'], an['segnalata_da'], an['cliente']),
                         ('AS_001693', '293/IS', 'Angelo', 'FAZA Srl'))
        self.assertEqual(len(self.c.get('/conto-lavoro/api/anomalie-angelo').json), 1)
        self.assertEqual(self.c.post(f"/conto-lavoro/api/anomalie-angelo/{an['id']}/risolvi", json={}).status_code, 400)
        r = self.c.post(f"/conto-lavoro/api/anomalie-angelo/{an['id']}/risolvi", json={'nota': 'tempi aggiornati'})
        self.assertTrue(r.json['anomalia']['risolta'])
        self.assertEqual(self.c.get('/conto-lavoro/api/anomalie-angelo').json, [])
        self.assertEqual(self.c.get('/conto-lavoro/api/anomalie-angelo?stato=risolte').json[0]['nota_risoluzione'],
                         'tempi aggiornati')

    def test_pagine_si_aprono(self):
        self.app.context_processor(lambda: {'now': '', 'kanban_gruppi': [], 'macchine_monitor': [], 'sidebar_gruppi': []})
        for url in ('/conto-lavoro/anomalie', '/conto-lavoro/anagrafiche', '/conto-lavoro/ordini', '/conto-lavoro/live',
                    '/conto-lavoro/'):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertEqual(self.c.get('/conto-lavoro/magazzino').status_code, 302)    # uniti
        self.assertEqual(self.c.get('/conto-lavoro/riepilogo').status_code, 302)

    def test_solo_angelo_inserisce_codici_a_mano(self):
        with self.app.app_context():
            cid = clm.ClCliente.query.first().id
        corpo = {'cliente_id': cid, 'codice': 'URGENTE-1', 'descrizione': 'pezzo urgente', 'udm': 'PZ'}
        self.assertEqual(self.c.post('/conto-lavoro/api/articoli', json=corpo).status_code, 403)
        r = self.c.post(f'/conto-lavoro/api/ordini/{self.oid}/righe', json={'codice': 'NUOVO-X', 'quantita': 2,
                                                                             'descrizione': 'x'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.post('/conto-lavoro/api/angelo/accesso', json={'pin': '1'}).status_code, 503)  # PIN non impostato
        self.app.config['CL_PIN_ANGELO'] = '2468'
        self.assertEqual(self.c.post('/conto-lavoro/api/angelo/accesso', json={'pin': '1'}).status_code, 403)
        self.assertTrue(self.c.post('/conto-lavoro/api/angelo/accesso', json={'pin': '2468'}).json['ok'])
        self.assertTrue(self.c.get('/conto-lavoro/api/angelo').json['attivo'])
        self.assertTrue(self.c.post('/conto-lavoro/api/articoli', json=corpo).json['ok'])
        self.c.post('/conto-lavoro/api/angelo/esci')
        self.assertEqual(self.c.post('/conto-lavoro/api/articoli', json={**corpo, 'codice': 'URGENTE-2'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
