"""
BUG REALE segnalato da Mauri, 06/10/2026 — caso reale PINX110: a differenza
di un Ciclo di Lavoro a più fasi dello STESSO codice (dove la WIP segue già
i pezzi che avanzano tra una fase e l'altra), PINX110 viene prodotto
attraverso una CATENA di codici DIVERSI di distinta base — ciascuno una
"fase" dal punto di vista di MasterWork, ma un ARTICOLO a parte qui:
PINXTT110 (Segatrice->Satinatrice) -> PINX110-A (Saldatura Tappo) ->
PINX110-B (Molatura Tappo e Satinatura) -> PINX110 (Saldatura Finale).

Quando dei pezzi di PINXTT110 vengono consumati per produrre PINX110-A, la
loro giacenza/WIP come PINXTT110 scende correttamente — ma quei pezzi NON
vanno ritagliati di nuovo: hanno già superato la Segatrice per sempre,
semplicemente non esistono più CON QUEL codice. Senza un credito che segua
la catena, l'ordine alla Segatrice per quei pezzi (ormai avanzati a
PINX110-A) riappariva come "ancora da produrre" — un falso allarme che,
visto da chi lavora in reparto, sembra un ordine "riaperto dal nulla".
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, GiacenzaWood,
                     WipFaseWood, OrdineProduzione, EventoConsuntivoPP)
from blueprints.produzione_pp.routes import pp_bp, _registra_evento_consuntivo


class TestDichiarazioneCreditoCatenaDistinta(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (EventoConsuntivoPP, WipFaseWood, DistintaBaseWood, GiacenzaWood,
                            OrdineProduzione, CicloLavoroWood, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            segatrice = CentroCostoWood(nome='Segatrice')
            satinatrice = CentroCostoWood(nome='Satinatrice')
            saldatura = CentroCostoWood(nome='Saldatura Tappo')
            molatura = CentroCostoWood(nome='Molatura Tappo e Satinatura')
            saldatura_finale = CentroCostoWood(nome='Saldatura Finale')
            db.session.add_all([segatrice, satinatrice, saldatura, molatura, saldatura_finale])
            db.session.flush()
            self.segatrice_id = segatrice.id
            self.satinatrice_id = satinatrice.id
            self.saldatura_id = saldatura.id

            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=segatrice.id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=satinatrice.id),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=saldatura.id),
                CicloLavoroWood(codice='PINX110-B', sequenza=1, centro_costo_id=molatura.id),
                CicloLavoroWood(codice='PINX110', sequenza=1, centro_costo_id=saldatura_finale.id),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-B', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-B', quantita=1.0),
                GiacenzaWood(codice='PINXTT110', quantita=250),
                WipFaseWood(codice='PINXTT110', centro_costo_id=segatrice.id, quantita=0),
                WipFaseWood(codice='PINXTT110', centro_costo_id=satinatrice.id, quantita=250),
                OrdineProduzione(codice='26100072', codice_articolo='PINX110',
                                  qta_pianificata=287, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def _righe_segatrice(self):
        r = self.client.get(f'/api/dichiarazione-produzione/{self.segatrice_id}/op-aperti')
        payload = r.get_json()
        if not payload:
            return None
        gruppo = payload[0]
        return next((c for c in gruppo['componenti'] if c['codice_lavorato'] == 'PINXTT110'), None)

    def test_senza_produzione_a_valle_segatrice_mostra_37_da_tagliare(self):
        """Verifica preliminare dello scenario: 250 ancora come PINXTT110,
        287 richiesti -> 37 ancora da tagliare, come nello screenshot."""
        comp = self._righe_segatrice()
        self.assertIsNotNone(comp)
        self.assertEqual(comp['saldo'], 37)

    def test_pezzi_avanzati_a_pinx110a_restano_creditati_alla_segatrice(self):
        """37 PINXTT110 sono stati consumati per produrre 37 PINX110-A
        (dichiarazione approvata) — quei 37 pezzi hanno GIÀ superato la
        Segatrice, per sempre: l'ordine alla Segatrice deve richiudersi
        (saldo 0, riga non più dichiarabile), non ripresentarsi."""
        with self.app.app_context():
            # Stato di partenza REALE (come nello screenshot prima della
            # dichiarazione di ieri): i 287 pezzi pianificati erano TUTTI
            # già tagliati e satinati (giacenza = somma WIP = 287) — non
            # 250. Il 250 compare SOLO dopo che 37 di questi sono stati
            # consumati per produrre PINX110-A: usare 250 come partenza di
            # questo test, invece, significherebbe che in totale sono
            # davvero stati tagliati solo 250 pezzi (37 mancherebbero
            # ancora per davvero) — uno scenario diverso, già coperto dal
            # test precedente.
            g = GiacenzaWood.query.get('PINXTT110')
            g.quantita = 287
            riga_sat = WipFaseWood.query.filter_by(
                codice='PINXTT110', centro_costo_id=self.satinatrice_id).first()
            riga_sat.quantita = 287
            db.session.commit()

            o = OrdineProduzione.query.filter_by(codice='26100072').first()
            _registra_evento_consuntivo(
                o, 'Saldatura Tappo', datetime.utcnow(), good=37, scrap=0, tempo=0, event_id=1,
                componente='PINX110-A', approvato_direzione=True)
            db.session.commit()
            # Lo scarico del componente deve comunque essere avvenuto come sempre
            # (287 di partenza - 37 consumati per produrre PINX110-A = 250).
            self.assertEqual(GiacenzaWood.query.get('PINXTT110').quantita, 250)
        comp = self._righe_segatrice()
        self.assertIsNone(comp, "i 37 pezzi avanzati a PINX110-A sono già stati tagliati: "
                                 "la riga non deve ripresentarsi come 'da tagliare'")

    def test_credito_resta_capped_alla_quantita_necessaria(self):
        """Più PINX110-A dichiarati di quanti PINXTT110 servissero in
        teoria (dato limite/storico) non deve mai generare un saldo
        negativo o un 'fatti' superiore al necessario."""
        with self.app.app_context():
            o = OrdineProduzione.query.filter_by(codice='26100072').first()
            _registra_evento_consuntivo(
                o, 'Saldatura Tappo', datetime.utcnow(), good=400, scrap=0, tempo=0, event_id=1,
                componente='PINX110-A', approvato_direzione=True)
            db.session.commit()
        comp = self._righe_segatrice()
        self.assertIsNone(comp)

    def test_senza_distinta_a_valle_nessun_effetto(self):
        """Nessuna regressione: un OP il cui codice_articolo è direttamente
        PINXTT110 (nessuna catena a valle — niente da cui prendere credito)
        si comporta come prima. Pianificati 1000 per superare abbondantemente
        i 250 già disponibili come PINXTT110 e avere comunque un saldo da
        verificare (con un fabbisogno piccolo i 250 già in giacenza
        basterebbero da soli, a prescindere dalla fix di oggi)."""
        with self.app.app_context():
            db.session.add(OrdineProduzione(codice='OP-DIRETTO', codice_articolo='PINXTT110',
                                             qta_pianificata=1000, stato='Rilasciato', priorita=2))
            db.session.commit()
        r = self.client.get(f'/api/dichiarazione-produzione/{self.segatrice_id}/op-aperti')
        gruppi = {g['codice']: g for g in r.get_json()}
        self.assertIn('OP-DIRETTO', gruppi)
        comp = gruppi['OP-DIRETTO']['componenti'][0]
        self.assertEqual(comp['saldo'], 750)  # 1000 - 250 già pronti, nessun credito da valle in gioco


if __name__ == '__main__':
    unittest.main()
