"""
Avanzamento AUTOMATICO del WIP per fase (richiesta Mauri, 03/10/2026:
"le WIP devono essere automatiche mica manuali", dopo la prima versione
di WIP Station che richiedeva un inserimento a mano).

Da qui in avanti ogni dichiarazione di produzione APPROVATA sposta da sola
i pezzi dalla fase precedente a quella appena dichiarata (vedi
_avanza_wip_fase_automatico, agganciata dentro
_applica_effetti_evento_consuntivo) — nessun inserimento manuale in WIP
Station richiesto per la produzione da oggi in poi. Resta manuale solo la
giacenza che esisteva GIÀ prima di questo aggancio (coperta da
test_wip_station_fase.py).
"""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, WipFaseWood
from blueprints.produzione_pp.routes import (pp_bp, _avanza_wip_fase_automatico,
                                              _applica_effetti_evento_consuntivo)


class TestWipFaseAutomatico(unittest.TestCase):
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
            for modello in (WipFaseWood, CicloLavoroWood, OrdineProduzione, CentroCostoWood):
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
            db.session.commit()

    def _righe_wip(self):
        return {w.centro_costo_id: w.quantita for w in WipFaseWood.query.filter_by(codice='PINXTT110').all()}

    def test_dichiarare_la_prima_fase_crea_la_riga_wip(self):
        """96 pezzi buoni dichiarati al Taglio (prima fase): nessuna fase
        precedente da cui sottrarre, si crea la riga WIP al Taglio."""
        with self.app.app_context():
            _avanza_wip_fase_automatico('PINXTT110', 'TAGLIO', good=96, scrap=0)
            db.session.commit()
            self.assertEqual(self._righe_wip(), {self.taglio_id: 96})

    def test_dichiarare_la_fase_successiva_sposta_i_pezzi_avanti(self):
        """96 pezzi già fermi al Taglio; ora se ne dichiarano 60 buoni alla
        Satinatura: devono SPOSTARSI (36 restano al Taglio, 60 passano
        alla Satinatura), non semplicemente aggiungersi altrove."""
        with self.app.app_context():
            _avanza_wip_fase_automatico('PINXTT110', 'TAGLIO', good=96, scrap=0)
            db.session.commit()
            _avanza_wip_fase_automatico('PINXTT110', 'SATINATURA', good=60, scrap=0)
            db.session.commit()
            self.assertEqual(self._righe_wip(), {self.taglio_id: 36, self.satinatura_id: 60})

    def test_lo_scarto_esce_dalla_fase_precedente_senza_avanzare(self):
        """100 pezzi al Taglio; alla Satinatura si dichiarano 90 buoni e 10
        di scarto: tutti e 100 lasciano il Taglio (scarto compreso — sono
        stati comunque lavorati), ma solo i 90 buoni arrivano in Satinatura."""
        with self.app.app_context():
            _avanza_wip_fase_automatico('PINXTT110', 'TAGLIO', good=100, scrap=0)
            db.session.commit()
            _avanza_wip_fase_automatico('PINXTT110', 'SATINATURA', good=90, scrap=10)
            db.session.commit()
            self.assertEqual(self._righe_wip(), {self.taglio_id: 0, self.satinatura_id: 90})

    def test_wip_non_scende_mai_sotto_zero(self):
        """Dichiarare più pezzi di quanti risultassero fermi alla fase
        precedente (es. dati storici mai tracciati) non deve produrre un
        residuo negativo — si ferma a zero, non blocca la dichiarazione."""
        with self.app.app_context():
            _avanza_wip_fase_automatico('PINXTT110', 'TAGLIO', good=10, scrap=0)
            db.session.commit()
            _avanza_wip_fase_automatico('PINXTT110', 'SATINATURA', good=50, scrap=0)
            db.session.commit()
            self.assertEqual(self._righe_wip(), {self.taglio_id: 0, self.satinatura_id: 50})

    def test_codice_mono_fase_non_genera_righe_wip(self):
        """Un codice con un'unica fase non ha nulla da spezzare: nessuna
        riga WIP, resta sulla sola giacenza totale (comportamento invariato)."""
        with self.app.app_context():
            centro_unico = CentroCostoWood(nome='SOLO_UNA_FASE')
            db.session.add(centro_unico)
            db.session.flush()
            db.session.add(CicloLavoroWood(codice='MONOFASE', sequenza=1, centro_costo_id=centro_unico.id))
            db.session.commit()
            _avanza_wip_fase_automatico('MONOFASE', 'SOLO_UNA_FASE', good=50, scrap=0)
            db.session.commit()
            self.assertEqual(WipFaseWood.query.filter_by(codice='MONOFASE').count(), 0)

    def test_fase_non_riconosciuta_non_fa_nulla(self):
        """Una fase dichiarata (testo libero MasterWork) che non corrisponde
        a nessun centro del ciclo di questo codice non deve creare righe
        fantasiose né far crashare la dichiarazione."""
        with self.app.app_context():
            _avanza_wip_fase_automatico('PINXTT110', 'REPARTO SCONOSCIUTO QUALUNQUE', good=50, scrap=0)
            db.session.commit()
            self.assertEqual(self._righe_wip(), {})

    def test_agganciato_dentro_applica_effetti_evento_consuntivo(self):
        """Verifica di integrazione: una vera dichiarazione approvata (il
        percorso usato da Dichiarazione Produzione / approvazione
        MasterWork) deve muovere il WIP da sola, senza che nessuno chiami
        _avanza_wip_fase_automatico a mano."""
        with self.app.app_context():
            o = OrdineProduzione(codice='OP-TEST', codice_articolo='PRODOTTO-FINITO',
                                  qta_pianificata=100, stato='Rilasciato', priorita=1)
            db.session.add(o)
            db.session.commit()
            _applica_effetti_evento_consuntivo(
                o, 'TAGLIO', None, good=96, scrap=0, tempo=0, event_id=1,
                componente_finale=False, codice_lavorato='PINXTT110', avanza_op=False)
            db.session.commit()
            self.assertEqual(self._righe_wip(), {self.taglio_id: 96})


if __name__ == '__main__':
    unittest.main()
