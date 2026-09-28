"""
CONTO LAVORO — modulo AUTONOMO per ordini cliente in conto lavorazione
(saldatura attrezzature agricole, montaggio stelle ranghinatrici) con beni
e componenti di proprietà del CLIENTE.

Regole di isolamento che valgono per tutto il modulo:
  - NON importa e NON chiama mai: _registra_movimento_giacenza /
    GiacenzaWood / Kanban / OrdineProduzione / _registra_evento_consuntivo /
    MovimentoContabileWood / masterlogistic_client / masterledgerlight_client /
    requests. La merce del cliente non entra MAI nella giacenza Iron Wood.
  - Le tabelle del modulo vivono nello schema Postgres 'conto_lavoro'
    (bind 'conto_lavoro'): le crea solo migra.py, mai db.create_all() all'avvio.
  - Feature flag CL_ENABLED: spento, il blueprint non viene nemmeno
    registrato (app.py) e il cancello qui sotto risponde comunque 404.
  - Nessun login (decisione di Mauri, 28/09/2026: si usa dal computer del
    capo, entrando dal pulsante di IronProduction). Ogni scrittura resta
    comunque tracciata in cl_audit (solo inserimento), intestata a 'capo'.
"""
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import db
from blueprints.conto_lavoro.importa_articoli import analizza_file, crea_modello_xlsx
from blueprints.conto_lavoro.models import (ClAudit, ClArticoloCliente, ClCliente, ClSchemaVersion,
                                            TRACCIAMENTI, UDM)

VERSIONE_MODULO = 'anagrafiche'
VERSIONE_SCHEMA_RICHIESTA = 1
UTENTE_AUDIT = 'capo'

cl_bp = Blueprint('conto_lavoro', __name__, url_prefix='/conto-lavoro')


def modulo_abilitato():
    return bool(current_app.config.get('CL_ENABLED'))


def versione_schema():
    """Versione dello schema conto_lavoro presente nel database (0 = tabelle non ancora create)."""
    try:
        return db.session.query(db.func.max(ClSchemaVersion.versione)).scalar() or 0
    except SQLAlchemyError:
        db.session.rollback()
        return 0


@cl_bp.before_request
def _cancello_modulo():
    if not modulo_abilitato():
        abort(404)
    # Le API che leggono/scrivono dati richiedono lo schema già creato.
    if request.path.startswith('/conto-lavoro/api/') and request.endpoint != 'conto_lavoro.api_stato':
        if versione_schema() < VERSIONE_SCHEMA_RICHIESTA:
            return jsonify(ok=False, error='Tabelle del Conto lavoro non ancora create: impostare '
                                           'CL_MIGRA_AUTO=0001 su Railway e riavviare.'), 503


def _audit(azione, entita, entita_id='', prima=None, dopo=None):
    db.session.add(ClAudit(azione=azione, entita=entita, entita_id=str(entita_id or ''),
                           prima=prima, dopo={**(dopo or {}), '_utente': UTENTE_AUDIT},
                           ip=(request.headers.get('X-Forwarded-For', request.remote_addr or '') or '')[:45]))


def _testo(d, chiave, massimo, obbligatorio=False):
    v = str(d.get(chiave) or '').strip()
    if obbligatorio and not v:
        raise ValueError(f'Campo obbligatorio: {chiave}')
    if len(v) > massimo:
        raise ValueError(f'{chiave}: massimo {massimo} caratteri')
    return v


def _cliente_dict(c):
    return {'id': c.id, 'ragione_sociale': c.ragione_sociale, 'piva': c.piva or '',
            'codice_fiscale': c.codice_fiscale or '', 'indirizzo': c.indirizzo, 'cap': c.cap,
            'citta': c.citta, 'provincia': c.provincia, 'nazione': c.nazione, 'attivo': c.attivo}


def _articolo_dict(a):
    return {'id': a.id, 'cliente_id': a.cliente_id, 'codice': a.codice, 'descrizione': a.descrizione,
            'udm': a.udm, 'tracciamento': a.tracciamento, 'attivo': a.attivo}


# ── Pagine ────────────────────────────────────────────────────────────────────
@cl_bp.get('/')
def pagina_indice():
    return render_template('conto_lavoro/index.html', active='conto_lavoro', versione=VERSIONE_MODULO,
                           schema=versione_schema(), schema_richiesto=VERSIONE_SCHEMA_RICHIESTA)


