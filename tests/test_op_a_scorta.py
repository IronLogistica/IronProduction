"""
OP A SCORTA (richiesta Mauri, 14/10/2026): commessa di produzione lanciata
senza ordine cliente, per magazzino (semilavorati/codici nipote preparati in
anticipo). Deve essere ben riconoscibile: flag sull'OP, cliente "A SCORTA",
audit, quadro giacenza/impegnato/in produzione alla creazione.
"""
import unittest

from flask import Flask

from models import db, OrdineProduzione, GiacenzaWood, DistintaBaseWood, AuditPP, EventoConsuntivoPP
from blueprints.produzione_pp.routes import pp_bp, _situazione_scorta_codice


class TestOPAScorta(unittest.TestCase):
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
            for m in (AuditPP, EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, OrdineProduzione):
                db.session.query(m).delete()
            db.session.commit()
            db.session.add_all([
                DistintaBaseWood(codice_padre='S-20', codice_figlio='S20-GAMBA', quantita=4.0),
                GiacenzaWood(codice='S20-GAMBA', quantita=30),
                # OP cliente che consuma 40 gambe; OP a scorta gia' aperto di 50 gambe
                OrdineProduzione(codice='OP-CLI', codice_articolo='S-20', commessa='C1', cliente='ROSSI',
                                 qta_pianificata=10, stato='Rilasciato', priorita=1),
                OrdineProduzione(codice='OP-SC1', codice_articolo='S20-GAMBA', commessa='C2', cliente='A SCORTA',
                                 qta_pianificata=50, qta_buona=10, stato='Rilasciato', priorita=2, a_scorta=True),
            ])
            db.session.commit()
        self.c = self.app.test_client()

    def _crea(self, **extra):
        body = {'codice_articolo': 'S20-GAMBA', 'qta_pianificata': 100, 'asa': 'Carpenteria Propria'}
        body.update(extra)
        return self.c.post('/api/ordini-produzione', json=body)

    def test_creazione_a_scorta(self):
        r = self._crea(a_scorta=True)
        self.assertEqual(r.status_code, 201)
        d = r.get_json()
        self.assertTrue(d['ordine']['a_scorta'])
        self.assertEqual(d['ordine']['cliente'], 'A SCORTA')
        s = d['situazione_scorta']
        self.assertEqual(s['giacenza'], 30)
        self.assertEqual(s['impegnato'], 40, "l'OP cliente chiede 40 gambe (impegnato lordo, come in Magazzino)")
        self.assertEqual(s['libero'], -10, 'negativo = gia mancanti 10 per gli OP aperti')
        self.assertEqual(s['in_produzione_scorta'], 40, "OP-SC1: 50 pianificati - 10 fatti; il nuovo OP escluso")
        self.assertEqual([o['op_code'] for o in s['op_scorta_aperti']], ['OP-SC1'])
        with self.app.app_context():
            o = OrdineProduzione.query.filter_by(codice=d['ordine']['codice']).one()
            self.assertTrue(o.a_scorta)
            self.assertIn('A SCORTA', AuditPP.query.filter_by(op_code=o.codice).one().dettaglio)

    def test_checkbox_html_on_e_cliente_esplicito(self):
        d = self._crea(a_scorta='on', cliente='Magazzino Nord').get_json()
        self.assertTrue(d['ordine']['a_scorta'])
        self.assertEqual(d['ordine']['cliente'], 'Magazzino Nord')

    def test_op_normale_non_a_scorta(self):
        d = self._crea(cliente='ROSSI').get_json()
        self.assertFalse(d['ordine']['a_scorta'])
        self.assertIsNone(d['situazione_scorta'])
        lista = self.c.get('/api/ordini-produzione').get_json()
        self.assertEqual(sorted(x['codice'] for x in lista if x['a_scorta']), ['OP-SC1'])

    def test_modifica_flag(self):
        oid = self._crea(cliente='').get_json()['ordine']['id']
        d = self.c.put(f'/api/ordini-produzione/{oid}', json={'a_scorta': True}).get_json()
        self.assertTrue(d['ordine']['a_scorta'])
        self.assertEqual(d['ordine']['cliente'], 'A SCORTA')
        d = self.c.put(f'/api/ordini-produzione/{oid}', json={'a_scorta': False}).get_json()
        self.assertFalse(d['ordine']['a_scorta'])
        self.assertEqual(d['ordine']['cliente'], '')

    def test_situazione_mai_eccezioni(self):
        with self.app.app_context():
            s = _situazione_scorta_codice('NON-ESISTE')
            self.assertEqual(s['giacenza'], 0)
            self.assertEqual(s['copertura_futura'], 0)


if __name__ == '__main__':
    unittest.main()
