"""Test Scheda Identificazione Magazzino (singola e da DDT di ingresso)."""
import unittest

from flask import Flask

from models import (db, DistintaBaseWood, DDTCaricoWood, RigaDDTCaricoWood,
                    DescrizioneCodiceWood)
from blueprints.produzione_pp.routes import pp_bp


class TestSchedaMagazzinoStampa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (RigaDDTCaricoWood, DDTCaricoWood, DistintaBaseWood, DescrizioneCodiceWood):
                db.session.query(modello).delete()
            db.session.add_all([
                DistintaBaseWood(codice_padre='SEMI-A', codice_figlio='TUBO1'),
                DistintaBaseWood(codice_padre='FINITO-1', codice_figlio='SEMI-A'),
                DistintaBaseWood(codice_padre='FINITO-2', codice_figlio='TUBO1'),
                DescrizioneCodiceWood(codice='TUBO1', descrizione='Tubo interno D.101'),
            ])
            ddt = DDTCaricoWood(filename='ddt1.pdf', fornitore='Mericat Srl', numero_ddt='555',
                                data_ddt='24/09/2026')
            db.session.add(ddt)
            db.session.flush()
            db.session.add_all([
                RigaDDTCaricoWood(ddt_id=ddt.id, codice='TUBO1', descrizione='Tubo fornitore',
                                  quantita=132.0, quantita_verificata=130.0,
                                  ubicazione_allocata='03-02-05-01-F', ordine_n_riferimento='79'),
                RigaDDTCaricoWood(ddt_id=ddt.id, codice='FONDELLO', descrizione='Fondello bombato',
                                  quantita=350.0),
            ])
            db.session.commit()
            self.ddt_id = ddt.id
        self.client = self.app.test_client()

    def test_ddt_una_pagina_per_codice(self):
        t = self.client.get('/scheda-magazzino-stampa', query_string={'ddt_id': self.ddt_id}).get_data(as_text=True)
        self.assertEqual(t.count('class="pagina"'), 2)
        self.assertIn('SCHEDA IDENTIFICAZIONE MAGAZZINO', t)
        self.assertIn('Mericat Srl', t)
        self.assertIn('555', t)
        self.assertIn('>130<', t)            # verificata, senza decimali
        self.assertIn('>350<', t)
        self.assertIn('Tubo interno D.101', t)  # descrizione interna, non del fornitore
        self.assertIn('FINITO-1 · FINITO-2', t)
        self.assertIn('03-02-05-01-F', t)
        self.assertNotIn('Prossima Lavorazione', t)

    def test_singolo_codice_prende_riferimenti_ultimo_ddt(self):
        t = self.client.get('/scheda-magazzino-stampa',
                            query_string={'codice': 'TUBO1', 'quantita': '12'}).get_data(as_text=True)
        self.assertEqual(t.count('class="pagina"'), 1)
        self.assertIn('>12<', t)
        self.assertIn('Mericat Srl', t)

    def test_codice_senza_ddt(self):
        r = self.client.get('/scheda-magazzino-stampa', query_string={'codice': 'NUOVO'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('NUOVO', r.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
