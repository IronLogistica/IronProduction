"""
Valorizzazione del WIP per fase (richiesta Mauri, 03/10/2026: "ci servirà
per calcolare le scorte finali") — costo standard CONGELATO (la versione
salvata per il codice, stessa filosofia di LegameCostoStandardOrdineWood
usata per le varianze OP, non le tariffe correnti di CentroCostoWood):
materiali per intero (si consumano alla prima fase) + lavorazione/
manodopera/overhead solo delle fasi già fatte fino a quella riga.
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, WipFaseWood,
                     CostoStandardVersioneWood, CostoStandardVersioneFaseWood)
from blueprints.produzione_pp.routes import pp_bp, _avanza_wip_fase_automatico
from blueprints.magazzino.routes import _valore_wip_a_fase


class TestWipValorizzazione(unittest.TestCase):
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
            for modello in (WipFaseWood, CostoStandardVersioneFaseWood, CostoStandardVersioneWood,
                            CicloLavoroWood, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            taglio = CentroCostoWood(nome='TAGLIO')
            satinatura = CentroCostoWood(nome='SATINATURA')
            db.session.add_all([taglio, satinatura])
            db.session.flush()
            self.taglio_id, self.satinatura_id = taglio.id, satinatura.id
            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.taglio_id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=self.satinatura_id),
            ])
            # Costo Standard congelato: materiali=10, overhead_materiali=1 (fisso),
            # TAGLIO (seq1): 60 pz/h a 120 €/h macchina + 30 €/h manodopera -> 2.0 + 0.5 €/pz
            # SATINATURA (seq2): 30 pz/h a 90 €/h macchina + 24 €/h manodopera -> 3.0 + 0.8 €/pz
            # overhead produzione 10% su (lavorazione+manodopera) cumulata.
            versione = CostoStandardVersioneWood(
                codice='PINXTT110', versione=1, costo_materiali=10.0,
                costo_lavorazione=5.0, costo_manodopera=1.3,
                costo_overhead_materiali=1.0, costo_overhead_produzione=0.63,
                costo_overhead=1.63, costo_totale=17.93, overhead_produzione_pct_usata=10.0)
            db.session.add(versione)
            db.session.flush()
            db.session.add_all([
                CostoStandardVersioneFaseWood(versione_id=versione.id, sequenza=1, nome_reparto='TAGLIO',
                                               produttivita_oraria_congelata=60, costo_orario_congelato=120,
                                               tariffa_manodopera_congelata=30),
                CostoStandardVersioneFaseWood(versione_id=versione.id, sequenza=2, nome_reparto='SATINATURA',
                                               produttivita_oraria_congelata=30, costo_orario_congelato=90,
                                               tariffa_manodopera_congelata=24),
            ])
            db.session.commit()
            self.versione_id = versione.id

    def test_valore_alla_prima_fase_e_solo_materiali_piu_quella_fase(self):
        with self.app.app_context():
            valore, versione_id = _valore_wip_a_fase('PINXTT110', self.taglio_id)
            # materiali(10) + overhead_materiali(1) + lavorazione(2.0) + manodopera(0.5) + overhead_prod(0.25)
            self.assertAlmostEqual(valore, 13.75, places=2)
            self.assertEqual(versione_id, self.versione_id)

    def test_valore_alla_seconda_fase_accumula_anche_la_prima(self):
        with self.app.app_context():
            valore, _ = _valore_wip_a_fase('PINXTT110', self.satinatura_id)
            self.assertAlmostEqual(valore, 17.93, places=2)

    def test_valore_alla_ultima_fase_coincide_col_costo_totale_standard(self):
        """Controllo di coerenza: un pezzo che ha completato TUTTE le fasi
        del ciclo deve valere esattamente come il costo standard pieno del
        prodotto finito — nessuna fase lasciata fuori, nessuna doppiata."""
        with self.app.app_context():
            valore, _ = _valore_wip_a_fase('PINXTT110', self.satinatura_id)
            versione = CostoStandardVersioneWood.query.filter_by(codice='PINXTT110').first()
            self.assertAlmostEqual(valore, versione.costo_totale, places=2)

    def test_nessun_costo_standard_salvato_ritorna_none(self):
        with self.app.app_context():
            valore, versione_id = _valore_wip_a_fase('CODICE-SENZA-STANDARD-MAI-SALVATO', self.taglio_id)
            self.assertIsNone(valore)
            self.assertIsNone(versione_id)

    def test_avanzamento_automatico_valorizza_la_riga(self):
        """L'aggancio automatico (_avanza_wip_fase_automatico) deve
        valorizzare la riga da solo, non solo spostare la quantità."""
        with self.app.app_context():
            _avanza_wip_fase_automatico('PINXTT110', 'TAGLIO', good=96, scrap=0)
            db.session.commit()
            riga = WipFaseWood.query.filter_by(codice='PINXTT110', centro_costo_id=self.taglio_id).first()
            self.assertAlmostEqual(riga.valore_unitario, 13.75, places=2)
            self.assertEqual(riga.versione_costo_id, self.versione_id)

            _avanza_wip_fase_automatico('PINXTT110', 'SATINATURA', good=60, scrap=0)
            db.session.commit()
            riga_sat = WipFaseWood.query.filter_by(codice='PINXTT110', centro_costo_id=self.satinatura_id).first()
            self.assertAlmostEqual(riga_sat.valore_unitario, 17.93, places=2)


if __name__ == '__main__':
    unittest.main()
