from datetime import datetime
from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError
from models import (db, OrdineProduzione, EventoConsuntivoPP, AuditPP,
                    STATI_ORDINE_PP, ASA_MASTERWORK, prossimo_codice_ordine_pp,
                    prossimo_numero_commessa, GiacenzaWood, CicloLavoroWood,
                    CentroCostoWood, VarianzaProduzioneWood)
from blueprints.magazzino.routes import (_esplodi_bom_wood, _flatten_componenti,
                    _registra_movimento_giacenza, _giacenza_residua_dopo_impegni,
                    _netta_e_esplodi_wood, _calcola_costo_standard)

pp_bp = Blueprint("produzione_pp", __name__)

def _date(value):
    if not value: return None
    return datetime.strptime(str(value), "%Y-%m-%d").date()

def _integer(value, name, minimum=0):
    try: value = int(value)
    except (TypeError, ValueError): raise ValueError(f"{name} non valido")
    if value < minimum: raise ValueError(f"{name} non può essere negativo")
    return value

def _ordine(o):
    return {"id": o.id, "op_code": o.codice, "codice": o.codice,
      "codice_articolo": o.codice_articolo, "descrizione": o.descrizione,
      "cliente": o.cliente, "commessa": o.commessa,
      "cliente_commessa_esterna": o.cliente_commessa_esterna,
      "qta_pianificata": o.qta_pianificata, "qta_buona": o.qta_buona,
      "qta_scarto": o.qta_scarto, "tempo_consuntivo_minuti": o.tempo_consuntivo_minuti,
      "stato": o.stato, "asa": o.asa, "priorita": o.priorita,
      "data_inizio": o.data_inizio.isoformat() if o.data_inizio else None,
      "data_prevista": o.data_prevista.isoformat() if o.data_prevista else None,
      "avanzamento_pct": round(100 * o.qta_buona / o.qta_pianificata, 1) if o.qta_pianificata else 0}

def _api_auth():
    token = current_app.config.get("PP_API_TOKEN", "")
    if not token: return jsonify(ok=False, error="API PP disabilitata: configurare PP_API_TOKEN"), 503
    bearer = request.headers.get("Authorization", "")
    got = bearer[7:].strip() if bearer.lower().startswith("bearer ") else request.headers.get("X-PP-Token", "")
    if got != token: return jsonify(ok=False, error="non autorizzato"), 401

def _is_carpenteria(asa): return (asa or "").strip().casefold() == ASA_MASTERWORK.casefold()

def _audit(o, action, detail="", event_id=""):
    db.session.add(AuditPP(op_code=o.codice, event_id=event_id, azione=action, dettaglio=detail))

@pp_bp.get('/ordini-produzione')
def pagina(): return render_template('produzione_pp/index.html', active='produzione_pp', stati=STATI_ORDINE_PP)

@pp_bp.get('/ordini-produzione/avanzamento')
def avanzamento(): return render_template('produzione_pp/avanzamento.html', active='produzione_pp')

@pp_bp.get('/api/ordini-produzione')
def lista_ui():
    return jsonify([_ordine(x) for x in OrdineProduzione.query.order_by(OrdineProduzione.creato_il.desc()).all()])

@pp_bp.post('/api/ordini-produzione')
def crea():
    d = request.get_json(silent=True) or {}
    article = str(d.get('codice_articolo', '')).strip()
    if not article: return jsonify(ok=False, error='Codice articolo obbligatorio'), 400
    try:
        qty = _integer(d.get('qta_pianificata', 0), 'Quantità pianificata')
        priority = _integer(d.get('priorita', 5), 'Priorità', 1)
        if priority > 9: raise ValueError('Priorità deve essere fra 1 e 9')
        client = str(d.get('cliente','')).strip()
        job = prossimo_numero_commessa()  # generato in automatico, ignora eventuale valore inviato dal client
        legacy = str(d.get('cliente_commessa_esterna','')).strip() or ' / '.join(x for x in (client, job) if x)
        o = OrdineProduzione(codice=prossimo_codice_ordine_pp(), codice_articolo=article,
            descrizione=str(d.get('descrizione','')).strip(), cliente=client, commessa=job,
            cliente_commessa_esterna=legacy, qta_pianificata=qty, asa=str(d.get('asa','')).strip(),
            priorita=priority, data_inizio=_date(d.get('data_inizio')), data_prevista=_date(d.get('data_prevista')))
        db.session.add(o); _audit(o, 'CREATO', 'Creato da pagina Ordini Produzione'); db.session.commit()

        # Controllo scorta: giacenza/impegnato/disponibile/mancante per questo OP
        # appena creato, rispetto agli OP già aperti (l'OP appena creato è in
        # stato "Creato", quindi NON impegna ancora nulla — è solo un'anteprima).
        controllo_scorta = None
        try:
            giacenza_iniziale = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
            giacenza_residua = _giacenza_residua_dopo_impegni()
            disponibile_prima = dict(giacenza_residua)
            righe_netting = {}
            _netta_e_esplodi_wood(article, qty, giacenza_residua, righe_netting)
            controllo_scorta = []
            for cod, r in righe_netting.items():
                giacenza_tot = giacenza_iniziale.get(cod, 0.0)
                disponibile = disponibile_prima.get(cod, 0.0)
                controllo_scorta.append({
                    'codice': cod, 'giacenza': round(giacenza_tot, 3),
                    'gia_impegnato': round(max(giacenza_tot - disponibile, 0), 3),
                    'disponibile': round(max(disponibile, 0), 3),
                    'fabbisogno_nuovo_op': round(r['fabbisogno'], 3),
                    'mancante': round(r['mancante'], 3),
                })
            controllo_scorta.sort(key=lambda x: (-x['mancante'], x['codice']))
        except Exception:
            controllo_scorta = None  # non deve mai bloccare la creazione dell'OP

        return jsonify(ok=True, ordine=_ordine(o), controllo_scorta=controllo_scorta), 201
    except (ValueError, IntegrityError) as exc:
        db.session.rollback(); return jsonify(ok=False, error=str(exc)), 400

