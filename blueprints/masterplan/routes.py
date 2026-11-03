"""MASTERPLAN · AMPS — pagine e API (R1: qualità dati, famiglie, previsioni, domanda, impostazioni)."""
from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request

from blueprints.masterplan import calcolo, dati, impostazioni, mrp, qualita, storico
from blueprints.masterplan.modelli import (AFFIDABILITA, MpFamiglia, MpFamigliaCodice, MpPrevisione)
from blueprints.masterplan.motore import MixNonValido, aggiungi_mesi, controlla_mix, primo_del_mese
from models import db

masterplan_bp = Blueprint('masterplan', __name__)


def _err(msg, code=400):
    return jsonify({'ok': False, 'error': msg}), code


# ── pagine ────────────────────────────────────────────────────────────────
@masterplan_bp.route('/masterplan')
def index():
    return render_template('masterplan/index.html', active='masterplan', sezione='index')


@masterplan_bp.route('/masterplan/qualita-dati')
def pagina_qualita():
    return render_template('masterplan/qualita_dati.html', active='masterplan', sezione='qualita')


@masterplan_bp.route('/masterplan/famiglie')
def pagina_famiglie():
    return render_template('masterplan/famiglie.html', active='masterplan', sezione='famiglie')


@masterplan_bp.route('/masterplan/previsioni')
def pagina_previsioni():
    return render_template('masterplan/previsioni.html', active='masterplan', sezione='previsioni')


@masterplan_bp.route('/masterplan/domanda')
def pagina_domanda():
    return render_template('masterplan/domanda.html', active='masterplan', sezione='domanda')


@masterplan_bp.route('/masterplan/mrp')
def pagina_mrp():
    return render_template('masterplan/mrp.html', active='masterplan', sezione='mrp')


@masterplan_bp.route('/api/masterplan/mrp')
def api_mrp():
    return jsonify(mrp.calcola_mrp())


@masterplan_bp.route('/masterplan/impostazioni')
def pagina_impostazioni():
    return render_template('masterplan/impostazioni.html', active='masterplan', sezione='impostazioni',
                           parametri=impostazioni.PARAMETRI)


# ── API: qualità, domanda, impostazioni ───────────────────────────────────
@masterplan_bp.route('/api/masterplan/qualita')
def api_qualita():
    return jsonify(qualita.controlla())


@masterplan_bp.route('/api/masterplan/domanda')
def api_domanda():
    r = calcolo.calcola()
    r['settimane'] = [w.isoformat() for w in r['settimane']]
    for c in r['codici']:
        for x in c['righe']:
            x['settimana'] = x['settimana'].isoformat()
    return jsonify(r)


@masterplan_bp.route('/api/masterplan/impostazioni', methods=['GET', 'POST'])
def api_impostazioni():
    if request.method == 'POST':
        errori = impostazioni.salva(request.get_json(force=True) or {})
        if errori:
            return _err('; '.join(errori))
    return jsonify({'ok': True, 'valori': impostazioni.valori()})


# ── API: famiglie ─────────────────────────────────────────────────────────
def _famiglia_dict(f):
    return {'id': f.id, 'codice': f.codice, 'descrizione': f.descrizione, 'origine': f.origine,
            'somma_mix': round(sum(c.mix_pct for c in f.codici), 2),
            'codici': [{'codice': c.codice, 'mix_pct': c.mix_pct} for c in f.codici]}


@masterplan_bp.route('/api/masterplan/famiglie')
def api_famiglie():
    return jsonify([_famiglia_dict(f) for f in MpFamiglia.query.order_by(MpFamiglia.codice).all()])


@masterplan_bp.route('/api/masterplan/famiglie/proposta')
def api_famiglie_proposta():
    gia = {c.codice for c in MpFamigliaCodice.query.all()}
    out = []
    for p in storico.proponi_famiglie():
        p['codici'] = [c for c in p['codici'] if c['codice'] not in gia]
        if p['codici']:
            out.append(p)
    return jsonify(out)


@masterplan_bp.route('/api/masterplan/prodotti-finiti')
def api_prodotti_finiti():
    finiti = dati.prodotti_finiti()
    desc = dati.descrizioni(finiti)
    return jsonify([{'codice': c, 'descrizione': desc.get(c, '')} for c in sorted(finiti)])


@masterplan_bp.route('/api/masterplan/famiglie', methods=['POST'])
def api_famiglia_salva():
    """Crea o aggiorna una famiglia con i suoi codici e il mix. Il mix deve fare 100% (o la famiglia non ha codici)."""
    d = request.get_json(force=True) or {}
    codice = (d.get('codice') or '').strip().upper()
    if not codice:
        return _err('Codice famiglia obbligatorio')
    codici = {}
    for c in d.get('codici', []):
        k = (c.get('codice') or '').strip().upper()
        if k:
            try:
                codici[k] = float(c.get('mix_pct') or 0)
            except (TypeError, ValueError):
                return _err(f'Mix non valido per {k}')
    if codici:
        try:
            controlla_mix(codici)
        except MixNonValido as e:
            return _err(str(e))
    f = db.session.get(MpFamiglia, d['id']) if d.get('id') else None
    if f is None:
        if MpFamiglia.query.filter_by(codice=codice).first():
            return _err(f'Famiglia {codice} già esistente')
        f = MpFamiglia(codice=codice)
        db.session.add(f)
    f.codice, f.descrizione = codice, (d.get('descrizione') or '').strip()
    f.origine = d.get('origine') or f.origine or 'MANUALE'
    db.session.flush()
    for k in codici:                     # un codice sta in una sola famiglia
        altro = MpFamigliaCodice.query.filter_by(codice=k).first()
        if altro and altro.famiglia_id != f.id:
            db.session.rollback()
            return _err(f'{k} è già nella famiglia {altro.famiglia.codice}')
    f.codici[:] = [MpFamigliaCodice(codice=k, mix_pct=v) for k, v in sorted(codici.items())]
    db.session.commit()
    return jsonify({'ok': True, 'famiglia': _famiglia_dict(f)})


