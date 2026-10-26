"""
INCONGRUENZA (Mauri, 14/10/2026 — OP-2026-000078, T250DT, commessa 26100078):
la card Situazione dava TPD32L3940 "Mancante 40" mentre l'OL Segatrice CUT/054
dava "Da produrre 0". Causa: il credito da valle contava la giacenza LORDA del
semilavorato a valle TR2501, gia' impegnata da un'altra commessa. Ora conta solo
la giacenza disponibile per questo OP (come la card).
"""
from datetime import datetime
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP)
from blueprints.produzione_pp.routes import _lista_lavoro_op


class TestCreditoValleGiacenzaImpegnata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def _setup(self, giacenza_tr2501, con_altra_commessa):
        with self.app.app_context():
            db.session.remove()
            for m in (EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg = CentroCostoWood(nome='Segatrice')
            sal = CentroCostoWood(nome='Saldatura')
            db.session.add_all([seg, sal])
            db.session.flush()
            righe = [
                DistintaBaseWood(codice_padre='T250DT', codice_figlio='TR2501', quantita=1.0),
                DistintaBaseWood(codice_padre='TR2501', codice_figlio='TPD32L3940', quantita=1.0),
                DistintaBaseWood(codice_padre='TPD32L3940', codice_figlio='TTD32SP15L7', quantita=1.0),
                CicloLavoroWood(codice='TPD32L3940', sequenza=1, centro_costo_id=seg.id),
                CicloLavoroWood(codice='TR2501', sequenza=1, centro_costo_id=sal.id),
                GiacenzaWood(codice='TR2501', quantita=giacenza_tr2501),
                GiacenzaWood(codice='TPD32L3940', quantita=0),
                GiacenzaWood(codice='TTD32SP15L7', quantita=400),
                OrdineProduzione(codice='OP-2026-000078', codice_articolo='T250DT', commessa='26100078',
                                 qta_pianificata=40, stato='Rilasciato', priorita=5),
            ]
            if con_altra_commessa:
                # commessa rilasciata PRIMA (ordine cronologico di rilascio), che si prende i 40 TR2501 a magazzino
                righe.append(OrdineProduzione(codice='OP-2026-000070', codice_articolo='T250DT',
                                              commessa='26100070', qta_pianificata=40,
                                              stato='Rilasciato', data_rilascio=datetime(2026, 10, 1, 8, 0)))
            db.session.add_all(righe)
            db.session.commit()
            return seg.id

    def _saldo(self, seg_id):
        with self.app.app_context():
            o = OrdineProduzione.query.filter_by(codice='OP-2026-000078').one()
            dati = _lista_lavoro_op(o, db.session.get(CentroCostoWood, seg_id), assegna_numero=False)
            return {r['codice']: r['saldo'] for g in dati['gruppi'] for r in g['righe']}['TPD32L3940']

    def test_tr2501_impegnati_da_altra_commessa_non_coprono(self):
        seg = self._setup(giacenza_tr2501=40, con_altra_commessa=True)
        self.assertEqual(self._saldo(seg), 40, 'prima della fix: 0 (TR2501 gia impegnati contati come pronti)')

    def test_tr2501_liberi_coprono_ancora(self):
        """Regola 13/10 (Pierantonio) intatta: semilavorati a valle LIBERI coprono."""
        seg = self._setup(giacenza_tr2501=40, con_altra_commessa=False)
        self.assertEqual(self._saldo(seg), 0)

    def test_copertura_parziale(self):
        seg = self._setup(giacenza_tr2501=55, con_altra_commessa=True)
        self.assertEqual(self._saldo(seg), 25, '55 TR2501: 40 alla commessa 070, 15 liberi per la 078')


if __name__ == '__main__':
    unittest.main()
