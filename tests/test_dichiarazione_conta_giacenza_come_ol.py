"""
Regressione (segnalato da Mauri, 12/10/2026 — commessa 26090062,
OP-2026-000062, T250): il TR2005 (tubolare tagliato) era gia' a magazzino,
l'Ordine di Lavoro Segatrice CUT/043 dava 'Da produrre 0', ma la
Dichiarazione di Produzione (totem e pagina amministratore) mostrava ancora
'Saldo 240' da dichiarare. La Dichiarazione era l'unica schermata che
ignorava la giacenza residua per OP usata da OL, Monitor LIVE e Cruscotto.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP)
from blueprints.produzione_pp.routes import pp_bp, _lista_lavoro_op

OP = 'OP-2026-000062'


class TestDichiarazioneContaGiacenzaComeOL(unittest.TestCase):
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
            for m in (EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg = CentroCostoWood(nome='Segatrice')
            db.session.add(seg)
            db.session.flush()
            self.seg_id = seg.id
            db.session.add_all([
                DistintaBaseWood(codice_padre='T250', codice_figlio='TR2005', quantita=2.0),
                DistintaBaseWood(codice_padre='TR2005', codice_figlio='T6030SP3', quantita=0.03),
                CicloLavoroWood(codice='TR2005', sequenza=1, centro_costo_id=self.seg_id),
                OrdineProduzione(codice=OP, codice_articolo='T250', commessa='26090062',
                                 qta_pianificata=120, qta_buona=0, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def _set_giacenza(self, qta):
        db.session.add(GiacenzaWood(codice='TR2005', quantita=qta))
        db.session.commit()

    def _saldo_dichiarazione(self):
        dati = self.app.test_client().get(f'/api/dichiarazione-produzione/{self.seg_id}/op-aperti').get_json()
        for gruppo in dati or []:
            for c in gruppo.get('componenti', []):
                if c['codice_lavorato'] == 'TR2005':
                    return c['saldo']
        return 0  # riga assente = gia' completata, non dichiarabile

    def _saldo_ol(self):
        o = OrdineProduzione.query.filter_by(codice=OP).one()
        dati = _lista_lavoro_op(o, db.session.get(CentroCostoWood, self.seg_id), assegna_numero=False)
        return next(r['saldo'] for g in dati['gruppi'] for r in g['righe'] if r['codice'] == 'TR2005')

    def test_tr2005_gia_a_magazzino_chiude_anche_la_dichiarazione(self):
        with self.app.app_context():
            self._set_giacenza(240)
            self.assertEqual(self._saldo_ol(), 0)
            self.assertEqual(self._saldo_dichiarazione(), 0, 'prima della fix: Saldo 240')

    def test_giacenza_parziale_lascia_solo_il_vero_residuo(self):
        with self.app.app_context():
            self._set_giacenza(100)
            self.assertEqual(self._saldo_ol(), 140)
            self.assertEqual(self._saldo_dichiarazione(), 140)

    def test_senza_giacenza_resta_tutto_da_tagliare(self):
        with self.app.app_context():
            self.assertEqual(self._saldo_dichiarazione(), 240)


if __name__ == '__main__':
    unittest.main()
