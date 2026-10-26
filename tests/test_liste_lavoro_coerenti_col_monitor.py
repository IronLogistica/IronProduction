"""
Regressione (segnalata da Mauri, 26/10/2026, commessa 26100077 / OP-2026-000077 PINX110, OL CUT/052):
la pagina "Ordini di lavoro da stampare" (api_liste_lavoro, riepilogo leggero) teneva APERTO sulla
Segatrice un OP che sul Monitor Live Segatrice non c'era piu', perche' evaso. Il riepilogo leggero
ignorava il credito "da valle" (pezzi gia' avanzati a PINX110-A/B/finale) e la regola del prodotto
finito: stesso calcolo del Monitor e dell'OL stampato, mai tre versioni.
"""
import unittest

from tests.test_lista_lavoro_allineata_al_monitor import (OP, TestListaLavoroAllineataAlMonitor as Base)
from blueprints.monitor.routes import _righe_macchina
from blueprints.produzione_pp.routes import pp_bp
from models import CentroCostoWood, db


class TestListeLavoroCoerentiColMonitor(Base):
    def _voce_lista(self, centro_id):
        client = self.app.test_client()
        return [v for v in client.get(f'/api/liste-lavoro/{centro_id}').get_json() if v['op_codice'] == OP]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'produzione_pp' not in cls.app.blueprints:
            cls.app.register_blueprint(pp_bp)

    def test_segatrice_evasa_non_compare_nelle_liste_da_stampare(self):
        with self.app.app_context():
            monitor = [x for s in _righe_macchina(db.session.get(CentroCostoWood, self.seg_id)).values()
                       for x in s if x['op_codice'] == OP]
            self.assertTrue(all(x['saldo'] == 0 for x in monitor), 'il Monitor la considera evasa')
            self.assertEqual(self._voce_lista(self.seg_id), [], 'prima della fix: residuo 84 ancora da stampare')

    def test_saldatura_ancora_aperta_resta_in_lista(self):
        with self.app.app_context():
            voci = self._voce_lista(self.sal_id)
            self.assertEqual(len(voci), 1)
            self.assertEqual(voci[0]['residuo_pz'], 203 * 3)       # A, B e finale: 287-84 ciascuno


if __name__ == '__main__':
    unittest.main()
