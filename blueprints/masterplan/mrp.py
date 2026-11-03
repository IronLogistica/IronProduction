"""
MASTERPLAN · AMPS — R2: esplosione della distinta con le date + griglia MRP settimanale (SOLA LETTURA).
Dalla domanda settimanale dei prodotti finiti (calcolo.py) scende lungo la distinta: ogni livello anticipa il
fabbisogno di 'settimane_per_livello'. Sui codici netta giacenza e ordini fornitore aperti e dice quando
ordinare (fabbisogno − lead time del fornitore).
"""
import math
from datetime import date

from blueprints.masterplan import calcolo, dati, impostazioni
from blueprints.masterplan.modelli import MpParametroArticolo
from blueprints.masterplan.motore import lunedi
from models import (ArticoloApprovvigionamento, CentroCostoWood, CicloLavoroWood, GiacenzaWood, OrdineAcquistoWood, RigaOrdineAcquistoWood)

PROFONDITA_MAX = 12


def esplodi(domanda_finiti, settimane, mappa, per_livello):
    """
    domanda_finiti: {CODICE: [pezzi per settimana]} (stessa lunghezza di 'settimane').
    Ritorna ({CODICE: [fabbisogno lordo per settimana]}, {CODICE: livello minimo}, avvisi).
    Un fabbisogno che cadrebbe prima della prima settimana è messo nella prima e contato in 'in_ritardo'.
    """
    n = len(settimane)
    lordo, livello, ritardo, avvisi = {}, {}, {}, []

    def scendi(padre, qta_per_sett, prof, visitati):
        if prof > PROFONDITA_MAX:
            avvisi.append(f'{padre}: distinta troppo profonda, esplosione interrotta')
            return
        for r in dati._righe_attive(mappa, padre):
            figlio = r.codice_figlio.upper()
            if figlio in visitati:
                avvisi.append(f'ciclo nella distinta: {padre} → {r.codice_figlio}')
                continue
            fq = [0.0] * n
            for i, q in enumerate(qta_per_sett):
                if not q:
                    continue
                j = i - per_livello
                if j < 0:
                    ritardo[figlio] = ritardo.get(figlio, 0) + q * (r.quantita or 0)
                    j = 0
                fq[j] += q * (r.quantita or 0)
            if not any(fq):
                continue
            acc = lordo.setdefault(figlio, [0.0] * n)
            for i, v in enumerate(fq):
                acc[i] += v
            livello[figlio] = min(livello.get(figlio, prof), prof)
            scendi(r.codice_figlio, fq, prof + 1, visitati | {figlio})

    for cod, q in domanda_finiti.items():
        if any(q):
            scendi(cod, [float(x) for x in q], 1, {cod.upper()})
    return lordo, livello, ritardo, avvisi


