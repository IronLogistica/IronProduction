"""MASTERPLAN · AMPS — regole del motore (numeri a mano, nessun database)."""
import unittest
from datetime import date

from blueprints.masterplan import motore as m


class TestMotore(unittest.TestCase):
    def test_spalma_somma_sempre_il_totale(self):
        for q in (0, 1, 7, 100, 101, 999):
            r = m.spalma(q, {'A': 50, 'B': 30, 'C': 20})
            self.assertEqual(sum(r.values()), q)
        self.assertEqual(m.spalma(100, {'A': 50, 'B': 30, 'C': 20}), {'A': 50, 'B': 30, 'C': 20})

    def test_spalma_resto_al_peso_piu_alto(self):
        self.assertEqual(m.spalma(1, {'A': 1, 'B': 1, 'C': 2}), {'A': 0, 'B': 0, 'C': 1})

    def test_mix_deve_fare_100(self):
        with self.assertRaises(m.MixNonValido):
            m.controlla_mix({'A': 60, 'B': 30})
        m.controlla_mix({'A': 60, 'B': 40})

    def test_famiglia_articolo_prevale(self):
        ris, av = m.distribuisci_famiglia(100, {'A': 50, 'B': 30, 'C': 20}, {'A': 70})
        self.assertEqual(ris['A'], {'qta': 70, 'origine': 'ARTICOLO', 'pct': 50})
        self.assertEqual(ris['B']['qta'] + ris['C']['qta'], 30)
        self.assertEqual((ris['B']['qta'], ris['C']['qta']), (18, 12))
        self.assertEqual(av, [])

    def test_famiglia_articoli_oltre_il_totale_avvisa(self):
        _r, av = m.distribuisci_famiglia(50, {'A': 50, 'B': 50}, {'A': 80})
        self.assertTrue(av)

    def test_mensile_su_settimane_conserva_il_totale(self):
        r = m.mensile_su_settimane(date(2026, 11, 1), 1000)
        self.assertEqual(sum(r.values()), 1000)
        self.assertTrue(all(d.weekday() == 0 for d in r))

    def test_consumo_domanda_e_max_non_somma(self):
        sett = m.elenco_settimane(date(2026, 11, 2), 6)
        righe = m.consuma_previsione(sett, {sett[3]: 100}, {sett[3]: 60}, finestra=2, congelate=2)
        m.chiudi_domanda(righe, 2)
        self.assertEqual(righe[3]['domanda'], 100)          # 60 ordini + 40 residuo
        self.assertEqual(righe[3]['residuo'], 40)

    def test_zona_congelata_ignora_previsione(self):
        sett = m.elenco_settimane(date(2026, 11, 2), 6)
        righe = m.consuma_previsione(sett, {sett[0]: 50, sett[4]: 50}, {sett[0]: 10}, finestra=0, congelate=2)
        m.chiudi_domanda(righe, 2)
        self.assertEqual(righe[0]['domanda'], 10)
        self.assertEqual(righe[4]['domanda'], 50)

    def test_ordine_consuma_settimana_vicina(self):
        sett = m.elenco_settimane(date(2026, 11, 2), 8)
        righe = m.consuma_previsione(sett, {sett[4]: 100}, {sett[5]: 100}, finestra=2, congelate=0)
        m.chiudi_domanda(righe, 0)
        self.assertEqual(sum(r['domanda'] for r in righe), 100)


if __name__ == '__main__':
    unittest.main()


class TestMrp(unittest.TestCase):
    def test_netta_ordine_e_ritardo(self):
        from blueprints.masterplan.mrp import netta
        lordo = [0, 0, 50, 0, 30, 0]
        righe, ordini = netta(lordo, giacenza=20, arrivi=[0, 0, 0, 0, 0, 0], lead_sett=2)
        self.assertEqual([o['quantita'] for o in ordini], [30, 30])
        _r, o2 = netta(lordo, giacenza=20, arrivi=None, lead_sett=2, lotto_minimo=100)
        self.assertEqual([o['quantita'] for o in o2], [100])      # il lotto copre anche il secondo fabbisogno
        self.assertEqual((ordini[0]['arrivo'], ordini[0]['ordina_in'], ordini[0]['in_ritardo']), (2, 0, False))
        self.assertEqual((ordini[1]['arrivo'], ordini[1]['ordina_in']), (4, 2))
        righe, ordini = netta([40, 0, 0], giacenza=0, arrivi=None, lead_sett=2)
        self.assertTrue(ordini[0]['in_ritardo'])

    def test_netta_arrivi_coprono(self):
        from blueprints.masterplan.mrp import netta
        _r, ordini = netta([0, 40, 0], giacenza=10, arrivi=[0, 30, 0], lead_sett=1)
        self.assertEqual(ordini, [])
