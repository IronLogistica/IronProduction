"""
CONTO LAVORO — modulo AUTONOMO per ordini cliente in conto lavorazione
(saldatura attrezzature agricole, montaggio stelle ranghinatrici) con beni
e componenti di proprietà del CLIENTE.

Regole di isolamento che valgono per tutto il modulo:
  - NON importa e NON chiama mai: _registra_movimento_giacenza /
    GiacenzaWood / Kanban / _registra_evento_consuntivo /
    MovimentoContabileWood / masterlogistic_client / masterledgerlight_client /
    requests. La merce del cliente non entra MAI nella giacenza Iron Wood.
  - UNICA ECCEZIONE (decisione di Mauri, 29/09/2026): la quantità "Prodotta"
    di una riga ordine si legge in SOLA LETTURA da OrdineProduzione.qta_buona,
    filtrando per codice_articolo == codice dell'articolo cliente — MasterWork
    traduce il proprio codice in un codice_articolo IronProduction tramite
    MappaCodiceMasterWork, e per il conto lavoro quel codice_articolo È lo
    stesso codice dell'ordine cliente (nessuna distinta base in ingresso, il
    capo apre l'OP direttamente con quel codice). Nessuna scrittura, nessuna
    creazione OP, nessun'altra funzione di produzione_pp viene mai chiamata
    da qui: solo una query di lettura su OrdineProduzione per mostrare
    "quanto è già stato dichiarato prodotto" nel dettaglio ordine.
  - Le tabelle del modulo vivono nello schema Postgres 'conto_lavoro'
    (bind 'conto_lavoro'): le crea solo migra.py, mai db.create_all() all'avvio.
  - Feature flag CL_ENABLED: spento, il blueprint non viene nemmeno
    registrato (app.py) e il cancello qui sotto risponde comunque 404.
  - Nessun login (decisione di Mauri, 28/09/2026: si usa dal computer del
    capo, entrando dal pulsante di IronProduction). Ogni scrittura resta
    comunque tracciata in cl_audit (solo inserimento), intestata a 'capo'.
"""
import base64
import io
from datetime import datetime

import PyPDF2
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import db, OrdineProduzione  # sola lettura, vedi eccezione documentata sopra
from blueprints.conto_lavoro.importa_articoli import analizza_file, crea_modello_xlsx
from blueprints.conto_lavoro.importa_ordine import estrai_dati_ordine
from blueprints.conto_lavoro.models import (ClAudit, ClArticoloCliente, ClCliente, ClDdt, ClFattura,
                                            ClFotoArticolo, ClImpostazione, ClLotto, ClMovimento, ClOrdine,
                                            ClOrdineRiga, ClSchemaVersion, TRACCIAMENTI, UDM, _adesso)

VERSIONE_MODULO = 'ordini'
VERSIONE_SCHEMA_BASE = 1      # clienti/articoli — invariato dalla 0001, non deve regredire
VERSIONE_SCHEMA_ORDINI = 2    # ordini, fatture, impostazioni/triple watch — dalla 0002
VERSIONE_SCHEMA_LIVE = 3      # foto articolo + monitor LIVE — dalla 0003
VERSIONE_SCHEMA_RICHIESTA = VERSIONE_SCHEMA_BASE  # compatibilità: usato dal cancello e dal tile "Clienti e articoli"
PREFISSI_SCHEMA_ORDINI = ('/conto-lavoro/api/ordini', '/conto-lavoro/api/impostazioni', '/conto-lavoro/api/fatture',
                          '/conto-lavoro/api/magazzino')
# Endpoint (non prefisso di path: l'id articolo sta in mezzo, es.
# /api/articoli/12/foto) che richiedono la 0003 — il resto di /api/articoli/
# (anagrafica base) resta alla 0001 e non deve regredire.
ENDPOINT_SCHEMA_LIVE = {'conto_lavoro.api_foto_articolo_carica', 'conto_lavoro.api_foto_articolo_leggi',
                        'conto_lavoro.api_foto_articolo_elimina', 'conto_lavoro.api_live'}
