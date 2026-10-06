"""
BUG REALE segnalato da Mauri, 06/10/2026 — caso PINXTT110: la pagina WIP
Station mostrava "la somma inserita (287) non coincide con la giacenza
totale (250)" dopo che una dichiarazione di produzione di 37 PINX110-A
aveva consumato 37 pezzi di PINXTT110 come componente. La giacenza piana
era scesa correttamente a 250, ma la suddivisione per fase (WipFaseWood —
Satinatrice=287) non si era mossa: _avanza_wip_fase_automatico sposta la
WIP solo del codice che viene DICHIARATO (qui PINX110-A, mono-fase, nessun
WipFaseWood proprio), mai di un componente consumato come materiale da
un ALTRO codice.

Verifica che _consuma_wip_multi_fase (agganciata dentro
_applica_effetti_evento_consuntivo, sui due scarichi di 'consumi' e
'contestuali', e sulla Dichiarazione Libera) tenga allineata la WIP del
componente consumato, prendendo i pezzi dalla fase PIÙ AVANZATA del suo
ciclo (quella fisicamente "pronta" per essere usata altrove).
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, GiacenzaWood,
                     WipFaseWood, OrdineProduzione)
from blueprints.produzione_pp.routes import pp_bp, _applica_effetti_evento_consuntivo, _consuma_wip_multi_fase


class TestWipConsumoComponente(unittest.TestCase):
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
            for modello in (WipFaseWood, DistintaBaseWood, GiacenzaWood, OrdineProduzione,
                            CicloLavoroWood, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            segatrice = CentroCostoWood(nome='Segatrice')
            satinatrice = CentroCostoWood(nome='Satinatrice')
            assemblaggio = CentroCostoWood(nome='Assemblaggio')
            db.session.add_all([segatrice, satinatrice, assemblaggio])
            db.session.flush()
            self.segatrice_id, self.satinatrice_id = segatrice.id, satinatrice.id
            self.assemblaggio_id = assemblaggio.id

            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.segatrice_id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=self.satinatrice_id),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=self.assemblaggio_id),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                GiacenzaWood(codice='PINXTT110', quantita=287),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.segatrice_id, quantita=0),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatrice_id, quantita=287),
            ])
            db.session.commit()

    def _wip(self):
        return {w.centro_costo_id: w.quantita for w in WipFaseWood.query.filter_by(codice='PINXTT110').all()}

    # ------------------------------------------------------------------
    # Unità: _consuma_wip_multi_fase da sola
    # ------------------------------------------------------------------
    def test_consuma_dalla_fase_piu_avanzata(self):
        with self.app.app_context():
            _consuma_wip_multi_fase('PINXTT110', 37)
            db.session.commit()
            self.assertEqual(self._wip(), {self.segatrice_id: 0, self.satinatrice_id: 250})

    def test_scende_alla_fase_precedente_se_quella_avanzata_non_basta(self):
        with self.app.app_context():
            riga_sat = WipFaseWood.query.filter_by(codice='PINXTT110', centro_costo_id=self.satinatrice_id).first()
            riga_sat.quantita = 20
            riga_seg = WipFaseWood.query.filter_by(codice='PINXTT110', centro_costo_id=self.segatrice_id).first()
            riga_seg.quantita = 50
            db.session.commit()
            _consuma_wip_multi_fase('PINXTT110', 37)  # 20 dalla Satinatrice, 17 dalla Segatrice
            db.session.commit()
            self.assertEqual(self._wip(), {self.segatrice_id: 33, self.satinatrice_id: 0})

    def test_non_scende_mai_sotto_zero(self):
        with self.app.app_context():
            _consuma_wip_multi_fase('PINXTT110', 999999)
            db.session.commit()
            self.assertEqual(self._wip(), {self.segatrice_id: 0, self.satinatrice_id: 0})

    def test_codice_mono_fase_non_fa_nulla(self):
        with self.app.app_context():
            _consuma_wip_multi_fase('PINX110-A', 10)  # mono-fase, nessuna riga WIP da toccare
            db.session.commit()  # non deve sollevare eccezioni

    # ------------------------------------------------------------------
    # Integrazione: una vera dichiarazione che consuma PINXTT110
    # ------------------------------------------------------------------
    def test_dichiarare_pinx110a_allinea_la_wip_del_componente_consumato(self):
        with self.app.app_context():
            o = OrdineProduzione(codice='OP-TEST', codice_articolo='PINX110-A',
                                  qta_pianificata=37, stato='Rilasciato', priorita=1)
            db.session.add(o)
            db.session.commit()

            _applica_effetti_evento_consuntivo(
                o, 'Assemblaggio', None, good=37, scrap=0, tempo=0, event_id=1,
                componente_finale=True, codice_lavorato='PINX110-A', avanza_op=True)
            db.session.commit()

            g = GiacenzaWood.query.get('PINXTT110')
            self.assertEqual(g.quantita, 250, "il componente va scaricato come sempre")
            self.assertEqual(self._wip(), {self.segatrice_id: 0, self.satinatrice_id: 250},
                              "prima della fix la WIP restava a {Segatrice: 0, Satinatrice: 287} "
                              "— disallineata dalla giacenza piana appena scaricata")


if __name__ == '__main__':
    unittest.main()