@masterplan_bp.route('/api/masterplan/famiglie/<int:fid>', methods=['DELETE'])
def api_famiglia_elimina(fid):
    f = db.session.get(MpFamiglia, fid)
    if not f:
        return _err('Famiglia inesistente', 404)
    MpPrevisione.query.filter_by(famiglia_id=fid).delete()
    db.session.delete(f)
    db.session.commit()
    return jsonify({'ok': True})


# ── API: previsioni ───────────────────────────────────────────────────────
def _chiave(livello, codice, famiglia_id):
    return f'A:{codice}' if livello == 'ARTICOLO' else f'F:{famiglia_id}'


def _prev_dict(p):
    return {'id': p.id, 'livello': p.livello, 'codice': p.codice, 'famiglia_id': p.famiglia_id,
            'famiglia': p.famiglia.codice if p.famiglia else None, 'cliente': p.cliente,
            'affidabilita': p.affidabilita, 'mese': p.mese.isoformat(), 'quantita': p.quantita,
            'origine': p.origine, 'stato': p.stato}


@masterplan_bp.route('/api/masterplan/previsioni')
def api_previsioni():
    q = MpPrevisione.query
    if request.args.get('stato'):
        q = q.filter_by(stato=request.args['stato'])
    return jsonify([_prev_dict(p) for p in q.order_by(MpPrevisione.mese, MpPrevisione.chiave_rif).all()])


@masterplan_bp.route('/api/masterplan/previsioni', methods=['POST'])
def api_previsione_salva():
    """Crea/aggiorna una cella. Ogni modifica riporta la cella a BOZZA."""
    d = request.get_json(force=True) or {}
    livello = d.get('livello', 'ARTICOLO')
    if livello not in ('ARTICOLO', 'FAMIGLIA'):
        return _err('Livello non valido')
    codice = (d.get('codice') or '').strip().upper() or None
    fid = d.get('famiglia_id')
    if livello == 'ARTICOLO' and not codice:
        return _err('Codice articolo obbligatorio')
    if livello == 'FAMIGLIA' and not (fid and db.session.get(MpFamiglia, fid)):
        return _err('Famiglia inesistente')
    aff = (d.get('affidabilita') or 'CERTA').upper()
    if aff not in AFFIDABILITA:
        return _err('Affidabilità non valida')
    try:
        mese = primo_del_mese(date.fromisoformat(str(d['mese'])[:10]))
        qta = int(d.get('quantita'))
    except (KeyError, TypeError, ValueError):
        return _err('Mese o quantità non validi')
    if qta < 0:
        return _err('La quantità non può essere negativa')
    cliente = (d.get('cliente') or '').strip()
    rif = _chiave(livello, codice, fid)
    p = MpPrevisione.query.filter_by(chiave_rif=rif, cliente=cliente, affidabilita=aff, mese=mese).first()
    if p is None:
        p = MpPrevisione(livello=livello, codice=codice if livello == 'ARTICOLO' else None,
                         famiglia_id=fid if livello == 'FAMIGLIA' else None, chiave_rif=rif,
                         cliente=cliente, affidabilita=aff, mese=mese)
        db.session.add(p)
    p.quantita, p.stato = qta, 'BOZZA'
    p.origine = d.get('origine') or p.origine or 'MANUALE'
    p.aggiornato_il = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'previsione': _prev_dict(p)})


@masterplan_bp.route('/api/masterplan/previsioni/<int:pid>', methods=['DELETE'])
def api_previsione_elimina(pid):
    p = db.session.get(MpPrevisione, pid)
    if not p:
        return _err('Previsione inesistente', 404)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'ok': True})


@masterplan_bp.route('/api/masterplan/previsioni/approva', methods=['POST'])
def api_previsioni_approva():
    """Approva le BOZZA indicate (id) o tutte. Solo le APPROVATA entrano nel calcolo."""
    ids = (request.get_json(silent=True) or {}).get('ids')
    q = MpPrevisione.query.filter_by(stato='BOZZA')
    if ids:
        q = q.filter(MpPrevisione.id.in_(ids))
    n = q.update({'stato': 'APPROVATA'}, synchronize_session=False)
    db.session.commit()
    return jsonify({'ok': True, 'approvate': n})


@masterplan_bp.route('/api/masterplan/previsioni/proposta/<path:codice>')
def api_previsioni_proposta(codice):
    return jsonify([{'mese': p['mese'].isoformat(), 'quantita': p['quantita']} for p in storico.proponi_previsioni(codice)])
