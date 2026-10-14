"""
SCHEMI DI TAGLIO e PIANO DI TAGLIO (richiesta Mauri, 14/10/2026).

Caso reale: barra TTD32SP15L7 da 7.000 mm per la transenna T250DT.
  - schema A = 1 TPD32L3940 (3.940) + 1 TR2502DT (2.460)   -> scarto 600
  - schema B = 2 TR2502DT (2 x 2.460) + 2 TPD32L780 (zampe) -> scarto 520
Per 50 T250DT (50 TPD32L3940 + 100 TR2502DT): 50 barre A + 25 barre B = 75
barre (contro 100 tagliando un codice alla volta) e 50 zampe in piu' che vanno a
magazzino come semilavorato: la prossima commessa di zampe le trova gia' pronte.

Lo sfrido della lama e' gia' compreso nelle misure (indicazione di Mauri): lo
scarto di uno schema e' semplicemente barra - somma dei pezzi.

Il Piano di Taglio e' calcolato per ogni gruppo-materiale dell'Ordine di Lavoro:
i bisogni sono i 'Da produrre' delle righe dell'OL; si sceglie la combinazione
di schemi con MENO barre (a parita', meno pezzi in eccesso). Tutto cio' che
esce oltre il bisogno (codici non richiesti dall'OL + eccedenze) e' "extra":
si carica a magazzino dall'OL con PIN Direzione, tracciato per OP.
"""
import math
import os
import re

from flask import Blueprint, jsonify, render_template, request

from models import (db, SchemaTaglioWood, SchemaTaglioRigaWood, CaricoExtraTaglioWood, OrdineProduzione,
                    DistintaBaseWood, GiacenzaWood, ParametriLavorazioneWood, CentroCostoWood, AuditPP)

schemi_taglio_bp = Blueprint('schemi_taglio', __name__)

LIMITE_NODI_RICERCA = 200_000


def _pin_ok(pin):
    return str(pin or '').strip() == os.environ.get('PIN_DIREZIONE', '1234')


def _norm(c):
    return str(c or '').strip().upper()


# ---------------------------------------------------------------------------
# Anagrafica
# ---------------------------------------------------------------------------

def _schema_dict(s):
    righe = [{'codice': r.codice, 'quantita': r.quantita, 'lunghezza_mm': r.lunghezza_mm} for r in s.righe]
    usati = sum((r['lunghezza_mm'] or 0) * r['quantita'] for r in righe)
    tutte_le_misure = all(r['lunghezza_mm'] for r in righe) and righe
    return {'id': s.id, 'materiale': s.materiale, 'nome': s.nome, 'lunghezza_barra_mm': s.lunghezza_barra_mm,
            'note': s.note or '', 'attivo': bool(s.attivo), 'righe': righe,
            'usati_mm': round(usati, 1) if tutte_le_misure else None,
            'scarto_mm': round(s.lunghezza_barra_mm - usati, 1) if tutte_le_misure else None}


def schemi_per_materiale(materiale, solo_attivi=True):
    q = SchemaTaglioWood.query.filter(db.func.upper(SchemaTaglioWood.materiale) == _norm(materiale))
    if solo_attivi:
        q = q.filter(SchemaTaglioWood.attivo.is_(True))
    return [_schema_dict(s) for s in q.order_by(SchemaTaglioWood.nome, SchemaTaglioWood.id).all()]


# ---------------------------------------------------------------------------
# Ottimizzatore: minimo numero di barre che copre i bisogni
# ---------------------------------------------------------------------------

def _greedy(bisogni, schemi):
    """Soluzione di partenza sempre valida: barra per barra, lo schema che copre
    piu' pezzi ancora mancanti."""
    rem = dict(bisogni)
    conteggi = [0] * len(schemi)
    while any(v > 0 for v in rem.values()):
        migliore, punti = None, 0
        for i, s in enumerate(schemi):
            p = sum(min(q, max(rem.get(c, 0), 0)) for c, q in s['_q'].items())
            if p > punti:
                migliore, punti = i, p
        if migliore is None:
            return None
        conteggi[migliore] += 1
        for c, q in schemi[migliore]['_q'].items():
            if c in rem:
                rem[c] -= q
    return conteggi


def _valuta(conteggi, bisogni, schemi):
    prodotti = {}
    for n, s in zip(conteggi, schemi):
        for c, q in s['_q'].items():
            prodotti[c] = prodotti.get(c, 0) + n * q
    eccesso = sum(prodotti.get(c, 0) - b for c, b in bisogni.items())
    return (sum(conteggi), eccesso, sum(1 for n in conteggi if n)), prodotti