@cl_bp.get('/anagrafiche')
def pagina_anagrafiche():
    return render_template('conto_lavoro/anagrafiche.html', active='conto_lavoro', udm=UDM,
                           tracciamenti=TRACCIAMENTI, schema=versione_schema())


@cl_bp.get('/api/stato')
def api_stato():
    v = versione_schema()
    return jsonify(ok=True, modulo='conto_lavoro', versione=VERSIONE_MODULO, abilitato=True,
                   schema=v, schema_pronto=v >= VERSIONE_SCHEMA_RICHIESTA)


# ── Clienti ───────────────────────────────────────────────────────────────────
CAMPI_CLIENTE = (('ragione_sociale', 200, True), ('piva', 20, False), ('codice_fiscale', 20, False),
                 ('indirizzo', 200, False), ('cap', 10, False), ('citta', 100, False),
                 ('provincia', 5, False), ('nazione', 2, False))


@cl_bp.get('/api/clienti')
def api_clienti():
    q = ClCliente.query
    if request.args.get('tutti') != '1':
        q = q.filter(ClCliente.attivo.is_(True))
    return jsonify([_cliente_dict(c) for c in q.order_by(ClCliente.ragione_sociale).all()])


def _applica_cliente(c, d):
    for campo, massimo, obbl in CAMPI_CLIENTE:
        if campo in d or obbl:
            valore = _testo(d, campo, massimo, obbl)
            if campo == 'piva':
                valore = valore.replace(' ', '').upper() or None
            elif campo == 'codice_fiscale':
                valore = valore.upper() or None
            elif campo in ('provincia', 'nazione'):
                valore = valore.upper() or ('IT' if campo == 'nazione' else '')
            setattr(c, campo, valore)
    if 'attivo' in d:
        c.attivo = bool(d.get('attivo'))
    if c.piva:
        doppione = ClCliente.query.filter(ClCliente.piva == c.piva, ClCliente.id != c.id).first() \
            if c.id else ClCliente.query.filter(ClCliente.piva == c.piva).first()
        if doppione:
            raise IntegrityError('piva duplicata', None, None)


@cl_bp.post('/api/clienti')
def api_crea_cliente():
    d = request.get_json(silent=True) or {}
    try:
        c = ClCliente()
        _applica_cliente(c, d)
        db.session.add(c)
        db.session.flush()
        _audit('CLIENTE_CREATO', 'cl_cliente', c.id, dopo=_cliente_dict(c))
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Esiste già un cliente con questa Partita IVA.'), 409
    return jsonify(ok=True, cliente=_cliente_dict(c)), 201


@cl_bp.put('/api/clienti/<int:cid>')
def api_modifica_cliente(cid):
    c = db.session.get(ClCliente, cid) or abort(404)
    d = request.get_json(silent=True) or {}
    prima = _cliente_dict(c)
    try:
        _applica_cliente(c, {**prima, **d})
        _audit('CLIENTE_MODIFICATO', 'cl_cliente', c.id, prima=prima, dopo=_cliente_dict(c))
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Esiste già un cliente con questa Partita IVA.'), 409
    return jsonify(ok=True, cliente=_cliente_dict(c))


# ── Articoli del cliente ──────────────────────────────────────────────────────
@cl_bp.get('/api/articoli')
def api_articoli():
    q = ClArticoloCliente.query
    cid = request.args.get('cliente_id', type=int)
    if cid:
        q = q.filter(ClArticoloCliente.cliente_id == cid)
    if request.args.get('tutti') != '1':
        q = q.filter(ClArticoloCliente.attivo.is_(True))
    return jsonify([_articolo_dict(a) for a in q.order_by(ClArticoloCliente.codice).all()])


def _applica_articolo(a, d, nuovo):
    if nuovo:
        cid = d.get('cliente_id')
        if not isinstance(cid, int) or not db.session.get(ClCliente, cid):
            raise ValueError('Cliente non valido')
        a.cliente_id = cid
    a.codice = _testo(d, 'codice', 100, True).upper()
    a.descrizione = _testo(d, 'descrizione', 300)
    udm = _testo(d, 'udm', 10, True).upper()
    if udm not in UDM:
        raise ValueError(f'Unità di misura non valida (ammesse: {", ".join(UDM)})')
    a.udm = udm
    tracc = (_testo(d, 'tracciamento', 10) or 'LOTTO').upper()
    if tracc not in TRACCIAMENTI:
        raise ValueError(f'Tracciamento non valido (ammessi: {", ".join(TRACCIAMENTI)})')
    a.tracciamento = tracc
    if 'attivo' in d:
        a.attivo = bool(d.get('attivo'))


