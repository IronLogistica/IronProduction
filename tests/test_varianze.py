"""
Test delle formule di calcolo varianza (standard costing) — nessun database,
nessuna app Flask: testano solo la matematica pura in
blueprints/produzione_pp/varianze_calc.py. Eseguibili con:
    python -m unittest tests.test_varianze -v
(o con pytest, se installato: pytest tests/test_varianze.py -v)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.produzione_pp.varianze_calc import (
    varianza_quantita_materiale, varianza_prezzo_materiale,
    varianza_efficienza_tempo, varianza_tariffa, classifica_varianza,
)


class TestVarianzaMateriale(unittest.TestCase):
    def test_quantita_sfavorevole_consumato_di_piu(self):
        # Standard: 100 pz a 2€; effettivo: 112 pz → +12 pz a costo standard = +24€ (sfavorevole)
        v = varianza_quantita_materiale(qty_standard=100, qty_effettiva=112, prezzo_standard=2.0)
        self.assertAlmostEqual(v, 24.0)
        self.assertEqual(classifica_varianza(v), 'sfavorevole')

    def test_quantita_favorevole_consumato_di_meno(self):
        v = varianza_quantita_materiale(qty_standard=100, qty_effettiva=95, prezzo_standard=2.0)
        self.assertAlmostEqual(v, -10.0)
        self.assertEqual(classifica_varianza(v), 'favorevole')

    def test_quantita_nulla_quando_coincide_con_lo_standard(self):
        v = varianza_quantita_materiale(qty_standard=50, qty_effettiva=50, prezzo_standard=3.5)
        self.assertAlmostEqual(v, 0.0)
        self.assertEqual(classifica_varianza(v), 'nulla')

    def test_prezzo_sfavorevole_pagato_di_piu(self):
        # Standard 2€, pagato 2.20€, su 112 pz effettivi → +0.20 × 112 = +22.4€ (sfavorevole)
        v = varianza_prezzo_materiale(prezzo_standard=2.0, prezzo_effettivo=2.20, qty_effettiva=112)
        self.assertAlmostEqual(v, 22.4)
        self.assertEqual(classifica_varianza(v), 'sfavorevole')

    def test_prezzo_favorevole_pagato_di_meno(self):
        v = varianza_prezzo_materiale(prezzo_standard=2.0, prezzo_effettivo=1.80, qty_effettiva=100)
        self.assertAlmostEqual(v, -20.0)
        self.assertEqual(classifica_varianza(v), 'favorevole')


class TestVarianzaTempoETariffa(unittest.TestCase):
    def test_efficienza_sfavorevole_tempo_maggiore(self):
        # Standard 10 ore, reali 14.5 ore, tariffa standard 20€/h → +4.5×20 = +90€ (sfavorevole)
        v = varianza_efficienza_tempo(ore_effettive=14.5, ore_standard=10.0, tariffa_standard_oraria=20.0)
        self.assertAlmostEqual(v, 90.0)
        self.assertEqual(classifica_varianza(v), 'sfavorevole')

    def test_efficienza_favorevole_tempo_minore(self):
        v = varianza_efficienza_tempo(ore_effettive=8.0, ore_standard=10.0, tariffa_standard_oraria=20.0)
        self.assertAlmostEqual(v, -40.0)
        self.assertEqual(classifica_varianza(v), 'favorevole')

    def test_tariffa_sfavorevole_costo_orario_aumentato(self):
        # Tariffa standard 20€/h, reale 22€/h, su 14.5 ore reali → +2×14.5 = +29€
        v = varianza_tariffa(tariffa_effettiva_oraria=22.0, tariffa_standard_oraria=20.0, ore_effettive=14.5)
        self.assertAlmostEqual(v, 29.0)
        self.assertEqual(classifica_varianza(v), 'sfavorevole')

    def test_tariffa_nulla_se_invariata(self):
        v = varianza_tariffa(tariffa_effettiva_oraria=20.0, tariffa_standard_oraria=20.0, ore_effettive=14.5)
        self.assertAlmostEqual(v, 0.0)
        self.assertEqual(classifica_varianza(v), 'nulla')


class TestEsempioNumericoCompleto(unittest.TestCase):
    """
    Riproduce l'esempio numerico di un'intera commessa (100 pz), usato anche
    nella risposta descrittiva finale: materiale standard 2€/pz×2pz=4€/pz,
    100 pz buoni, consumati 212 pz di materiale invece di 200, a 2.10€ invece
    di 2.00€ — più 4.5 ore di lavorazione oltre lo standard a tariffa invariata.
    """
    def test_scenario_commessa_100_pezzi(self):
        qty_standard = 200      # 2 pz materiale × 100 pz buoni
        qty_effettiva = 212
        prezzo_standard = 2.00
        prezzo_effettivo = 2.10

        var_quantita = varianza_quantita_materiale(qty_standard, qty_effettiva, prezzo_standard)
        var_prezzo = varianza_prezzo_materiale(prezzo_standard, prezzo_effettivo, qty_effettiva)
        self.assertAlmostEqual(var_quantita, 24.0)     # (212-200)×2.00
        self.assertAlmostEqual(var_prezzo, 21.2)        # (2.10-2.00)×212
        self.assertAlmostEqual(var_quantita + var_prezzo, 45.2)

        ore_standard, ore_effettive, tariffa_standard = 10.0, 14.5, 20.0
        var_efficienza = varianza_efficienza_tempo(ore_effettive, ore_standard, tariffa_standard)
        self.assertAlmostEqual(var_efficienza, 90.0)   # (14.5-10)×20

        varianza_totale_esempio = var_quantita + var_prezzo + var_efficienza
        self.assertAlmostEqual(varianza_totale_esempio, 135.2)
        self.assertEqual(classifica_varianza(varianza_totale_esempio), 'sfavorevole')


if __name__ == '__main__':
    unittest.main()
