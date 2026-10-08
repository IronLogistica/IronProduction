"""
Regressione "WIP Station" sul Cruscotto KPI/Avanzamento Commesse (segnalato
da Mauri, 05/10/2026, STESSO caso reale PINX110/PINXTT110 già coperto da
tests/test_wip_station_fase.py per il PDF e tests/test_wip_station_fase_monitor.py
per il Monitor Live): calcola_avanzamento_commesse() (blueprints/produzione_pp/
avanzamento.py), che alimenta la tabella "Avanzamento Commesse" del Cruscotto
KPI, non guardava affatto le righe WIP Station — contava come "fatto" per
ogni zona SOLO i pezzi con una dichiarazione di produzione (EventoConsuntivoPP),
ignorando i pezzi già registrati come fermi ad almeno una certa fase via WIP
Station. Un codice i cui 96/191 pezzi erano stati censiti in WIP Station ma
mai "dichiarati" via evento restava quindi sempre a 0% sulle zone Taglio e
Sgola/Sati, anche dopo che il Monitor Live e il PDF erano già stati corretti.
"""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood, WipFaseWood
from blueprints.produzione_pp.avanzamento import calcola_avanzamento_commesse


def _risultato(risultati, op_codice):
    return next(r for r in risultati if r['op_codice'] == op_codice)


class TestWipStationFaseAvanzamento(unittest.TestCase):
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
            for modello in (WipFaseWood, GiacenzaWood, CicloLavoroWood, OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            # I nomi devono contenere le parole chiave di ZONE_STANDARD
            # ('sega' -> Taglio, 'satin' -> Sgola/Sati).
            self.segatrice = CentroCostoWood(nome='Segatrice', n_risorse_equivalenti=1)
            self.satinatrice = CentroCostoWood(nome='Satinatrice', n_risorse_equivalenti=1)
            db.session.add_all([self.segatrice, self.satinatrice])
            db.session.flush()
            self.segatrice_id, self.satinatrice_id = self.segatrice.id, self.satinatrice.id

            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.segatrice_id,
                                 produttivita_oraria=10),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=self.satinatrice_id,
                                 produttivita_oraria=10),
                GiacenzaWood(codice='PINXTT110', quantita=287),
                OrdineProduzione(codice='OP-PINX', codice_articolo='PINXTT110',
                                  qta_pianificata=287, qta_buona=0, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def test_con_wip_le_zone_riflettono_i_pezzi_gia_fermi_a_quella_fase(self):
        """Caso reale: 96 pezzi fermi dopo la Segatrice, 191 già oltre la
        Satinatrice — anche senza NESSUNA dichiarazione di produzione, la
        zona Taglio deve risultare 100% (287 coperti), la zona Sgola/Sati
        deve risultare 191/287 = 67%, non più bloccata a 0%."""
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.segatrice_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatrice_id, quantita=191),
            ])
            db.session.commit()
            risultati, _ = calcola_avanzamento_commesse()
            r = _risultato(risultati, 'OP-PINX')
            self.assertEqual(r['zone']['taglio'], 100,
                              "Segatrice coperta per intero (96+191=287), prima della fix restava 0%")
            self.assertEqual(r['zone']['sgola_sati'], 67,
                              "Satinatrice già a 191/287, prima della fix restava 0%")

    def test_senza_wip_la_giacenza_gia_a_magazzino_chiude_le_zone(self):
        """Pierantonio 08/10/2026 (PINX110, OP-2026-000072): se il semilavorato e' gia' tutto a magazzino gli
        Ordini di Lavoro sono chiusi ('da produrre 0') e il Cruscotto deve dire lo stesso: 100%, non 0%/71%."""
        with self.app.app_context():
            risultati, _ = calcola_avanzamento_commesse()
            r = _risultato(risultati, 'OP-PINX')
            self.assertEqual(r['zone']['taglio'], 100)
            self.assertEqual(r['zone']['sgola_sati'], 100)

    def test_senza_giacenza_ne_wip_ne_dichiarazioni_restano_a_zero(self):
        with self.app.app_context():
            GiacenzaWood.query.filter_by(codice='PINXTT110').delete()
            db.session.commit()
            risultati, _ = calcola_avanzamento_commesse()
            r = _risultato(risultati, 'OP-PINX')
            self.assertEqual(r['zone']['taglio'], 0)
            self.assertEqual(r['zone']['sgola_sati'], 0)

    def test_giacenza_piu_pezzi_gia_passati_a_valle_coprono_il_pianificato(self):
        """Caso reale: 287 pianificati del finito PINX110, 84 gia' fatti (qta_buona), 203 semilavorati a
        magazzino: Taglio e Sgola/Sati sono coperti al 100% (203 + 84 = 287)."""
        from models import DistintaBaseWood
        with self.app.app_context():
            OrdineProduzione.query.delete()
            GiacenzaWood.query.filter_by(codice='PINXTT110').update({'quantita': 203})
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINXTT110', quantita=1.0),
                OrdineProduzione(codice='OP-PINX', codice_articolo='PINX110', qta_pianificata=287, qta_buona=84,
                                 stato='Rilasciato', priorita=1)])
            db.session.commit()
            risultati, _ = calcola_avanzamento_commesse()
            r = _risultato(risultati, 'OP-PINX')
            self.assertEqual(r['zone']['taglio'], 100)
            self.assertEqual(r['zone']['sgola_sati'], 100)


if __name__ == '__main__':
    unittest.main()
