"""
Regressione (26/10/2026, OP-2026-000077 commessa 26100077 / OP-2026-000089 commessa 26100089):
due OP di PINX110 si contendono la stessa scorta di semilavorati (59 PINX110-A). Monitor Live la assegna
per (priorita', id): all'OP 77 che li ha prodotti. Avanzamento Commesse la assegnava nell'ordine
manuale di Angelo (89 prima di 77: 89 al 98%, 77 al 90%) e l'Ordine di Lavoro stampato toglieva gli impegni
di TUTTI gli altri OP, anche dei successivi (Segatrice 'Da produrre 25'). Ora tutti e tre allocano uguale.
"""
import unittest
from datetime import datetime

from flask import Flask

from blueprints.monitor.routes import _righe_macchina
from blueprints.produzione_pp.avanzamento import calcola_avanzamento_commesse
from blueprints.produzione_pp.routes import _lista_lavoro_op
from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, EventoConsuntivoPP, GiacenzaWood,
                    OrdineProduzione, SequenzaAvanzamentoKPI)

OP77, OP89 = 'OP-2026-000077', 'OP-2026-000089'


def _ev(n, op, comp, buoni, fase):
    return EventoConsuntivoPP(event_id=f'e{n}', op_code=op, fase=fase, componente=comp,
                              timestamp_evento=datetime.utcnow(), pezzi_buoni=buoni, pezzi_scarto=0,
                              tempo_minuti=0, approvato_direzione=True)


class TestScortaCondivisa(unittest.TestCase):
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
            for m in (SequenzaAvanzamentoKPI, EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg, sat, sal = CentroCostoWood(nome='Segatrice'), CentroCostoWood(nome='Satinatrice'), CentroCostoWood(nome='Saldatura')
            db.session.add_all([seg, sat, sal])
            db.session.flush()
            self.seg_id, self.sat_id = seg.id, sat.id
            op77 = OrdineProduzione(codice=OP77, codice_articolo='PINX110', qta_pianificata=252, qta_buona=193,
                                    stato='In esecuzione', priorita=1)
            op89 = OrdineProduzione(codice=OP89, codice_articolo='PINX110', qta_pianificata=60, qta_buona=0,
                                    stato='Rilasciato', priorita=1)
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-B', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=seg.id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=sat.id),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=sal.id),
                CicloLavoroWood(codice='PINX110-B', sequenza=1, centro_costo_id=sal.id),
                CicloLavoroWood(codice='PINX110', sequenza=1, centro_costo_id=sal.id),
                GiacenzaWood(codice='PINXTT110', quantita=0), GiacenzaWood(codice='PINX110-A', quantita=59),
                op77, op89,
                _ev(1, OP77, 'PINXTT110', 25, 'Segatrice'), _ev(2, OP77, 'PINXTT110', 25, 'Satinatrice'),
                _ev(3, OP77, 'PINX110-A', 227, 'Saldatura'), _ev(4, OP77, 'PINX110-B', 193, 'Saldatura'),
                _ev(5, OP77, None, 193, 'Saldatura'),
            ])
            db.session.flush()
            # Angelo ha trascinato la 89 PRIMA della 77 nella tabella Avanzamento Commesse
            db.session.add_all([SequenzaAvanzamentoKPI(ordine_produzione_id=op89.id, posizione=1),
                                SequenzaAvanzamentoKPI(ordine_produzione_id=op77.id, posizione=2)])
            db.session.commit()

    def _ol_saldo(self, op_code, centro_id):
        o = OrdineProduzione.query.filter_by(codice=op_code).one()
        return _lista_lavoro_op(o, db.session.get(CentroCostoWood, centro_id), assegna_numero=False)['gruppi'][0]['righe'][0]['saldo']

    def _mon_saldo(self, op_code, centro_id):
        return next(x['saldo'] for s in _righe_macchina(db.session.get(CentroCostoWood, centro_id)).values()
                    for x in s if x['op_codice'] == op_code)

    def test_op77_segatrice_e_satinatrice_evase_ovunque(self):
        with self.app.app_context():
            for cid in (self.seg_id, self.sat_id):
                self.assertEqual(self._mon_saldo(OP77, cid), 0)
                self.assertEqual(self._ol_saldo(OP77, cid), 0, 'prima della fix: 25')
            kpi = {r['op_codice']: r['zone'] for r in calcola_avanzamento_commesse()[0]}
            self.assertEqual((kpi[OP77]['taglio'], kpi[OP77]['sgola_sati']), (100, 100), 'prima della fix: 90 / 90')

    def test_op89_non_riceve_la_scorta_dell_op77(self):
        with self.app.app_context():
            self.assertEqual(self._mon_saldo(OP89, self.seg_id), 60)
            self.assertEqual(self._ol_saldo(OP89, self.seg_id), 60)
            kpi = {r['op_codice']: r['zone'] for r in calcola_avanzamento_commesse()[0]}
            self.assertEqual(kpi[OP89]['taglio'], 0, 'prima della fix: 98')


if __name__ == '__main__':
    unittest.main()