@cl_bp.post('/api/articoli')
def api_crea_articolo():
    d = request.get_json(silent=True) or {}
    try:
        a = ClArticoloCliente()
        _applica_articolo(a, d, nuovo=True)
        db.session.add(a)
        db.session.flush()
        _audit('ARTICOLO_CREATO', 'cl_articolo_cliente', a.id, dopo=_articolo_dict(a))
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Questo cliente ha già un articolo con questo codice.'), 409
    return jsonify(ok=True, articolo=_articolo_dict(a)), 201


@cl_bp.put('/api/articoli/<int:aid>')
def api_modifica_articolo(aid):
    a = db.session.get(ClArticoloCliente, aid) or abort(404)
    d = request.get_json(silent=True) or {}
    prima = _articolo_dict(a)
    try:
        _applica_articolo(a, {**prima, **d}, nuovo=False)
        _audit('ARTICOLO_MODIFICATO', 'cl_articolo_cliente', a.id, prima=prima, dopo=_articolo_dict(a))
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Questo cliente ha già un articolo con questo codice.'), 409
    return jsonify(ok=True, articolo=_articolo_dict(a))


# ── Import articoli da Excel/CSV (anteprima → conferma, come il resto del programma) ──
@cl_bp.get('/api/articoli/modello.xlsx')
def api_modello_articoli():
    return Response(crea_modello_xlsx(),
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=modello_articoli_conto_lavoro.xlsx'})


@cl_bp.post('/api/articoli/importa')
def api_importa_articoli():
    """
    Carica gli articoli di UN cliente da file. Senza conferma=true: SOLO
    anteprima (nuovi / aggiornati / scartati), nessuna scrittura. Con
    conferma=true: stesso file ricaricato, applica in UNA transazione.
    Un codice già esistente per quel cliente viene aggiornato (descrizione
    se presente nel file, UdM, tracciamento); mai toccati gli articoli
    degli altri clienti.
    """
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(ok=False, error='Nessun file selezionato.'), 400
    cliente_id = request.form.get('cliente_id', type=int)
    cliente = db.session.get(ClCliente, cliente_id) if cliente_id else None
    if not cliente:
        return jsonify(ok=False, error='Scegli prima il cliente.'), 400
    conferma = (request.form.get('conferma') or '').lower() == 'true'
    raw = f.read()
    if len(raw) > 10 * 1024 * 1024:
        return jsonify(ok=False, error='File troppo grande (massimo 10 MB).'), 400
    try:
        righe, scartate, formato = analizza_file(raw, f.filename)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    if not righe:
        return jsonify(ok=False, error='Nessuna riga valida nel file.', scartate=scartate[:50]), 400

    esistenti = {a.codice: a for a in ClArticoloCliente.query.filter(
        ClArticoloCliente.cliente_id == cliente.id,
        ClArticoloCliente.codice.in_([r['codice'] for r in righe])).all()}
    nuovi = [r for r in righe if r['codice'] not in esistenti]
    aggiornati = [r for r in righe if r['codice'] in esistenti]

    if not conferma:
        return jsonify(ok=True, anteprima=True, formato=formato, cliente=cliente.ragione_sociale,
                       totale=len(righe), nuovi=len(nuovi), aggiornati=len(aggiornati),
                       scartate=scartate[:50], n_scartate=len(scartate), esempio=righe[:8])

    try:
        for r in nuovi:
            db.session.add(ClArticoloCliente(cliente_id=cliente.id, codice=r['codice'], descrizione=r['descrizione'],
                                             udm=r['udm'], tracciamento=r['tracciamento'], attivo=True))
        for r in aggiornati:
            a = esistenti[r['codice']]
            if r['descrizione']:
                a.descrizione = r['descrizione']
            a.udm = r['udm']
            a.tracciamento = r['tracciamento']
        _audit('ARTICOLI_IMPORTATI', 'cl_cliente', cliente.id, dopo={
            'file': f.filename[:200], 'formato': formato, 'nuovi': [r['codice'] for r in nuovi],
            'aggiornati': [r['codice'] for r in aggiornati], 'scartate': len(scartate)})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Conflitto sui codici: qualcuno li ha modificati nel frattempo, ripeti l\'anteprima.'), 409
    return jsonify(ok=True, anteprima=False, nuovi=len(nuovi), aggiornati=len(aggiornati), n_scartate=len(scartate))