def calcola_piano(bisogni, schemi):
    """bisogni: {codice: pezzi da tagliare}; schemi: lista di _schema_dict.
    Ritorna None se nessuno schema riguarda questi codici."""
    bisogni = {_norm(c): int(math.ceil(b)) for c, b in bisogni.items() if b and b > 0}
    utili = []
    for s in schemi:
        qmap = {}
        for r in s['righe']:
            if r['quantita'] and r['quantita'] > 0:
                qmap[_norm(r['codice'])] = qmap.get(_norm(r['codice']), 0) + int(r['quantita'])
        if any(c in bisogni for c in qmap):
            utili.append(dict(s, _q=qmap))
    if not utili:
        return None
    coperti = {c: b for c, b in bisogni.items() if any(c in s['_q'] for s in utili)}
    non_coperti = sorted(c for c in bisogni if c not in coperti)

    migliore = _greedy(coperti, utili)
    miglior_chiave = _valuta(migliore, coperti, utili)[0]
    n = len(utili)
    nodi = [0]
    ottimo = [True]
    # massimo pezzi per barra di ciascun codice negli schemi da i in poi (per il limite inferiore)
    max_da = [{c: max((utili[j]['_q'].get(c, 0) for j in range(i, n)), default=0) for c in coperti}
              for i in range(n + 1)]

    def cerca(i, rem, conteggi, barre):
        nonlocal migliore, miglior_chiave
        nodi[0] += 1
        if nodi[0] > LIMITE_NODI_RICERCA:
            ottimo[0] = False
            return
        mancanti = {c: v for c, v in rem.items() if v > 0}
        if not mancanti:
            chiave = _valuta(conteggi + [0] * (n - len(conteggi)), coperti, utili)[0]
            if chiave < miglior_chiave:
                migliore, miglior_chiave = conteggi + [0] * (n - len(conteggi)), chiave
            return
        if i == n:
            return
        lb = 0
        for c, v in mancanti.items():
            m = max_da[i][c]
            if m == 0:
                return
            lb = max(lb, math.ceil(v / m))
        if barre + lb > miglior_chiave[0]:
            return
        q = utili[i]['_q']
        alto = max((math.ceil(v / q[c]) for c, v in mancanti.items() if q.get(c)), default=0)
        for k in range(alto, -1, -1):
            nuovo = {c: v - k * q.get(c, 0) for c, v in rem.items()}
            cerca(i + 1, nuovo, conteggi + [k], barre + k)

    cerca(0, dict(coperti), [], 0)
    _chiave, prodotti = _valuta(migliore, coperti, utili)

    righe_piano = []
    for k, s in zip(migliore, utili):
        if not k:
            continue
        righe_piano.append({
            'schema_id': s['id'], 'nome': s['nome'], 'barre': k, 'lunghezza_barra_mm': s['lunghezza_barra_mm'],
            'scarto_mm': s['scarto_mm'],
            'composizione': [{'codice': _norm(r['codice']), 'quantita': r['quantita'], 'lunghezza_mm': r['lunghezza_mm'],
                              'pezzi_totali': k * r['quantita'], 'richiesto': _norm(r['codice']) in coperti}
                             for r in s['righe']],
        })
    extra = {c: p - coperti.get(c, 0) for c, p in prodotti.items() if p - coperti.get(c, 0) > 0}
    lunghezze = {s['lunghezza_barra_mm'] for s in utili}
    return {'righe_piano': righe_piano, 'barre_totali': sum(migliore), 'prodotti': prodotti,
            'coperti': sorted(coperti), 'non_coperti': non_coperti, 'extra': dict(sorted(extra.items())),
            'lunghezza_barra_mm': lunghezze.pop() if len(lunghezze) == 1 else None,
            'metri_barre': round(sum(k * s['lunghezza_barra_mm'] for k, s in zip(migliore, utili)) / 1000, 3),
            'ottimo': ottimo[0]}


# ---------------------------------------------------------------------------
# Integrazione con l'Ordine di Lavoro
# ---------------------------------------------------------------------------

def caricati_extra(op_id, centro_id=None):
    q = CaricoExtraTaglioWood.query.filter_by(op_id=op_id)
    if centro_id is not None:
        q = q.filter_by(centro_id=centro_id)
    out = {}
    for c in q.all():
        out[_norm(c.codice)] = out.get(_norm(c.codice), 0) + (c.quantita or 0)
    return out


