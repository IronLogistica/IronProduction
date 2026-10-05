"""
Regressione "WIP Station" sul MONITOR LIVE (segnalato da Mauri, 05/10/2026 —
caso reale PINX110/PINXTT110): la fix "WIP Station" (vedi
tests/test_wip_station_fase.py) era stata applicata solo alla Lista di
Lavoro stampata (_lista_lavoro_op), non al Monitor Live/Totem
(_righe_macchina, blueprints/monitor/routes.py) — che continuava ad
applicare la giacenza TOTALE come "già fatto" a OGNI fase. Con 287 pezzi di
cui 191 già oltre la Satinatura e 96 fermi dopo il solo Taglio, il Monitor
Live della Satinatura mostrava ancora Saldo 287 (da satinare tutto),
mentre l'Ordine di Lavoro stampato mostrava correttamente 0/Residuo
coerente con la fase. Questo test copre _righe_macchina direttamente.
"""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood, WipFaseWood, DistintaBaseWood
from blueprints.monitor.routes import _righe_macchina


def _trova_riga(righe_per_sezione, op_codice):
    for sezione in righe_per_sezione.values():
        for r in sezione:
            if r['op_codice'] == op_codice:
                return r
    return None


class TestWipStationFaseMonitor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (WipFaseWood, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                            OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            self.taglio = CentroCostoWood(nome='TAGLIO')
            self.satinatura = CentroCostoWood(nome='SATINATURA')
            db.session.add_all([self.taglio, self.satinatura])
            db.session.flush()
            self.taglio_id, self.satinatura_id = self.taglio.id, self.satinatura.id

            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.taglio_id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=self.satinatura_id),
                GiacenzaWood(codice='PINXTT110', quantita=287),
                OrdineProduzione(codice='OP-PINX', codice_articolo='PINXTT110',
                                  qta_pianificata=287, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def test_con_wip_il_monitor_live_satinatura_vede_ancora_96_pezzi(self):
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
            taglio = db.session.get(CentroCostoWood, self.taglio_id)
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)

            righe_taglio = _righe_macchina(taglio)
            righe_satin = _righe_macchina(satinatura)

            riga_taglio = _trova_riga(righe_taglio, 'OP-PINX')
            riga_satin = _trova_riga(righe_satin, 'OP-PINX')

            self.assertEqual(riga_taglio['saldo'], 0,
                              "il Taglio è coperto per intero: 96+191 = 287")
            self.assertEqual(riga_satin['saldo'], 96,
                              "la Satinatura deve ancora chiedere i 96 pezzi non ancora satinati "
                              "(prima della fix mostrava 287, stesso bug già corretto nel PDF)")

    def test_con_wip_la_satinatura_risulta_producibile_non_in_attesa(self):
        """Caso reale segnalato (screenshot 05/10): la Satinatrice mostrava
        'IN ATTESA, 0/287 pronti da Segatrice' anche con l'ordine di
        Segatrice già chiuso (tutto presente) — 'disponibile_da_monte'
        leggeva SOLO le dichiarazioni MasterWork, mai WIP Station. Con
        96+191=287 (l'intero fabbisogno) già fermi ad almeno il Taglio, la
        Satinatura deve risultare PRODUCIBILE, non IN ATTESA."""
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)
            riga_satin = _trova_riga(_righe_macchina(satinatura), 'OP-PINX')
            self.assertEqual(riga_satin['disponibile_da_monte'], 287,
                              "prima della fix era 0 (nessuna dichiarazione MasterWork alla Segatrice)")
            self.assertEqual(riga_satin['stato_producibilita'], 'producibile',
                              "prima della fix restava IN ATTESA nonostante la Segatrice fosse già chiusa")

    def test_satinatura_producibile_anche_con_barra_grezza_esaurita(self):
        """Caso reale segnalato con screenshot (05/10, secondo giro): dopo
        la fix precedente la Satinatrice mostrava correttamente "287/287
        pronti da «Segatrice»" ma restava IN ATTESA lo stesso — perché il
        controllo materiale (barra grezza tagliata alla Segatrice)
        confrontava la giacenza della barra con l'INTERA qta_necessaria
        (287) su OGNI fase, non solo sulla prima: a taglio completo la
        barra grezza è CORRETTAMENTE esaurita (consumata per tagliare tutti
        i 287 pezzi), ma questo non è più un blocco una volta che il taglio
        è finito. Con la Segatrice già chiusa (96+191=287 via WIP Station)
        e la barra grezza a 0 in giacenza, la Satinatrice deve risultare
        PRODUCIBILE."""
        with self.app.app_context():
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINXTT110', codice_figlio='BARRA-GREZZA', quantita=1.0),
                GiacenzaWood(codice='BARRA-GREZZA', quantita=0),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)
            riga_satin = _trova_riga(_righe_macchina(satinatura), 'OP-PINX')
            self.assertTrue(riga_satin['materiale_disponibile'],
                             "la barra grezza esaurita NON deve più bloccare una volta che il "
                             "taglio (fase a monte) è completo: era già stata consumata per "
                             "produrre i 287 pezzi, non manca nulla di nuovo")
            self.assertEqual(riga_satin['stato_producibilita'], 'producibile',
                              "prima della fix: IN ATTESA, bloccata dal controllo materiale "
                              "sulla barra grezza confrontato con l'intera qta invece che col "
                              "residuo ancora da tagliare (0, a taglio completo)")

    def test_segatrice_resta_in_attesa_se_la_barra_grezza_manca_davvero(self):
        """Nessuna regressione sul caso che questo controllo deve ancora
        intercettare (richiesta esplicita di Mauri, 02/10/2026): se il
        taglio NON è ancora completo e la barra grezza manca davvero, la
        Segatrice deve restare IN ATTESA — qui senza nessuna riga WIP
        Station, quindi nulla risulta ancora tagliato."""
        with self.app.app_context():
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINXTT110', codice_figlio='BARRA-GREZZA', quantita=1.0),
                GiacenzaWood(codice='BARRA-GREZZA', quantita=0),
            ])
            db.session.commit()
            taglio = db.session.get(CentroCostoWood, self.taglio_id)
            riga_taglio = _trova_riga(_righe_macchina(taglio), 'OP-PINX')
            self.assertFalse(riga_taglio['materiale_disponibile'])
            self.assertEqual(riga_taglio['stato_producibilita'], 'in_attesa')

    def test_senza_wip_la_giacenza_totale_copre_ancora_entrambe_le_fasi(self):
        """Nessuna regressione per i codici senza righe WIP."""
        with self.app.app_context():
            taglio = db.session.get(CentroCostoWood, self.taglio_id)
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)
            riga_taglio = _trova_riga(_righe_macchina(taglio), 'OP-PINX')
            riga_satin = _trova_riga(_righe_macchina(satinatura), 'OP-PINX')
            self.assertEqual(riga_taglio['saldo'], 0)
            self.assertEqual(riga_satin['saldo'], 0)


if __name__ == '__main__':
    unittest.main()
