from flask import Blueprint, render_template, jsonify, request
from models import db, Terzista, LavorazioneTerzista, RigaCommessa, FaseRiga, log
from datetime import datetime, date

terzisti_bp = Blueprint('terzisti', __name__)

@terzisti_bp.route('/terzisti')
def index():
    return render_template('terzisti/index.html', active='terzisti')

@terzisti_bp.route('/api/terzisti')
def api_lista():
    return jsonify([{'id': t.id, 'nome': t.nome, 'email': t.email,
                     'telefono': t.telefono, 'tipo': t.tipo} for t in Terzista.query.order_by(Terzista.nome)])

@terzisti_bp.route('/api/terzisti', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        t = Terzista(**{k: d.get(k,'') for k in ['nome','email','telefono','tipo','note']})
        db.session.add(t)
        db.session.commit()
        return jsonify({'ok': True, 'id': t.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@terzisti_bp.route('/api/terzisti/<int:tid>', methods=['DELETE'])
def api_elimina(tid):
    try:
        t = Terzista.query.get_or_404(tid)
        db.session.delete(t)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@terzisti_bp.route('/api/lavorazioni')
def api_lavorazioni():
    oggi = date.today()
    lav = LavorazioneTerzista.query.order_by(LavorazioneTerzista.data_rientro_prev).all()
    risultati = []
    for l in lav:
        stato = l.stato
        if stato == 'ATTESA_RIENTRO' and l.data_rientro_prev:
            try:
                if datetime.strptime(l.data_rientro_prev,'%d/%m/%Y').date() < oggi: stato = 'IN_RITARDO'
            except: pass
        riga = RigaCommessa.query.get(l.riga_id)
        terzista = Terzista.query.get(l.terzista_id)
        risultati.append({'id': l.id,
            'commessa': riga.commessa.numero if riga else '?',
            'codice': riga.codice if riga else '?',
            'terzista': terzista.nome if terzista else '?',
            'fase': l.fase, 'qta': l.qta,
            'data_uscita': l.data_uscita, 'data_rientro_prev': l.data_rientro_prev,
            'data_rientro': l.data_rientro, 'stato': stato,
            'costo': l.costo, 'ddt_uscita': l.ddt_uscita, 'ddt_rientro': l.ddt_rientro, 'note': l.note})
    return jsonify(risultati)

@terzisti_bp.route('/api/lavorazioni', methods=['POST'])
def api_crea_lavorazione():
    try:
        d = request.get_json(force=True)
        l = LavorazioneTerzista(
            riga_id=int(d['riga_id']), terzista_id=int(d['terzista_id']),
            fase=d.get('fase',''), qta=int(d.get('qta',0)),
            data_uscita=d.get('data_uscita',''), data_rientro_prev=d.get('data_rientro_prev',''),
            stato='ATTESA_RIENTRO', costo=float(d.get('costo',0)),
            ddt_uscita=d.get('ddt_uscita',''), note=d.get('note',''))
        db.session.add(l)
        fa = FaseRiga.query.filter_by(riga_id=l.riga_id, fase=l.fase).first()
        if fa: fa.stato = 'esternalizzata'
        log(f'Lavorazione terzista: riga {l.riga_id} → terzista {l.terzista_id}')
        db.session.commit()
        return jsonify({'ok': True, 'id': l.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@terzisti_bp.route('/api/lavorazioni/<int:lid>/rientro', methods=['POST'])
def api_rientro(lid):
    try:
        l = LavorazioneTerzista.query.get_or_404(lid)
        d = request.get_json(force=True)
        l.data_rientro = d.get('data_rientro', datetime.utcnow().strftime('%d/%m/%Y'))
        l.ddt_rientro = d.get('ddt_rientro','')
        l.stato = 'RIENTRATA'
        l.costo = float(d.get('costo', l.costo))
        fa = FaseRiga.query.filter_by(riga_id=l.riga_id, fase=l.fase).first()
        if fa: fa.stato = d.get('stato_fase','completata')
        log(f'Rientro lavorazione terzista {lid}')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
