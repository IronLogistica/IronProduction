"""Mauri 08/10/2026: ogni registrazione da MasterWork resta 'morta' (in attesa di Approva/Elimina): finche' non e'
approvata NON deve modificare ne' magazzino, ne' ordini, ne' WIP. Eliminata, sparisce senza lasciare tracce."""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, GiacenzaWood, WipFaseWood,
                    OrdineProduzione, EventoConsuntivoPP, MovimentoGiacenzaWood)
from blueprints.produzione_pp.routes import pp_bp, PIN_DIREZIONE, _registra_evento_con_ripartizione


class TestMasterworkInAttesa(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(pp_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            c, t = CentroCostoWood(nome='Curvatubi'), CentroCostoWood(nome='Trapani')
            db.session.add_all([c, t])
            db.session.flush()
            db.session.add_all([
                CicloLavoroWood(codice='Z01', sequenza=1, centro_costo_id=c.id),
                CicloLavoroWood(codice='Z01', sequenza=2, centro_costo_id=t.id),
                DistintaBaseWood(codice_padre='ZT', codice_figlio='Z01', quantita=1.0),
                DistintaBaseWood(codice_padre='Z01', codice_figlio='TUBO', quantita=1.0),
                GiacenzaWood(codice='TUBO', quantita=1000),
                OrdineProduzione(codice='OP-1', codice_articolo='ZT', qta_pianificata=100, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()
            self.c_id = c.id
            o = OrdineProduzione.query.filter_by(codice='OP-1').one()
            _registra_evento_con_ripartizione(o, 'Curvatubi', datetime.utcnow(), 30, 0, 45, 'MW1', componente='Z01',
                                              operatore='SERGIY')
            db.session.commit()
        self.c = self.app.test_client()

    def _stato(self):
        with self.app.app_context():
            o = OrdineProduzione.query.filter_by(codice='OP-1').one()
            giac = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
            wip = {(w.codice, w.centro_costo_id): w.quantita for w in WipFaseWood.query.all()}
            return (o.qta_buona, o.qta_scarto, o.tempo_consuntivo_minuti, giac, wip, MovimentoGiacenzaWood.query.count())

    def test_in_attesa_non_muove_nulla_e_approvata_si(self):
        with self.app.app_context():
            e = EventoConsuntivoPP.query.one()
            self.assertFalse(e.approvato_direzione)
            eid = e.id
        prima = self._stato()
        self.assertEqual(prima[3], {'TUBO': 1000}); self.assertEqual(prima[4], {}); self.assertEqual(prima[5], 0)
        # compare nell'elenco da gestire, che e' l'unico posto dove si decide
        lista = self.c.get('/api/dichiarazione-produzione/masterwork-pendenti').get_json()['eventi']
        self.assertEqual([x['id'] for x in lista], [eid])
        r = self.c.post(f'/api/dichiarazione-produzione/eventi/{eid}/approva', json={'pin': PIN_DIREZIONE})
        self.assertTrue(r.get_json()['ok'])
        dopo = self._stato()
        self.assertNotEqual(dopo, prima, "approvata: ora si muovono magazzino/WIP")
        self.assertEqual(self.c.get('/api/dichiarazione-produzione/masterwork-pendenti').get_json()['eventi'], [])

    def test_eliminata_non_lascia_tracce(self):
        with self.app.app_context():
            eid = EventoConsuntivoPP.query.one().id
        prima = self._stato()
        r = self.c.post(f'/api/dichiarazione-produzione/eventi/{eid}/annulla', json={'pin': PIN_DIREZIONE})
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(self._stato(), prima)
        with self.app.app_context():
            self.assertEqual(EventoConsuntivoPP.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
