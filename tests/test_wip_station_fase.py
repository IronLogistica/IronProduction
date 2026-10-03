"""
Regressione "WIP Station" (richiesta Mauri, 03/10/2026 — caso reale
PINX110/PINXTT110): un codice con Ciclo di Lavoro a più fasi (TAGLIO poi
SATINATURA) applicava la STESSA giacenza totale come "già fatto" a OGNI
fase. Con 287 pezzi totali di cui 191 già oltre la Satinatura e 96 fermi
dopo il solo Taglio, prima di questa fix la Satinatura veniva considerata
chiusa (saldo 0) tanto quanto il Taglio, quando in realtà mancavano ancora
96 pezzi da satinare.

Copre sia la Lista di Lavoro di dettaglio (_lista_lavoro_op, quella
stampata) sia la versione batch leggera usata dalle card di Situazione OP
(_riepilogo_ordini_lavoro_per_op), e verifica che un codice SENZA righe
WIP continui a comportarsi esattamente come prima (nessuna regressione).
"""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood, WipFaseWood
from blueprints.produzione_pp.routes import pp_bp, _lista_lavoro_op, _riepilogo_ordini_lavoro_per_op
from blueprints.magazzino.routes import _carica_mappa_distinta_base_wood


def _saldo_riga(lista_lavoro):
    """Il 'saldo' (quanto resta davvero da fare, al netto della giacenza
    già disponibile per quella fase) vive dentro gruppi[].righe[], non nel
    totale_pz/residuo_pz di primo livello — quello riflette solo
    pianificato-vs-dichiarato, SENZA la giacenza (vedi _lista_lavoro_op)."""
    return lista_lavoro['gruppi'][0]['righe'][0]['saldo']


