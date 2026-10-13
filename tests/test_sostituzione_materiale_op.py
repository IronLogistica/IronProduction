"""
Sostituzione materiale per commessa (richiesta Mauri, 13/10/2026): per il
cavalletto S-20 la distinta prevede T1515; per UNA commessa si usa T1515SP1
perche' T1515 non e' disponibile. La distinta base non cambia: la sostituzione
vale solo per quell'OP (Ordine di Lavoro, fabbisogni, scarico alla
dichiarazione) e le altre commesse continuano a usare T1515.
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP, SostituzioneMaterialeOP, AuditPP)
from blueprints.produzione_pp.routes import pp_bp, PIN_DIREZIONE, _lista_lavoro_op, _calcola_consumi_standard
from blueprints.magazzino.routes import (_esplodi_componenti_op, impegni_op_per_codice,
                                         _righe_bom_attive_wood)


class TestSostituzioneMaterialeOP(unittest.TestCase):
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
            for m in (SostituzioneMaterialeOP, AuditPP, EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood,
                      CicloLavoroWood, OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg = CentroCostoWood(nome='Segatrice')
            db.session.add(seg)
            db.session.flush()
            self.seg_id = seg.id
            a = OrdineProduzione(codice='OP-A', codice_articolo='S-20', commessa='C-A',
                                 qta_pianificata=10, stato='Rilasciato', priorita=1)
            b = OrdineProduzione(codice='OP-B', codice_articolo='S-20', commessa='C-B',
                                 qta_pianificata=10, stato='Rilasciato', priorita=2)
            db.session.add_all([
                DistintaBaseWood(codice_padre='S-20', codice_figlio='S20-GAMBA', quantita=4.0),
                DistintaBaseWood(codice_padre='S20-GAMBA', codice_figlio='T1515', quantita=1.0),
                CicloLavoroWood(codice='S20-GAMBA', sequenza=1, centro_costo_id=seg.id),
                GiacenzaWood(codice='T1515', quantita=0),
                GiacenzaWood(codice='T1515SP1', quantita=100),
                a, b,
            ])
            db.session.commit()
            self.a_id, self.b_id = a.id, b.id
        self.c = self.app.test_client()

    def _crea(self, **extra):
        body = {'codice_padre': 'S20-GAMBA', 'originale': 'T1515', 'sostitutivo': 't1515sp1',
                'motivo': 'T1515 non disponibile', 'pin': PIN_DIREZIONE}
        body.update(extra)
        return self.c.post(f'/api/ordini-produzione/{self.a_id}/sostituzioni-materiale', json=body)

    def test_pin_e_motivo_obbligatori(self):
        self.assertEqual(self._crea(pin='0000').status_code, 403)
        self.assertEqual(self._crea(motivo='').status_code, 400)
        self.assertEqual(self._crea(sostitutivo='NONESISTE').status_code, 400)

    def test_sostituzione_solo_per_la_commessa_scelta(self):
        self.assertTrue(self._crea().get_json()['ok'])
        with self.app.app_context():
            a = db.session.get(OrdineProduzione, self.a_id)
            b = db.session.get(OrdineProduzione, self.b_id)
            codici_a = {n['codice'] for n in _esplodi_componenti_op(a)}
            codici_b = {n['codice'] for n in _esplodi_componenti_op(b)}
            self.assertIn('T1515SP1', codici_a)
            self.assertNotIn('T1515', codici_a)
            self.assertIn('T1515', codici_b, "l'altra commessa resta sul materiale della distinta")
            # la distinta base vera non cambia
            self.assertEqual([r.codice_figlio for r in _righe_bom_attive_wood('S20-GAMBA')], ['T1515'])
            self.assertEqual(DistintaBaseWood.query.filter_by(codice_padre='S20-GAMBA').one().codice_figlio, 'T1515')

            # Ordine di Lavoro: colonna Materiale e riquadro sostituzioni
            ol = _lista_lavoro_op(a, db.session.get(CentroCostoWood, self.seg_id), assegna_numero=False)
            self.assertEqual(ol['gruppi'][0]['materiale'], 'T1515SP1')
            self.assertEqual(ol['sostituzioni'][0]['sostitutivo'], 'T1515SP1')
            self.assertEqual(_lista_lavoro_op(b, db.session.get(CentroCostoWood, self.seg_id),
                                              assegna_numero=False)['gruppi'][0]['materiale'], 'T1515')

            # scarico alla dichiarazione: esce T1515SP1
            consumi, _c, _l = _calcola_consumi_standard(a, False, 'S20-GAMBA', 40)
            self.assertIn('T1515SP1', consumi)
            self.assertNotIn('T1515', consumi)

            # impegni/fabbisogni: A impegna T1515SP1, B impegna T1515
            imp = impegni_op_per_codice()
            self.assertEqual({r['op_code'] for r in imp.get('T1515SP1', [])}, {'OP-A'})
            self.assertEqual({r['op_code'] for r in imp.get('T1515', [])}, {'OP-B'})

    def test_revoca_torna_al_materiale_originale(self):
        sid = self._crea().get_json()['sostituzione']['id']
        self.assertEqual(self.c.post(f'/api/sostituzioni-materiale/{sid}/revoca', json={'pin': '0'}).status_code, 403)
        self.assertTrue(self.c.post(f'/api/sostituzioni-materiale/{sid}/revoca',
                                    json={'pin': PIN_DIREZIONE}).get_json()['ok'])
        with self.app.app_context():
            a = db.session.get(OrdineProduzione, self.a_id)
            self.assertIn('T1515', {n['codice'] for n in _esplodi_componenti_op(a)})
        self.assertEqual(self.c.get('/api/sostituzioni-materiale/attive').get_json(), {})


if __name__ == '__main__':
    unittest.main()
