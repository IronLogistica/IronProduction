"""
SCHEMI DI TAGLIO e PIANO DI TAGLIO (richiesta Mauri, 14/10/2026).

Caso reale: barra TTD32SP15L7 da 7.000 mm per la transenna T250DT (foglio di
taglio di Angelo "Tubo Tondo 32 - Barra 7 metri"):
  1 TPD32L3940 (3.940) | 1o taglio | 1 TR2502DT (2.460) | 2o taglio | scarto 600
Se lo schema e' attivato sul TR2502DT, OGNI TR2502DT si taglia cosi': per 100
TR2502DT servono 100 barre ed escono anche 100 TPD32L3940 (quelli che servono
alla commessa li coprono, gli altri vanno a magazzino).

Lo sfrido della lama e' gia' compreso nelle misure (indicazione di Mauri): lo
scarto di uno schema e' semplicemente barra - somma dei pezzi.

REGOLA (correzione di Mauri, 14/10/2026): lo schema non e' una scelta di
ottimizzazione ma una regola OBBLIGATORIA legata al codice. Su ogni schema si
spuntano i codici che lo "attivano": quando l'OL deve tagliare quel codice, ogni
barra si taglia con quello schema. Gli altri pezzi che escono dalla barra coprono
il resto dell'OL; quello che avanza e' "extra" e si carica a magazzino dall'OL
con PIN Direzione, tracciato per OP.
"""
import math
import os
import re

from flask import Blueprint, g, jsonify, render_template, request

from models import (db, SchemaTaglioWood, SchemaTaglioRigaWood, CaricoExtraTaglioWood, OrdineProduzione,
                    DistintaBaseWood, GiacenzaWood, ParametriLavorazioneWood, CentroCostoWood, AuditPP)

schemi_taglio_bp = Blueprint('schemi_taglio', __name__)

def _pin_ok(pin):
    return str(pin or '').strip() == os.environ.get('PIN_DIREZIONE', '1234')


def _norm(c):
    return str(c or '').strip().upper()


# ---------------------------------------------------------------------------
# Anagrafica
# ---------------------------------------------------------------------------

def _schema_dict(s):
    righe = [{'codice': r.codice, 'quantita': r.quantita, 'lunghezza_mm': r.lunghezza_mm,
              'attiva_schema': bool(r.attiva_schema)} for r in s.righe]
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
# Piano di taglio: lo schema scatta quando si taglia il suo codice "attivatore"
# ---------------------------------------------------------------------------