def applica_piano_al_gruppo(o, centro_id, materiale, righe, caricati=None):
    """Arricchisce un gruppo dell'OL con il piano di taglio. Ritorna
    (piano_su_saldo, extra_taglio, barre_righe_fuori_piano) oppure None se il
    materiale non ha schemi che riguardano queste righe."""
    schemi = schemi_per_materiale(materiale)
    if not schemi:
        return None
    # pezzi che questo OL taglia in tutto (gia' tagliati + ancora da tagliare):
    # base per gli extra previsti, che non devono sparire man mano che si taglia
    base = {r['codice']: min((r['saldo'] or 0) + (r['pezzi_fatti'] or 0), r['nr_pz_da_fare'] or 0) for r in righe}
    piano_totale = calcola_piano(base, schemi)
    if piano_totale is None:
        return None
    piano = calcola_piano({r['codice']: r['saldo'] for r in righe}, schemi)
    coperti = set(piano_totale['coperti'])
    for r in righe:
        if _norm(r['codice']) in coperti:
            r['nel_piano'] = True
            r['nr_barre'] = None
            r['pz_per_barra'] = None
    barre_fuori = sum(r['nr_barre'] or 0 for r in righe if not r.get('nel_piano'))
    if caricati is None:
        caricati = caricati_extra(o.id, centro_id)
    extra = [{'codice': c, 'previsti': q, 'caricati': caricati.get(c, 0),
              'da_caricare': max(q - caricati.get(c, 0), 0)}
             for c, q in piano_totale['extra'].items()]
    return piano, extra, barre_fuori


# ---------------------------------------------------------------------------
# Pagine e API
# ---------------------------------------------------------------------------

@schemi_taglio_bp.route('/schemi-taglio')
def pagina_schemi_taglio():
    return render_template('schemi_taglio.html', active='schemi_taglio')


@schemi_taglio_bp.get('/api/schemi-taglio')
def api_lista_schemi():
    tutti = SchemaTaglioWood.query.order_by(SchemaTaglioWood.materiale, SchemaTaglioWood.nome).all()
    return jsonify([_schema_dict(s) for s in tutti])


def _lunghezza_da_sviluppo(sviluppo):
    """'SVIL.3.940' / 'L. 2.460' -> 3940 / 2460 (mm). None se non leggibile."""
    cifre = re.sub(r'\D', '', str(sviluppo or ''))
    return float(cifre) if cifre and 50 <= int(cifre) <= 20000 else None


@schemi_taglio_bp.get('/api/schemi-taglio/info-codice')
def api_info_codice():
    """Per compilare lo schema: materiale (primo figlio in distinta), lunghezza
    barra del materiale e lunghezza del pezzo dai Parametri di Lavorazione."""
    codice = _norm(request.args.get('codice'))
    figlio = DistintaBaseWood.query.filter(db.func.upper(DistintaBaseWood.codice_padre) == codice).first()
    par = ParametriLavorazioneWood.query.get(codice)
    return jsonify(codice=codice, materiale=_norm(figlio.codice_figlio) if figlio else '',
                   lunghezza_barra_mm=par.lunghezza_barra_mm if par else None,
                   lunghezza_mm=_lunghezza_da_sviluppo(par.sviluppo) if par else None)


@schemi_taglio_bp.get('/api/schemi-taglio/cerca-codici')
def api_cerca_codici():
    q = _norm(request.args.get('q'))
    if len(q) < 2:
        return jsonify([])
    like = f'%{q}%'
    codici = set()
    for (c,) in db.session.query(DistintaBaseWood.codice_padre).filter(db.func.upper(DistintaBaseWood.codice_padre).like(like)).distinct().limit(40):
        codici.add(_norm(c))
    for (c,) in db.session.query(DistintaBaseWood.codice_figlio).filter(db.func.upper(DistintaBaseWood.codice_figlio).like(like)).distinct().limit(40):
        codici.add(_norm(c))
    for (c,) in db.session.query(GiacenzaWood.codice).filter(db.func.upper(GiacenzaWood.codice).like(like)).limit(40):
        codici.add(_norm(c))
    return jsonify(sorted(codici, key=lambda c: (not c.startswith(q), c))[:30])


