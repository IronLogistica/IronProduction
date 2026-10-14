"""Inventario Codice Padre: colonne Impegnato / Disponibile (Mauri, 14/10/2026).
TR2005 e' usato sia da T200 sia da T250: l'impegnato deve comprendere le commesse
di ENTRAMBI i padri, e 'Attuale' (fisico) non deve cambiare."""
import unittest

from flask import Flask

from models import db, OrdineProduzione, GiacenzaWood, DistintaBaseWood
from blueprints.magazzino.routes import magazzino_bp


class TestInventarioPadreImpegnato(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(magazzino_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
            db.session.add_all([
                DistintaBaseWood(codice_padre='T200', codice_figlio='TR2005', quantita=2.0),
                DistintaBaseWood(codice_padre='T250', codice_figlio='TR2005', quantita=2.0),
                GiacenzaWood(codice='TR2005', quantita=1418),
                OrdineProduzione(codice='OP-1', codice_articolo='T250', commessa='26090062',
                                 qta_pianificata=120, stato='Rilasciato', priorita=1),
                OrdineProduzione(codice='OP-2', codice_articolo='T200', commessa='26100090',
                                 qta_pianificata=50, stato='Rilasciato', priorita=2),
            ])
            db.session.commit()

    def test_impegnato_comprende_altri_codici_padre(self):
        d = self.app.test_client().get('/api/inventario-codice-padre/T200').get_json()
        r = next(x for x in d['righe'] if x['codice'] == 'TR2005')
        self.assertEqual(r['giacenza_attuale'], 1418, "'Attuale' resta il fisico")
        self.assertEqual(r['impegnato'], 340)          # 240 per T250 + 100 per T200
        self.assertEqual(r['disponibile'], 1078)
        self.assertEqual({i['commessa'] for i in r['impegni']}, {'26090062', '26100090'})

    def test_foglio_stampato_mostra_impegnato_e_disponibile(self):
        html = self.app.test_client().get('/inventario-codice-padre-stampa/T200').get_data(as_text=True)
        self.assertIn('Impegnato', html)
        self.assertIn('Disponibile', html)
        self.assertIn('26090062 (T250): 240', html)
        self.assertIn('>1078<', html.replace(' ', '').replace('\n', ''))


if __name__ == '__main__':
    unittest.main()