class TestWipStationFase(unittest.TestCase):
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
            for modello in (WipFaseWood, GiacenzaWood, CicloLavoroWood, OrdineProduzione, CentroCostoWood):
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
            self.op = OrdineProduzione.query.filter_by(codice='OP-PINX').first()

    def test_senza_wip_la_giacenza_totale_copre_entrambe_le_fasi(self):
        """Comportamento INVARIATO per un codice senza righe WIP: la
        giacenza intera vale per ogni fase (come prima di questa fix)."""
        with self.app.app_context():
            op = OrdineProduzione.query.filter_by(codice='OP-PINX').first()
            taglio = db.session.get(CentroCostoWood, self.taglio_id)
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)
            lista_taglio = _lista_lavoro_op(op, taglio, assegna_numero=False)
            lista_satin = _lista_lavoro_op(op, satinatura, assegna_numero=False)
            self.assertEqual(_saldo_riga(lista_taglio), 0)
            self.assertEqual(_saldo_riga(lista_satin), 0)

    def test_con_wip_la_satinatura_vede_ancora_96_pezzi_da_fare(self):
        """Caso reale segnalato: 191 pezzi già oltre la Satinatura (sequenza
        2), 96 fermi dopo il solo Taglio (sequenza 1) — il Taglio deve
        risultare chiuso (287 >= 287), la Satinatura deve ancora chiedere
        96 pezzi (287 - 191)."""
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
            op = OrdineProduzione.query.filter_by(codice='OP-PINX').first()
            taglio = db.session.get(CentroCostoWood, self.taglio_id)
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)
            lista_taglio = _lista_lavoro_op(op, taglio, assegna_numero=False)
            lista_satin = _lista_lavoro_op(op, satinatura, assegna_numero=False)
            self.assertEqual(_saldo_riga(lista_taglio), 0,
                              "il Taglio è coperto per intero: 96+191 = 287")
            self.assertEqual(_saldo_riga(lista_satin), 96,
                              "la Satinatura deve ancora chiedere i 96 pezzi non ancora satinati")

    def test_riepilogo_batch_situazione_op_coerente_con_la_lista_di_dettaglio(self):
        """La versione leggera usata dalle card di Situazione OP deve dare
        lo stesso risultato della Lista di Lavoro di dettaglio."""
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
            op = OrdineProduzione.query.filter_by(codice='OP-PINX').first()
            mappa = _carica_mappa_distinta_base_wood()
            riepilogo = _riepilogo_ordini_lavoro_per_op([op], mappa)
            per_centro = {r['centro_id']: r for r in riepilogo[op.id]}
            self.assertEqual(per_centro[self.taglio_id]['residuo_pz'], 0)
            self.assertEqual(per_centro[self.satinatura_id]['residuo_pz'], 96)

    def test_wip_non_supera_mai_quanto_gia_libero_da_impegni(self):
        """La suddivisione per fase può solo RESTRINGERE 'già disponibile',
        mai aumentarlo oltre quanto un secondo OP a priorità più alta ha
        già impegnato sullo stesso codice."""
        with self.app.app_context():
            # Un secondo OP, priorità più alta (numero più basso), che
            # impegna già 250 dei 287 pezzi — restano liberi solo 37.
            db.session.add(OrdineProduzione(codice='OP-ALTRO', codice_articolo='PINXTT110',
                                             qta_pianificata=250, stato='Rilasciato', priorita=0))
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
            op = OrdineProduzione.query.filter_by(codice='OP-PINX').first()
            satinatura = db.session.get(CentroCostoWood, self.satinatura_id)
            lista_satin = _lista_lavoro_op(op, satinatura, assegna_numero=False)
            # Senza impegni concorrenti il saldo sarebbe 96 (287-191); con
            # solo 37 pezzi davvero liberi, il saldo non può scendere sotto
            # 287 (richiesta) - 37 (liberi) = 250.
            self.assertEqual(_saldo_riga(lista_satin), 250)

    def test_pagina_wip_station_si_apre(self):
        self.client = self.app.test_client()
        resp = self.client.get('/wip-station')
        self.assertEqual(resp.status_code, 200)

    def test_api_wip_station_elenca_solo_codici_multi_fase(self):
        self.client = self.app.test_client()
        with self.app.app_context():
            # Un codice mono-fase non deve comparire: non può avere nessuna
            # giacenza "spezzata" tra fasi diverse.
            centro_unico = CentroCostoWood(nome='SOLO_UNA_FASE')
            db.session.add(centro_unico)
            db.session.flush()
            db.session.add(CicloLavoroWood(codice='MONOFASE', sequenza=1, centro_costo_id=centro_unico.id))
            db.session.commit()
        resp = self.client.get('/api/wip-station')
        dati = resp.get_json()
        codici = {r['codice'] for r in dati}
        self.assertIn('PINXTT110', codici)
        self.assertNotIn('MONOFASE', codici)
        riga = next(r for r in dati if r['codice'] == 'PINXTT110')
        self.assertEqual(riga['giacenza_totale'], 287)
        self.assertEqual([f['centro_nome'] for f in riga['fasi']], ['TAGLIO', 'SATINATURA'])

    def test_api_salva_wip_station_sostituisce_le_righe_precedenti(self):
        self.client = self.app.test_client()
        r1 = self.client.post('/api/wip-station/PINXTT110', json={
            'fasi': {str(self.taglio_id): 96, str(self.satinatura_id): 191},
        })
        self.assertEqual(r1.get_json(), {'ok': True})
        with self.app.app_context():
            righe = {w.centro_costo_id: w.quantita for w in WipFaseWood.query.filter_by(codice='PINXTT110').all()}
            self.assertEqual(righe, {self.taglio_id: 96, self.satinatura_id: 191})

        # Una seconda chiamata con valori diversi SOSTITUISCE, non somma.
        r2 = self.client.post('/api/wip-station/PINXTT110', json={
            'fasi': {str(self.taglio_id): 0, str(self.satinatura_id): 287},
        })
        self.assertEqual(r2.get_json(), {'ok': True})
        with self.app.app_context():
            righe = {w.centro_costo_id: w.quantita for w in WipFaseWood.query.filter_by(codice='PINXTT110').all()}
            # La quantità 0 per il taglio non genera una riga (0 non è >0).
            self.assertEqual(righe, {self.satinatura_id: 287})

    def test_api_salva_wip_station_ignora_centro_non_nel_ciclo(self):
        """Un centro_costo_id che non fa parte del Ciclo di Lavoro di quel
        codice viene scartato — non deve inquinare il calcolo del saldo con
        una fase inesistente per quel codice."""
        self.client = self.app.test_client()
        with self.app.app_context():
            estraneo = CentroCostoWood(nome='CENTRO_ESTRANEO')
            db.session.add(estraneo)
            db.session.commit()
            estraneo_id = estraneo.id
        resp = self.client.post('/api/wip-station/PINXTT110', json={'fasi': {str(estraneo_id): 50}})
        self.assertEqual(resp.get_json(), {'ok': True})
        with self.app.app_context():
            self.assertEqual(WipFaseWood.query.filter_by(codice='PINXTT110').count(), 0)


if __name__ == '__main__':
    unittest.main()