def _valida_e_salva(s, d):
    materiale = _norm(d.get('materiale'))
    nome = str(d.get('nome') or '').strip()
    try:
        barra = float(d.get('lunghezza_barra_mm') or 0)
    except (TypeError, ValueError):
        barra = 0
    if not materiale or not nome:
        raise ValueError('Materiale e nome dello schema sono obbligatori')
    if barra <= 0:
        raise ValueError('Lunghezza barra obbligatoria (mm)')
    righe = []
    for r in d.get('righe') or []:
        codice = _norm(r.get('codice'))
        if not codice:
            continue
        try:
            qta = int(r.get('quantita') or 0)
            lung = float(r['lunghezza_mm']) if r.get('lunghezza_mm') not in (None, '') else None
        except (TypeError, ValueError):
            raise ValueError(f'{codice}: quantità o lunghezza non valida')
        if qta <= 0:
            raise ValueError(f'{codice}: quantità per barra deve essere almeno 1')
        righe.append((codice, qta, lung))
    if not righe:
        raise ValueError('Inserisci almeno un codice nello schema')
    usati = sum((l or 0) * q for _c, q, l in righe)
    if usati > barra + 0.01:
        raise ValueError(f'I pezzi ({usati:.0f} mm) superano la barra ({barra:.0f} mm)')
    s.materiale, s.nome, s.lunghezza_barra_mm = materiale, nome, barra
    s.note = str(d.get('note') or '').strip()[:300]
    s.attivo = bool(d.get('attivo', True))
    s.righe = [SchemaTaglioRigaWood(codice=c, quantita=q, lunghezza_mm=l) for c, q, l in righe]


@schemi_taglio_bp.post('/api/schemi-taglio')
def api_crea_schema():
    d = request.get_json(silent=True) or {}
    s = SchemaTaglioWood()
    try:
        _valida_e_salva(s, d)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    db.session.add(s)
    db.session.commit()
    return jsonify(ok=True, schema=_schema_dict(s)), 201


@schemi_taglio_bp.put('/api/schemi-taglio/<int:sid>')
def api_modifica_schema(sid):
    s = SchemaTaglioWood.query.get_or_404(sid)
    d = request.get_json(silent=True) or {}
    try:
        _valida_e_salva(s, d)
    except ValueError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    db.session.commit()
    return jsonify(ok=True, schema=_schema_dict(s))


@schemi_taglio_bp.delete('/api/schemi-taglio/<int:sid>')
def api_elimina_schema(sid):
    s = SchemaTaglioWood.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    return jsonify(ok=True)


@schemi_taglio_bp.post('/api/schemi-taglio/simula')
def api_simula():
    """Prova il piano: {materiale, bisogni: {codice: pezzi}}."""
    d = request.get_json(silent=True) or {}
    piano = calcola_piano({_norm(k): float(v or 0) for k, v in (d.get('bisogni') or {}).items()},
                          schemi_per_materiale(d.get('materiale')))
    return jsonify(ok=True, piano=piano)


@schemi_taglio_bp.post('/api/ordini-produzione/<op_codice>/lista-lavoro/<int:cid>/carica-extra-taglio')
def api_carica_extra(op_codice, cid):
    """Carica a magazzino i pezzi extra del piano di taglio (es. zampe TPD32L780)."""
    from blueprints.produzione_pp.routes import _lista_lavoro_op
    from blueprints.magazzino.routes import _registra_movimento_giacenza
    d = request.get_json(silent=True) or {}
    if not _pin_ok(d.get('pin')):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    o = OrdineProduzione.query.filter_by(codice=op_codice).first_or_404()
    centro = CentroCostoWood.query.get_or_404(cid)
    codice = _norm(d.get('codice'))
    try:
        qta = float(d.get('quantita') or 0)
    except (TypeError, ValueError):
        qta = 0
    if qta <= 0:
        return jsonify(ok=False, error='Quantità non valida'), 400
    dati = _lista_lavoro_op(o, centro, assegna_numero=False)
    voce = next((e for g in dati['gruppi'] for e in (g.get('extra_taglio') or []) if e['codice'] == codice), None)
    if voce is None:
        return jsonify(ok=False, error=f'{codice} non è un pezzo extra del piano di taglio di questo OL'), 400
    if qta > voce['da_caricare'] + 1e-9:
        return jsonify(ok=False, error=f'Al massimo {voce["da_caricare"]:g} pz ancora da caricare per {codice} '
                                       f'(previsti {voce["previsti"]:g}, già caricati {voce["caricati"]:g})'), 400
    nota = f'Pezzi extra dal piano di taglio — {o.codice} commessa {o.commessa or ""} ({centro.nome})'
    _registra_movimento_giacenza(codice, qta, 'carico_produzione', riferimento=o.codice, note=nota)
    db.session.add(CaricoExtraTaglioWood(op_id=o.id, op_codice=o.codice, centro_id=centro.id, codice=codice, quantita=qta))
    db.session.add(AuditPP(op_code=o.codice, event_id='', azione='CARICO_EXTRA_TAGLIO',
                           dettaglio=f'{qta:g} pz {codice} caricati a magazzino dal piano di taglio ({centro.nome})'))
    db.session.commit()
    return jsonify(ok=True, codice=codice, caricati=voce['caricati'] + qta)
