import os
from datetime import datetime
from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError
from models import db, OrdineProduzione, EventoConsuntivoPP, AuditPP, STATI_ORDINE_PP, prossimo_codice_ordine_pp

pp_bp = Blueprint("produzione_pp", __name__)

def _ordine(o):
    return {"id":o.id,"op_code":o.codice,"codice":o.codice,"codice_articolo":o.codice_articolo,"descrizione":o.descrizione,"cliente_commessa_esterna":o.cliente_commessa_esterna,"qta_pianificata":o.qta_pianificata,"qta_buona":o.qta_buona,"qta_scarto":o.qta_scarto,"stato":o.stato,"asa":o.asa,"data_prevista":o.data_prevista.isoformat() if o.data_prevista else None}

def _api_auth():
    token = current_app.config.get("PP_API_TOKEN", "")
    if not token: return jsonify({"ok":False,"error":"API PP disabilitata: configurare PP_API_TOKEN"}), 503
    got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.headers.get("X-PP-Token", "")
    if got != token: return jsonify({"ok":False,"error":"non autorizzato"}), 401

@pp_bp.route('/ordini-produzione')
def pagina(): return render_template('produzione_pp/index.html', active='produzione_pp', stati=STATI_ORDINE_PP)

@pp_bp.route('/api/ordini-produzione', methods=['GET'])
def lista_ui(): return jsonify([_ordine(x) for x in OrdineProduzione.query.order_by(OrdineProduzione.creato_il.desc()).all()])

@pp_bp.route('/api/ordini-produzione', methods=['POST'])
def crea():
    d=request.get_json(force=True)
    if not str(d.get('codice_articolo','')).strip(): return jsonify(ok=False,error='Codice articolo obbligatorio'),400
    try:
        o=OrdineProduzione(codice=prossimo_codice_ordine_pp(), codice_articolo=d['codice_articolo'].strip(), descrizione=d.get('descrizione',''), cliente_commessa_esterna=d.get('cliente_commessa_esterna',''), qta_pianificata=max(0,int(d.get('qta_pianificata',0))), asa=d.get('asa',''), data_prevista=datetime.strptime(d['data_prevista'],'%Y-%m-%d').date() if d.get('data_prevista') else None)
        db.session.add(o); db.session.add(AuditPP(op_code=o.codice,azione='CREATO',dettaglio='Creato da UI')); db.session.commit()
        return jsonify(ok=True,ordine=_ordine(o)),201
    except Exception as e: db.session.rollback(); return jsonify(ok=False,error=str(e)),400

@pp_bp.route('/api/ordini-produzione/<int:oid>', methods=['PUT'])
def modifica(oid):
    o=OrdineProduzione.query.get_or_404(oid); d=request.get_json(force=True)
    for k in ('codice_articolo','descrizione','cliente_commessa_esterna','asa'):
        if k in d: setattr(o,k,str(d[k]))
    if 'qta_pianificata' in d: o.qta_pianificata=max(0,int(d['qta_pianificata']))
    if 'stato' in d and d['stato'] in STATI_ORDINE_PP: o.stato=d['stato']
    db.session.add(AuditPP(op_code=o.codice,azione='MODIFICATO',dettaglio='Aggiornamento UI')); db.session.commit(); return jsonify(ok=True,ordine=_ordine(o))

@pp_bp.route('/api/ordini-produzione/<int:oid>/rilascia', methods=['POST'])
def rilascia(oid):
    o=OrdineProduzione.query.get_or_404(oid)
    if o.stato == 'Creato': o.stato='Rilasciato'; o.data_rilascio=datetime.utcnow()
    db.session.add(AuditPP(op_code=o.codice,azione='RILASCIATO',dettaglio='Rilascio UI')); db.session.commit(); return jsonify(ok=True,ordine=_ordine(o))

@pp_bp.route('/api/pp/orders', methods=['GET'])
def api_ordini_attivi():
    auth=_api_auth()
    if auth: return auth
    rows=OrdineProduzione.query.filter(OrdineProduzione.stato.in_(['Rilasciato','In esecuzione'])).order_by(OrdineProduzione.data_prevista,OrdineProduzione.codice).all()
    return jsonify({"orders":[_ordine(x) for x in rows]})

@pp_bp.route('/api/pp/events', methods=['POST'])
def api_evento():
    auth=_api_auth()
    if auth: return auth
    d=request.get_json(force=True) or {}; required=['event_id','op_code','fase','timestamp']
    if any(not str(d.get(x,'')).strip() for x in required): return jsonify(ok=False,error='Campi obbligatori: '+', '.join(required)),400
    existing=EventoConsuntivoPP.query.filter_by(event_id=d['event_id']).first()
    if existing: return jsonify(ok=True,deduplicated=True),200
    o=OrdineProduzione.query.filter_by(codice=d['op_code'].strip()).with_for_update().first()
    if not o: return jsonify(ok=False,error='OP non trovato'),404
    try: ts=datetime.fromisoformat(str(d['timestamp']).replace('Z','+00:00')).replace(tzinfo=None)
    except ValueError: return jsonify(ok=False,error='timestamp ISO-8601 non valido'),400
    try: good=max(0,int(d.get('pezzi_buoni',0))); scrap=max(0,int(d.get('pezzi_scarto',0)))
    except (ValueError,TypeError): return jsonify(ok=False,error='quantità non valida'),400
    try:
        db.session.add(EventoConsuntivoPP(event_id=d['event_id'],op_code=o.codice,fase=d['fase'],timestamp_evento=ts,pezzi_buoni=good,pezzi_scarto=scrap))
        o.qta_buona += good; o.qta_scarto += scrap
        if o.stato=='Rilasciato': o.stato='In esecuzione'
        if o.qta_pianificata and o.qta_buona >= o.qta_pianificata: o.stato='Tecnicamente completato'; o.data_completamento=datetime.utcnow()
        db.session.add(AuditPP(op_code=o.codice,event_id=d['event_id'],azione='EVENTO_CONSUNTIVO',dettaglio=f"fase={d['fase']}; buoni={good}; scarto={scrap}")); db.session.commit()
        return jsonify(ok=True,deduplicated=False,ordine=_ordine(o)),201
    except IntegrityError:
        db.session.rollback(); return jsonify(ok=True,deduplicated=True),200