@pp_bp.put('/api/ordini-produzione/<int:oid>')
def modifica(oid):
    o = OrdineProduzione.query.get_or_404(oid); d = request.get_json(silent=True) or {}
    if o.stato not in ('Creato', 'Rilasciato'): return jsonify(ok=False, error='OP non modificabile nello stato corrente'), 409
    try:
        for k in ('codice_articolo','descrizione','cliente','asa'):
            if k in d: setattr(o, k, str(d[k]).strip())
        if 'qta_pianificata' in d: o.qta_pianificata = _integer(d['qta_pianificata'], 'Quantità pianificata')
        if 'priorita' in d: o.priorita = _integer(d['priorita'], 'Priorità', 1)
        for k in ('data_inizio','data_prevista'):
            if k in d: setattr(o, k, _date(d[k]))
        o.cliente_commessa_esterna = ' / '.join(x for x in (o.cliente, o.commessa) if x)
        _audit(o, 'MODIFICATO', 'Aggiornamento OP'); db.session.commit()
        return jsonify(ok=True, ordine=_ordine(o))
    except ValueError as exc: db.session.rollback(); return jsonify(ok=False, error=str(exc)), 400

@pp_bp.delete('/api/ordini-produzione/<int:oid>')
def elimina(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Creato':
        return jsonify(ok=False, error='Solo un OP ancora "Creato" (non rilasciato) può essere eliminato'), 409
    codice = o.codice
    _audit(o, 'ELIMINATO', 'Eliminazione manuale da pagina Ordini Produzione')
    db.session.delete(o); db.session.commit()
    return jsonify(ok=True, codice=codice)

@pp_bp.post('/api/ordini-produzione/<int:oid>/rilascia')
def rilascia(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Creato': return jsonify(ok=False, error='Solo un OP Creato può essere rilasciato'), 409
    o.stato, o.data_rilascio = 'Rilasciato', datetime.utcnow(); _audit(o, 'RILASCIATO', 'Rilascio manuale')
    db.session.commit(); return jsonify(ok=True, ordine=_ordine(o))

@pp_bp.post('/api/ordini-produzione/<int:oid>/chiudi-co')
def chiudi_co(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Tecnicamente completato': return jsonify(ok=False, error='Chiusura CO possibile solo dopo completamento tecnico'), 409
    o.stato, o.data_chiusura_co = 'Chiuso CO', datetime.utcnow(); _audit(o, 'CHIUSO_CO', 'Chiusura CO manuale')
    db.session.commit(); return jsonify(ok=True, ordine=_ordine(o))

@pp_bp.get('/api/pp/orders')
def api_ordini_attivi():
    auth = _api_auth()
    if auth: return auth
    rows = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(['Rilasciato','In esecuzione'])).all()
    rows = [o for o in rows if _is_carpenteria(o.asa)]
    return jsonify(orders=[_ordine(o) for o in sorted(rows, key=lambda o: (o.data_prevista is None, o.data_prevista, o.priorita, o.codice))])

@pp_bp.post('/api/pp/events')
def api_evento():
    auth = _api_auth()
    if auth: return auth
    d = request.get_json(silent=True) or {}
    required = ('event_id', 'op_code', 'fase', 'timestamp')
    if any(not str(d.get(k, '')).strip() for k in required): return jsonify(ok=False, error='Campi obbligatori: '+', '.join(required)), 400
    if EventoConsuntivoPP.query.filter_by(event_id=str(d['event_id']).strip()).first(): return jsonify(ok=True, deduplicated=True), 200
    try:
        ts = datetime.fromisoformat(str(d['timestamp']).replace('Z', '+00:00')).replace(tzinfo=None)
        good = _integer(d.get('pezzi_buoni', 0), 'Pezzi buoni'); scrap = _integer(d.get('pezzi_scarto', 0), 'Scarto')
        tempo = _integer(d.get('tempo_minuti', d.get('tempo', 0)), 'Tempo')
        o = OrdineProduzione.query.filter_by(codice=str(d['op_code']).strip()).with_for_update().first()
        if not o: return jsonify(ok=False, error='OP non trovato'), 404
        if not _is_carpenteria(o.asa) or o.stato not in ('Rilasciato','In esecuzione'):
            return jsonify(ok=False, error='OP non attivo o non disponibile per Carpenteria Propria'), 409
        db.session.add(EventoConsuntivoPP(event_id=str(d['event_id']).strip(), op_code=o.codice, fase=str(d['fase']).strip(), timestamp_evento=ts, pezzi_buoni=good, pezzi_scarto=scrap, tempo_minuti=tempo))
        o.qta_buona += good; o.qta_scarto += scrap; o.tempo_consuntivo_minuti += tempo
        if o.stato == 'Rilasciato': o.stato = 'In esecuzione'
        if o.qta_pianificata and o.qta_buona >= o.qta_pianificata:
            o.stato, o.data_completamento = 'Tecnicamente completato', datetime.utcnow()
        _audit(o, 'EVENTO_CONSUNTIVO', f'fase={d["fase"]}; buoni={good}; scarto={scrap}; minuti={tempo}', str(d['event_id']).strip())

        # Scarico automatico giacenza Iron Wood in proporzione ai pezzi buoni
        # appena consuntivati: esplode l'intera distinta (nessun netting qui,
        # solo la quantità reale consumata) e scarica ogni componente toccato.
        # Poi carica il PRODOTTO FINITO stesso a magazzino, valorizzato al suo
        # costo standard (materiali + lavorazione + overhead), e registra la
        # varianza di lavorazione (tempo reale vs standard) quando la fase
        # dell'evento è abbinabile per nome a un reparto del suo Ciclo di Lavoro.
        if good > 0:
            try:
                componenti = _esplodi_bom_wood(o.codice_articolo, qta=good)
                consumi = {}
                _flatten_componenti(componenti, consumi)
                for cod, qta_consumata in consumi.items():
                    if qta_consumata:
                        _registra_movimento_giacenza(cod, -qta_consumata, 'scarico_produzione',
                                                      riferimento=o.codice, note=f'Consuntivo {good} pz buoni')
            except Exception:
                pass  # lo scarico giacenza non deve mai bloccare la registrazione del consuntivo

            try:
                costo = _calcola_costo_standard(o.codice_articolo)
                _registra_movimento_giacenza(o.codice_articolo, good, 'carico_produzione',
                                              riferimento=o.codice, note=f'Consuntivo {good} pz buoni',
                                              costo_unitario=costo['costo_totale'])
            except Exception:
                pass  # il carico a magazzino non deve mai bloccare la registrazione del consuntivo

            try:
                fase_nome = str(d['fase']).strip()
                riga_ciclo = (CicloLavoroWood.query.filter_by(codice=o.codice_articolo)
                              .join(CicloLavoroWood.centro_costo)
                              .filter(db.func.lower(CentroCostoWood.nome) == fase_nome.lower())
                              .first())
                if riga_ciclo and riga_ciclo.produttivita_oraria:
                    tempo_standard_min = (good / riga_ciclo.produttivita_oraria) * 60
                    costo_orario = riga_ciclo.centro_costo.costo_orario or 0
                    costo_standard_lav = (tempo_standard_min / 60) * costo_orario
                    costo_reale_lav = (tempo / 60) * costo_orario
                    db.session.add(VarianzaProduzioneWood(
                        op_code=o.codice, codice_articolo=o.codice_articolo, fase=fase_nome, quantita=good,
                        tempo_standard_minuti=round(tempo_standard_min, 2), tempo_reale_minuti=tempo,
                        costo_standard_lavorazione=round(costo_standard_lav, 4),
                        costo_reale_lavorazione=round(costo_reale_lav, 4),
                        varianza=round(costo_reale_lav - costo_standard_lav, 4),
                    ))
            except Exception:
                pass  # nessuna varianza registrabile (fase non abbinabile) non deve bloccare il consuntivo

        db.session.commit(); return jsonify(ok=True, deduplicated=False, ordine=_ordine(o)), 201
    except ValueError as exc: return jsonify(ok=False, error=str(exc)), 400
    except IntegrityError:
        db.session.rollback(); return jsonify(ok=True, deduplicated=True), 200
