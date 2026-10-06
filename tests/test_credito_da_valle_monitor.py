"""
Regressione "credito da valle" sul MONITOR MACCHINA (segnalato da Mauri,
06/10/2026, STESSO giorno della fix originale, STESSA commessa reale
26100072 — "Ancora incongruenza: Il codice che è alla segatrice all'inizio
di questa commessa era già a magazzino quindi l'ordine alla segatrice era
chiuso perché era presente a magazzino, magari adesso scaricandolo con la
saldatura lui riapre l'ordine della segatrice"): il bug "credito da valle"
(vedi tests/test_dichiarazione_credito_catena_distinta.py) era stato
corretto SOLO in 'api_dichiarazione_op_aperti'
(blueprints/produzione_pp/routes.py). '_righe_macchina'
(blueprints/monitor/routes.py), che alimenta il Monitor Macchina/Totem (e a
cascata l'Inventario Codice Padre, che mostra gli stessi numeri), duplicava
una logica SEPARATA ("gia_disponibile" dalla sola giacenza grezza del
componente) che non guardava affatto i codici a valle della stessa catena
di distinta base — con 287 pezzi pianificati di cui 83 già saldati
(diventati PINX110-A) e 204 ancora fermi come PINXTT110, il Monitor Macchina
mostrava "83 da produrre" alla Segatrice, riaprendo un ordine già di fatto
coperto (204 + 83 = 287).

Questo test copre '_righe_macchina' direttamente, isolando lo stesso
scenario minimale già usato per la fix originale (37 pezzi avanzati), più
il caso reale con 83/204 di questo specifico report.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP)
from blueprints.monitor.routes import _righe_macchina


def _trova_riga(righe_per_sezione, op_codice):
    for sezione in righe_per_sezione.values():
        for r in sezione:
            if r['op_codice'] == op_codice:
                return r
    return None


class TestCreditoDaValleMonitor(unittest.TestCase):
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
            for modello in (EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                            OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            self.segatrice = CentroCostoWood(nome='Segatrice')
            self.saldatura = CentroCostoWood(nome='Saldatura')
            db.session.add_all([self.segatrice, self.saldatura])
            db.session.flush()
            self.segatrice_id, self.saldatura_id = self.segatrice.id, self.saldatura.id

            # Catena reale: PINX110 (prodotto finito dell'OP) -> PINX110-A
            # (saldato) -> PINXTT110 (grezzo tagliato alla Segatrice).
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.segatrice_id),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=self.saldatura_id),
                GiacenzaWood(codice='PINXTT110', quantita=204),
                GiacenzaWood(codice='PINX110-A', quantita=83),
                OrdineProduzione(codice='OP-2026-000072', codice_articolo='PINX110',
                                  qta_pianificata=287, qta_buona=0, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def test_83_pezzi_saldati_restano_creditati_alla_segatrice(self):
        """Caso reale 06/10/2026, commessa 26100072: 83 pezzi di PINXTT110
        sono stati consumati per produrre PINX110-A (saldatura) — la
        Segatrice NON deve tornare "83 da produrre", deve restare coperta
        (204 ancora fermi + 83 già avanzati = 287 = tutto il necessario)."""
        with self.app.app_context():
            db.session.add(EventoConsuntivoPP(
                event_id='evt-1', op_code='OP-2026-000072', fase='Saldatura Tappo',
                componente='PINX110-A', timestamp_evento=datetime.utcnow(),
                pezzi_buoni=83, pezzi_scarto=0, tempo_minuti=0, approvato_direzione=True))
            db.session.commit()

            segatrice = db.session.get(CentroCostoWood, self.segatrice_id)
            riga = _trova_riga(_righe_macchina(segatrice), 'OP-2026-000072')
            self.assertIsNotNone(riga)
            self.assertEqual(riga['saldo'], 0,
                              "prima della fix: 83 'da produrre' nonostante 204+83=287 già coperti "
                              "(204 ancora a magazzino come PINXTT110 + 83 già saldati a PINX110-A)")

    def test_senza_saldatura_a_valle_la_segatrice_mostra_ancora_83_da_tagliare(self):
        """Nessuna regressione: senza nessun EventoConsuntivoPP su
        PINX110-A, i 204 di giacenza non bastano e la Segatrice deve ancora
        mostrare il vero residuo (287 - 204 = 83)."""
        with self.app.app_context():
            segatrice = db.session.get(CentroCostoWood, self.segatrice_id)
            riga = _trova_riga(_righe_macchina(segatrice), 'OP-2026-000072')
            self.assertEqual(riga['saldo'], 83)

    def test_credito_resta_tettato_alla_quantita_necessaria(self):
        """Il credito da valle non deve mai far scendere il saldo sotto
        zero né 'prestare' pezzi che non esistono: anche con PIÙ pezzi
        dichiarati a valle di quanti ne servirebbero, il saldo resta 0, non
        negativo."""
        with self.app.app_context():
            db.session.add(EventoConsuntivoPP(
                event_id='evt-1', op_code='OP-2026-000072', fase='Saldatura Tappo',
                componente='PINX110-A', timestamp_evento=datetime.utcnow(),
                pezzi_buoni=400, pezzi_scarto=0, tempo_minuti=0, approvato_direzione=True))
            db.session.commit()
            segatrice = db.session.get(CentroCostoWood, self.segatrice_id)
            riga = _trova_riga(_righe_macchina(segatrice), 'OP-2026-000072')
            self.assertEqual(riga['saldo'], 0)


if __name__ == '__main__':
    unittest.main()
