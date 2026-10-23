"""
BUG REALE, 23/10/2026 — caso TR2501 (tubo con ciclo Curvatubi -> Trapani):
WIP Station mostrava giacenza 56 contro WIP 26, differenza 30. I movimenti
di magazzino mostravano consumi come componente di T250 (-29, -1) poi
stornati (+29, +1 "STORNO consuntivo"). Lo storno restituiva i pezzi alla
giacenza piana ma NON alla suddivisione per fase, che la dichiarazione
originale aveva consumato dalla fase piu' avanzata (Trapani).

Verifica che _storna_evento_consuntivo riporti i pezzi anche in WIP, sulla
fase piu' avanzata, senza mai superare la giacenza.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, GiacenzaWood,
                     WipFaseWood, OrdineProduzione, EventoConsuntivoPP)
from blueprints.produzione_pp.routes import (pp_bp, _applica_effetti_evento_consuntivo,
                                              _storna_evento_consuntivo, _ripristina_wip_multi_fase)


class TestWipStornoComponente(unittest.TestCase):
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
            for modello in (EventoConsuntivoPP, WipFaseWood, DistintaBaseWood, GiacenzaWood,
                            OrdineProduzione, CicloLavoroWood, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            curvatubi = CentroCostoWood(nome='Curvatubi')
            trapani = CentroCostoWood(nome='Trapani')
            assemblaggio = CentroCostoWood(nome='Assemblaggio')
            db.session.add_all([curvatubi, trapani, assemblaggio])
            db.session.flush()
            self.curvatubi_id, self.trapani_id = curvatubi.id, trapani.id

            db.session.add_all([
                CicloLavoroWood(codice='TR2501', sequenza=1, centro_costo_id=self.curvatubi_id),
                CicloLavoroWood(codice='TR2501', sequenza=2, centro_costo_id=self.trapani_id),
                CicloLavoroWood(codice='T250', sequenza=1, centro_costo_id=assemblaggio.id),
                DistintaBaseWood(codice_padre='T250', codice_figlio='TR2501', quantita=1.0),
                GiacenzaWood(codice='TR2501', quantita=120),
                WipFaseWood(codice='TR2501', centro_costo_id=self.curvatubi_id, quantita=0),
                WipFaseWood(codice='TR2501', centro_costo_id=self.trapani_id, quantita=120),
            ])
            db.session.commit()

    def _wip(self):
        return {w.centro_costo_id: w.quantita for w in WipFaseWood.query.filter_by(codice='TR2501').all()}

    def test_ripristina_sulla_fase_piu_avanzata_entro_la_giacenza(self):
        with self.app.app_context():
            # consumo di 30 (giacenza 120 -> 90, WIP Trapani 120 -> 90), poi storno.
            g = GiacenzaWood.query.get('TR2501'); g.quantita = 90
            WipFaseWood.query.filter_by(codice='TR2501', centro_costo_id=self.trapani_id).first().quantita = 90
            db.session.commit()
            g.quantita = 120  # il movimento di storno ha gia' riportato la giacenza
            _ripristina_wip_multi_fase('TR2501', 30)
            db.session.commit()
            self.assertEqual(self._wip(), {self.curvatubi_id: 0, self.trapani_id: 120})

    def test_non_supera_mai_la_giacenza(self):
        with self.app.app_context():
            # WIP gia' pari alla giacenza: nessun pezzo in piu' da inventare.
            _ripristina_wip_multi_fase('TR2501', 30)
            db.session.commit()
            self.assertEqual(self._wip(), {self.curvatubi_id: 0, self.trapani_id: 120})

    def test_senza_righe_wip_non_fa_nulla(self):
        with self.app.app_context():
            WipFaseWood.query.filter_by(codice='TR2501').delete()
            db.session.commit()
            _ripristina_wip_multi_fase('TR2501', 30)
            db.session.commit()
            self.assertEqual(self._wip(), {})

    def test_dichiarare_e_stornare_t250_lascia_giacenza_e_wip_allineate(self):
        with self.app.app_context():
            o = OrdineProduzione(codice='OP-TEST', codice_articolo='T250',
                                  qta_pianificata=30, stato='Rilasciato', priorita=1)
            db.session.add(o)
            db.session.commit()

            _applica_effetti_evento_consuntivo(
                o, 'Assemblaggio', None, good=30, scrap=0, tempo=0, event_id='ev-1',
                componente_finale=True, codice_lavorato='T250', avanza_op=True)
            db.session.commit()
            self.assertEqual(GiacenzaWood.query.get('TR2501').quantita, 90)
            self.assertEqual(self._wip(), {self.curvatubi_id: 0, self.trapani_id: 90})

            e = EventoConsuntivoPP(event_id='ev-1', op_code='OP-TEST', fase='Assemblaggio',
                                    timestamp_evento=datetime.utcnow(), pezzi_buoni=30, pezzi_scarto=0,
                                    tempo_minuti=0, approvato_direzione=True)
            db.session.add(e)
            db.session.commit()
            _storna_evento_consuntivo(e, o)
            db.session.commit()

            self.assertEqual(GiacenzaWood.query.get('TR2501').quantita, 120)
            self.assertEqual(self._wip(), {self.curvatubi_id: 0, self.trapani_id: 120},
                              "prima della fix la WIP restava a Trapani=90 con giacenza 120")


if __name__ == '__main__':
    unittest.main()
