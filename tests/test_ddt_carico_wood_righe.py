"""Regressione per l'endpoint di eliminazione/modifica riga di un DDT di
carico Iron Wood (pagina Ordini di Acquisto, fase di verifica prima della
conferma) — copre il caso normale e i casi limite già corretti in
6333f64 (righe già cancellate, DDT già confermato), che finora non erano
testati."""
import unittest

from flask import Flask

from models import db, DDTCaricoWood, RigaDDTCaricoWood
from blueprints.acquisti_wood.routes import acquisti_wood_bp


class TestEliminaRigaDdt(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                                SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(acquisti_wood_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
        self.client = self.app.test_client()

    def _crea_ddt(self, confermato=False):
        with self.app.app_context():
            ddt = DDTCaricoWood(filename='test.pdf', fornitore='TGT', numero_ddt='123',
                                 data_ddt='01/09/2026', confermato=confermato)
            db.session.add(ddt)
            db.session.flush()
            riga = RigaDDTCaricoWood(ddt_id=ddt.id, codice='ABC', descrizione='Test', quantita=10)
            db.session.add(riga)
            db.session.commit()
            return ddt.id, riga.id

    def test_elimina_riga_bozza_normale(self):
        """Caso base: riga di una bozza non confermata — deve sparire."""
        _, riga_id = self._crea_ddt(confermato=False)
        resp = self.client.delete(f'/api/ddt_carico_wood/righe/{riga_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'ok': True})
        with self.app.app_context():
            self.assertIsNone(db.session.get(RigaDDTCaricoWood, riga_id))

    def test_elimina_riga_gia_eliminata_risponde_json_non_html(self):
        """Doppio click / riga già sparita: prima di 6333f64 il .get_or_404()
        restituiva una pagina HTML, mandando in eccezione il r.json() del
        frontend (il cestino sembrava 'non fare niente')."""
        _, riga_id = self._crea_ddt(confermato=False)
        self.client.delete(f'/api/ddt_carico_wood/righe/{riga_id}')  # prima cancellazione
        resp = self.client.delete(f'/api/ddt_carico_wood/righe/{riga_id}')  # seconda: riga già sparita
        self.assertEqual(resp.content_type, 'application/json')
        self.assertEqual(resp.get_json(), {'ok': True})

    def test_elimina_riga_id_mai_esistito(self):
        resp = self.client.delete('/api/ddt_carico_wood/righe/999999')
        self.assertEqual(resp.content_type, 'application/json')
        self.assertEqual(resp.get_json(), {'ok': True})

    def test_elimina_riga_ddt_gia_confermato_e_rifiutata(self):
        """Dopo la CONFERMA LETTURA le righe non sono più modificabili da qui."""
        _, riga_id = self._crea_ddt(confermato=True)
        resp = self.client.delete(f'/api/ddt_carico_wood/righe/{riga_id}')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.content_type, 'application/json')
        body = resp.get_json()
        self.assertTrue(body['errore'])
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(RigaDDTCaricoWood, riga_id))  # non toccata


if __name__ == '__main__':
    unittest.main()
