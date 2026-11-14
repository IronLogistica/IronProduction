"""
Regressione (Mauri, 14/11/2026 — scheda S-14): lo Storico Produzione si
aggiornava al RIENTRO DAL TERZISTA — gli articoli inox non vanno mai dal
terzista e non venivano mai contati. Ora conta quando il prodotto finito
entra in GREZZI IW (ultima fase del ciclo del codice padre, approvata).
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, KanbanProdotto, StoricoProduzione, OrdineProduzione, EventoConsuntivoPP,
                     CicloLavoroWood, CentroCostoWood, GiacenzaWood, MovimentoGiacenzaWood)
from blueprints.kanban.routes import kanban_bp, storico_ricalcola_da_grezzi_iw
from blueprints.terzisti.routes import terzisti_bp, _aggiorna_kanban_da_rientro_ddt
from blueprints.produzione_pp.routes import _storico_kanban_da_grezzi_iw


class TestStoricoDaGrezziIW(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(kanban_bp)
        cls.app.register_blueprint(terzisti_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (StoricoProduzione, EventoConsuntivoPP, OrdineProduzione, CicloLavoroWood,
                      CentroCostoWood, MovimentoGiacenzaWood, GiacenzaWood, KanbanProdotto):
                db.session.query(m).delete()
            p = KanbanProdotto(categoria='IW', prodotto='S-14 — CAVALLETTO S-14')
            sega = CentroCostoWood(nome='Segatrice'); sald = CentroCostoWood(nome='Saldatura')
            db.session.add_all([p, sega, sald]); db.session.flush()
            db.session.add_all([CicloLavoroWood(codice='S-14', sequenza=1, centro_costo_id=sega.id),
                                CicloLavoroWood(codice='S-14', sequenza=2, centro_costo_id=sald.id),
                                OrdineProduzione(codice='OP-1', codice_articolo='S-14', qta_pianificata=500)])
            db.session.commit()
            self.kid = p.id

    def _tot(self, anno, mese):
        r = StoricoProduzione.query.filter_by(kanban_id=self.kid, anno=anno, mese=mese).first()
        return r.qta_auto if r else 0

    def test_rientro_terzista_non_conta_piu(self):
        with self.app.app_context():
            _aggiorna_kanban_da_rientro_ddt('S-14', 100)
            db.session.commit()
            self.assertEqual(StoricoProduzione.query.count(), 0)

    def test_grezzi_iw_conta_e_storno_scala(self):
        with self.app.app_context():
            _storico_kanban_da_grezzi_iw('S-14', 143, datetime(2026, 8, 20))
            _storico_kanban_da_grezzi_iw('S-14', 50, datetime(2026, 11, 3))
            _storico_kanban_da_grezzi_iw('S-14', -20, datetime(2026, 11, 3))
            _storico_kanban_da_grezzi_iw('S-14', 99, datetime(2026, 5, 1))  # prima del go-live
            db.session.commit()
            self.assertEqual(self._tot(2026, 8), 143)
            self.assertEqual(self._tot(2026, 11), 30)
            self.assertEqual(self._tot(2026, 5), 0)

    def test_ricalcolo_conta_solo_ultima_fase_approvata(self):
        with self.app.app_context():
            def ev(eid, fase, buoni, ts, approvato=True, componente=None):
                db.session.add(EventoConsuntivoPP(event_id=eid, op_code='OP-1', fase=fase, pezzi_buoni=buoni,
                                                  timestamp_evento=ts, approvato_direzione=approvato,
                                                  componente=componente))
            ev('a', 'Saldatura', 40, datetime(2026, 9, 5))
            ev('b', 'Saldatura Frontale', 60, datetime(2026, 9, 25))
            ev('c', 'Segatrice', 500, datetime(2026, 9, 2))            # fase intermedia: no
            ev('d', 'Saldatura', 70, datetime(2026, 10, 1), approvato=False)  # stand-by: no
            ev('e', 'Saldatura', 30, datetime(2026, 10, 2), componente='TR2002')  # componente: no
            # vecchio contatore gonfiato dai rientri + import CSV da preservare
            db.session.add(StoricoProduzione(kanban_id=self.kid, anno=2026, mese=9, qta_import=7, qta_auto=582))
            db.session.add(StoricoProduzione(kanban_id=self.kid, anno=2026, mese=11, qta_import=0, qta_auto=381))
            db.session.commit()
            storico_ricalcola_da_grezzi_iw()
            db.session.commit()
            sett = StoricoProduzione.query.filter_by(kanban_id=self.kid, anno=2026, mese=9).first()
            self.assertEqual((sett.qta_auto, sett.qta_import), (100, 7))
            self.assertEqual(self._tot(2026, 10), 0)
            self.assertEqual(self._tot(2026, 11), 0)


if __name__ == '__main__':
    unittest.main()