def calcola_piano(bisogni, schemi):
    """bisogni: {codice: pezzi da tagliare}; schemi: lista di _schema_dict.

    Regola (Mauri, 14/10/2026): uno schema NON e' una scelta di ottimizzazione,
    e' OBBLIGATORIO per i codici su cui e' attivato. Se l'OL deve tagliare N pezzi
    di un codice attivatore, si tagliano ceil(N / pezzi per barra) barre con quello
    schema; gli altri codici che escono dalla barra coprono prima il resto dell'OL,
    l'avanzo va a magazzino. Un codice coperto solo in parte dai pezzi dello
    schema resta da tagliare per la differenza (a barra intera o col suo schema).

    Ordine: prima gli schemi i cui pezzi "insieme" coprono altri codici
    attivatori (es. A attivato da TPD32L3940 da' anche TR2502DT, che a sua volta
    attiva B): cosi' B taglia solo i TR2502DT che mancano ancora.
    Ritorna None se nessuno schema scatta per questi codici."""
    bisogni = {_norm(c): int(math.ceil(b)) for c, b in bisogni.items() if b and b > 0}
    per_attivatore = {}
    for s in schemi:
        for r in s['righe']:
            if r.get('attiva_schema') and _norm(r['codice']) in bisogni:
                per_attivatore.setdefault(_norm(r['codice']), s)
    if not per_attivatore:
        return None

    def qmap(s):
        out = {}
        for r in s['righe']:
            out[_norm(r['codice'])] = out.get(_norm(r['codice']), 0) + int(r['quantita'] or 0)
        return out

    prodotti, righe_piano, da_fare = {}, [], list(per_attivatore)
    while da_fare:
        altri_pezzi = {c for a in da_fare for c in qmap(per_attivatore[a]) if c != a}
        pronti = [a for a in da_fare if a not in altri_pezzi] or da_fare
        a = max(pronti, key=lambda c: (bisogni[c] - prodotti.get(c, 0), c))
        da_fare.remove(a)
        s = per_attivatore[a]
        q = qmap(s)
        resto = bisogni[a] - prodotti.get(a, 0)
        if resto <= 0 or not q.get(a):
            continue
        barre = math.ceil(resto / q[a])
        for c, n in q.items():
            prodotti[c] = prodotti.get(c, 0) + barre * n
        righe_piano.append({
            'schema_id': s['id'], 'nome': s['nome'], 'barre': barre, 'attivato_da': a,
            'lunghezza_barra_mm': s['lunghezza_barra_mm'], 'scarto_mm': s['scarto_mm'],
            'composizione': [{'codice': _norm(r['codice']), 'quantita': r['quantita'], 'lunghezza_mm': r['lunghezza_mm'],
                              'pezzi_totali': barre * r['quantita'], 'richiesto': _norm(r['codice']) in bisogni,
                              'attiva_schema': bool(r.get('attiva_schema'))}
                             for r in s['righe']],
        })
    if not righe_piano:
        return None
    coperti = sorted(c for c in bisogni if prodotti.get(c, 0) >= bisogni[c])
    parziali = {c: bisogni[c] - prodotti[c] for c in bisogni if 0 < prodotti.get(c, 0) < bisogni[c]}
    non_coperti = sorted(c for c in bisogni if not prodotti.get(c))
    extra = {c: p - bisogni.get(c, 0) for c, p in prodotti.items() if p - bisogni.get(c, 0) > 0}
    lunghezze = {r['lunghezza_barra_mm'] for r in righe_piano}
    return {'righe_piano': righe_piano, 'barre_totali': sum(r['barre'] for r in righe_piano), 'prodotti': prodotti,
            'coperti': coperti, 'parziali': parziali, 'non_coperti': non_coperti, 'extra': dict(sorted(extra.items())),
            'lunghezza_barra_mm': lunghezze.pop() if len(lunghezze) == 1 else None,
            'metri_barre': round(sum(r['barre'] * r['lunghezza_barra_mm'] for r in righe_piano) / 1000, 3)}


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
    if caricati is None:
        caricati = caricati_extra(o.id, centro_id)
    # I pezzi extra gia' caricati a magazzino da questo OL finiscono nella giacenza e
    # (come ogni semilavorato) abbasserebbero il 'Da produrre' dello stesso OL: si
    # rimettono nel conto, altrimenti gli extra crescerebbero a ogni carico.
    def _con_caricati(r, base):
        return min(base + caricati.get(_norm(r['codice']), 0), r['nr_pz_da_fare'] or 0)
    # pezzi che questo OL taglia in tutto (gia' tagliati + ancora da tagliare):
    # base per gli extra previsti, che non devono sparire man mano che si taglia
    base = {r['codice']: _con_caricati(r, (r['saldo'] or 0) + (r['pezzi_fatti'] or 0)) for r in righe}
    piano_totale = calcola_piano(base, schemi)
    if piano_totale is None:
        return None
    da_tagliare = {r['codice']: min((r['saldo'] or 0) + caricati.get(_norm(r['codice']), 0),
                                    max((r['nr_pz_da_fare'] or 0) - (r['pezzi_fatti'] or 0), 0))
                   for r in righe}
    piano = calcola_piano(da_tagliare, schemi)
    coperti = set((piano or piano_totale)['coperti'])
    parziali = (piano or {}).get('parziali') or {}
    for r in righe:
        cod = _norm(r['codice'])
        if cod in coperti:
            r['nel_piano'] = True
            r['nr_barre'] = None
            r['pz_per_barra'] = None
        elif cod in parziali:
            # i pezzi che escono dallo schema ne coprono una parte: il resto si taglia a parte
            resto = parziali[cod]
            r['dal_piano'] = max(da_tagliare[r['codice']] - resto, 0)
            r['nr_barre'] = int(math.ceil(resto / r['pz_per_barra'])) if r.get('pz_per_barra') else None
    barre_fuori = sum(r['nr_barre'] or 0 for r in righe if not r.get('nel_piano'))
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
        righe.append((codice, qta, lung, bool(r.get('attiva_schema'))))
    if not righe:
        raise ValueError('Inserisci almeno un codice nello schema')
    usati = sum((l or 0) * q for _c, q, l, _a in righe)
    if usati > barra + 0.01:
        raise ValueError(f'I pezzi ({usati:.0f} mm) superano la barra ({barra:.0f} mm)')
    attivatori = [c for c, _q, _l, a in righe if a]
    if not attivatori:
        raise ValueError('Spunta almeno un codice che attiva lo schema (quando si taglia quel codice si usa questo schema)')
    if bool(d.get('attivo', True)):
        doppi = (db.session.query(SchemaTaglioRigaWood.codice, SchemaTaglioWood.nome)
                 .join(SchemaTaglioWood, SchemaTaglioWood.id == SchemaTaglioRigaWood.schema_id)
                 .filter(SchemaTaglioWood.attivo.is_(True), SchemaTaglioRigaWood.attiva_schema.is_(True),
                         db.func.upper(SchemaTaglioRigaWood.codice).in_(attivatori),
                         SchemaTaglioWood.id != (s.id or -1)).first())
        if doppi:
            raise ValueError(f'{doppi[0]} attiva già lo schema {doppi[1]}: un codice può attivare un solo schema')
    s.materiale, s.nome, s.lunghezza_barra_mm = materiale, nome, barra
    s.note = str(d.get('note') or '').strip()[:300]
    s.attivo = bool(d.get('attivo', True))
    s.righe = [SchemaTaglioRigaWood(codice=c, quantita=q, lunghezza_mm=l, attiva_schema=a) for c, q, l, a in righe]


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


