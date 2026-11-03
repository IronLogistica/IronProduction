"""
MASTERPLAN · AMPS — calcolo della domanda (R1): previsioni approvate + ordini certi -> domanda settimanale per codice.
Solo lettura: il risultato viene restituito, non salvato.
"""
from datetime import date

from blueprints.masterplan import dati, impostazioni
from blueprints.masterplan.modelli import AFFIDABILITA, MpFamiglia, MpPrevisione
from blueprints.masterplan.motore import (MixNonValido, chiudi_domanda, consuma_famiglia, consuma_previsione,
                                          distribuisci_famiglia, elenco_settimane, mensile_su_settimane)


SCENARI = ('pessimista', 'base', 'ottimista')


def _quantita_pesata(imp, p, scenario='base'):
    """
    Quantità della cella dopo il peso di affidabilità.
    pessimista: solo «Certa» · base: «Certa» + «Probabile» · ottimista: anche «Possibile».
    """
    ammesse = {'pessimista': ('CERTA',), 'base': ('CERTA', 'PROBABILE')}.get(scenario, AFFIDABILITA)
    if p.affidabilita not in ammesse:
        return 0.0
    return p.quantita * impostazioni.peso_affidabilita(imp, p.affidabilita)


def previsioni_mensili(imp, scenario='base'):
    """({codice: {mese: pezzi}} per articolo, {famiglia_id: {mese: pezzi}}) — solo celle APPROVATA, arrotondate al pezzo."""
    art, fam = {}, {}
    for p in MpPrevisione.query.filter_by(stato='APPROVATA').all():
        q = _quantita_pesata(imp, p, scenario)
        if p.livello == 'ARTICOLO':
            tgt = art.setdefault(p.codice.upper(), {})
        else:
            tgt = fam.setdefault(p.famiglia_id, {})
        tgt[p.mese] = tgt.get(p.mese, 0) + q
    arrot = lambda d: {k: {m: int(round(v)) for m, v in mm.items()} for k, mm in d.items()}
    return arrot(art), arrot(fam)


def calcola(oggi=None, scenario='base'):
    oggi = oggi or date.today()
    scenario = scenario if scenario in SCENARI else 'base'
    imp = impostazioni.valori()
    settimane = elenco_settimane(oggi, imp['orizzonte_settimane'])
    avvisi = []
    art_mese, fam_mese = previsioni_mensili(imp, scenario)
    ordini, senza_data = dati.ordini_certi_per_settimana(settimane)
    if senza_data:
        avvisi.append(f"{len(senza_data)} ordini aperti senza data prevista: contati nella prima settimana ({', '.join(senza_data[:5])}{'…' if len(senza_data) > 5 else ''})")

    # previsione mensile per codice (articolo + quota di famiglia)
    prev_cod_mese = {c: dict(mm) for c, mm in art_mese.items()}
    in_famiglia = {}                       # codice -> famiglia_id
    for f in MpFamiglia.query.all():
        mix = {c.codice.upper(): c.mix_pct for c in f.codici}
        for c in mix:
            in_famiglia[c] = f.id
        mesi = set(fam_mese.get(f.id, {}))
        if not mesi:
            continue
        try:
            for m in mesi:
                art_m = {c: art_mese.get(c, {}).get(m, 0) for c in mix}
                ris, av = distribuisci_famiglia(fam_mese[f.id][m], mix, art_m)
                avvisi += [f'{f.codice} {m:%m/%Y}: {a}' for a in av]
                for c, r in ris.items():
                    if r['origine'] == 'FAMIGLIA' and r['qta']:
                        prev_cod_mese.setdefault(c, {})[m] = prev_cod_mese.get(c, {}).get(m, 0) + r['qta']
        except MixNonValido as e:
            avvisi.append(f'Famiglia {f.codice}: {e} — previsione di famiglia ignorata')

    # settimanale per codice
    prev_sett = {}
    for c, mm in prev_cod_mese.items():
        for m, q in mm.items():
            for w, v in mensile_su_settimane(m, q).items():
                if w in settimane:
                    prev_sett.setdefault(c, {})
                    prev_sett[c][w] = prev_sett[c].get(w, 0) + v

    codici = sorted(set(prev_sett) | set(ordini))
    righe = {c: consuma_previsione(settimane, prev_sett.get(c, {}), ordini.get(c, {}),
                                   imp['finestra_consumo_settimane'], imp['zona_congelata_settimane']) for c in codici}
    gruppi = {}
    for c in codici:
        if c in in_famiglia:
            gruppi.setdefault(in_famiglia[c], {})[c] = righe[c]
    for g in gruppi.values():
        consuma_famiglia(settimane, g, imp['finestra_consumo_settimane'])
    for c in codici:
        chiudi_domanda(righe[c], imp['zona_congelata_settimane'])

    desc = dati.descrizioni(codici)
    out = []
    for c in codici:
        r = righe[c]
        out.append({'codice': c, 'descrizione': desc.get(c, ''), 'righe': r,
                    'tot_ordini': sum(x['ordini'] for x in r), 'tot_previsto': sum(x['previsto'] for x in r),
                    'tot_residuo': sum(x['residuo_contato'] for x in r), 'tot_domanda': sum(x['domanda'] for x in r)})
    return {'scenario': scenario, 'settimane': settimane, 'impostazioni': imp, 'codici': out, 'avvisi': avvisi,
            'totali': {'ordini': sum(o['tot_ordini'] for o in out), 'previsione': sum(o['tot_residuo'] for o in out),
                       'domanda': sum(o['tot_domanda'] for o in out)}}