MAX_FOTO_BYTES = 5 * 1024 * 1024
TIPI_FOTO_AMMESSI = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}
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
    # Le API che leggono/scrivono dati richiedono lo schema già creato — Ordini/
    # fatture/impostazioni richiedono la 0002, il resto si ferma alla 0001 (non
    # deve regredire una funzione già in uso solo perché non è ancora stata
    # applicata la migrazione successiva).
    if request.path.startswith('/conto-lavoro/api/') and request.endpoint != 'conto_lavoro.api_stato':
        if request.endpoint in ENDPOINT_SCHEMA_LIVE:
            richiesta = VERSIONE_SCHEMA_LIVE
        elif request.path.startswith(PREFISSI_SCHEMA_ORDINI):
            richiesta = VERSIONE_SCHEMA_ORDINI
        else:
            richiesta = VERSIONE_SCHEMA_BASE
        if versione_schema() < richiesta:
            return jsonify(ok=False, error=f'Tabelle del Conto lavoro non ancora alla versione {richiesta}: '
                                           f'impostare CL_MIGRA_AUTO={richiesta:04d} su Railway e riavviare.'), 503


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


def _articolo_dict(a, con_foto=None):
    d = {'id': a.id, 'cliente_id': a.cliente_id, 'codice': a.codice, 'descrizione': a.descrizione,
        'udm': a.udm, 'tracciamento': a.tracciamento, 'attivo': a.attivo}
    if con_foto is not None:
        d['ha_foto'] = a.id in con_foto
    return d


# ── Pagine ────────────────────────────────────────────────────────────────────
@cl_bp.get('/')
def pagina_indice():
    return render_template('conto_lavoro/index.html', active='conto_lavoro', versione=VERSIONE_MODULO,
                           schema=versione_schema(), schema_richiesto=VERSIONE_SCHEMA_BASE,
                           schema_richiesto_ordini=VERSIONE_SCHEMA_ORDINI,
                           schema_richiesto_live=VERSIONE_SCHEMA_LIVE)


@cl_bp.get('/anagrafiche')
def pagina_anagrafiche():
    return render_template('conto_lavoro/anagrafiche.html', active='conto_lavoro', udm=UDM,
                           tracciamenti=TRACCIAMENTI, schema=versione_schema())


@cl_bp.get('/ordini')
def pagina_ordini():
    return render_template('conto_lavoro/ordini.html', active='conto_lavoro',
                           schema=versione_schema(), schema_richiesto=VERSIONE_SCHEMA_ORDINI)


@cl_bp.get('/riepilogo')
def pagina_riepilogo():
    """Riepilogo ordini — stessa vista d'insieme del monitor LIVE (ordini
    CONFERMATO raggruppati per cliente, con Ordinata/Prodotta/Evasa/Saldo),
    ma come pagina normale con sidebar/topbar per consultazione da PC
    (indicazione di Mauri, 29/09/2026), non per un monitor/totem d'officina.
    Usa la stessa /api/live del monitor LIVE — un solo posto che calcola
    questi numeri."""
    return render_template('conto_lavoro/riepilogo.html', active='conto_lavoro',
                           schema=versione_schema(), schema_richiesto=VERSIONE_SCHEMA_LIVE)


@cl_bp.get('/magazzino')
def pagina_magazzino():
    """Magazzino Conto lavoro — TUTTI i codici articolo dei clienti passano
    da qui (indicazione di Mauri, 29/09/2026), raggruppati per cliente:
    Giacenza (dai movimenti cl_movimento — 0 finché DDT ricezione/consumi
    non sono costruiti, la tabella esiste dalla 0001 ma nessuno ci scrive
    ancora) affiancata a Ordinata/Prodotta/Evasa/Saldo per dare un quadro
    unico, non solo i codici con un ordine confermato."""
    return render_template('conto_lavoro/magazzino.html', active='conto_lavoro',
                           schema=versione_schema(), schema_richiesto=VERSIONE_SCHEMA_ORDINI)


