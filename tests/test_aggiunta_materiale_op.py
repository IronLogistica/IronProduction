"""
Componente aggiunto per commessa (richiesta Mauri, 20/11/2026): per una
commessa di T250DT va saldata anche la targhetta TAVR1015, che la distinta
base non prevede. Non si cambia un materiale, se ne AGGIUNGE uno solo per
quell'OP: deve essere impegnato a magazzino, comparire nell'Ordine di Lavoro
e venire scaricato alla dichiarazione del codice padre. Le altre commesse e
la distinta base restano invariate.
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP, AggiuntaMaterialeOP, SostituzioneMaterialeOP, AuditPP)
from blueprints.produzione_pp.routes import pp_bp, PIN_DIREZIONE, _lista_lavoro_op, _calcola_consumi_standard
from blueprints.magazzino.routes import (_esplodi_componenti_op, impegni_op_per_codice,
                                         _righe_bom_attive_wood)


class TestAggiuntaMaterialeOP(unittest.TestCase):
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
            for m in (AggiuntaMaterialeOP, SostituzioneMaterialeOP, AuditPP, EventoConsuntivoPP, DistintaBaseWood,
                      GiacenzaWood, CicloLavoroWood, OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            sald = CentroCostoWood(nome='Saldatura')
            db.session.add(sald)
            db.session.flush()
            self.sald_id = sald.id
            a = OrdineProduzione(codice='OP-A', codice_articolo='T250DT', commessa='26110098',
                                 qta_pianificata=10, stato='Rilasciato', priorita=1)
            b = OrdineProduzione(codice='OP-B', codice_articolo='T250DT', commessa='26110099',
                                 qta_pianificata=10, stato='Rilasciato', priorita=2)
            db.session.add_all([
                DistintaBaseWood(codice_padre='T250DT', codice_figlio='TR2005', quantita=1.0),
                DistintaBaseWood(codice_padre='TR2005', codice_figlio='T6030SP3', quantita=0.03),
                CicloLavoroWood(codice='T250DT', sequenza=1, centro_costo_id=sald.id),
                GiacenzaWood(codice='TR2005', quantita=100),
                GiacenzaWood(codice='T6030SP3', quantita=100),
                GiacenzaWood(codice='TAVR1015', quantita=50),
                a, b,
            ])
            db.session.commit()
            self.a_id, self.b_id = a.id, b.id
        self.c = self.app.test_client()

    def _crea(self, **extra):
        body = {'codice_padre': 'T250DT', 'codice': 'tavr1015', 'quantita': '1',
                'motivo': 'targhetta richiesta dal cliente', 'pin': PIN_DIREZIONE}
        body.update(extra)
        return self.c.post(f'/api/ordini-produzione/{self.a_id}/aggiunte-materiale', json=body)

    def test_controlli(self):
        self.assertEqual(self._crea(pin='0000').status_code, 403)
        self.assertEqual(self._crea(motivo='').status_code, 400)
        self.assertEqual(self._crea(quantita='0').status_code, 400)
        self.assertEqual(self._crea(codice='NONESISTE').status_code, 400)
        self.assertEqual(self._crea(codice_padre='ALTRO').status_code, 400)
        self.assertTrue(self._crea().get_json()['ok'])
        self.assertEqual(self._crea().status_code, 400, 'stessa aggiunta due volte: no')

    def test_padri_proposti(self):
        d = self.c.get(f'/api/ordini-produzione/{self.a_id}/aggiunte-materiale').get_json()
        self.assertEqual([p['codice'] for p in d['padri']], ['T250DT', 'TR2005'],
                         'prodotto finito + semilavorati, non le materie prime')

    def test_aggiunta_solo_per_la_commessa_scelta(self):
        self.assertTrue(self._crea().get_json()['ok'])
        with self.app.app_context():
            a = db.session.get(OrdineProduzione, self.a_id)
            b = db.session.get(OrdineProduzione, self.b_id)
            self.assertIn('TAVR1015', {n['codice'] for n in _esplodi_componenti_op(a)})
            self.assertNotIn('TAVR1015', {n['codice'] for n in _esplodi_componenti_op(b)})
            # la distinta base vera non cambia
            self.assertEqual([r.codice_figlio for r in _righe_bom_attive_wood('T250DT')], ['TR2005'])
            self.assertEqual(DistintaBaseWood.query.filter_by(codice_padre='T250DT').count(), 1)

            # impegno a magazzino: 10 pz di T250DT x 1 targhetta = 10, solo OP-A
            imp = impegni_op_per_codice().get('TAVR1015', [])
            self.assertEqual({r['op_code'] for r in imp}, {'OP-A'})

            # scarico alla dichiarazione del padre: 4 T250DT -> 4 TAVR1015
            consumi, _c, _l = _calcola_consumi_standard(a, False, 'T250DT', 4)
            self.assertIn('TAVR1015', consumi)
            self.assertAlmostEqual(float(consumi['TAVR1015'] if not isinstance(consumi['TAVR1015'], dict)
                                         else consumi['TAVR1015'].get('qta', consumi['TAVR1015'].get('quantita'))), 4.0)
            consumi_b, _c, _l = _calcola_consumi_standard(b, False, 'T250DT', 4)
            self.assertNotIn('TAVR1015', consumi_b)

            # Ordine di Lavoro: riquadro con il componente in piu'
            ol = _lista_lavoro_op(a, db.session.get(CentroCostoWood, self.sald_id), assegna_numero=False)
            self.assertEqual(ol['aggiunte'][0]['codice'], 'TAVR1015')
            self.assertEqual(_lista_lavoro_op(b, db.session.get(CentroCostoWood, self.sald_id),
                                              assegna_numero=False)['aggiunte'], [])
            self.assertEqual(AuditPP.query.filter_by(azione='AGGIUNTA_MATERIALE').count(), 1)

    def test_revoca(self):
        aid = self._crea().get_json()['aggiunta']['id']
        self.assertEqual(self.c.get('/api/aggiunte-materiale/attive').get_json(), {str(self.a_id): 1})
        self.assertEqual(self.c.post(f'/api/aggiunte-materiale/{aid}/revoca', json={'pin': '0'}).status_code, 403)
        self.assertTrue(self.c.post(f'/api/aggiunte-materiale/{aid}/revoca',
                                    json={'pin': PIN_DIREZIONE}).get_json()['ok'])
        with self.app.app_context():
            a = db.session.get(OrdineProduzione, self.a_id)
            self.assertNotIn('TAVR1015', {n['codice'] for n in _esplodi_componenti_op(a)})
        self.assertEqual(self.c.get('/api/aggiunte-materiale/attive').get_json(), {})


if __name__ == '__main__':
    unittest.main()
