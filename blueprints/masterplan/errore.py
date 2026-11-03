"""
MASTERPLAN · AMPS — R4: misura dell'errore di previsione (SOLA LETTURA).
Confronta, per i mesi già chiusi, la previsione APPROVATA (quantità inserita, senza pesi) con i pezzi
effettivamente ordinati (OP non a scorta, stessa base dello storico). WAPE = Σ|errore| / Σ effettivo;
bias = (Σ previsto − Σ effettivo) / Σ effettivo (positivo = sovrastima).
"""
from datetime import date

from blueprints.masterplan import dati
from blueprints.masterplan.modelli import MpFamiglia, MpPrevisione
from blueprints.masterplan.motore import primo_del_mese


def _metriche(righe):
    prev = sum(r['previsto'] for r in righe)
    eff = sum(r['effettivo'] for r in righe)
    ass = sum(abs(r['previsto'] - r['effettivo']) for r in righe)
    return {'previsto': prev, 'effettivo': eff,
            'wape': round(100.0 * ass / eff, 1) if eff else None,
            'bias': round(100.0 * (prev - eff) / eff, 1) if eff else None}


def misura(oggi=None, mesi=12):
    oggi = oggi or date.today()
    corrente = primo_del_mese(oggi)
    storico, _disp = dati.serie_mensile_op(oggi, mesi)
    famiglia_di = {c.codice.upper(): f.id for f in MpFamiglia.query.all() for c in f.codici}
    nome_fam = {f.id: f.codice for f in MpFamiglia.query.all()}
    prev = {}                                   # (tipo, chiave, mese) -> pezzi previsti
    for p in MpPrevisione.query.filter(MpPrevisione.stato == 'APPROVATA', MpPrevisione.mese < corrente).all():
        k = ('ARTICOLO', p.codice.upper(), p.mese) if p.livello == 'ARTICOLO' else ('FAMIGLIA', p.famiglia_id, p.mese)
        prev[k] = prev.get(k, 0) + p.quantita
    righe = []
    for (tipo, chiave, mese), q in sorted(prev.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2])):
        if tipo == 'ARTICOLO':
            eff, nome = storico.get(chiave, {}).get(mese, 0), chiave
        else:
            eff = sum(m.get(mese, 0) for c, m in storico.items() if famiglia_di.get(c) == chiave)
            nome = nome_fam.get(chiave, f'famiglia {chiave}')
        righe.append({'livello': tipo, 'chiave': nome, 'mese': mese.isoformat(), 'previsto': q, 'effettivo': eff,
                      'errore': q - eff})
    per = {}
    for r in righe:
        per.setdefault((r['livello'], r['chiave']), []).append(r)
    sintesi = [dict(livello=l, chiave=k, mesi=len(rr), **_metriche(rr)) for (l, k), rr in sorted(per.items())]
    return {'righe': righe, 'per_prodotto': sintesi, 'totale': _metriche(righe) if righe else None}
