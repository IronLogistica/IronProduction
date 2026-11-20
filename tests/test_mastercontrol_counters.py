"""
Contatori per MASTERCONTROL (GET /api/control/counters): token obbligatorio,
numeri calcolati con gli stessi filtri della dashboard di IronProduction.
"""
import os
import unittest
from datetime import date, timedelta

from flask import Flask

from models import (db, OrdineProduzione, EventoConsuntivoPP, LavorazioneTerzista,
                    OrdineAcquistoWood, GiacenzaWood, ModuloNonConformita8D)
from blueprints.mastercontrol.routes import mastercontrol_bp, CONTATORI


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
        with self.app.app_context():
            for m in (OrdineProduzione, EventoConsuntivoPP, LavorazioneTerzista,
                      OrdineAcquistoWood, GiacenzaWood, ModuloNonConformita8D):
                db.session.query(m).delete()
            db.session.add_all([
                # OP: 2 in ritardo (aperti, data passata), 1 nei tempi, 1 chiuso con data passata
                OrdineProduzione(codice='OP1', codice_articolo='A', stato='Rilasciato', data_prevista=ieri),
                OrdineProduzione(codice='OP2', codice_articolo='A', stato='In esecuzione', data_prevista=ieri,
                                 bandiera_stato='SOSPESA'),
                OrdineProduzione(codice='OP3', codice_articolo='A', stato='Rilasciato', data_prevista=domani,
                                 bandiera_stato='ATTESA_MATERIALI'),
                OrdineProduzione(codice='OP4', codice_articolo='A', stato='Chiuso CO', data_prevista=ieri,
                                 bandiera_stato='SOSPESA'),
                # dichiarazioni: 1 da approvare, 1 visionata, 1 approvata
                EventoConsuntivoPP(event_id='e1', op_code='OP1', fase='A', timestamp_evento=oggi),
                EventoConsuntivoPP(event_id='e2', op_code='OP1', fase='A', timestamp_evento=oggi, visionato=True),
                EventoConsuntivoPP(event_id='e3', op_code='OP1', fase='A', timestamp_evento=oggi, approvato_direzione=True),
                # terzisti: 1 in ritardo, 1 nei tempi, 1 rientrata
                LavorazioneTerzista(stato='ATTESA_RIENTRO', data_rientro_prev=ieri.strftime('%d/%m/%Y')),
                LavorazioneTerzista(stato='ATTESA_RIENTRO', data_rientro_prev=domani.strftime('%d/%m/%Y')),
                LavorazioneTerzista(stato='RIENTRATA', data_rientro_prev=ieri.strftime('%d/%m/%Y')),
                # ordini fornitore
                OrdineAcquistoWood(filename='a.pdf', stato_label='DA_CONFERMARE'),
                OrdineAcquistoWood(filename='b.pdf', stato_label='LETTURA_DA_VERIFICARE'),
                OrdineAcquistoWood(filename='c.pdf', stato_label='ORDINE_DA_RITIRARE', confermato=True, data_consegna=ieri),
                OrdineAcquistoWood(filename='d.pdf', stato_label='ORDINE_RICEVUTO', confermato=True, data_consegna=ieri),
                # giacenze: 1 negativa
                GiacenzaWood(codice='X', quantita=-3),
                GiacenzaWood(codice='Y', quantita=5),
            ])
            db.session.commit()
        self.c = self.app.test_client()

    def _valori(self):
        r = self.c.get('/api/control/counters', headers={'X-Control-Token': 'tok-test'})
        self.assertEqual(r.status_code, 200)
        return {c['code']: c for c in r.get_json()['counters']}

    def test_token_obbligatorio(self):
        self.assertEqual(self.c.get('/api/control/counters').status_code, 403)
        self.assertEqual(self.c.get('/api/control/counters', headers={'X-Control-Token': 'no'}).status_code, 403)
        os.environ['MASTERCONTROL_TOKEN'] = ''
        self.assertEqual(self.c.get('/api/control/counters', headers={'X-Control-Token': ''}).status_code, 403,
                         'token vuoto su Railway = endpoint disabilitato')

    def test_conteggi(self):
        v = self._valori()
        self.assertEqual(v['PRODUZIONE_OP_RITARDO']['value'], 2, 'OP chiuso non conta anche se in ritardo')
        self.assertEqual(v['PRODUZIONE_OP_FERMI']['value'], 1, 'ATTESA_MATERIALI e OP chiusi esclusi')
        self.assertEqual(v['PRODUZIONE_DICHIARAZIONI_DA_APPROVARE']['value'], 1)
        self.assertEqual(v['PRODUZIONE_TERZISTI_RITARDO']['value'], 1)
        self.assertEqual(v['ACQUISTI_IW_NON_CONFERMATI']['value'], 2)
        self.assertEqual(v['ACQUISTI_IW_DA_RITIRARE']['value'], 1)
        self.assertEqual(v['ACQUISTI_IW_RITARDO']['value'], 1, 'solo non ricevuti con data consegna passata')
        self.assertEqual(v['MAGAZZINO_IW_GIACENZE_NEGATIVE']['value'], 1)

    def test_link_e_aree(self):
        v = self._valori()
        self.assertTrue(v['PRODUZIONE_OP_RITARDO']['url'].endswith('/ordini-produzione'))
        for c in v.values():
            self.assertIn(c['area'], ('PRODUZIONE', 'ACQUISTI', 'MAGAZZINO'))
            self.assertTrue(c['label'])

    def test_contatore_rotto_non_blocca_gli_altri(self):
        import blueprints.mastercontrol.routes as r
        originale = list(r.CONTATORI)
        try:
            r.CONTATORI.insert(0, ('ROTTO', 'PRODUZIONE', 'Rotto', lambda: 1 / 0, '/'))
            v = self._valori()
            self.assertNotIn('ROTTO', v)
            self.assertIn('PRODUZIONE_OP_RITARDO', v)
        finally:
            r.CONTATORI[:] = originale


if __name__ == '__main__':
    unittest.main()
