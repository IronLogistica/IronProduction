"""
Caso reale (Pierantonio, 13/10/2026 — OP-2026-000077, commessa 26100077, 237 PINX110):
trovati 25 PINX110-A gia' saldati in capannone e caricati a magazzino SENZA
dichiarazione. Magazzino: 212 PINXTT110 (fermi dopo Satinatrice) + 25 PINX110-A = 237.
Segatrice e Satinatrice chiedevano ancora 25 PINXTT110 da lavorare.
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP, WipFaseWood)
from blueprints.monitor.routes import _righe_macchina
from blueprints.produzione_pp.routes import pp_bp, _lista_lavoro_op

OP = 'OP-2026-000077'


class TestSemilavoratiAValleInGiacenza(unittest.TestCase):
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
            for m in (WipFaseWood, EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg, sat, sal = (CentroCostoWood(nome='Segatrice'), CentroCostoWood(nome='Satinatrice'),
                             CentroCostoWood(nome='Saldatura'))
            db.session.add_all([seg, sat, sal])
            db.session.flush()
            self.ids = {'seg': seg.id, 'sat': sat.id, 'sal': sal.id}
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-B', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-B', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=seg.id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=sat.id),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=sal.id),
                GiacenzaWood(codice='PINX110', quantita=44),
                GiacenzaWood(codice='PINX110-A', quantita=25),
                GiacenzaWood(codice='PINXTT110', quantita=212),
                WipFaseWood(codice='PINXTT110', centro_costo_id=sat.id, quantita=212),
                OrdineProduzione(codice=OP, codice_articolo='PINX110', commessa='26100077',
                                  qta_pianificata=237, qta_buona=0, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def _saldo_ol(self, chiave, codice):
        o = OrdineProduzione.query.filter_by(codice=OP).one()
        d = _lista_lavoro_op(o, db.session.get(CentroCostoWood, self.ids[chiave]), assegna_numero=False)
        return next(r['saldo'] for g in d['gruppi'] for r in g['righe'] if r['codice'] == codice)

    def _saldo_dich(self, chiave, codice):
        dati = self.app.test_client().get(f"/api/dichiarazione-produzione/{self.ids[chiave]}/op-aperti").get_json()
        for g in dati or []:
            for c in g.get('componenti', []):
                if c['codice_lavorato'] == codice:
                    return c['saldo']
        return 0

    def test_tubi_coperti_da_212_piu_25_A(self):
        with self.app.app_context():
            for chiave in ('seg', 'sat'):
                self.assertEqual(self._saldo_ol(chiave, 'PINXTT110'), 0, chiave)
                self.assertEqual(self._saldo_dich(chiave, 'PINXTT110'), 0, chiave)
                riga = next(r for s in _righe_macchina(db.session.get(CentroCostoWood, self.ids[chiave])).values()
                            for r in s if r['op_codice'] == OP and r['codice_lavorato'] == 'PINXTT110')
                self.assertEqual(riga['saldo'], 0, chiave)

    def test_saldatura_A_resta_212(self):
        with self.app.app_context():
            self.assertEqual(self._saldo_ol('sal', 'PINX110-A'), 212)


if __name__ == '__main__':
    unittest.main()
