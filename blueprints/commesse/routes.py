from flask import Blueprint, render_template, jsonify, request
from models import db, Commessa, RigaCommessa, FaseRiga, FASI_DISPONIBILI, log, commessa_to_dict
from datetime import datetime

commesse_bp = Blueprint('commesse', __name__)

@commesse_bp.route('/commesse')
def index():
    return render_template('commesse/index.html', active='commesse')

@commesse_bp.route('/api/commesse')
def api_lista():
    stati = request.args.getlist('stato')
    q = Commessa.query
    if stati:
        q = q.filter(Commessa.stato.in_(stati))
    else:
        q = q.filter(Commessa.stato.notin_(['ANNULLATA']))
    commesse = q.order_by(Commessa.priorita, Commessa.data_consegna).all()
    return jsonify([commessa_to_dict(c) for c in commesse])

@commesse_bp.route('/api/commesse', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        numero = d.get('numero','').strip()
        if not numero: return jsonify({'ok': False, 'error': 'Numero obbligatorio'}), 400
        if Commessa.query.filter_by(numero=numero).first():
            return jsonify({'ok': False, 'error': f'Commessa {numero} già esiste'}), 400
        c = Commessa(numero=numero,
                     cliente_nome=d.get('cliente_nome','').strip(),
                     data_ordine=d.get('data_ordine',''),
                     data_consegna=d.get('data_consegna',''),
                     priorita=int(d.get('priorita',5)),
                     stato=d.get('stato','APERTA'),
                     note=d.get('note',''),
                     ref_masterlogistic=d.get('ref_ml',''))
        db.session.add(c)
        db.session.flush()
        for r_data in d.get('righe', []):
            codice = r_data.get('codice','').strip()
            if not codice: continue
            r = RigaCommessa(commessa_id=c.id, codice=codice,
                             descrizione=r_data.get('descrizione',''),
                             qta_totale=int(r_data.get('qta_totale',0)),
                             qta_prodotta=int(r_data.get('qta_prodotta',0)),
                             note=r_data.get('note',''))
            db.session.add(r)
            db.session.flush()
            for nome_fase in r_data.get('fasi', FASI_DISPONIBILI):
                db.session.add(FaseRiga(riga_id=r.id, fase=nome_fase,
                                        stato=r_data.get(f'fase_{nome_fase}','da_fare')))
        log(f'Commessa {numero} creata')
        db.session.commit()
        return jsonify({'ok': True, 'id': c.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@commesse_bp.route('/api/commesse/<int:cid>', methods=['PUT'])
def api_aggiorna(cid):
    try:
        c = Commessa.query.get_or_404(cid)
        d = request.get_json(force=True)
        for campo in ['cliente_nome','data_consegna','data_ordine','stato','note','ref_masterlogistic']:
            if campo in d: setattr(c, campo, d[campo])
        if 'priorita' in d: c.priorita = int(d['priorita'])
        c.aggiornato_il = datetime.utcnow()
        log(f'Commessa {c.numero} aggiornata')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@commesse_bp.route('/api/commesse/<int:cid>', methods=['DELETE'])
def api_elimina(cid):
    try:
        c = Commessa.query.get_or_404(cid)
        numero = c.numero
        db.session.delete(c)
        log(f'Commessa {numero} eliminata')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@commesse_bp.route('/api/commesse/<int:cid>/righe', methods=['POST'])
def api_aggiungi_riga(cid):
    try:
        c = Commessa.query.get_or_404(cid)
        d = request.get_json(force=True)
        codice = d.get('codice','').strip()
        if not codice: return jsonify({'ok': False, 'error': 'Codice obbligatorio'}), 400
        r = RigaCommessa(commessa_id=cid, codice=codice,
                         descrizione=d.get('descrizione',''),
                         qta_totale=int(d.get('qta_totale',0)))
        db.session.add(r)
        db.session.flush()
        for nome_fase in d.get('fasi', FASI_DISPONIBILI):
            db.session.add(FaseRiga(riga_id=r.id, fase=nome_fase,
                                    stato=d.get(f'stato_{nome_fase}','da_fare')))
        c.aggiornato_il = datetime.utcnow()
        log(f'Riga {codice} aggiunta a commessa {c.numero}')
        db.session.commit()
        return jsonify({'ok': True, 'riga_id': r.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@commesse_bp.route('/api/righe/<int:rid>', methods=['DELETE'])
def api_elimina_riga(rid):
    try:
        r = RigaCommessa.query.get_or_404(rid)
        comm = r.commessa
        db.session.delete(r)
        comm.aggiornato_il = datetime.utcnow()
        log(f'Riga {r.codice} eliminata da {comm.numero}')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@commesse_bp.route('/api/import_commessa_rapida', methods=['POST'])
def api_import_rapida():
    try:
        d = request.get_json(force=True)
        numero = d.get('numero','').strip()
        codice = d.get('codice','').strip()
        if not numero or not codice:
            return jsonify({'ok': False, 'error': 'Numero e codice obbligatori'}), 400
        c = Commessa.query.filter_by(numero=numero).first()
        if not c:
            c = Commessa(numero=numero, cliente_nome=d.get('cliente',''),
                         data_consegna=d.get('data_consegna',''),
                         priorita=int(d.get('priorita',5)), stato='APERTA')
            db.session.add(c)
            db.session.flush()
        fasi_stati = d.get('fasi_stati', {})
        r = RigaCommessa(commessa_id=c.id, codice=codice,
                         descrizione=d.get('descrizione',''),
                         qta_totale=int(d.get('qta',0)))
        db.session.add(r)
        db.session.flush()
        for nome_fase in FASI_DISPONIBILI:
            db.session.add(FaseRiga(riga_id=r.id, fase=nome_fase,
                                    stato=fasi_stati.get(nome_fase,'da_fare')))
        c.aggiornato_il = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'commessa_id': c.id, 'riga_id': r.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
