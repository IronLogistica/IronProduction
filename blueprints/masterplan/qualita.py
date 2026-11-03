"""
MASTERPLAN · AMPS — Fase 0: controllo qualità dei dati (SOLA LETTURA).
Un MRP con dati sporchi produce acquisti sbagliati: questo report elenca cosa manca e dice quanti prodotti
finiti sono «pronti per l'MRP». Non modifica nulla.
"""
from blueprints.masterplan import dati
from blueprints.masterplan.motore import aggiungi_mesi, primo_del_mese
from models import (ArticoloApprovvigionamento, CicloLavoroWood, OrdineProduzione, Prodotto,
                    RigaOrdineAcquistoWood)

PROFONDITA_MAX = 12


def _foglie_e_interni(mappa, radice):
    """Esplode la distinta attiva di 'radice': ({foglie}, {codici con distinta}) — in MAIUSCOLO, con protezione dai cicli."""
    foglie, interni = set(), set()

    def visita(codice, visitati, prof):
        if prof > PROFONDITA_MAX or codice.upper() in visitati:
            return
        righe = dati._righe_attive(mappa, codice)
        if not righe:
            foglie.add(codice.upper())
            return
        interni.add(codice.upper())
        for r in righe:
            visita(r.codice_figlio, visitati | {codice.upper()}, prof + 1)

    visita(radice, set(), 0)
    foglie.discard(radice.upper())
    return foglie, interni


def controlla(oggi=None):
    from blueprints.acquisti_wood.routes import TIPI_DA_ACQUISTARE
    mappa = dati.mappa_distinta()
    finiti = dati.prodotti_finiti(mappa)
    articoli = {a.codice.upper(): a for a in ArticoloApprovvigionamento.query.all() if a.codice}
    cicli = {}
    for c in CicloLavoroWood.query.all():
        cicli.setdefault(c.codice.upper(), []).append(c)

    problemi = []                      # {tipo, codice, dettaglio, finito}

    def segnala(tipo, codice, dettaglio, finito=''):
        problemi.append({'tipo': tipo, 'codice': codice, 'dettaglio': dettaglio, 'finito': finito})

    bloccati = set()                   # prodotti finiti con almeno un problema che ferma l'MRP
    tutti_interni, tutte_foglie = {}, {}
    for up, nome in sorted(finiti.items()):
        foglie, interni = _foglie_e_interni(mappa, nome)
        for c in interni:
            tutti_interni.setdefault(c, set()).add(nome)
        for c in foglie:
            tutte_foglie.setdefault(c, set()).add(nome)

    # 1) distinta incompleta: foglie non classificate (non si sa se comprarle o produrle)
    for c, padri in sorted(tutte_foglie.items()):
        a = articoli.get(c)
        if a is None or a.tipo_approvvigionamento == 'DA_CLASSIFICARE':
            segnala('DA_CLASSIFICARE', c, 'foglia della distinta non classificata (si compra o si produce?)', ', '.join(sorted(padri)[:3]))
            bloccati |= {p.upper() for p in padri}
    for c, a in sorted(articoli.items()):
        if a.tipo_approvvigionamento == 'DA_CLASSIFICARE' and c not in tutte_foglie and (c in tutti_interni or c in finiti):
            segnala('DA_CLASSIFICARE', c, 'codice con distinta ancora segnato «da classificare»')

    # 2) codici prodotti internamente senza ciclo di lavoro o con produttività = 0
    prodotti_internamente = set(tutti_interni) | set(finiti)
    for c in sorted(prodotti_internamente):
        fasi = cicli.get(c, [])
        padri = tutti_interni.get(c, set()) | ({finiti[c]} if c in finiti else set())
        if not fasi:
            segnala('SENZA_CICLO', c, 'si produce ma non ha un ciclo di lavoro', ', '.join(sorted(padri)[:3]))
            bloccati |= {p.upper() for p in padri}
        elif any((f.produttivita_oraria or 0) <= 0 for f in fasi):
            segnala('PRODUTTIVITA_ZERO', c, 'ciclo con produttività oraria = 0 in almeno un reparto', ', '.join(sorted(padri)[:3]))
            bloccati |= {p.upper() for p in padri}

    # 3) codici da acquistare senza lead time fornitore
    da_acquistare = {c for c, a in articoli.items() if a.tipo_approvvigionamento in TIPI_DA_ACQUISTARE}
    for c in sorted(da_acquistare):
        if not articoli[c].lead_time_fornitura_giorni:
            segnala('SENZA_LEAD_TIME', c, 'codice da acquistare senza lead time del fornitore (AMPS userà il valore prudente)',
                    ', '.join(sorted(tutte_foglie.get(c, set()))[:3]))

    # 4) unità di misura: righe d'ordine fornitore diverse da quella del codice, o codice in distinta senza U.M.
    um_ordini = {}
    for r in RigaOrdineAcquistoWood.query.all():
        if r.codice and (r.unita_misura or '').strip():
            um_ordini.setdefault(r.codice.upper(), set()).add(r.unita_misura.strip().upper())
    for c, a in sorted(articoli.items()):
        um = (a.unita_misura or '').strip().upper()
        if um and um_ordini.get(c) and um_ordini[c] - {um}:
            segnala('UM_INCOERENTE', c, f"U.M. del codice «{um}» ma ordini fornitore in «{', '.join(sorted(um_ordini[c] - {um}))}»")
        elif not um and c in tutte_foglie:
            segnala('UM_MANCANTE', c, 'foglia della distinta senza unità di misura')

    # copertura dei dati che servono alle fasi successive
    storico, mesi_storico = dati.serie_mensile_op(oggi)
    prodotti = Prodotto.query.all()
    con_categoria = sum(1 for p in prodotti if (p.categoria or '').strip())
    lead_ok = sum(1 for c in da_acquistare if articoli[c].lead_time_fornitura_giorni)
    n_op = OrdineProduzione.query.count()

    conteggi = {}
    for p in problemi:
        conteggi[p['tipo']] = conteggi.get(p['tipo'], 0) + 1
    pronti = len(finiti) - len(bloccati)
    return {
        'sintesi': {
            'prodotti_finiti': len(finiti), 'pronti': pronti, 'bloccati': len(bloccati),
            'percentuale_pronti': round(100.0 * pronti / len(finiti), 1) if finiti else 0.0,
            'problemi_per_tipo': conteggi,
        },
        'copertura': {
            'prodotti_totali': len(prodotti), 'prodotti_con_categoria': con_categoria,
            'da_acquistare': len(da_acquistare), 'con_lead_time': lead_ok,
            'ordini_totali': n_op, 'mesi_storico': mesi_storico,
        },
        'finiti_bloccati': sorted(finiti[u] for u in bloccati if u in finiti),
        'problemi': problemi,
    }