# ---------------------------------------------------------------------------
# Monitor LIVE: bollino "⚡ PIANO DI TAGLIO" sui codici che attivano uno schema
# (richiesta Mauri, 15/10/2026)
# ---------------------------------------------------------------------------

def _attivatori():
    """{codice attivatore: nome schema} degli schemi attivi, una query per richiesta."""
    if not hasattr(g, '_schemi_taglio_attivatori'):
        try:
            righe = (db.session.query(SchemaTaglioRigaWood.codice, SchemaTaglioWood.nome)
                     .join(SchemaTaglioWood, SchemaTaglioWood.id == SchemaTaglioRigaWood.schema_id)
                     .filter(SchemaTaglioWood.attivo.is_(True), SchemaTaglioRigaWood.attiva_schema.is_(True)).all())
            g._schemi_taglio_attivatori = {_norm(c): n for c, n in righe}
        except Exception:
            db.session.rollback()
            g._schemi_taglio_attivatori = {}
    return g._schemi_taglio_attivatori


@schemi_taglio_bp.app_context_processor
def _contesto_schemi_taglio():
    return {'schema_taglio_attivato': lambda codice: _attivatori().get(_norm(codice))}


@schemi_taglio_bp.get('/api/ordini-produzione/<op_codice>/lista-lavoro/<int:cid>/piano-taglio')
def api_piano_taglio_ol(op_codice, cid):
    """Il piano di taglio dell'OL di questo OP su questo centro (stesso calcolo
    dell'Ordine di Lavoro) — per il riquadro del monitor LIVE."""
    from blueprints.produzione_pp.routes import _lista_lavoro_op
    o = OrdineProduzione.query.filter_by(codice=op_codice).first_or_404()
    centro = CentroCostoWood.query.get_or_404(cid)
    codice = _norm(request.args.get('codice'))
    dati = _lista_lavoro_op(o, centro, assegna_numero=False)
    gruppi = [{'materiale': gr['materiale'], 'piano': gr['piano_taglio'], 'extra': gr.get('extra_taglio') or []}
              for gr in dati['gruppi'] if gr.get('piano_taglio') or gr.get('extra_taglio')]
    schemi = []
    if codice:
        schemi = [_schema_dict(s) for s in SchemaTaglioWood.query.join(SchemaTaglioRigaWood)
                  .filter(SchemaTaglioWood.attivo.is_(True), SchemaTaglioRigaWood.attiva_schema.is_(True),
                          db.func.upper(SchemaTaglioRigaWood.codice) == codice).all()]
    return jsonify(ok=True, op_codice=o.codice, commessa=o.commessa or '', codice_articolo=o.codice_articolo,
                   centro=centro.nome, gruppi=gruppi, schemi=schemi,
                   url_ol=f'/ordini-produzione/{o.codice}/lista-lavoro/{centro.id}')
