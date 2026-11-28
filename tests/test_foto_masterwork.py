"""Il LIVE del Conto Lavoro mostra la foto dell'articolo presa da MasterWork (anagrafica_articoli.foto_url)."""
import unittest
from unittest import mock

from flask import Flask

from models import db, MappaCodiceMasterWork
from blueprints.produzione_pp import routes as pp


class TestFotoMasterWork(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False, MASTERWORK_URL='https://mw.example')
        db.init_app(self.app)
        self.app.register_blueprint(pp.pp_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            db.session.add(MappaCodiceMasterWork(codice_ironproduction='STELLA-SX', codice_masterwork='S1-SX'))
            db.session.commit()
        pp._FOTO_MW_CACHE.clear()

    def _risposta(self, url, params=None, timeout=None):
        r = mock.Mock(ok=True)
        cod = (params or {}).get('codice') or url.rsplit('/', 1)[-1]
        r.json.return_value = {'foto_url': 'https://xyz.supabase.co/foto/s1sx.jpg' if cod == 'S1-SX' else ''}
        if cod in ('AS_001693', 'S1-SX') and 'foto_articolo' in url:
            r.json.return_value['articoli'] = [{'codice_interno': 'FZ240' if cod == 'AS_001693' else 'S1-SX',
                                                'codice_esterno': cod}]
        return r

    def test_codice_tradotto_con_la_mappa(self):
        with mock.patch.object(pp.requests, 'get', side_effect=self._risposta):
            r = self.app.test_client().get('/api/masterwork/foto/STELLA-SX')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['Location'], 'https://xyz.supabase.co/foto/s1sx.jpg')

    def test_senza_foto_404(self):
        with mock.patch.object(pp.requests, 'get', side_effect=self._risposta):
            self.assertEqual(self.app.test_client().get('/api/masterwork/foto/NIENTE').status_code, 404)

    def test_codici_interni_da_masterwork(self):
        with mock.patch.object(pp.requests, 'get', side_effect=self._risposta):
            r = self.app.test_client().post('/api/masterwork/codici-interni',
                                            json={'codici': ['AS_001693', 'STELLA-SX', 'NIENTE']})
        self.assertEqual(r.json, {'AS_001693': 'FZ240', 'STELLA-SX': 'S1-SX', 'NIENTE': ''})


if __name__ == '__main__':
    unittest.main()
