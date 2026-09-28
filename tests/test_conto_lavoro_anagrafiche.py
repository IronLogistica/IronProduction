"""Conto lavoro — anagrafiche clienti e articoli (bind conto_lavoro su SQLite in memoria)."""
import os
import unittest

from flask import Flask

from models import db, GiacenzaWood, MovimentoGiacenzaWood
from blueprints.conto_lavoro.routes import cl_bp
from blueprints.conto_lavoro.models import ClAudit, ClSchemaVersion

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestAnagrafiche(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=os.path.join(RADICE, 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='x', CL_ENABLED=True,
                               SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_BINDS={'conto_lavoro': 'sqlite:///:memory:'},
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(cl_bp)

        @self.app.context_processor
        def _g():
            return {'now': '', 'kanban_gruppi': [], 'macchine_monitor': [], 'sidebar_gruppi': []}
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all(bind_key=None)
        db.create_all(bind_key='conto_lavoro')
        db.session.add(ClSchemaVersion(versione=1, descrizione='test'))
        db.session.commit()
        self.c = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _cliente(self, **k):
        return self.c.post('/conto-lavoro/api/clienti', json={'ragione_sociale': 'FAZA srl', 'piva': '01234567890', **k})

    def test_pagine(self):
        self.assertIn('Database pronto', self.c.get('/conto-lavoro/').get_data(as_text=True))
        self.assertEqual(self.c.get('/conto-lavoro/anagrafiche').status_code, 200)

    def test_crea_modifica_cliente(self):
        r = self._cliente(provincia='pg')
        self.assertEqual(r.status_code, 201)
        cid = r.get_json()['cliente']['id']
        self.assertEqual(r.get_json()['cliente']['provincia'], 'PG')
        self.assertEqual(self._cliente().status_code, 409)             # stessa P.IVA
        self.assertEqual(self.c.post('/conto-lavoro/api/clienti', json={'ragione_sociale': ''}).status_code, 400)
        r = self.c.put(f'/conto-lavoro/api/clienti/{cid}', json={'citta': 'Città di Castello', 'attivo': False})
        self.assertEqual(r.get_json()['cliente']['citta'], 'Città di Castello')
        self.assertEqual(len(self.c.get('/conto-lavoro/api/clienti').get_json()), 0)       # non attivo
        self.assertEqual(len(self.c.get('/conto-lavoro/api/clienti?tutti=1').get_json()), 1)
        self.assertEqual([a.azione for a in ClAudit.query.order_by(ClAudit.id)], ['CLIENTE_CREATO', 'CLIENTE_MODIFICATO'])
        self.assertEqual(ClAudit.query.first().dopo['_utente'], 'capo')

    def test_articoli_separati_per_cliente(self):
        a = self._cliente().get_json()['cliente']['id']
        b = self.c.post('/conto-lavoro/api/clienti', json={'ragione_sociale': 'Altro', 'piva': '999'}).get_json()['cliente']['id']
        art = {'codice': 'tubo-101', 'descrizione': 'Tubo', 'udm': 'pz'}
        r = self.c.post('/conto-lavoro/api/articoli', json={**art, 'cliente_id': a})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['articolo']['codice'], 'TUBO-101')
        self.assertEqual(r.get_json()['articolo']['tracciamento'], 'LOTTO')
        self.assertEqual(self.c.post('/conto-lavoro/api/articoli', json={**art, 'cliente_id': a}).status_code, 409)
        self.assertEqual(self.c.post('/conto-lavoro/api/articoli', json={**art, 'cliente_id': b}).status_code, 201)
        self.assertEqual(self.c.post('/conto-lavoro/api/articoli', json={**art, 'codice': 'X', 'udm': 'LITRI', 'cliente_id': a}).status_code, 400)
        self.assertEqual(self.c.post('/conto-lavoro/api/articoli', json={**art, 'codice': 'Y', 'cliente_id': 9999}).status_code, 400)
        self.assertEqual(len(self.c.get(f'/conto-lavoro/api/articoli?cliente_id={a}').get_json()), 1)

    def test_nessun_effetto_su_iron_wood(self):
        a = self._cliente().get_json()['cliente']['id']
        self.c.post('/conto-lavoro/api/articoli', json={'codice': 'T', 'udm': 'PZ', 'cliente_id': a})
        self.assertEqual(GiacenzaWood.query.count(), 0)
        self.assertEqual(MovimentoGiacenzaWood.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
