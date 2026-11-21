"""
Contatori per MASTERCONTROL (GET /api/control/counters) — elenco deciso da
Mauri il 21/11/2026: Acquisti Produzione, Produzione, Magazzino Produzione.
"""
import json
import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from flask import Flask

from models import (db, OrdineProduzione, EventoConsuntivoPP, OrdineAcquistoWood, GiacenzaWood,
                    CentroCostoWood, CicloLavoroWood, LavorazioneTerzista)
import blueprints.mastercontrol.routes as mc
from blueprints.mastercontrol.routes import mastercontrol_bp


class TestMasterControlCounters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(mastercontrol_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        os.environ['MASTERCONTROL_TOKEN'] = 'tok-test'
        oggi = date.today()
        ieri = oggi - timedelta(days=1)
        domani = oggi + timedelta(days=1)
        adesso = datetime.utcnow()
        with self.app.app_context():
            for m in (OrdineProduzione, EventoConsuntivoPP, OrdineAcquistoWood, GiacenzaWood,
                      CicloLavoroWood, CentroCostoWood, LavorazioneTerzista):
                db.session.query(m).delete()
            taglio = CentroCostoWood(nome='Taglio')
            sald = CentroCostoWood(nome='Saldatura')
            db.session.add_all([taglio, sald])
            db.session.flush()
            db.session.add_all([
                CicloLavoroWood(codice='C10', sequenza=1, centro_costo_id=taglio.id),
                CicloLavoroWood(codice='C10', sequenza=2, centro_costo_id=sald.id),
                # OP-INIZIO: rilasciato 15 gg fa, nessuna fase -> da iniziare in ritardo
                OrdineProduzione(codice='OP-INIZIO', codice_articolo='C10', stato='Rilasciato', qta_pianificata=10,
                                 data_rilascio=adesso - timedelta(days=15)),
                # OP-NUOVO: rilasciato 3 gg fa -> ancora nei tempi
                OrdineProduzione(codice='OP-NUOVO', codice_articolo='C10', stato='Rilasciato', qta_pianificata=10,
                                 data_rilascio=adesso - timedelta(days=3)),
                # OP-SALD: taglio finito 9 gg fa, saldatura solo 4/10 -> da saldare in ritardo
                OrdineProduzione(codice='OP-SALD', codice_articolo='C10', stato='In esecuzione', qta_pianificata=10,
                                 data_rilascio=adesso - timedelta(days=20)),
                EventoConsuntivoPP(event_id='s1', op_code='OP-SALD', fase='Taglio', pezzi_buoni=10,
                                   timestamp_evento=adesso - timedelta(days=9)),
                EventoConsuntivoPP(event_id='s2', op_code='OP-SALD', fase='Saldatura', pezzi_buoni=4,
                                   timestamp_evento=adesso - timedelta(days=8)),
                # OP-OK: taglio 2 gg fa -> nei tempi
                OrdineProduzione(codice='OP-OK', codice_articolo='C10', stato='In esecuzione', qta_pianificata=10,
                                 data_rilascio=adesso - timedelta(days=20)),
                EventoConsuntivoPP(event_id='o1', op_code='OP-OK', fase='Taglio', pezzi_buoni=10,
                                   timestamp_evento=adesso - timedelta(days=2)),
                # OP-FATTO: saldatura completa -> niente
                OrdineProduzione(codice='OP-FATTO', codice_articolo='C10', stato='In esecuzione', qta_pianificata=10,
                                 data_rilascio=adesso - timedelta(days=30)),
                EventoConsuntivoPP(event_id='f1', op_code='OP-FATTO', fase='Taglio', pezzi_buoni=10,
                                   timestamp_evento=adesso - timedelta(days=20)),
                EventoConsuntivoPP(event_id='f2', op_code='OP-FATTO', fase='Saldatura', pezzi_buoni=10,
                                   timestamp_evento=adesso - timedelta(days=19)),
                # ordini fornitore
                OrdineAcquistoWood(filename='a.pdf', stato_label='DA_CONFERMARE'),
                OrdineAcquistoWood(filename='b.pdf', stato_label='LETTURA_DA_VERIFICARE'),
                OrdineAcquistoWood(filename='c.pdf', stato_label='ORDINE_CONFERMATO', confermato=True, data_consegna=domani),
                OrdineAcquistoWood(filename='d.pdf', stato_label='ORDINE_IN_ARRIVO', confermato=True, data_consegna=ieri),
                OrdineAcquistoWood(filename='e.pdf', stato_label='ORDINE_DA_RITIRARE', confermato=True, data_consegna=domani),
                OrdineAcquistoWood(filename='f.pdf', stato_label='ORDINE_RICEVUTO', confermato=True, data_consegna=ieri),
                # giacenze
                GiacenzaWood(codice='X', quantita=-3),
                GiacenzaWood(codice='Y', quantita=5),
                # terzista: C10 con 6 pezzi ancora fuori
                LavorazioneTerzista(stato='ATTESA_RIENTRO', qta=6, note=json.dumps({'codice': 'C10', 'qta_rientrata': 0})),
            ])
            db.session.commit()
        self.c = self.app.test_client()

    def _valori(self, fabbisogni=None, hpi=None, grezzo=None):
        fabbisogni = fabbisogni if fabbisogni is not None else [
            {'codice': 'T6030', 'tipo': 'MATERIA_PRIMA_FORNITORE', 'sottoscorta': 50, 'stato': 'DA_ORDINARE',
             'impegni': [{'op_code': 'OP-INIZIO', 'mancante': 3}, {'op_code': 'OP-NUOVO', 'mancante': 0}]},
            {'codice': 'L1', 'tipo': 'LASERATO', 'sottoscorta': 5, 'stato': 'COPERTO_DA_ORDINI',
             'impegni': [{'op_code': 'OP-OK', 'mancante': 2}]},
            {'codice': 'V1', 'tipo': 'COMPONENTE_ACQUISTO', 'sottoscorta': 0, 'stato': 'DA_ORDINARE',
             'impegni': [{'op_code': 'OP-SALD', 'mancante': 1}]},
            {'codice': 'V2', 'tipo': 'MATERIALE_CONSUMO', 'sottoscorta': 2, 'stato': 'COPERTO_DA_ORDINI', 'impegni': []},
        ]
        hpi = hpi if hpi is not None else [{'prodotti': [{'sotto_scorta': True}, {'sotto_scorta': False}, {'sotto_scorta': True}]}]
        grezzo = grezzo if grezzo is not None else {'C10': 40, 'S14': 0, 'Z9': -2}

        class _R:
            def __init__(self, d): self._d = d
            def get_json(self): return self._d
        with mock.patch('blueprints.acquisti_wood.routes.calcola_lista_fabbisogni', return_value=fabbisogni), \
                mock.patch('blueprints.kanban.routes.api_kanban_hpi_dati', return_value=_R(hpi)), \
                mock.patch('blueprints.magazzino.routes._grezzo_iw_per_codici', return_value=grezzo):
            r = self.c.get('/api/control/counters', headers={'X-Control-Token': 'tok-test'})
        self.assertEqual(r.status_code, 200)
        return {c['code']: c for c in r.get_json()['counters']}

    def test_token_obbligatorio(self):
        self.assertEqual(self.c.get('/api/control/counters').status_code, 403)
        self.assertEqual(self.c.get('/api/control/counters', headers={'X-Control-Token': 'no'}).status_code, 403)
        os.environ['MASTERCONTROL_TOKEN'] = ''
        self.assertEqual(self.c.get('/api/control/counters', headers={'X-Control-Token': ''}).status_code, 403)

    def test_acquisti_produzione(self):
        v = self._valori()
        self.assertEqual(v['ACQ_PROD_DA_CONFERMARE']['value'], 2)
        self.assertEqual(v['ACQ_PROD_IN_ARRIVO']['value'], 1, 'quello in ritardo non conta come "in arrivo"')
        self.assertEqual(v['ACQ_PROD_DA_RITIRARE']['value'], 1)
        self.assertEqual(v['ACQ_PROD_RITARDO']['value'], 1, 'il ricevuto non conta')

    def test_produzione(self):
        v = self._valori()
        self.assertEqual(v['PROD_SOTTOSCORTA_DA_PIANIFICARE']['value'], 2)
        self.assertEqual(v['PROD_OP_MATERIALI_DA_ORDINARE']['value'], 2,
                         'OP-INIZIO e OP-SALD: solo materiali DA_ORDINARE con mancante > 0')
        self.assertEqual(v['PROD_OP_INIZIO_RITARDO']['value'], 1, 'solo OP-INIZIO (>10 gg, nessuna fase)')
        self.assertEqual(v['PROD_OP_SALDATURA_RITARDO']['value'], 1, 'solo OP-SALD (>7 gg, saldatura 4/10)')

    def test_magazzino_produzione(self):
        v = self._valori()
        self.assertEqual(v['MAGP_MP_SOTTOSCORTA']['value'], 1)
        self.assertEqual(v['MAGP_LASERATI_SOTTOSCORTA']['value'], 1)
        self.assertEqual(v['MAGP_ALTRI_SOTTOSCORTA']['value'], 1, 'V1 ha sottoscorta 0: non conta')
        self.assertEqual(v['MAGP_GIACENZA_NEGATIVA']['value'], 2, 'X a magazzino + Z9 grezzo IW')
        self.assertEqual(v['MAGP_DA_TRATTARE']['value'], 1)
        self.assertEqual(v['MAGP_IN_TRATTAMENTO']['value'], 1)

    def test_aree_e_link(self):
        v = self._valori()
        self.assertEqual(len(v), len(mc.CONTATORI))
        for c in v.values():
            self.assertIn(c['area'], ('ACQUISTI_PRODUZIONE', 'PRODUZIONE', 'MAGAZZINO_PRODUZIONE'))
            self.assertTrue(c['url'].startswith('http'))

    def test_contatore_rotto_non_blocca_gli_altri(self):
        originale = list(mc.CONTATORI)
        try:
            mc.CONTATORI.insert(0, ('ROTTO', 'PRODUZIONE', 'Rotto', lambda ctx: 1 / 0, '/'))
            v = self._valori()
            self.assertNotIn('ROTTO', v)
            self.assertIn('ACQ_PROD_RITARDO', v)
        finally:
            mc.CONTATORI[:] = originale


if __name__ == '__main__':
    unittest.main()
