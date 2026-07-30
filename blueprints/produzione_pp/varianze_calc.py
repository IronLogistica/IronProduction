"""
Formule PURE di calcolo varianza (nessun accesso a DB/Flask) — condivise da
produzione_pp/routes.py e testate direttamente in tests/test_varianze.py.
Tenerle qui, isolate, permette di verificarle senza dover avviare l'app o un
database: sono la stessa identica matematica usata nel calcolo reale.

Convenzione dei segni (SAP-style): varianza > 0 = SFAVOREVOLE (costato di più
del previsto), varianza < 0 = FAVOREVOLE (costato di meno). Tutte le funzioni
ritornano float non arrotondati: l'arrotondamento a 2 decimali è responsabilità
del chiamante (coerente con round(...) usato ovunque nel resto del modulo).
"""


def varianza_quantita_materiale(qty_standard, qty_effettiva, prezzo_standard):
    """
    Varianza di quantità/consumo materiale = (quantità effettiva − quantità
    standard) × costo standard unitario. Isola l'effetto "ho consumato più o
    meno del previsto", a prezzo standard fisso — la variazione di prezzo è
    isolata separatamente da varianza_prezzo_materiale.
    """
    return (qty_effettiva - qty_standard) * prezzo_standard


def varianza_prezzo_materiale(prezzo_standard, prezzo_effettivo, qty_effettiva):
    """
    Varianza di prezzo materiale = (prezzo effettivo − prezzo standard) ×
    quantità effettiva. Isola l'effetto "il prezzo è cambiato", sulla
    quantità REALMENTE consumata (convenzione standard SAP).
    """
    return (prezzo_effettivo - prezzo_standard) * qty_effettiva


def varianza_efficienza_tempo(ore_effettive, ore_standard, tariffa_standard_oraria):
    """
    Varianza di efficienza/impiego tempo = (ore effettive − ore standard) ×
    costo standard orario. Isola l'effetto "ci ho messo più o meno tempo del
    previsto", a tariffa standard fissa.
    """
    return (ore_effettive - ore_standard) * tariffa_standard_oraria


def varianza_tariffa(tariffa_effettiva_oraria, tariffa_standard_oraria, ore_effettive):
    """
    Varianza di tariffa (macchina o manodopera) = (costo orario effettivo −
    costo orario standard) × ore effettive. Isola l'effetto "la tariffa oraria
    è cambiata rispetto a quella congelata allo standard".
    """
    return (tariffa_effettiva_oraria - tariffa_standard_oraria) * ore_effettive


def classifica_varianza(valore):
    """'favorevole' se valore < 0, 'sfavorevole' se > 0, 'nulla' se 0 (entro arrotondamento)."""
    if abs(valore) < 0.005:
        return 'nulla'
    return 'sfavorevole' if valore > 0 else 'favorevole'