@cl_bp.get('/live')
def pagina_live():
    """Monitor LIVE Conto lavoro — stile del monitor MasterWork (Saldatura,
    ecc.): niente sidebar/topbar, pensato per un totem/monitor d'officina.
    URL a parte apposta (indicazione di Mauri, 29/09/2026): dalla dashboard
    del conto lavoro c'è solo il collegamento, questa pagina vive per conto
    suo. Nessun login (stessa scelta di tutto il modulo)."""
    return render_template('conto_lavoro/live.html', schema=versione_schema(),
                           schema_richiesto=VERSIONE_SCHEMA_LIVE)


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
    articoli = q.order_by(ClArticoloCliente.codice).all()
    # "ha_foto" richiede la 0003: se non ancora applicata, la tabella non
    # esiste — non deve rompere questa API, che resta valida dalla 0001.
    con_foto = set()
    if versione_schema() >= VERSIONE_SCHEMA_LIVE and articoli:
        con_foto = {aid for (aid,) in db.session.query(ClFotoArticolo.articolo_id)
                   .filter(ClFotoArticolo.articolo_id.in_([a.id for a in articoli])).all()}
    return jsonify([_articolo_dict(a, con_foto=con_foto) for a in articoli])


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


# ── Foto di riferimento articolo (riquadro immagine del monitor LIVE) ──────
# Una sola foto corrente per articolo (uq_cl_foto_articolo_articolo): un
# nuovo caricamento sostituisce il precedente, niente galleria da gestire.
@cl_bp.post('/api/articoli/<int:aid>/foto')
def api_foto_articolo_carica(aid):
    a = db.session.get(ClArticoloCliente, aid) or abort(404)
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(ok=False, error='Nessun file selezionato.'), 400
    raw = f.read()
    if len(raw) > MAX_FOTO_BYTES:
        return jsonify(ok=False, error=f'File troppo grande (massimo {MAX_FOTO_BYTES // (1024*1024)} MB).'), 400
    tipo = (f.mimetype or '').lower()
    if tipo not in TIPI_FOTO_AMMESSI:
        return jsonify(ok=False, error='Formato non ammesso — solo JPG, PNG o WEBP.'), 400
    foto = ClFotoArticolo.query.filter_by(articolo_id=aid).first()
    if not foto:
        foto = ClFotoArticolo(articolo_id=aid)
        db.session.add(foto)
    foto.nome_file = f.filename[:255]
    foto.tipo_mime = tipo
    foto.contenuto_base64 = base64.b64encode(raw).decode('ascii')
    foto.caricato_il = _adesso()
    _audit('FOTO_ARTICOLO_CARICATA', 'cl_articolo_cliente', aid, dopo={'codice': a.codice, 'file': f.filename[:200]})
    db.session.commit()
    return jsonify(ok=True)


@cl_bp.get('/api/articoli/<int:aid>/foto')
def api_foto_articolo_leggi(aid):
    foto = ClFotoArticolo.query.filter_by(articolo_id=aid).first()
    if not foto:
        abort(404)
    return Response(base64.b64decode(foto.contenuto_base64), mimetype=foto.tipo_mime)


@cl_bp.delete('/api/articoli/<int:aid>/foto')
def api_foto_articolo_elimina(aid):
    foto = ClFotoArticolo.query.filter_by(articolo_id=aid).first()
    if not foto:
        return jsonify(ok=True)
    db.session.delete(foto)
    _audit('FOTO_ARTICOLO_ELIMINATA', 'cl_articolo_cliente', aid)
    db.session.commit()
    return jsonify(ok=True)