def ricevimenti_aperti(settimane):
    """{CODICE: [pezzi in arrivo per settimana]} dagli ordini fornitore non ricevuti; senza data → prima settimana."""
    n, primo = len(settimane), settimane[0]
    out, senza_data = {}, 0
    q = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
         .filter(OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO'))
    for r in q.all():
        residuo = (r.qta_originale or 0) - (r.qta_ricevuta or 0)
        if residuo <= 0 or not r.codice:
            continue
        d = r.ordine.data_consegna if getattr(r, 'ordine', None) else None
        if d is None:
            senza_data += 1
            w = primo
        else:
            w = max(lunedi(d), primo)
        if w > settimane[-1]:
            continue
        acc = out.setdefault(r.codice.upper(), [0.0] * n)
        acc[settimane.index(w)] += residuo
    return out, senza_data


def netta(lordo, giacenza, arrivi, lead_sett, lotto_minimo=0, multiplo=0):
    """
    Un codice. Ordine = fabbisogno netto, portato al lotto minimo e arrotondato al multiplo; l'eccedenza
    resta come disponibile nelle settimane dopo. Ritorna righe per settimana [{lordo, arrivi, disponibile,
    netto}] e ordini pianificati [{arrivo, quantita, ordina_in, in_ritardo}] (indici di settimana).
    """
    n = len(lordo)
    disp, righe, ordini = float(giacenza), [], []
    for i in range(n):
        arr = arrivi[i] if arrivi else 0.0
        disp += arr - lordo[i]
        netto = 0.0
        if disp < -1e-9:
            netto = -disp
            qta = math.ceil(netto - 1e-9)
            if lotto_minimo:
                qta = max(qta, math.ceil(lotto_minimo))
            if multiplo:
                qta = int(math.ceil(qta / multiplo - 1e-9) * multiplo)
            quando = i - lead_sett
            ordini.append({'arrivo': i, 'quantita': qta, 'ordina_in': max(quando, 0), 'in_ritardo': quando < 0})
            disp += qta
        righe.append({'lordo': round(lordo[i], 3), 'arrivi': round(arr, 3), 'disponibile': round(disp, 3),
                      'netto': round(netto, 3)})
    return righe, ordini


def carico_reparti(qta_prodotte, settimane, imp):
    """
    Ore necessarie per reparto e settimana: pezzi da produrre / produttività oraria di ogni fase del ciclo.
    qta_prodotte: {CODICE: [pezzi per settimana]}. Capacità = risorse × ore/giorno × giorni/sett × efficienza.
    """
    n = len(settimane)
    centri = {c.id: c for c in CentroCostoWood.query.all()}
    cicli = {}
    for f in CicloLavoroWood.query.all():
        cicli.setdefault(f.codice.upper(), []).append(f)
    ore, senza_ciclo = {}, set()
    for cod, q in qta_prodotte.items():
        if not any(q):
            continue
        fasi = cicli.get(cod)
        if not fasi:
            senza_ciclo.add(cod)
            continue
        for f in fasi:
            if (f.produttivita_oraria or 0) <= 0 or f.centro_costo_id not in centri:
                continue
            acc = ore.setdefault(f.centro_costo_id, [0.0] * n)
            for i, v in enumerate(q):
                acc[i] += v / f.produttivita_oraria
    out = []
    for cid, o in sorted(ore.items(), key=lambda kv: centri[kv[0]].nome):
        c = centri[cid]
        cap = (c.n_risorse_equivalenti or 1) * imp['ore_giorno'] * imp['giorni_settimana'] * (c.pct_efficienza or 100) / 100.0
        out.append({'centro': c.nome, 'esterno': bool(c.esterno), 'capacita_ore': round(cap, 1),
                    'ore': [round(x, 1) for x in o], 'saturazione': [round(100 * x / cap, 0) if cap else 0 for x in o]})
    return out, sorted(senza_ciclo)


def calcola_mrp(oggi=None, scenario='base'):
    from blueprints.acquisti_wood.routes import TIPI_DA_ACQUISTARE
    imp = impostazioni.valori()
    dom = calcolo.calcola(oggi, scenario)
    settimane = dom['settimane']
    mappa = dati.mappa_distinta()
    finiti = dati.prodotti_finiti(mappa)
    dom_fin = {}
    for c in dom['codici']:
        if c['codice'] in finiti:
            dom_fin[finiti[c['codice']]] = [r['domanda'] for r in c['righe']]
    avvisi = list(dom['avvisi'])
    non_finiti = [c['codice'] for c in dom['codici'] if c['codice'] not in finiti and c['codice'] not in {k.upper() for k in mappa}]
    if non_finiti:
        avvisi.append(f"Domanda su codici che non sono prodotti finiti della distinta (ignorata nell'esplosione): {', '.join(non_finiti[:5])}")
    lordo, livello, ritardo, av = esplodi(dom_fin, settimane, mappa, imp['settimane_per_livello'])
    avvisi += av
    arrivi, senza_data = ricevimenti_aperti(settimane)
    if senza_data:
        avvisi.append(f'{senza_data} righe di ordini fornitore senza data di consegna: contate nella prima settimana')
    giac = {g.codice.upper(): g.quantita or 0 for g in GiacenzaWood.query.all()}
    art = {a.codice.upper(): a for a in ArticoloApprovvigionamento.query.all() if a.codice}
    lotti = {p.codice.upper(): p for p in MpParametroArticolo.query.all()}
    desc = dati.descrizioni(lordo)
    out = []
    for c in sorted(lordo, key=lambda k: (livello[k], k)):
        a = art.get(c)
        tipo = a.tipo_approvvigionamento if a else 'DA_CLASSIFICARE'
        acquisto = tipo in TIPI_DA_ACQUISTARE
        lt_giorni = (a.lead_time_fornitura_giorni if a and a.lead_time_fornitura_giorni else imp['lead_time_default_giorni'])
        lead_sett = math.ceil(lt_giorni / imp['giorni_settimana']) if acquisto else imp['settimane_per_livello']
        lp = lotti.get(c)
        righe, ordini = netta(lordo[c], giac.get(c, 0), arrivi.get(c), lead_sett,
                              lp.lotto_minimo if lp else 0, lp.multiplo if lp else 0)
        out.append({'codice': c, 'descrizione': desc.get(c, ''), 'tipo': tipo, 'acquisto': acquisto,
                    'livello': livello[c], 'giacenza': giac.get(c, 0), 'lead_time_settimane': lead_sett,
                    'lead_time_stimato': acquisto and not (a and a.lead_time_fornitura_giorni),
                    'lotto_minimo': lp.lotto_minimo if lp else 0, 'multiplo': lp.multiplo if lp else 0,
                    'in_ritardo_pezzi': round(ritardo.get(c, 0), 3), 'righe': righe,
                    'ordini_pianificati': [dict(o, arrivo_data=settimane[o['arrivo']].isoformat(),
                                                ordina_data=settimane[o['ordina_in']].isoformat()) for o in ordini]})
    # proposte (acquisto / produzione) ordinate per data
    proposte = {'acquisto': [], 'produzione': []}
    for x in out:
        for o in x['ordini_pianificati']:
            riga = {'codice': x['codice'], 'descrizione': x['descrizione'], 'quantita': o['quantita'],
                    'ordina_data': o['ordina_data'], 'arrivo_data': o['arrivo_data'], 'in_ritardo': o['in_ritardo'],
                    'lotto_minimo': x['lotto_minimo'], 'multiplo': x['multiplo'], 'stimato': x['lead_time_stimato']}
            proposte['acquisto' if x['acquisto'] else 'produzione'].append(riga)
    for k in proposte:
        proposte[k].sort(key=lambda r: (r['ordina_data'], r['codice']))
    # carico reparti: finiti (domanda) + semilavorati prodotti internamente (lordo)
    qta_prod = {finiti[k.upper()].upper() if k.upper() in finiti else k.upper(): v for k, v in dom_fin.items()}
    for x in out:
        if not x['acquisto']:
            qta_prod[x['codice']] = [r['lordo'] for r in x['righe']]
    carico, senza_ciclo = carico_reparti(qta_prod, settimane, imp)
    if senza_ciclo:
        avvisi.append(f"Carico reparti incompleto: senza ciclo di lavoro {', '.join(senza_ciclo[:5])}{'…' if len(senza_ciclo) > 5 else ''}")
    return {'scenario': dom['scenario'], 'settimane': [w.isoformat() for w in settimane], 'codici': out,
            'proposte': proposte, 'carico': carico, 'avvisi': avvisi,
            'totali': {'codici': len(out), 'da_ordinare': sum(1 for x in out if x['acquisto'] and x['ordini_pianificati']),
                       'in_ritardo': sum(1 for x in out if any(o['in_ritardo'] for o in x['ordini_pianificati'])),
                       'reparti_saturi': sum(1 for c in carico if max(c['saturazione'] or [0]) > 100)}}
