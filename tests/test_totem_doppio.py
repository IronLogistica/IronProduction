"""LIVE doppio verticale: Satinatrice sopra / Sgolatrice sotto, Pressopiegatrice / Punzonatrice."""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, DistintaBaseWood
from blueprints.monitor.routes import monitor_bp
from blueprints.produzione_pp.routes import pp_bp


class TestTotemDoppio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(monitor_bp)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
            centri = {n: CentroCostoWood(nome=n) for n in
                      ('Satinatrice', 'Sgolatrice', 'Pressopiegatrice', 'Punzonatrice', 'Segatrice')}
            db.session.add_all(centri.values())
            db.session.flush()
            db.session.add_all([
                CicloLavoroWood(codice='PSAT', sequenza=1, centro_costo_id=centri['Satinatrice'].id),
                CicloLavoroWood(codice='PSGO', sequenza=1, centro_costo_id=centri['Sgolatrice'].id),
                CicloLavoroWood(codice='PPIE', sequenza=1, centro_costo_id=centri['Pressopiegatrice'].id),
                CicloLavoroWood(codice='PPUN', sequenza=1, centro_costo_id=centri['Punzonatrice'].id),
                CicloLavoroWood(codice='PSEG', sequenza=1, centro_costo_id=centri['Segatrice'].id),
                OrdineProduzione(codice='OP-SAT', codice_articolo='PSAT', qta_pianificata=10, stato='Rilasciato', priorita=1),
                OrdineProduzione(codice='OP-SGO', codice_articolo='PSGO', qta_pianificata=20, stato='Rilasciato', priorita=2),
            ])
            db.session.commit()
        cls.client = cls.app.test_client()

    def test_satinatrice_sopra_sgolatrice_sotto(self):
        r = self.client.get('/totem/doppio/satinatrice-sgolatrice')
        self.assertEqual(r.status_code, 200)
        t = r.get_data(as_text=True)
        self.assertLess(t.index('>Satinatrice<'), t.index('>Sgolatrice<'))
        self.assertIn('PSAT', t)
        self.assertIn('PSGO', t)
        self.assertLess(t.index('PSAT'), t.index('PSGO'))

    def test_pressopiegatrice_punzonatrice(self):
        t = self.client.get('/totem/doppio/pressopiegatrice-punzonatrice').get_data(as_text=True)
        self.assertLess(t.index('>Pressopiegatrice<'), t.index('>Punzonatrice<'))

    def test_nav_mostra_coppie_una_volta(self):
        t = self.client.get('/totem/doppio/satinatrice-sgolatrice').get_data(as_text=True)
        self.assertEqual(t.count('href="/totem/doppio/satinatrice-sgolatrice"'), 1)
        self.assertEqual(t.count('href="/totem/doppio/pressopiegatrice-punzonatrice"'), 1)
        self.assertIn('>Segatrice</a>', t)

    def test_totem_singolo_invariato(self):
        with self.app.app_context():
            cid = CentroCostoWood.query.filter_by(nome='Segatrice').first().id
        r = self.client.get(f'/totem/macchina/{cid}')
        self.assertEqual(r.status_code, 200)
        self.assertIn('LIVE Segatrice', r.get_data(as_text=True))

    def test_slug_inesistente(self):
        self.assertEqual(self.client.get('/totem/doppio/xyz').status_code, 404)


if __name__ == '__main__':
    unittest.main()