# ── Ordini cliente (import PDF) ────────────────────────────────────────────
# Regola (Mauri, 29/09/2026): 1) l'ordine cliente importato in PDF genera il
# codice di magazzino (cl_articolo_cliente) se non esiste già; 2) l'operaio
# dichiara la produzione in MasterWork; 3) la produzione dichiarata viene
# riversata qui come prodotto finito; 4) il DDT di consegna lo scarica. Non
# gestiamo distinta base in ingresso: solo codice/descrizione/UdM come
# scritti sull'ordine. I passi 2-3 (dichiarazione MasterWork -> riversamento)
# sono un'integrazione cross-app a parte, non ancora costruita qui.
def _data_ordine(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%d/%m/%Y').date()
    except ValueError:
        return None


def _quantita_prodotta(codice_articolo):
    """SOLA LETTURA su OrdineProduzione — vedi eccezione documentata in cima
    al file. Somma qta_buona di tutti gli OP con quel codice_articolo,
    qualunque sia lo stato: una volta dichiarata prodotta, la quantità resta
    valida anche se l'OP viene poi chiuso/archiviato."""
    if not codice_articolo:
        return 0.0
    tot = (db.session.query(db.func.coalesce(db.func.sum(OrdineProduzione.qta_buona), 0))
           .filter(OrdineProduzione.codice_articolo == codice_articolo).scalar())
    return float(tot or 0)


def _ordine_dict(o, con_righe=False):
    d = {'id': o.id, 'cliente_id': o.cliente_id, 'cliente': o.cliente.ragione_sociale if o.cliente else '',
        'numero_ordine': o.numero_ordine, 'rif_cliente': o.rif_cliente,
        'data_documento': o.data_documento.strftime('%d/%m/%Y') if o.data_documento else '',
        'stato': o.stato, 'filename': o.filename,
        'creato_il': o.creato_il.strftime('%d/%m/%Y %H:%M') if o.creato_il else ''}
    if con_righe:
        righe = []
        for r in o.righe:
            codice = r.articolo.codice if r.articolo else ''
            ordinata = float(r.quantita)
            prodotta = _quantita_prodotta(codice)
            # Evasa (consegnata con DDT di uscita) resta 0 finché quel modulo
            # non esiste — vedi cl_ddt.ordine_id, predisposto ma non ancora
            # popolato da nessuna scrittura reale.
            evasa = 0.0
            righe.append({'id': r.id, 'n_riga': r.n_riga, 'codice': codice,
                          'descrizione': r.descrizione, 'quantita': ordinata,
                          'quantita_prodotta': prodotta, 'quantita_evasa': evasa,
                          'saldo_da_produrre': round(ordinata - prodotta, 3),
                          'prezzo_unitario': float(r.prezzo_unitario) if r.prezzo_unitario is not None else None})
        d['righe'] = righe
    return d


@cl_bp.get('/api/ordini')
def api_ordini():
    q = ClOrdine.query
    cid = request.args.get('cliente_id', type=int)
    if cid:
        q = q.filter(ClOrdine.cliente_id == cid)
    return jsonify([_ordine_dict(o) for o in q.order_by(ClOrdine.creato_il.desc()).limit(200).all()])


@cl_bp.get('/api/ordini/<int:oid>')
def api_ordine_dettaglio(oid):
    o = db.session.get(ClOrdine, oid) or abort(404)
    return jsonify(_ordine_dict(o, con_righe=True))


@cl_bp.post('/api/ordini/importa')
def api_importa_ordine():
    """
    Carica l'ORDINE CLIENTE da PDF. Senza conferma=true: SOLO anteprima
    (nessuna scrittura) — mostra il cliente riconosciuto (per Partita IVA),
    numero ordine, riferimento cliente ("Rif. n. cliente"), data e righe,
    segnalando quali codici sono NUOVI (non ancora tra i codici di quel
    cliente). Con conferma=true: stesso file ricaricato + cliente_id scelto/
    confermato dall'operatore, crea l'ordine (stato BOZZA) e i codici
    mancanti in UNA transazione.
    """
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        return jsonify(ok=False, error='Carica un file PDF valido.'), 400
    raw = f.read()
    if len(raw) > 15 * 1024 * 1024:
        return jsonify(ok=False, error='File troppo grande (massimo 15 MB).'), 400
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(raw))
        testo = "\n".join(page.extract_text() or '' for page in reader.pages)
    except Exception as e:
        return jsonify(ok=False, error=f'Impossibile leggere il PDF: {e}'), 400

    dati = estrai_dati_ordine(testo)
    if not dati['righe']:
        return jsonify(ok=False, error='Nessuna riga articolo riconosciuta in questo PDF — formato non previsto.'), 400

    conferma = (request.form.get('conferma') or '').lower() == 'true'
    cliente_id = request.form.get('cliente_id', type=int)
    cliente = db.session.get(ClCliente, cliente_id) if cliente_id else None
    if not cliente and dati['cliente_piva']:
        cliente = ClCliente.query.filter(ClCliente.piva == dati['cliente_piva']).first()

    if not conferma:
        esistenti = set()
        if cliente:
            esistenti = {a.codice for a in ClArticoloCliente.query.filter(
                ClArticoloCliente.cliente_id == cliente.id,
                ClArticoloCliente.codice.in_([r['codice'] for r in dati['righe']])).all()}
        righe_anteprima = [{**r, 'nuovo': r['codice'] not in esistenti} for r in dati['righe']]
        return jsonify(ok=True, anteprima=True, cliente_trovato=bool(cliente),
                       cliente_id=cliente.id if cliente else None,
                       cliente_nome=cliente.ragione_sociale if cliente else dati['cliente_nome'],
                       cliente_piva_pdf=dati['cliente_piva'], numero_ordine=dati['numero_ordine'],
                       rif_cliente=dati['rif_cliente'], data_documento=dati['data_documento'],
                       righe=righe_anteprima, n_nuovi=sum(1 for r in righe_anteprima if r['nuovo']))

    if not cliente:
        return jsonify(ok=False, error='Scegli il cliente prima di confermare.'), 400
    if not dati['numero_ordine']:
        return jsonify(ok=False, error='Numero ordine non riconosciuto sul PDF — impossibile salvare.'), 400

    try:
        esistenti = {a.codice: a for a in ClArticoloCliente.query.filter(
            ClArticoloCliente.cliente_id == cliente.id,
            ClArticoloCliente.codice.in_([r['codice'] for r in dati['righe']])).all()}
        creati = []
        for r in dati['righe']:
            if r['codice'] not in esistenti:
                nuovo = ClArticoloCliente(cliente_id=cliente.id, codice=r['codice'], descrizione=r['descrizione'],
                                          udm=r['udm'] if r['udm'] in UDM else 'PZ', tracciamento='LOTTO', attivo=True)
                db.session.add(nuovo)
                db.session.flush()
                esistenti[r['codice']] = nuovo
                creati.append(r['codice'])

        o = ClOrdine(cliente_id=cliente.id, numero_ordine=dati['numero_ordine'], rif_cliente=dati['rif_cliente'],
                     data_documento=_data_ordine(dati['data_documento']), stato='BOZZA',
                     filename=f.filename[:255], testo_grezzo_pdf=testo)
        db.session.add(o)
        db.session.flush()
        for n, r in enumerate(dati['righe'], start=1):
            db.session.add(ClOrdineRiga(ordine_id=o.id, n_riga=n, articolo_id=esistenti[r['codice']].id,
                                        descrizione=r['descrizione'], quantita=r['quantita'] or 0,
                                        prezzo_unitario=r['prezzo_unitario']))
        _audit('ORDINE_IMPORTATO', 'cl_ordine', o.id, dopo={'file': f.filename[:200], 'numero_ordine': o.numero_ordine,
                                                            'righe': len(dati['righe']), 'codici_creati': creati})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Esiste già un ordine con questo numero per questo cliente.'), 409
    return jsonify(ok=True, anteprima=False, ordine=_ordine_dict(o, con_righe=True))


