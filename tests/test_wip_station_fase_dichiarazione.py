"""
Regressione "WIP Station" sulla DICHIARAZIONE DI PRODUZIONE (segnalato da
Mauri, 05/10/2026, STESSO caso reale PINX110/PINXTT110 già coperto dal PDF,
dal Monitor Live e dal Cruscotto KPI): /api/dichiarazione-produzione/<cid>/
op-aperti (usata sia da "Dichiarazione Produzione" sia da "Totem —
Alessandro") mostrava Saldo 287 per la Satinatrice, quando il Monitor Live
(già corretto) mostra correttamente 96 — rischio concreto che l'operaio
dichiari l'intera quantità dell'ordine invece del vero residuo da satinare,
mandandolo fuori strada.
"""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood, WipFaseWood
from blueprints.produzione_pp.routes import pp_bp


class TestWipStationFaseDichiarazione(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (WipFaseWood, GiacenzaWood, CicloLavoroWood, OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            self.taglio = CentroCostoWood(nome='Segatrice')
            self.satinatura = CentroCostoWood(nome='Satinatrice')
            db.session.add_all([self.taglio, self.satinatura])
            db.session.flush()
            self.taglio_id, self.satinatura_id = self.taglio.id, self.satinatura.id

            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.taglio_id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=self.satinatura_id),
                GiacenzaWood(codice='PINXTT110', quantita=287),
                OrdineProduzione(codice='26100072', codice_articolo='PINXTT110',
                                  qta_pianificata=287, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def _componente_pinxtt110(self, payload):
        gruppo = next(g for g in payload if g['codice'] == '26100072')
        return next(c for c in gruppo['componenti'] if c['codice_lavorato'] == 'PINXTT110')

    def test_dichiarazione_satinatrice_mostra_saldo_96_non_287(self):
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
        r = self.client.get(f'/api/dichiarazione-produzione/{self.satinatura_id}/op-aperti')
        self.assertEqual(r.status_code, 200)
        comp = self._componente_pinxtt110(r.get_json())
        self.assertEqual(comp['qta_necessaria'], 287)
        self.assertEqual(comp['fatti'], 0, "nessuna dichiarazione MasterWork — 'fatti' resta a 0, invariato")
        self.assertEqual(comp['saldo'], 96,
                          "prima della fix era 287 (l'intero ordine), rischiando di far dichiarare "
                          "all'operaio 287 pezzi invece dei 96 davvero da satinare")

    def test_dichiarazione_segatrice_gia_completa_non_compare(self):
        """Il Taglio è coperto per intero (96+191=287): non deve comparire
        come riga dichiarabile (saldo<=0 esclude la riga)."""
        with self.app.app_context():
            db.session.add_all([
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.taglio_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatura_id, quantita=191),
            ])
            db.session.commit()
        r = self.client.get(f'/api/dichiarazione-produzione/{self.taglio_id}/op-aperti')
        payload = r.get_json()
        self.assertEqual(payload, [], "la Segatrice è già completa: nessuna riga dichiarabile")

    def test_senza_wip_il_saldo_resta_287_come_prima(self):
        """Nessuna regressione per i codici senza righe WIP Station."""
        r = self.client.get(f'/api/dichiarazione-produzione/{self.satinatura_id}/op-aperti')
        comp = self._componente_pinxtt110(r.get_json())
        self.assertEqual(comp['saldo'], 287)


if __name__ == '__main__':
    unittest.main()
