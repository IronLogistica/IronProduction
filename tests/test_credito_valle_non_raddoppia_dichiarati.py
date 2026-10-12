"""
Regressione (segnalato da Mauri, 12/10/2026 — OP-2026-000072, commessa
26100072, Ordine Lavoro Saldatura LIST/047): PINX110-A e PINX110-B
mostravano 'Prodotti 158 / Da Produrre 0' mentre il padre PINX110 mostrava
'Prodotti 129 / Da Produrre 158', e il piede pagina diceva comunque
'Residuo 416' (= 129 + 129 + 158).

Causa: il credito da valle (pezzi già avanzati a uno stadio successivo)
veniva SOMMATO ai pezzi già dichiarati su questo stadio. Ma i pezzi
dichiarati qui COMPRENDONO già quelli poi passati a valle: i 158 PINX110-A
saldati sono gli stessi 158 diventati PINX110-B, e 129 di questi sono gli
stessi diventati PINX110. Sommarli li contava due volte (158 + 158 >= 287).

Regola corretta: coperto a questo stadio = MAX(dichiarati qui,
ancora presenti qui + già passati a valle) — mai la somma dei due.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP)
from blueprints.monitor.routes import _righe_macchina
from blueprints.produzione_pp.routes import _lista_lavoro_op

OP = 'OP-2026-000072'


class TestCreditoValleNonRaddoppiaDichiarati(unittest.TestCase):
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
            for modello in (EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                            OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            sald = CentroCostoWood(nome='Saldatura')
            db.session.add(sald)
            db.session.flush()
            self.saldatura_id = sald.id

            # Catena reale: PINXTT110 -> PINX110-A -> PINX110-B -> PINX110
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-B', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-B', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=self.saldatura_id),
                CicloLavoroWood(codice='PINX110-B', sequenza=1, centro_costo_id=self.saldatura_id),
                CicloLavoroWood(codice='PINX110', sequenza=1, centro_costo_id=self.saldatura_id),
                # Inventario reale: 158 A -> tutti diventati B; 129 B -> PINX110; 29 B ancora fermi
                GiacenzaWood(codice='PINX110-A', quantita=0),
                GiacenzaWood(codice='PINX110-B', quantita=29),
                GiacenzaWood(codice='PINX110', quantita=0),
                OrdineProduzione(codice=OP, codice_articolo='PINX110', commessa='26100072',
                                  qta_pianificata=287, qta_buona=129, stato='Rilasciato', priorita=1),
            ])
            for i, (comp, pz) in enumerate([('PINX110-A', 158), ('PINX110-B', 158), (None, 129)]):
                db.session.add(EventoConsuntivoPP(
                    event_id=f'evt-{i}', op_code=OP, fase='Saldatura', componente=comp,
                    timestamp_evento=datetime.utcnow(), pezzi_buoni=pz, pezzi_scarto=0,
                    tempo_minuti=0, approvato_direzione=True))
            db.session.commit()

    def _saldi_lista(self):
        o = OrdineProduzione.query.filter_by(codice=OP).first()
        dati = _lista_lavoro_op(o, db.session.get(CentroCostoWood, self.saldatura_id), assegna_numero=False)
        return {r['codice']: (r['pezzi_fatti'], r['saldo']) for g in dati['gruppi'] for r in g['righe']}

    def test_ordine_lavoro_saldatura_caso_reale(self):
        with self.app.app_context():
            saldi = self._saldi_lista()
            self.assertEqual(saldi['PINX110-A'], (158, 129))
            self.assertEqual(saldi['PINX110-B'], (158, 129))
            self.assertEqual(saldi['PINX110'], (129, 158))

    def test_monitor_saldatura_allineato(self):
        with self.app.app_context():
            righe = _righe_macchina(db.session.get(CentroCostoWood, self.saldatura_id))
            saldi = {r['codice_lavorato']: r['saldo'] for sez in righe.values() for r in sez
                     if r['op_codice'] == OP}
            self.assertEqual(saldi['PINX110-A'], 129)
            self.assertEqual(saldi['PINX110-B'], 129)
            self.assertEqual(saldi['PINX110'], 158)


if __name__ == '__main__':
    unittest.main()