@cl_bp.put('/api/ordini/<int:oid>/conferma')
def api_conferma_ordine(oid):
    """Chiude la revisione: da BOZZA a CONFERMATO. Non muove nessuna
    giacenza (il conto lavoro non ha una giacenza propria qui, solo
    tracciabilità lotti) — serve solo a bloccare l'ordine dopo il controllo
    umano della lettura PDF."""
    o = db.session.get(ClOrdine, oid) or abort(404)
    if o.stato != 'BOZZA':
        return jsonify(ok=False, error='Solo un ordine in BOZZA può essere confermato.'), 409
    o.stato = 'CONFERMATO'
    o.confermato_il = _adesso()
    _audit('ORDINE_CONFERMATO', 'cl_ordine', o.id)
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o))


@cl_bp.put('/api/ordini/<int:oid>/righe/<int:rid>')
def api_modifica_riga_ordine(oid, rid):
    """Corregge la quantità di una riga letta male dal PDF — solo finché
    l'ordine è in BOZZA (confermato, la riga è bloccata come l'ordine)."""
    o = db.session.get(ClOrdine, oid) or abort(404)
    r = next((x for x in o.righe if x.id == rid), None) or abort(404)
    if o.stato != 'BOZZA':
        return jsonify(ok=False, error='Solo le righe di una bozza si possono modificare.'), 409
    d = request.get_json(silent=True) or {}
    try:
        nuova_qta = float(d.get('quantita'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Quantità non valida.'), 400
    if nuova_qta < 0:
        return jsonify(ok=False, error='La quantità non può essere negativa.'), 400
    prima = {'quantita': float(r.quantita)}
    r.quantita = nuova_qta
    _audit('ORDINE_RIGA_MODIFICATA', 'cl_ordine_riga', r.id, prima=prima, dopo={'quantita': nuova_qta})
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True))


