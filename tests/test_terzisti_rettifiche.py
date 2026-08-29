"""Test delle rettifiche manuali e della vista operativa terzisti."""
import json
import unittest

from flask import Flask

from models import db, LavorazioneTerzista, Terzista
from blueprints.terzisti.routes import terzisti_bp


class TestTerzistiRettifiche(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(
            TESTING=True,
            SECRET_KEY='test',
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.app.register_blueprint(terzisti_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.app.app_context():
            db.session.query(LavorazioneTerzista).delete()
            db.session.query(Terzista).delete()
            terzista = Terzista(nome='Fornitore Test', tipo='SATINATURA')
            db.session.add(terzista)
            db.session.flush()
            lavorazione = LavorazioneTerzista(
                terzista_id=terzista.id,
                fase='SATINATURA',
                qta=100,
                stato='PARZIALE',
                ddt_uscita='500',
                note=json.dumps({
                    'codice': 'PINXTT110',
                    'desc': 'Componente test',
                    'trattamento': 'SATINATURA',
                    'qta_rientrata': 40,
                    'ddt_rientri': ['700'],
                }),
            )
            db.session.add(lavorazione)
            db.session.commit()
            self.lavorazione_id = lavorazione.id

    def test_vista_parte_dalle_aperte_e_usa_card_fornitore(self):
        risposta = self.client.get('/terzisti')
        testo = risposta.get_data(as_text=True)
        self.assertEqual(risposta.status_code, 200)
        self.assertIn("_filtro='APERTE'", testo)
        self.assertIn('Materiale in carico per fornitore', testo)
        self.assertIn('id="fornitori-cards"', testo)
        self.assertNotIn('residuo-fornitore-tbody', testo)

    def test_rettifica_quantita_rientrata_e_stato(self):
        risposta = self.client.post(
            f'/api/lavorazioni/{self.lavorazione_id}/correggi_qta_rientrata',
            json={'nuova_qta': 100},
        )
        self.assertEqual(risposta.status_code, 200)
        self.assertEqual(risposta.get_json()['stato'], 'RIENTRATA')
        with self.app.app_context():
            lav = db.session.get(LavorazioneTerzista, self.lavorazione_id)
            note = json.loads(lav.note)
            self.assertEqual(note['qta_rientrata'], 100)
            self.assertEqual(note['ddt_rientri'], ['700'])
            self.assertEqual(note['rettifiche_rientro'][-1]['da'], 40)

    def test_rettifica_non_puo_superare_la_spedita(self):
        risposta = self.client.post(
            f'/api/lavorazioni/{self.lavorazione_id}/correggi_qta_rientrata',
            json={'nuova_qta': 101},
        )
        self.assertEqual(risposta.status_code, 400)

    def test_riepilogo_card_senza_dettagli_ddt(self):
        risposta = self.client.get('/api/terzisti/residuo_per_fornitore')
        righe = risposta.get_json()
        self.assertEqual(righe[0]['terzista'], 'Fornitore Test')
        self.assertEqual(righe[0]['trattamento'], 'SATINATURA')
        self.assertEqual(righe[0]['residua'], 60)
        self.assertNotIn('dettaglio', righe[0])


if __name__ == '__main__':
    unittest.main()
