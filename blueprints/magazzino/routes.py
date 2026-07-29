from flask import Blueprint, render_template, jsonify, request
from models import db, RigaMonitor, log
from datetime import datetime

monitor_bp = Blueprint('monitor', __name__)

# ── Mappa sezioni SEGATRICE ───────────────────────────────────────────────────
SEZIONI_SEGATRICE = {
    'lavorazione': ('🚧 IN LAVORAZIONE', 'lavorazione-hdr'),
    'da_iniziare': ('✅ DA INIZIARE LAVORAZIONI', 'da_iniziare-hdr'),
    'in_attesa':   ('🛬 IN ATTESA — Componenti / MP / Sospesi', 'in_attesa-hdr'),
    'in_saldatura':('👨‍🏭 IN SALDATURA', 'in_saldatura-hdr'),
    'postazione':  ('🔜 POSTAZIONE DA ASSEGNARE / LAVORAZIONI DA FINIRE', 'postazione-hdr'),
    'terminati':   ('✅ APPENA TERMINATI', 'terminati-hdr'),
}

# ── Mappa sezioni TRAPANI ─────────────────────────────────────────────────────
# Ogni trapano ha le sue 3 sezioni, prefissate con trap1_ e trap2_
SEZIONI_TRAPANO = {
    'trap1_lavorazione': ('🔩 TRAPANO 1 — In Lavorazione',    'trap1-lav-hdr'),
    'trap1_in_attesa':   ('🛬 TRAPANO 1 — In Attesa',         'trap1-att-hdr'),
    'trap1_terminati':   ('✅ TRAPANO 1 — Appena Terminati',  'trap1-ter-hdr'),
    'trap2_lavorazione': ('🔩 TRAPANO 2 — In Lavorazione',    'trap2-lav-hdr'),
    'trap2_in_attesa':   ('🛬 TRAPANO 2 — In Attesa',         'trap2-att-hdr'),
    'trap2_terminati':   ('✅ TRAPANO 2 — Appena Terminati',  'trap2-ter-hdr'),
}

# Tutte le sezioni valide (per validazione)
TUTTE_SEZIONI = list(SEZIONI_SEGATRICE.keys()) + list(SEZIONI_TRAPANO.keys())

ICONE = {'done': '✅', 'wip': '🚧', 'todo': '🔜', 'na': '✗', 'nd': '·'}


def riga_to_dict(r):
    return {
        'id': r.id, 'sezione': r.sezione, 'comm_id': r.comm_id,
        'codice': r.codice, 'descrizione': r.descrizione,
        'totale': r.totale, 'saldo': r.saldo, 'pct': r.pct,
        'priority': r.priority or '',
        'taglio':   r.taglio   or 'nd',
        'sgola':    r.sgola    or 'nd',
        'piega':    r.piega    or 'nd',
        'saldatura':r.saldatura or 'nd',
    }


# ── ROUTE: Monitor Segatrice ──────────────────────────────────────────────────
@monitor_bp.route('/monitor')
def index():
    righe_per_sezione = {}
    for sezione in SEZIONI_SEGATRICE:
        righe_per_sezione[sezione] = RigaMonitor.query\
            .filter_by(sezione=sezione)\
            .order_by(RigaMonitor.ordine).all()
    return render_template('monitor/index.html',
        active='monitor',
        active_page='monitor',
        topbar_title='📊 Monitor Segatrice',
        topbar_badge='Monitor Attivo',
        sezioni_map=SEZIONI_SEGATRICE,
        righe_per_sezione=righe_per_sezione,
        icone=ICONE,
        macchina='segatrice',
        now=datetime.now().strftime('%d/%m/%Y'))


# ── ROUTE: Monitor Trapani ────────────────────────────────────────────────────
@monitor_bp.route('/monitor/trapani')
def index_trapani():
    righe_per_sezione = {}
    for sezione in SEZIONI_TRAPANO:
        righe_per_sezione[sezione] = RigaMonitor.query\
            .filter_by(sezione=sezione)\
            .order_by(RigaMonitor.ordine).all()
    return render_template('monitor/trapani.html',
        active='monitor_trapani',
        active_page='monitor_trapani',
        topbar_title='🔩 Monitor Trapani',
        topbar_badge='Trapani Attivi',
        sezioni_map=SEZIONI_TRAPANO,
        righe_per_sezione=righe_per_sezione,
        icone=ICONE,
        macchina='trapani',
        now=datetime.now().strftime('%d/%m/%Y'))


# ══════════════════════════════════════════════════════════════════════════════
#  API REST — compatibili con entrambi i monitor
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/api/monitor')
def api_lista():
    sezione_filter = request.args.get('macchina', None)
    righe = RigaMonitor.query.order_by(RigaMonitor.sezione, RigaMonitor.ordine).all()
    # Costruisce dict con tutte le sezioni della macchina richiesta
    if sezione_filter == 'trapani':
        result = {s: [] for s in SEZIONI_TRAPANO}
    elif sezione_filter == 'segatrice':
        result = {s: [] for s in SEZIONI_SEGATRICE}
    else:
        result = {s: [] for s in TUTTE_SEZIONI}
    for r in righe:
        if r.sezione in result:
            result[r.sezione].append(riga_to_dict(r))
    return jsonify(result)


@monitor_bp.route('/api/monitor', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        sezione = d.get('sezione', 'da_iniziare')
        if sezione not in TUTTE_SEZIONI:
            return jsonify({'ok': False, 'error': f'Sezione non valida: {sezione}'}), 400
        r = RigaMonitor(
            sezione=sezione,
            comm_id=d.get('comm_id', ''), codice=d.get('codice', ''),
            descrizione=d.get('descrizione', ''),
            totale=int(d.get('totale', 0)), saldo=int(d.get('saldo', 0)),
            pct=int(d.get('pct', 0)), priority=d.get('priority', ''),
            taglio=d.get('taglio', 'nd'), sgola=d.get('sgola', 'nd'),
            piega=d.get('piega', 'nd'), saldatura=d.get('saldatura', 'nd'),
        )
        db.session.add(r)
        log(f'Monitor: aggiunta riga {r.codice} in {r.sezione}')
        db.session.commit()
        return jsonify({'ok': True, 'id': r.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@monitor_bp.route('/api/monitor/<int:rid>', methods=['PUT'])
def api_sposta(rid):
    try:
        r = RigaMonitor.query.get_or_404(rid)
        d = request.get_json(force=True)
        if 'sezione' in d:
            if d['sezione'] not in TUTTE_SEZIONI:
                return jsonify({'ok': False, 'error': 'Sezione non valida'}), 400
            r.sezione = d['sezione']
        if 'ordine' in d:
            r.ordine = d['ordine']
        log(f'Monitor: spostata riga {r.codice} → {r.sezione}')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@monitor_bp.route('/api/monitor/<int:rid>/fase', methods=['PATCH'])
def api_fase(rid):
    try:
        r = RigaMonitor.query.get_or_404(rid)
        d = request.get_json(force=True)
        fase  = d.get('fase', '')
        stato = d.get('stato', 'nd')
        if fase in ('taglio', 'sgola', 'piega', 'saldatura'):
            setattr(r, fase, stato)
        else:
            return jsonify({'ok': False, 'error': f'Fase sconosciuta: {fase}'}), 400
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@monitor_bp.route('/api/monitor/<int:rid>', methods=['DELETE'])
def api_elimina(rid):
    try:
        r = RigaMonitor.query.get_or_404(rid)
        log(f'Monitor: eliminata riga {r.codice}')
        db.session.delete(r)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