@cl_bp.delete('/api/ordini/<int:oid>/righe/<int:rid>')
def api_elimina_riga_ordine(oid, rid):
    """Toglie una riga letta per errore dal PDF — solo in BOZZA, e solo se
    non è l'ultima (un ordine senza righe non ha senso: si elimina l'ordine)."""
    o = db.session.get(ClOrdine, oid) or abort(404)
    r = next((x for x in o.righe if x.id == rid), None) or abort(404)
    if o.stato != 'BOZZA':
        return jsonify(ok=False, error='Solo le righe di una bozza si possono eliminare.'), 409
    if len(o.righe) <= 1:
        return jsonify(ok=False, error='Un ordine deve avere almeno una riga — elimina l\'intera bozza, se serve.'), 409
    _audit('ORDINE_RIGA_ELIMINATA', 'cl_ordine_riga', r.id, prima={'codice': r.articolo.codice if r.articolo else '',
                                                                    'quantita': float(r.quantita)})
    db.session.delete(r)
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True))


@cl_bp.delete('/api/ordini/<int:oid>')
def api_elimina_ordine(oid):
    """Elimina un ordine SOLO se ancora in BOZZA (letto male dal PDF, o
    caricato per errore) — un ordine CONFERMATO non si tocca più da qui."""
    o = db.session.get(ClOrdine, oid) or abort(404)
    if o.stato != 'BOZZA':
        return jsonify(ok=False, error='Solo una bozza può essere eliminata.'), 409
    _audit('ORDINE_ELIMINATO', 'cl_ordine', o.id, prima=_ordine_dict(o, con_righe=True))
    db.session.delete(o)
    db.session.commit()
    return jsonify(ok=True)


# ── Triple Watch: Ordine <-> DDT di uscita <-> Fattura ─────────────────────
# Predisposto ora su richiesta di Mauri, NON attivo di default. Quando
# acceso mostra il pulsante di caricamento fattura e l'elenco degli ordini
# per cui il triplo abbinamento è completo. Il collegamento vero e proprio
# (cl_ddt.ordine_id / cl_ddt.fattura_id) resta vuoto finché il modulo DDT di
# uscita — non ancora costruito — non lo valorizza.
def _triple_watch_attivo():
    imp = db.session.get(ClImpostazione, 'triple_watch_attivo')
    return bool(imp and imp.valore == 'on')


@cl_bp.get('/api/impostazioni')
def api_impostazioni():
    return jsonify(ok=True, triple_watch_attivo=_triple_watch_attivo())


@cl_bp.post('/api/impostazioni/triple-watch')
def api_toggle_triple_watch():
    d = request.get_json(silent=True) or {}
    acceso = bool(d.get('attivo'))
    imp = db.session.get(ClImpostazione, 'triple_watch_attivo')
    valore_prima = imp.valore if imp else 'off'
    if not imp:
        imp = ClImpostazione(chiave='triple_watch_attivo')
        db.session.add(imp)
    imp.valore = 'on' if acceso else 'off'
    imp.aggiornato_il = _adesso()
    _audit('TRIPLE_WATCH_' + ('ATTIVATO' if acceso else 'DISATTIVATO'), 'cl_impostazione', 'triple_watch_attivo',
          prima={'valore': valore_prima}, dopo={'valore': imp.valore})
    db.session.commit()
    return jsonify(ok=True, triple_watch_attivo=acceso)


