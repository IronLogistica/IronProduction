"""
BUG REALE, 23/10/2026: contando un gruppo di Kanban Inventario la data
"ultimo inventario" non si aggiornava mai (il codice che la scriveva in
api_inventario_salva mancava), quindi il gruppo restava sempre "DA CONTARE".
"""
import unittest

from flask import Flask

from models import db, GruppoInventarioWood, GiacenzaWood
from blueprints.magazzino.routes import magazzino_bp


class TestInventarioGruppoData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(magazzino_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            db.session.query(GiacenzaWood).delete()
            db.session.query(GruppoInventarioWood).delete()
            g = GruppoInventarioWood(nome='Imballo', frequenza_giorni=21)
            db.session.add_all([g, GiacenzaWood(codice='X1', quantita=10)])
            db.session.commit()
            self.gid = g.id
        self.client = self.app.test_client()

    def _stato(self):
        with self.app.app_context():
            return self.client.get('/api/inventario/kanban-gruppi').get_json()[0]

    def test_gruppo_mai_contato_e_scaduto(self):
        self.assertTrue(self._stato()['scaduto'])

    def test_salvare_con_differenze_azzera_la_scadenza(self):
        r = self.client.post('/api/inventario/salva', json={'conteggi': {'X1': 12}, 'gruppo_kanban_id': self.gid})
        self.assertTrue(r.get_json()['ok'])
        stato = self._stato()
        self.assertFalse(stato['scaduto'])
        self.assertIsNotNone(stato['ultimo_inventario_il'])

    def test_segnare_contato_senza_differenze_azzera_la_scadenza(self):
        r = self.client.post('/api/inventario/salva', json={'conteggi': {}, 'gruppo_kanban_id': self.gid})
        self.assertTrue(r.get_json()['ok'])
        self.assertFalse(self._stato()['scaduto'])

    def test_senza_gruppo_e_senza_conteggi_resta_un_errore(self):
        r = self.client.post('/api/inventario/salva', json={'conteggi': {}})
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()
