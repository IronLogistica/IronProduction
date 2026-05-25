from flask import Blueprint, render_template, jsonify, request
from models import db, MaterialePrimo, MovimentoMagazzino, log
from datetime import datetime

magazzino_bp = Blueprint('magazzino', __name__)

@magazzino_bp.route('/magazzino')
def index():
    return render_template('magazzino/index.html', active='magazzino')

@magazzino_bp.route('/api/materiali')
def api_lista():
    materiali = MaterialePrimo.query.order_by(MaterialePrimo.codice).all()
    return jsonify([{'id': m.id, 'codice': m.codice, 'descrizione': m.descrizione,
                     'unita_misura': m.unita_misura, 'stock': m.stock,
                     'scorta_min': m.scorta_min, 'fornitore': m.fornitore,
                     'stato': m.stato} for m in materiali])

@magazzino_bp.route('/api/materiali', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        m = MaterialePrimo(codice=d.get('codice','').strip(),
                           descrizione=d.get('descrizione',''),
                           unita_misura=d.get('unita_misura','pz'),
                           stock=float(d.get('stock',0)),
                           scorta_min=float(d.get('scorta_min',0)),
                           fornitore=d.get('fornitore',''), note=d.get('note',''))
        db.session.add(m)
        db.session.commit()
        return jsonify({'ok': True, 'id': m.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@magazzino_bp.route('/api/materiali/<int:mid>', methods=['PUT'])
def api_aggiorna(mid):
    try:
        m = MaterialePrimo.query.get_or_404(mid)
        d = request.get_json(force=True)
        for campo in ['codice','descrizione','unita_misura','fornitore','note']:
            if campo in d: setattr(m, campo, d[campo])
        if 'stock' in d: m.stock = float(d['stock'])
        if 'scorta_min' in d: m.scorta_min = float(d['scorta_min'])
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@magazzino_bp.route('/api/materiali/<int:mid>', methods=['DELETE'])
def api_elimina(mid):
    try:
        m = MaterialePrimo.query.get_or_404(mid)
        db.session.delete(m)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@magazzino_bp.route('/api/materiali/<int:mid>/movimento', methods=['POST'])
def api_movimento(mid):
    try:
        m = MaterialePrimo.query.get_or_404(mid)
        d = request.get_json(force=True)
        tipo = d.get('tipo','carico')
        qta = float(d.get('qta',0))
        m.stock = m.stock + qta if tipo == 'carico' else max(0, m.stock - qta)
        mov = MovimentoMagazzino(materiale_id=mid, tipo=tipo, qta=qta,
                                 causale=d.get('causale',''), commessa_ref=d.get('commessa_ref',''),
                                 note=d.get('note',''))
        db.session.add(mov)
        log(f'Magazzino: {tipo} {qta} {m.codice} (stock→{m.stock})')
        db.session.commit()
        return jsonify({'ok': True, 'stock': m.stock})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@magazzino_bp.route('/api/materiali/<int:mid>/movimenti')
def api_movimenti(mid):
    movs = MovimentoMagazzino.query.filter_by(materiale_id=mid).order_by(MovimentoMagazzino.data_mov.desc()).limit(20).all()
    return jsonify([{'id': v.id, 'tipo': v.tipo, 'qta': v.qta,
                     'causale': v.causale, 'commessa_ref': v.commessa_ref,
                     'data_mov': v.data_mov.strftime('%d/%m/%Y %H:%M')} for v in movs])
