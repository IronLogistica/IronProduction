"""MASTERPLAN · AMPS — proposte costruite sullo storico degli ordini (mix per famiglia, previsione per codice). Solo calcolo: l'utente conferma."""
from blueprints.masterplan import dati
from blueprints.masterplan.motore import aggiungi_mesi, primo_del_mese, spalma


def proponi_famiglie(oggi=None):
    """Una famiglia per ogni categoria dei prodotti, con i soli prodotti finiti (radici della distinta) e un mix dallo storico."""
    mappa = dati.mappa_distinta()
    finiti = dati.prodotti_finiti(mappa)
    categorie = dati.categorie_prodotti()
    storico, _m = dati.serie_mensile_op(oggi)
    gruppi = {}
    for c in finiti:
        if c in categorie:
            gruppi.setdefault(categorie[c], []).append(c)
    out = []
    for cat, codici in sorted(gruppi.items()):
        pesi = {c: sum(storico.get(c, {}).values()) for c in codici}
        if sum(pesi.values()) <= 0:
            pesi = {c: 1 for c in codici}                     # nessuno storico: quote uguali
        mix = spalma(100, pesi)
        out.append({'codice': cat.upper()[:60], 'descrizione': cat,
                    'codici': [{'codice': c, 'mix_pct': float(mix[c])} for c in sorted(codici)]})
    return out


def proponi_previsioni(codice, oggi=None, mesi_futuri=6):
    """Media mensile dello storico per il codice, ripetuta sui prossimi mesi (come BOZZA da approvare)."""
    from datetime import date
    oggi = oggi or date.today()
    storico, disponibili = dati.serie_mensile_op(oggi)
    serie = storico.get(codice.upper(), {})
    if not disponibili or not serie:
        return []
    media = int(round(sum(serie.values()) / disponibili))
    if media <= 0:
        return []
    inizio = aggiungi_mesi(primo_del_mese(oggi), 1)
    return [{'mese': aggiungi_mesi(inizio, i), 'quantita': media} for i in range(mesi_futuri)]
