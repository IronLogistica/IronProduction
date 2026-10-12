"""
Regressione (segnalato da Mauri, 12/10/2026 — DDT rientro terzista 2955,
T.G.T. zincatura, 65 pz T200): la lavorazione risultava RIENTRATA ma la
Scheda di Magazzino di T200 non aveva il carico, e la successiva vendita a
Iron Segnaletica portava T200 a -65.

Causa: la scheda Kanban veniva cercata con prodotto LIKE 'T200%' prendendo
il PRIMO risultato — con schede anche per T200DT/T200PF/T200RMZF il carico
poteva finire su un altro codice; e senza nessuna scheda il carico non
avveniva affatto.
"""
import json
import unittest

from flask import Flask

from models import (db, KanbanProdotto, GiacenzaWood, MovimentoGiacenzaWood, Terzista,
                     LavorazioneTerzista, kanban_per_codice)
from blueprints.terzisti.routes import terzisti_bp, _aggiorna_kanban_da_rientro_ddt


class TestRientroTerzistaSkuEsatto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(terzisti_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (MovimentoGiacenzaWood, GiacenzaWood, KanbanProdotto, LavorazioneTerzista, Terzista):
                db.session.query(modello).delete()
            # Le schede "simili" sono create PRIMA di T200: con il vecchio
            # LIKE 'T200%' + first() vincevano loro.
            for nome in ('T200DT — TRANSENNA DT', 'T200PF — TRANSENNA PF',
                         'T200RMZF — TRANSENNA RMZF', 'T200 — TRANSENNA L.2.000 D.32 ZINC.'):
                db.session.add(KanbanProdotto(categoria='Transenne', prodotto=nome, verniciati=0))
            db.session.commit()

    def _giacenza(self, codice):
        g = db.session.get(GiacenzaWood, codice)
        return g.quantita if g else 0

    def test_helper_trova_solo_sku_esatto(self):
        with self.app.app_context():
            self.assertTrue(kanban_per_codice('T200').prodotto.startswith('T200 —'))
            self.assertTrue(kanban_per_codice('t200dt').prodotto.startswith('T200DT'))
            self.assertIsNone(kanban_per_codice('T20'))

    def test_rientro_65_carica_t200_e_non_un_codice_simile(self):
        with self.app.app_context():
            _aggiorna_kanban_da_rientro_ddt('T200', 65)
            db.session.commit()
            self.assertEqual(self._giacenza('T200'), 65)
            for altro in ('T200DT', 'T200PF', 'T200RMZF'):
                self.assertEqual(self._giacenza(altro), 0, altro)
            self.assertEqual(kanban_per_codice('T200').verniciati, 65)
            self.assertEqual(kanban_per_codice('T200DT').verniciati, 0)

    def test_senza_scheda_kanban_il_magazzino_si_carica_lo_stesso(self):
        with self.app.app_context():
            _aggiorna_kanban_da_rientro_ddt('X999', 10)
            db.session.commit()
            self.assertEqual(self._giacenza('X999'), 10)
            mov = MovimentoGiacenzaWood.query.filter_by(codice='X999').one()
            self.assertTrue(mov.note.startswith('Rientro DDT terzista'))

    def test_conferma_ddt_rientro_end_to_end(self):
        with self.app.app_context():
            tz = Terzista(nome='T.G.T. Srl')
            db.session.add(tz)
            db.session.flush()
            db.session.add(LavorazioneTerzista(
                terzista_id=tz.id, fase='ZINCATURA', qta=65, ddt_uscita='607',
                data_uscita='05/10/2026', stato='ATTESA_RIENTRO',
                note=json.dumps({'codice': 'T200', 'trattamento': 'ZINCATURA', 'qta_rientrata': 0})))
            db.session.commit()
        r = self.app.test_client().post('/api/conferma_ddt_rientro', json={
            'filename': 'x.pdf', 'numero_ddt': '2955', 'data_ddt': '08/10/2026',
            'righe': [{'codice': 'T200', 'qta': 65, 'ddt_riferimento': '607'}]})
        self.assertTrue(r.get_json()['ok'], r.get_json())
        with self.app.app_context():
            self.assertEqual(self._giacenza('T200'), 65)
            self.assertEqual(self._giacenza('T200DT'), 0)


if __name__ == '__main__':
    unittest.main()
