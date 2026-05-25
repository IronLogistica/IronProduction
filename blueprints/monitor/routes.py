from flask import Blueprint, render_template, jsonify, request
from models import db, RigaMonitor, log

monitor_bp = Blueprint('monitor', __name__)

SEZIONI_MAP = {
    'lavorazione': ('🚧 IN LAVORAZIONE', 'lavorazione-hdr'),
    'da_iniziare': ('✅ DA INIZIARE LAVORAZIONI', 'da_iniziare-hdr'),
    'in_attesa':   ('🛬 IN ATTESA — Componenti / MP / Sospesi', 'in_attesa-hdr'),
    'in_saldatura':('👨‍🏭 IN SALDATURA', 'in_saldatura-hdr'),
    'postazione':  ('🔜 POSTAZIONE DA ASSEGNARE / LAVORAZIONI DA FINIRE', 'postazione-hdr'),
    'terminati':   ('✅ APPENA TERMINATI', 'terminati-hdr'),
}

ICONE = {'done':'✅','wip':'🚧','todo':'🔜','na':'✗','nd':'·'}

def riga_to_dict(r):
    return {
        'id': r.id, 'sezione': r.sezione, 'comm_id': r.comm_id,
        'codice': r.codice, 'descrizione': r.descrizione,
        'totale': r.totale, 'saldo': r.saldo, 'pct': r.pct,
        'priority': r.priority or '',
        'taglio': r.taglio or 'nd', 'sgola': r.sgola or 'nd',
        'piega': r.piega or 'nd', 'saldatura': r.saldatura or 'nd',
    }

@monitor_bp.route('/monitor')
def index():
    righe_per_sezione = {}
    for sezione in SEZIONI_MAP:
        righe_per_sezione[sezione] = RigaMonitor.query\
            .filter_by(sezione=sezione)\
            .order_by(RigaMonitor.ordine).all()
    return render_template('monitor/index.html',
        active='monitor',
        active_page='monitor',
        topbar_title='📊 Monitor Segatrice',
        topbar_badge='Monitor Attivo',
        sezioni_map=SEZIONI_MAP,
        righe_per_sezione=righe_per_sezione,
        icone=ICONE)

# ── API REST ─────────────────────────────────────────────────────────────────
@monitor_bp.route('/api/monitor')
def api_lista():
    righe = RigaMonitor.query.order_by(RigaMonitor.sezione, RigaMonitor.ordine).all()
    result = {s: [] for s in SEZIONI_MAP}
    for r in righe:
        if r.sezione in result:
            result[r.sezione].append(riga_to_dict(r))
    return jsonify(result)

@monitor_bp.route('/api/monitor', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        r = RigaMonitor(
            sezione=d.get('sezione','da_iniziare'),
            comm_id=d.get('comm_id',''), codice=d.get('codice',''),
            descrizione=d.get('descrizione',''),
            totale=int(d.get('totale',0)), saldo=int(d.get('saldo',0)),
            pct=int(d.get('pct',0)), priority=d.get('priority',''),
            taglio=d.get('taglio','nd'), sgola=d.get('sgola','nd'),
            piega=d.get('piega','nd'), saldatura=d.get('saldatura','nd'),
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
        if 'sezione' in d: r.sezione = d['sezione']
        if 'ordine' in d: r.ordine = d['ordine']
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
        fase = d.get('fase','')
        stato = d.get('stato','nd')
        if fase in ('taglio','sgola','piega','saldatura'):
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
