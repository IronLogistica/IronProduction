"""
MASTERPLAN · AMPS — LETTURA dei dati esistenti di IronProduction (distinte, ordini, categorie).
Solo query di lettura: nessuna funzione qui dentro scrive nel database. Tutti i codici sono confrontati in MAIUSCOLO.
"""
from datetime import date, datetime

from blueprints.masterplan.motore import aggiungi_mesi, lunedi, primo_del_mese
from models import DescrizioneCodiceWood, DistintaBaseWood, OrdineProduzione, Prodotto

STATI_OP_APERTI = ('Creato', 'Rilasciato', 'In esecuzione')


def _righe_attive(mappa, padre):
    from blueprints.magazzino.routes import _righe_bom_attive_wood
    return _righe_bom_attive_wood(padre, mappa=mappa)


def mappa_distinta():
    """{PADRE: [righe distinta]} — come il resto del programma, una sola query per tutta la distinta."""
    from blueprints.magazzino.routes import _carica_mappa_distinta_base_wood
    return _carica_mappa_distinta_base_wood()


def prodotti_finiti(mappa=None):
    """Codici padre che non sono figlio di nessun altro: i prodotti finiti (le radici della distinta). {CODICE: codice come scritto}."""
    mappa = mappa if mappa is not None else mappa_distinta()
    figli = {r.codice_figlio.upper() for righe in mappa.values() for r in righe}
    return {p.upper(): p for p in mappa if p.upper() not in figli}


def categorie_prodotti():
    """{CODICE: categoria} dai prodotti con categoria compilata (suggerimento iniziale per le famiglie)."""
    return {p.codice.upper(): p.categoria.strip() for p in Prodotto.query.all() if (p.categoria or '').strip()}


def descrizioni(codici):
    """{CODICE: descrizione} per i codici dati (da descrizione_codice_wood, o dalla copia dell'OP se manca)."""
    voluti = {c.upper() for c in codici}
    out = {d.codice.upper(): d.descrizione for d in DescrizioneCodiceWood.query.all() if d.codice.upper() in voluti and d.descrizione}
    mancanti = voluti - set(out)
    if mancanti:
        for o in OrdineProduzione.query.filter(OrdineProduzione.descrizione != '').all():
            c = (o.codice_articolo or '').upper()
            if c in mancanti and c not in out:
                out[c] = o.descrizione
    return out


def serie_mensile_op(oggi=None, mesi=12):
    """
    Storico degli ordini per codice: pezzi pianificati per mese negli ultimi 'mesi' mesi COMPLETI
    (esclusi gli OP a scorta, che non sono ordini di clienti).
    Ritorna ({CODICE: {primo del mese: pezzi}}, mesi_di_storico_disponibili).
    """
    oggi = oggi or date.today()
    fine = primo_del_mese(oggi)                       # il mese in corso non è completo: escluso
    inizio = aggiungi_mesi(fine, -mesi)
    serie, primo_ordine = {}, None
    for o in OrdineProduzione.query.filter(OrdineProduzione.a_scorta.is_(False)).all():
        quando = o.creato_il.date() if isinstance(o.creato_il, datetime) else o.creato_il
        if not quando or not o.codice_articolo:
            continue
        primo_ordine = quando if primo_ordine is None or quando < primo_ordine else primo_ordine
        if inizio <= quando < fine:
            m = primo_del_mese(quando)
            c = o.codice_articolo.upper()
            serie.setdefault(c, {})
            serie[c][m] = serie[c].get(m, 0) + int(o.qta_pianificata or 0)
    disponibili = 0
    if primo_ordine and primo_ordine < fine:
        disponibili = min(mesi, (fine.year - primo_ordine.year) * 12 + fine.month - primo_ordine.month)
    return serie, disponibili


def ordini_certi_per_settimana(settimane):
    """
    Ordini certi = OP aperti NON a scorta: pezzi ancora da produrre (pianificati − buoni), nella settimana
    della data prevista di consegna. Senza data prevista contano nella prima settimana (e vengono segnalati).
    Ritorna ({CODICE: {lunedì: pezzi}}, [OP senza data prevista]).
    """
    primo, ultimo = settimane[0], settimane[-1]
    out, senza_data = {}, []
    q = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_OP_APERTI), OrdineProduzione.a_scorta.is_(False))
    for o in q.all():
        residuo = int(o.qta_pianificata or 0) - int(o.qta_buona or 0)
        if residuo <= 0 or not o.codice_articolo:
            continue
        if o.data_prevista:
            w = lunedi(o.data_prevista)
            w = primo if w < primo else w                 # in ritardo: va fatto subito
        else:
            w = primo
            senza_data.append(o.codice)
        if w > ultimo:
            continue
        c = o.codice_articolo.upper()
        out.setdefault(c, {})
        out[c][w] = out[c].get(w, 0) + residuo
    return out, senza_data
