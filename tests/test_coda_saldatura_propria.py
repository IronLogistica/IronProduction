"""Live Saldatura: coda PROPRIA, indipendente dall'ordine del cruscotto KPI."""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione,
                    SequenzaAvanzamentoKPI, SequenzaMonitorMacchina)
from blueprints.monitor.routes import monitor_bp, _righe_macchina
from blueprints.produzione_pp.routes import pp_bp


class TestCodaSaldatura(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False, CAPO_PIN='9999')
        db.init_app(self.app)
        self.app.register_blueprint(monitor_bp)
        self.app.register_blueprint(pp_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            sald = CentroCostoWood(nome='Saldatura')
            sega = CentroCostoWood(nome='Segatrice')
            db.session.add_all([sald, sega]); db.session.flush()
            self.sald, self.sega = sald.id, sega.id
            db.session.add_all([
                CicloLavoroWood(codice='PA', sequenza=1, centro_costo_id=sald.id),
                CicloLavoroWood(codice='PB', sequenza=1, centro_costo_id=sald.id),
                CicloLavoroWood(codice='PA', sequenza=2, centro_costo_id=sega.id),
                CicloLavoroWood(codice='PB', sequenza=2, centro_costo_id=sega.id),
            ])
            a = OrdineProduzione(codice='OP-A', codice_articolo='PA', qta_pianificata=5, stato='Rilasciato', priorita=1)
            b = OrdineProduzione(codice='OP-B', codice_articolo='PB', qta_pianificata=5, stato='Rilasciato', priorita=2)
            db.session.add_all([a, b]); db.session.flush()
            self.a, self.b = a.id, b.id
            # KPI: B davanti ad A
            db.session.add_all([SequenzaAvanzamentoKPI(ordine_produzione_id=b.id, posizione=0),
                                SequenzaAvanzamentoKPI(ordine_produzione_id=a.id, posizione=1)])
            db.session.commit()
        self.c = self.app.test_client()

    def _ordine(self, cid):
        with self.app.app_context():
            r = _righe_macchina(db.session.get(CentroCostoWood, cid))
            tutte = sorted(r['da_iniziare'] + r['lavorazione'] + r['terminati'],
                           key=lambda x: x['posizione_manuale'] if x['posizione_manuale'] is not None else 999)
            return [x['op_codice'] for x in tutte]

    def test_default_segue_kpi(self):
        self.assertEqual(self._ordine(self.sald), ['OP-B', 'OP-A'])

    def test_saldatura_propria_non_tocca_kpi_ne_altre_macchine(self):
        r = self.c.post(f'/api/totem/{self.sald}/ordina-coda', json={'ordine': [self.a, self.b], 'pin': '9999'})
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(self._ordine(self.sald), ['OP-A', 'OP-B'])
        self.assertEqual(self._ordine(self.sega), ['OP-B', 'OP-A'])   # KPI invariato
        with self.app.app_context():
            self.assertEqual(SequenzaAvanzamentoKPI.query.count(), 2)

    def test_ripristino(self):
        self.c.post(f'/api/totem/{self.sald}/ordina-coda', json={'ordine': [self.a, self.b], 'pin': '9999'})
        self.assertTrue(self.c.delete(f'/api/totem/{self.sald}/ordina-coda', json={'pin': '9999'}).get_json()['ok'])
        self.assertEqual(self._ordine(self.sald), ['OP-B', 'OP-A'])

    def test_pin_errato_e_altre_macchine_rifiutate(self):
        self.assertEqual(self.c.post(f'/api/totem/{self.sald}/ordina-coda', json={'ordine': [self.a], 'pin': 'x'}).status_code, 403)
        self.assertEqual(self.c.post(f'/api/totem/{self.sega}/ordina-coda', json={'ordine': [self.a], 'pin': '9999'}).status_code, 400)

    def test_pagina_mostra_riordina_solo_su_saldatura(self):
        self.assertIn('btn-riordina', self.c.get(f'/totem/macchina/{self.sald}').get_data(as_text=True))
        self.assertNotIn('btn-riordina', self.c.get(f'/totem/macchina/{self.sega}').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