def _fattura_dict(f):
    return {'id': f.id, 'cliente_id': f.cliente_id, 'cliente': f.cliente.ragione_sociale if f.cliente else '',
           'numero': f.numero, 'data_documento': f.data_documento.strftime('%d/%m/%Y') if f.data_documento else '',
           'importo': float(f.importo) if f.importo is not None else None}


@cl_bp.get('/api/fatture')
def api_fatture():
    if not _triple_watch_attivo():
        return jsonify(ok=False, error='Triple Watch non attivo.'), 409
    return jsonify([_fattura_dict(x) for x in ClFattura.query.order_by(ClFattura.creato_il.desc()).limit(200).all()])


@cl_bp.post('/api/fatture')
def api_crea_fattura():
    """Caricamento (per ora manuale: numero/data/importo) di una fattura —
    solo a Triple Watch attivo. Il collegamento automatico a Ordine + DDT di
    uscita arriva col modulo DDT di uscita; per ora la fattura viene solo
    registrata, pronta per essere agganciata."""
    if not _triple_watch_attivo():
        return jsonify(ok=False, error='Triple Watch non attivo.'), 409
    d = request.get_json(silent=True) or {}
    cliente = db.session.get(ClCliente, d.get('cliente_id')) if d.get('cliente_id') else None
    if not cliente:
        return jsonify(ok=False, error='Cliente non valido.'), 400
    try:
        numero = _testo(d, 'numero', 50, True)
        importo = float(d['importo']) if d.get('importo') not in (None, '') else None
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except (TypeError, KeyError):
        return jsonify(ok=False, error='Importo non valido.'), 400
    try:
        fatt = ClFattura(cliente_id=cliente.id, numero=numero,
                         data_documento=_data_ordine(d.get('data_documento') or ''), importo=importo)
        db.session.add(fatt)
        db.session.flush()
        _audit('FATTURA_CREATA', 'cl_fattura', fatt.id, dopo=_fattura_dict(fatt))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Esiste già una fattura con questo numero per questo cliente.'), 409
    return jsonify(ok=True, fattura=_fattura_dict(fatt)), 201


@cl_bp.get('/api/triple-watch')
def api_triple_watch():
    """Elenco ordini col triplo abbinamento Ordine <-> DDT di uscita <->
    Fattura completo — resta vuoto finché il modulo DDT di uscita non
    valorizza cl_ddt.ordine_id / cl_ddt.fattura_id."""
    if not _triple_watch_attivo():
        return jsonify(ok=False, error='Triple Watch non attivo.'), 409
    righe = (db.session.query(ClOrdine, ClDdt, ClFattura)
            .join(ClDdt, ClDdt.ordine_id == ClOrdine.id)
            .join(ClFattura, ClDdt.fattura_id == ClFattura.id)
            .filter(ClDdt.direzione == 'OUT').all())
    return jsonify([{'numero_ordine_cliente': o.numero_ordine, 'numero_ddt_spedizione': d.numero,
                     'numero_fattura': f.numero} for o, d, f in righe])


# ── Monitor LIVE Conto lavoro ───────────────────────────────────────────────
@cl_bp.get('/api/live')
def api_live():
    """
    Dati per il monitor LIVE (pagina /conto-lavoro/live): ordini CONFERMATO
    raggruppati per cliente, con Ordinata/Prodotta/Evasa/Saldo per riga e
    l'indicazione se l'articolo ha una foto di riferimento caricata. Solo
    lettura — stessa eccezione documentata in cima al file per "Prodotta".
    """
    ordini = (ClOrdine.query.filter(ClOrdine.stato == 'CONFERMATO')
             .order_by(ClOrdine.cliente_id, ClOrdine.numero_ordine).all())
    if not ordini:
        return jsonify([])
    articolo_ids = {r.articolo_id for o in ordini for r in o.righe}
    con_foto = {aid for (aid,) in db.session.query(ClFotoArticolo.articolo_id)
               .filter(ClFotoArticolo.articolo_id.in_(articolo_ids)).all()} if articolo_ids else set()

    gruppi = {}
    for o in ordini:
        g = gruppi.setdefault(o.cliente_id, {'cliente_id': o.cliente_id,
                                             'cliente': o.cliente.ragione_sociale if o.cliente else '', 'ordini': []})
        righe = []
        for r in o.righe:
            codice = r.articolo.codice if r.articolo else ''
            ordinata = float(r.quantita)
            prodotta = _quantita_prodotta(codice)
            saldo = round(ordinata - prodotta, 3)
            pct = round(min(100, prodotta / ordinata * 100)) if ordinata > 0 else 100
            righe.append({'articolo_id': r.articolo_id, 'codice': codice, 'descrizione': r.descrizione,
                          'quantita': ordinata, 'quantita_prodotta': prodotta, 'quantita_evasa': 0.0,
                          'saldo_da_produrre': saldo, 'pct': pct, 'ha_foto': r.articolo_id in con_foto})
        g['ordini'].append({'id': o.id, 'numero_ordine': o.numero_ordine, 'rif_cliente': o.rif_cliente, 'righe': righe})
    return jsonify(sorted(gruppi.values(), key=lambda g: g['cliente']))


# ── Magazzino Conto lavoro ──────────────────────────────────────────────────
@cl_bp.get('/api/magazzino')
def api_magazzino():
    """
    TUTTI i codici articolo attivi dei clienti conto lavoro, raggruppati per
    cliente — non solo quelli con un ordine confermato (a differenza di
    /api/live). Per ciascuno: Giacenza (somma cl_movimento per i suoi lotti
    — 0 finché DDT ricezione/consumi non scrivono ancora movimenti, la
    tabella esiste dalla 0001), più Ordinata/Prodotta/Evasa/Saldo sommati
    su tutti i suoi ordini CONFERMATO (0 se non ne ha).
    """
    articoli = (ClArticoloCliente.query.filter(ClArticoloCliente.attivo.is_(True))
               .order_by(ClArticoloCliente.codice).all())
    if not articoli:
        return jsonify([])
    articolo_ids = [a.id for a in articoli]

    giacenze = dict(db.session.query(ClLotto.articolo_id, db.func.coalesce(db.func.sum(ClMovimento.quantita), 0))
                    .join(ClMovimento, ClMovimento.lotto_id == ClLotto.id)
                    .filter(ClLotto.articolo_id.in_(articolo_ids))
                    .group_by(ClLotto.articolo_id).all())

    ordinate = {}
    righe_ordine = (db.session.query(ClOrdineRiga.articolo_id, db.func.coalesce(db.func.sum(ClOrdineRiga.quantita), 0))
                    .join(ClOrdine, ClOrdine.id == ClOrdineRiga.ordine_id)
                    .filter(ClOrdine.stato == 'CONFERMATO', ClOrdineRiga.articolo_id.in_(articolo_ids))
                    .group_by(ClOrdineRiga.articolo_id).all()) if versione_schema() >= VERSIONE_SCHEMA_ORDINI else []
    ordinate = dict(righe_ordine)

    con_foto = set()
    if versione_schema() >= VERSIONE_SCHEMA_LIVE:
        con_foto = {aid for (aid,) in db.session.query(ClFotoArticolo.articolo_id)
                   .filter(ClFotoArticolo.articolo_id.in_(articolo_ids)).all()}

    gruppi = {}
    for a in articoli:
        g = gruppi.setdefault(a.cliente_id, {'cliente_id': a.cliente_id,
                                             'cliente': a.cliente.ragione_sociale if a.cliente else '', 'articoli': []})
        ordinata = float(ordinate.get(a.id, 0) or 0)
        prodotta = _quantita_prodotta(a.codice)
        g['articoli'].append({'articolo_id': a.id, 'codice': a.codice, 'descrizione': a.descrizione, 'udm': a.udm,
                              'giacenza': float(giacenze.get(a.id, 0) or 0), 'quantita_ordinata': ordinata,
                              'quantita_prodotta': prodotta, 'quantita_evasa': 0.0,
                              'saldo_da_produrre': round(ordinata - prodotta, 3), 'ha_foto': a.id in con_foto})
    return jsonify(sorted(gruppi.values(), key=lambda g: g['cliente']))
