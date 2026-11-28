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
import re
import io
from datetime import datetime

import PyPDF2
import hmac
import os
from functools import wraps

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, render_template, request, session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from models import db, OrdineProduzione  # sola lettura, vedi eccezione documentata sopra
from blueprints.conto_lavoro.importa_articoli import analizza_file, crea_modello_xlsx
from blueprints.conto_lavoro.importa_ordine import estrai_dati_ordine
from blueprints.conto_lavoro.models import (ClAudit, ClArticoloCliente, ClCliente, ClDdt, ClFattura,
                                            ClFotoArticolo, ClImpostazione, ClLotto, ClMovimento, ClOrdine,
                                            ClOrdineRiga, ClSchemaVersion, TRACCIAMENTI, UDM, _adesso,
                                            ClRigaRevisione, ClAnomaliaRisolta, ClAnomalia,
                                            ClTwDocumento, ClTwRiferimento, ClTwRiga)
from blueprints.conto_lavoro.importa_documenti import estrai_documenti

VERSIONE_MODULO = 'ordini'
VERSIONE_SCHEMA_BASE = 1      # clienti/articoli — invariato dalla 0001, non deve regredire
VERSIONE_SCHEMA_ORDINI = 2    # ordini, fatture, impostazioni/triple watch — dalla 0002
VERSIONE_SCHEMA_LIVE = 3      # foto articolo + monitor LIVE — dalla 0003
# Quantità evasa reale + correzione manuale quantità prodotta, righe
# aggiungibili/eliminabili/modificabili sempre (non solo in BOZZA), ordine
# eliminabile anche da CONFERMATO se non movimentato — dalla 0004 (Mauri,
# 02/10/2026). _ordine_dict legge le nuove colonne per OGNI endpoint che
# tocca righe ordine: il cancello qui sotto richiede questa versione per
# tutto il prefisso ordini/magazzino, non solo le rotte nuove.
VERSIONE_SCHEMA_RIGHE_MODIFICABILI = 4
# Ordine SOSPESO, righe DA REVISIONARE, anomalie MasterWork risolte — dalla
# 0005 (Mauri, 28/11/2026). Tabelle a parte: tutto il resto funziona anche
# prima della 0005, solo queste funzioni rispondono 503 finché non c'è.
VERSIONE_SCHEMA_ANOMALIE = 5
VERSIONE_SCHEMA_TRIPLE_WATCH = 8      # cl_tw_*: DDT di vendita e fatture da PDF — dalla 0008
VERSIONE_SCHEMA_ANOMALIE_ANGELO = 7   # cl_anomalia (0006) con tipo TEMPO/PREZZO (0007)
STATI_ATTIVI = ('CONFERMATO', 'SOSPESO')   # ordini rilasciati: anche se sospesi restano in LIVE/magazzino
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
            richiesta = VERSIONE_SCHEMA_RIGHE_MODIFICABILI
        else:
            richiesta = VERSIONE_SCHEMA_BASE
        if versione_schema() < richiesta:
            return jsonify(ok=False, error=f'Tabelle del Conto lavoro non ancora alla versione {richiesta}: '
                                           f'impostare CL_MIGRA_AUTO={richiesta:04d} su Railway e riavviare.'), 503


# ── Accesso di Angelo (Mauri, 28/11/2026) ───────────────────────────────────
# Inserire/modificare a mano clienti e codici (oltre all'import PDF) lo può fare
# solo Angelo: PIN nella variabile Railway CL_PIN_ANGELO, valido per la sessione
# del browser. Senza variabile la funzione resta bloccata.
def _pin_angelo():
    return (os.environ.get('CL_PIN_ANGELO') or current_app.config.get('CL_PIN_ANGELO') or '').strip()


def angelo_attivo():
    return bool(session.get('cl_angelo')) and bool(_pin_angelo())


def solo_angelo(f):
    @wraps(f)
    def _avvolta(*a, **k):
        if not angelo_attivo():
            return jsonify(ok=False, angelo=False,
                           error='Solo Angelo può inserire o modificare a mano clienti e codici: '
                                 'premi "🔒 Accesso Angelo" e inserisci il PIN.'), 403
        return f(*a, **k)
    return _avvolta


@cl_bp.get('/api/angelo')
def api_angelo_stato():
    return jsonify(attivo=angelo_attivo(), configurato=bool(_pin_angelo()))


@cl_bp.post('/api/angelo/accesso')
def api_angelo_accesso():
    pin = str((request.get_json(silent=True) or {}).get('pin') or '').strip()
    atteso = _pin_angelo()
    if not atteso:
        return jsonify(ok=False, error='PIN di Angelo non impostato: aggiungere CL_PIN_ANGELO su Railway.'), 503
    if not pin or not hmac.compare_digest(pin, atteso):
        return jsonify(ok=False, error='PIN errato.'), 403
    session['cl_angelo'] = True
    session.permanent = True
    r = jsonify(ok=True)
    r.delete_cookie(COOKIE_TOTEM)     # il browser di Angelo non resta mai bloccato come postazione LIVE
    return r


@cl_bp.post('/api/angelo/esci')
def api_angelo_esci():
    session.pop('cl_angelo', None)
    return jsonify(ok=True)


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
                           schema=versione_schema(), schema_richiesto=VERSIONE_SCHEMA_RIGHE_MODIFICABILI)


@cl_bp.get('/riepilogo')
def pagina_riepilogo():
    """Unito alla pagina Ordini (Mauri, 28/11/2026): il riepilogo sta in cima a /ordini."""
    return redirect('/conto-lavoro/ordini')


@cl_bp.get('/triple-watch')
def pagina_triple_watch():
    """Triple Watch (Mauri, 28/11/2026): per ogni ordine cliente i DDT di vendita e le
    fatture collegati, dai PDF Zucchetti importati (a fine mese tutte le fatture)."""
    return render_template('conto_lavoro/triple_watch.html', active='conto_lavoro', schema=versione_schema(),
                           schema_richiesto=VERSIONE_SCHEMA_TRIPLE_WATCH, modo='tw')


@cl_bp.get('/ddt')
def pagina_ddt_vendita():
    """DDT di vendita: caricamento PDF e riepilogo (dati dal database, non dai PDF)."""
    return render_template('conto_lavoro/triple_watch.html', active='conto_lavoro', schema=versione_schema(),
                           schema_richiesto=VERSIONE_SCHEMA_TRIPLE_WATCH, modo='ddt')


@cl_bp.get('/fatture')
def pagina_fatture():
    """Fatture: caricamento PDF (anche tutte quelle del mese) e riepilogo."""
    return render_template('conto_lavoro/triple_watch.html', active='conto_lavoro', schema=versione_schema(),
                           schema_richiesto=VERSIONE_SCHEMA_TRIPLE_WATCH, modo='fatture')


@cl_bp.get('/anomalie')
def pagina_anomalie():
    """Tutte le anomalie del Conto Lavoro in un posto solo (Mauri, 28/11/2026): quelle
    degli operai (tasto sul cellulare in MasterWork) e quelle segnalate da Angelo dal
    pulsante su ogni codice in Magazzino / Riepilogo ordini. ?codice= filtra un codice."""
    return render_template('conto_lavoro/anomalie.html', active='conto_lavoro', schema=versione_schema(),
                           schema_richiesto=VERSIONE_SCHEMA_ANOMALIE_ANGELO)


@cl_bp.get('/magazzino')
def pagina_magazzino():
    """Unito a Clienti (Mauri, 28/11/2026): pagina "Clienti e magazzino"."""
    return redirect('/conto-lavoro/anagrafiche#magazzino')


# ── Postazione LIVE blindata per gli operai (Mauri, 28/11/2026) ─────────────
# Un dispositivo che apre il LIVE (indirizzo /conto-lavoro/live, senza ?capo=1)
# diventa "postazione LIVE": un cookie lo blocca sul monitor e QUALSIASI altra
# pagina di IronProduction lo rimanda al LIVE (anche col tasto Indietro o
# scrivendo un indirizzo). Si sblocca solo con il PIN di Angelo
# (/conto-lavoro/live/sblocca). Dal Conto lavoro il capo apre il LIVE con
# ?capo=1 e il suo browser non viene bloccato.
COOKIE_TOTEM = 'ip_postazione_live'
_TOTEM_GET = ('/conto-lavoro/live', '/conto-lavoro/api/live', '/conto-lavoro/api/anomalie-risolte',
              '/conto-lavoro/api/anomalie-angelo', '/api/masterwork/foto/', '/api/masterwork/tempi-standard/',
              '/favicon.ico', '/static/')
_TOTEM_POST = ('/api/masterwork/codici-interni', '/api/masterwork/anomalie')


def _consentito_alla_postazione(metodo, percorso):
    if percorso.startswith('/conto-lavoro/live/sblocca') or percorso == '/conto-lavoro/api/angelo/accesso':
        return True
    if metodo in ('GET', 'HEAD'):
        if percorso.startswith('/conto-lavoro/api/articoli/') and percorso.endswith('/foto'):
            return True
        return any(percorso == p or (p.endswith('/') and percorso.startswith(p)) or percorso.startswith(p + '?')
                   for p in _TOTEM_GET) or percorso == '/conto-lavoro/live'
    if metodo == 'POST':
        return percorso in _TOTEM_POST
    return False


@cl_bp.before_app_request
def _blocca_postazione_live():
    if request.cookies.get(COOKIE_TOTEM) != '1':
        return None
    if angelo_attivo():          # Angelo va dovunque, anche da un dispositivo segnato come postazione
        return None
    if _consentito_alla_postazione(request.method, request.path):
        return None
    if request.method == 'GET' and 'text/html' in (request.headers.get('Accept') or ''):
        return redirect('/conto-lavoro/live')
    return jsonify(ok=False, error='Postazione LIVE: qui si vede solo il monitor del Conto lavoro.'), 403


@cl_bp.get('/live')
def pagina_live():
    """Monitor LIVE Conto lavoro — stile del monitor MasterWork (Saldatura,
    ecc.): niente sidebar/topbar, pensato per un totem/monitor d'officina.
    Aperto senza ?capo=1 il dispositivo diventa POSTAZIONE LIVE (vedi sopra)."""
    capo = angelo_attivo() or (request.args.get('capo') == '1' and request.cookies.get(COOKIE_TOTEM) != '1')
    risposta = current_app.make_response(render_template('conto_lavoro/live.html', schema=versione_schema(),
                                                         schema_richiesto=VERSIONE_SCHEMA_LIVE, postazione=not capo))
    if angelo_attivo():
        risposta.delete_cookie(COOKIE_TOTEM)
    elif not capo:
        risposta.set_cookie(COOKIE_TOTEM, '1', max_age=10 * 365 * 24 * 3600, httponly=True, samesite='Lax',
                            secure=request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https')
    risposta.headers['Cache-Control'] = 'no-store'
    return risposta


@cl_bp.route('/live/sblocca', methods=['GET', 'POST'])
def pagina_sblocca_postazione():
    """Toglie il blocco "postazione LIVE" da questo dispositivo: serve il PIN di Angelo."""
    errore = ''
    if request.method == 'POST':
        pin = (request.form.get('pin') or '').strip()
        atteso = _pin_angelo()
        if not atteso:
            errore = 'PIN di Angelo non impostato su Railway (CL_PIN_ANGELO): impossibile sbloccare.'
        elif not pin or not hmac.compare_digest(pin, atteso):
            errore = 'PIN errato.'
        else:
            # il dispositivo torna libero e il browser resta riconosciuto come quello di Angelo:
            # aprendo di nuovo il LIVE non verra' piu' bloccato
            session['cl_angelo'] = True
            session.permanent = True
            vai = request.args.get('vai') or ''
            r = redirect(vai if vai.startswith('/') and not vai.startswith('//') else '/conto-lavoro/')
            r.delete_cookie(COOKIE_TOTEM)
            return r
    return f"""<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sblocca postazione LIVE</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#07101f;
color:#eaf4ff;font-family:'Segoe UI',Arial,sans-serif}}form{{background:#0a1623;border:2px solid #1e6fa5;border-radius:12px;
padding:24px;width:min(380px,92vw)}}input{{width:100%;box-sizing:border-box;padding:12px;font-size:18px;border-radius:8px;
border:1px solid #1e6fa5;background:#0f2237;color:#fff;margin:10px 0}}button{{width:100%;padding:12px;border:0;border-radius:8px;
background:#1a9e5c;color:#fff;font-weight:800;font-size:15px;cursor:pointer}}a{{color:#7fd3ff}}</style></head><body>
<form method="post"><div style="font-size:20px;font-weight:900">🔒 Postazione LIVE</div>
<div style="font-size:13px;color:#9fb8d0;margin-top:6px">Questo dispositivo può vedere solo il LIVE del Conto lavoro.
Per sbloccarlo serve il PIN di Angelo.</div>
<input type="password" name="pin" inputmode="numeric" autofocus placeholder="PIN di Angelo">
<div style="color:#ff6b6b;font-size:13px;min-height:18px">{errore}</div>
<button>🔓 Sblocca questo dispositivo</button>
<div style="margin-top:12px;font-size:13px"><a href="/conto-lavoro/live">← torna al LIVE</a></div></form></body></html>"""


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
@solo_angelo
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
@solo_angelo
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
@solo_angelo
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
@solo_angelo
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
@solo_angelo
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
    valida anche se l'OP viene poi chiuso/archiviato.
    Per UNA riga/UN codice isolato (dettaglio di un solo ordine). Quando
    serve per MOLTE righe insieme (lista ordini, Live, Magazzino) usare
    invece _prodotta_per_codici — una query sola invece di una per codice,
    vedi lì il motivo (bug reale, "Errore di rete" per troppe query)."""
    if not codice_articolo:
        return 0.0
    tot = (db.session.query(db.func.coalesce(db.func.sum(OrdineProduzione.qta_buona), 0))
           .filter(OrdineProduzione.codice_articolo == codice_articolo).scalar())
    return float(tot or 0)


def _prodotta_per_codici(codici):
    """BUG REALE CORRETTO (segnalato da Mauri, 06/10/2026 — /conto-lavoro/
    riepilogo mostrava 'Errore di rete'): _quantita_prodotta interrogava il
    DB una volta per OGNI riga/codice (N+1) — con decine di ordini e righe,
    /api/live (nessun limite) e /api/ordini (limite 200, ma più righe a
    testa) finivano per fare centinaia di query sequenziali nella stessa
    richiesta, abbastanza lente da far scadere il timeout del server: la
    risposta tornava una pagina di errore HTML invece di JSON, e il
    fetch().json() del browser la vedeva come un fallimento di rete.
    Qui invece UNA sola query SUM...GROUP BY per tutti i codici coinvolti,
    poi letta in memoria per ogni riga. Ritorna {codice: prodotta}."""
    codici = {c for c in codici if c}
    if not codici:
        return {}
    righe = (db.session.query(OrdineProduzione.codice_articolo,
                              db.func.coalesce(db.func.sum(OrdineProduzione.qta_buona), 0))
             .filter(OrdineProduzione.codice_articolo.in_(codici))
             .group_by(OrdineProduzione.codice_articolo).all())
    return {codice: float(tot or 0) for codice, tot in righe}


def _prodotta_riga(r, mappa_prodotta=None):
    """'Prodotta' di una riga: il calcolo automatico da OrdineProduzione
    (sola lettura, vedi eccezione in testata file), SALVO che il capo abbia
    inserito una correzione manuale per questa riga (quantita_prodotta_manuale
    non NULL) — in quel caso vale quella, sempre (dalla 0004).
    'mappa_prodotta' opzionale (vedi _prodotta_per_codici): se fornita, legge
    da lì invece di interrogare il DB — usarla sempre quando si processano
    più righe nella stessa richiesta."""
    if r.quantita_prodotta_manuale is not None:
        return float(r.quantita_prodotta_manuale)
    codice = r.articolo.codice if r.articolo else ''
    if mappa_prodotta is not None:
        return mappa_prodotta.get(codice, 0.0)
    return _quantita_prodotta(codice)


def _ordine_movimentato(o, mappa_prodotta=None):
    """True se almeno una riga ha già qualcosa di 'reale' sopra (prodotta >0
    o evasa >0) — da quel momento l'ordine non si elimina più, né dalla lista
    né dal popup, qualunque sia il suo stato (anche se ancora BOZZA): quei
    numeri verrebbero persi senza lasciar traccia. Un ordine BOZZA o
    CONFERMATO senza nulla sopra resta eliminabile."""
    return any(_prodotta_riga(r, mappa_prodotta) > 0 or float(r.quantita_evasa or 0) > 0 for r in o.righe)


def _revisioni(riga_ids):
    """{riga_id: ClRigaRevisione} — vuoto prima della 0005."""
    riga_ids = list(riga_ids)
    if not riga_ids or versione_schema() < VERSIONE_SCHEMA_ANOMALIE:
        return {}
    return {rv.riga_id: rv for rv in ClRigaRevisione.query.filter(ClRigaRevisione.riga_id.in_(riga_ids)).all()}


def _revisione_dict(rv):
    if not rv:
        return None
    return {'nota': rv.nota, 'segnalata_da': rv.segnalata_da,
            'segnalata_il': rv.segnalata_il.strftime('%d/%m/%Y %H:%M') if rv.segnalata_il else ''}


def _schema_anomalie_mancante():
    if versione_schema() < VERSIONE_SCHEMA_ANOMALIE:
        return jsonify(ok=False, error=f'Funzione disponibile dalla versione {VERSIONE_SCHEMA_ANOMALIE} delle tabelle '
                                       f'del Conto lavoro: impostare CL_MIGRA_AUTO={VERSIONE_SCHEMA_ANOMALIE:04d} '
                                       f'su Railway e riavviare.'), 503
    return None


def _ordine_dict(o, con_righe=False, mappa_prodotta=None):
    d = {'id': o.id, 'cliente_id': o.cliente_id, 'cliente': o.cliente.ragione_sociale if o.cliente else '',
        'numero_ordine': o.numero_ordine, 'rif_cliente': o.rif_cliente,
        'data_documento': o.data_documento.strftime('%d/%m/%Y') if o.data_documento else '',
        'stato': o.stato, 'filename': o.filename,
        'creato_il': o.creato_il.strftime('%d/%m/%Y %H:%M') if o.creato_il else '',
        'movimentato': _ordine_movimentato(o, mappa_prodotta)}
    if con_righe:
        righe = []
        revisioni = _revisioni(r.id for r in o.righe)
        for r in o.righe:
            codice = r.articolo.codice if r.articolo else ''
            ordinata = float(r.quantita)
            prodotta = _prodotta_riga(r, mappa_prodotta)
            evasa = float(r.quantita_evasa or 0)
            righe.append({'id': r.id, 'n_riga': r.n_riga, 'codice': codice,
                          'descrizione': r.descrizione, 'quantita': ordinata,
                          'quantita_prodotta': prodotta, 'prodotta_manuale': r.quantita_prodotta_manuale is not None,
                          'quantita_evasa': evasa,
                          'saldo_da_produrre': round(ordinata - prodotta, 3),
                          'revisione': _revisione_dict(revisioni.get(r.id)),
                          'prezzo_unitario': float(r.prezzo_unitario) if r.prezzo_unitario is not None else None})
        d['righe'] = righe
    return d


@cl_bp.get('/api/ordini')
def api_ordini():
    q = ClOrdine.query.options(db.joinedload(ClOrdine.cliente),
                               db.joinedload(ClOrdine.righe).joinedload(ClOrdineRiga.articolo))
    cid = request.args.get('cliente_id', type=int)
    if cid:
        q = q.filter(ClOrdine.cliente_id == cid)
    ordini = q.order_by(ClOrdine.creato_il.desc()).limit(200).all()
    codici = {r.articolo.codice for o in ordini for r in o.righe if r.articolo}
    mappa_prodotta = _prodotta_per_codici(codici)
    return jsonify([_ordine_dict(o, mappa_prodotta=mappa_prodotta) for o in ordini])


@cl_bp.get('/api/ordini/<int:oid>')
def api_ordine_dettaglio(oid):
    o = db.session.get(ClOrdine, oid) or abort(404)
    mappa_prodotta = _prodotta_per_codici(r.articolo.codice for r in o.righe if r.articolo)
    return jsonify(_ordine_dict(o, con_righe=True, mappa_prodotta=mappa_prodotta))


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
        a_mano = ClOrdine.query.filter_by(cliente_id=cliente.id, numero_ordine=dati['numero_ordine'],
                                          filename=ORDINE_A_MANO).first()
        if a_mano:
            # l'ordine era stato inserito a mano: il PDF arrivato dopo lo completa (righe invariate)
            a_mano.filename = f.filename[:255]
            a_mano.testo_grezzo_pdf = testo
            a_mano.rif_cliente = dati['rif_cliente'] or a_mano.rif_cliente
            a_mano.data_documento = _data_ordine(dati['data_documento']) or a_mano.data_documento
            _audit('ORDINE_A_MANO_COMPLETATO_DA_PDF', 'cl_ordine', a_mano.id, dopo={'file': f.filename[:200]})
            db.session.commit()
            return jsonify(ok=True, anteprima=False, agganciato_a_mano=True,
                           messaggio='Ordine già inserito a mano: agganciato il PDF (righe non toccate, '
                                     'controllale nel dettaglio).', ordine=_ordine_dict(a_mano, con_righe=True))
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


@cl_bp.put('/api/ordini/<int:oid>/sospendi')
def api_sospendi_ordine(oid):
    """{"sospeso": true/false, "motivo": "..."} — un ordine rilasciato (CONFERMATO)
    può essere SOSPESO e poi riattivato. Il motivo resta nelle note dell'ordine."""
    errore = _schema_anomalie_mancante()
    if errore:
        return errore
    o = db.session.get(ClOrdine, oid) or abort(404)
    d = request.get_json(silent=True) or {}
    sospeso = bool(d.get('sospeso'))
    motivo = str(d.get('motivo') or '').strip()[:500]
    if sospeso and o.stato != 'CONFERMATO':
        return jsonify(ok=False, error='Si può sospendere solo un ordine confermato (rilasciato).'), 409
    if not sospeso and o.stato != 'SOSPESO':
        return jsonify(ok=False, error="L'ordine non è sospeso."), 409
    prima = o.stato
    o.stato = 'SOSPESO' if sospeso else 'CONFERMATO'
    quando = datetime.now().strftime('%d/%m/%Y %H:%M')
    riga_nota = (f'⏸ SOSPESO il {quando}' + (f': {motivo}' if motivo else '')) if sospeso \
        else (f'▶ RIATTIVATO il {quando}' + (f': {motivo}' if motivo else ''))
    o.note = ((o.note or '') + '\n' + riga_nota).strip()
    _audit('ORDINE_SOSPESO' if sospeso else 'ORDINE_RIATTIVATO', 'cl_ordine', o.id,
           prima={'stato': prima}, dopo={'stato': o.stato, 'motivo': motivo})
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o))


@cl_bp.put('/api/ordini/<int:oid>/righe/<int:rid>/revisione')
def api_revisione_riga(oid, rid):
    """{"da_revisionare": true/false, "nota": "..."} — Angelo segnala un codice
    dell'ordine da revisionare (nuovi tempi, nuove fasi), o toglie la segnalazione."""
    errore = _schema_anomalie_mancante()
    if errore:
        return errore
    r = db.session.get(ClOrdineRiga, rid)
    if not r or r.ordine_id != oid:
        abort(404)
    d = request.get_json(silent=True) or {}
    rv = db.session.get(ClRigaRevisione, rid)
    if d.get('da_revisionare'):
        nota = str(d.get('nota') or '').strip()[:1000]
        if rv:
            rv.nota = nota
        else:
            db.session.add(ClRigaRevisione(riga_id=rid, nota=nota, segnalata_da=UTENTE_AUDIT))
        _audit('RIGA_DA_REVISIONARE', 'cl_ordine_riga', rid, dopo={'nota': nota})
    elif rv:
        _audit('RIGA_REVISIONATA', 'cl_ordine_riga', rid, prima={'nota': rv.nota})
        db.session.delete(rv)
    db.session.commit()
    o = db.session.get(ClOrdine, oid)
    mappa_prodotta = _prodotta_per_codici(x.articolo.codice for x in o.righe if x.articolo)
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True, mappa_prodotta=mappa_prodotta))


# ── Anomalie segnalate dagli operai su MasterWork ───────────────────────────
# Le anomalie le legge la pagina dal MasterWork tramite IronProduction
# (/api/masterwork/anomalie, fuori da questo modulo: qui niente rete). Qui si
# registra solo chi le ha risolte e con quale nota.
@cl_bp.get('/api/anomalie-risolte')
def api_anomalie_risolte():
    errore = _schema_anomalie_mancante()
    if errore:
        return errore
    righe = ClAnomaliaRisolta.query.order_by(ClAnomaliaRisolta.risolta_il.desc()).limit(500).all()
    return jsonify([{'mw_id': a.mw_id, 'operaio': a.operaio, 'codice_interno': a.codice_interno, 'motivo': a.motivo,
                     'nota_risoluzione': a.nota_risoluzione, 'risolta_da': a.risolta_da,
                     'segnalata_il': a.segnalata_il.isoformat() if a.segnalata_il else '',
                     'risolta_il': a.risolta_il.isoformat() if a.risolta_il else ''} for a in righe])


@cl_bp.post('/api/anomalie-risolte')
def api_risolvi_anomalia():
    """Pulsante "È stato risolto": {"mw_id", "nota", e la fotografia dell'anomalia
    (operaio, codice_interno, motivo, segnalata_il)}."""
    errore = _schema_anomalie_mancante()
    if errore:
        return errore
    d = request.get_json(silent=True) or {}
    try:
        mw_id = int(d.get('mw_id'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Anomalia non indicata.'), 400
    nota = str(d.get('nota') or '').strip()[:2000]
    if not nota:
        return jsonify(ok=False, error='Scrivi una nota su come è stata risolta.'), 400
    if ClAnomaliaRisolta.query.filter_by(mw_id=mw_id).first():
        return jsonify(ok=False, error='Anomalia già segnata come risolta.'), 409
    segnalata_il = None
    try:
        segnalata_il = datetime.fromisoformat(str(d.get('segnalata_il') or '')[:26]) if d.get('segnalata_il') else None
    except ValueError:
        pass
    a = ClAnomaliaRisolta(mw_id=mw_id, operaio=str(d.get('operaio') or '')[:120],
                          codice_interno=str(d.get('codice_interno') or '')[:80],
                          motivo=str(d.get('motivo') or '')[:5000], segnalata_il=segnalata_il,
                          nota_risoluzione=nota, risolta_da=str(d.get('risolta_da') or UTENTE_AUDIT)[:80])
    db.session.add(a)
    _audit('ANOMALIA_RISOLTA', 'cl_anomalia_risolta', mw_id, dopo={'nota': nota, 'operaio': a.operaio})
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Anomalia già segnata come risolta.'), 409
    return jsonify(ok=True)


def _schema_anomalie_angelo_mancante():
    if versione_schema() < VERSIONE_SCHEMA_ANOMALIE_ANGELO:
        return jsonify(ok=False, error=f'Funzione disponibile dalla versione {VERSIONE_SCHEMA_ANOMALIE_ANGELO} delle '
                                       f'tabelle del Conto lavoro: impostare CL_MIGRA_AUTO='
                                       f'{VERSIONE_SCHEMA_ANOMALIE_ANGELO:04d} su Railway e riavviare.'), 503
    return None


def _anomalia_dict(a):
    return {'id': a.id, 'articolo_id': a.articolo_id, 'codice': a.articolo.codice if a.articolo else '',
            'descrizione_articolo': a.articolo.descrizione if a.articolo else '',
            'cliente': a.articolo.cliente.ragione_sociale if a.articolo and a.articolo.cliente else '',
            'ordine_id': a.ordine_id, 'numero_ordine': a.ordine.numero_ordine if a.ordine else '',
            'tipo': a.tipo, 'descrizione': a.descrizione, 'segnalata_da': a.segnalata_da,
            'segnalata_il': a.segnalata_il.isoformat() if a.segnalata_il else '',
            'risolta': a.risolta_il is not None,
            'risolta_il': a.risolta_il.isoformat() if a.risolta_il else '',
            'nota_risoluzione': a.nota_risoluzione, 'risolta_da': a.risolta_da}


@cl_bp.get('/api/anomalie-angelo')
def api_anomalie_angelo():
    """?stato=aperte (default) | risolte | tutte"""
    errore = _schema_anomalie_angelo_mancante()
    if errore:
        return errore
    q = ClAnomalia.query.options(db.joinedload(ClAnomalia.articolo).joinedload(ClArticoloCliente.cliente),
                                 db.joinedload(ClAnomalia.ordine))
    stato = request.args.get('stato', 'aperte')
    if stato == 'aperte':
        q = q.filter(ClAnomalia.risolta_il.is_(None)).order_by(ClAnomalia.segnalata_il.desc())
    elif stato == 'risolte':
        q = q.filter(ClAnomalia.risolta_il.isnot(None)).order_by(ClAnomalia.risolta_il.desc())
    else:
        q = q.order_by(ClAnomalia.segnalata_il.desc())
    return jsonify([_anomalia_dict(a) for a in q.limit(500).all()])


@cl_bp.post('/api/anomalie-angelo')
def api_crea_anomalia_angelo():
    """{"articolo_id", "ordine_id"?, "tipo": TEMPO|PREZZO, "descrizione", "segnalata_da"?}.
    TEMPO = problema di produzione (compare nel LIVE); PREZZO = tempi già ottimizzati,
    si tratta solo col cliente (NON compare nel LIVE)."""
    errore = _schema_anomalie_angelo_mancante()
    if errore:
        return errore
    d = request.get_json(silent=True) or {}
    a = db.session.get(ClArticoloCliente, d.get('articolo_id') or 0)
    if not a:
        return jsonify(ok=False, error='Codice non trovato.'), 404
    descrizione = str(d.get('descrizione') or '').strip()[:5000]
    if not descrizione:
        return jsonify(ok=False, error="Descrivi l'anomalia."), 400
    ordine_id = d.get('ordine_id') or None
    if ordine_id and not db.session.get(ClOrdine, ordine_id):
        ordine_id = None
    tipo = str(d.get('tipo') or '').strip().upper()
    if tipo not in ('TEMPO', 'PREZZO'):
        return jsonify(ok=False, error="Scegli il tipo: anomalia di TEMPO o di PREZZO."), 400
    an = ClAnomalia(articolo_id=a.id, ordine_id=ordine_id, descrizione=descrizione, tipo=tipo,
                    segnalata_da=str(d.get('segnalata_da') or 'Angelo').strip()[:80] or 'Angelo')
    db.session.add(an)
    db.session.flush()
    _audit('ANOMALIA_SEGNALATA', 'cl_anomalia', an.id, dopo={'codice': a.codice, 'tipo': tipo, 'descrizione': descrizione})
    db.session.commit()
    return jsonify(ok=True, anomalia=_anomalia_dict(an))


@cl_bp.post('/api/anomalie-angelo/<int:aid>/risolvi')
def api_risolvi_anomalia_angelo(aid):
    errore = _schema_anomalie_angelo_mancante()
    if errore:
        return errore
    an = db.session.get(ClAnomalia, aid) or abort(404)
    d = request.get_json(silent=True) or {}
    nota = str(d.get('nota') or '').strip()[:2000]
    if not nota:
        return jsonify(ok=False, error='Scrivi una nota su come è stata risolta.'), 400
    if an.risolta_il:
        return jsonify(ok=False, error='Anomalia già segnata come risolta.'), 409
    an.risolta_il = _adesso()
    an.nota_risoluzione = nota
    an.risolta_da = str(d.get('risolta_da') or UTENTE_AUDIT).strip()[:80]
    _audit('ANOMALIA_RISOLTA', 'cl_anomalia', an.id, dopo={'nota': nota})
    db.session.commit()
    return jsonify(ok=True, anomalia=_anomalia_dict(an))



# ── TRIPLE WATCH: Ordine cliente <-> DDT di vendita <-> Fattura (Mauri, 28/11/2026) ──
def _schema_tw_mancante():
    if versione_schema() < VERSIONE_SCHEMA_TRIPLE_WATCH:
        return jsonify(ok=False, error=f'Triple Watch disponibile dalla versione {VERSIONE_SCHEMA_TRIPLE_WATCH} delle '
                                       f'tabelle: impostare CL_MIGRA_AUTO={VERSIONE_SCHEMA_TRIPLE_WATCH:04d} su Railway '
                                       f'e riavviare.'), 503
    return None


def _cifre(numero):
    m = re.match(r'\s*(\d+)', str(numero or ''))
    return str(int(m.group(1))) if m else ''


def _data_it(s):
    try:
        return datetime.strptime(s, '%d/%m/%Y').date() if s else None
    except ValueError:
        return None


def _cliente_documento(doc_letto, ordini_rif):
    """Cliente del documento: per P.IVA, poi dagli ordini richiamati, poi per nome."""
    if doc_letto.get('cliente_piva'):
        c = ClCliente.query.filter(ClCliente.piva == doc_letto['cliente_piva']).first()
        if c:
            return c
    for o in ordini_rif:
        return o.cliente
    nome = (doc_letto.get('cliente_nome') or '').strip()
    if nome:
        return ClCliente.query.filter(db.func.upper(ClCliente.ragione_sociale).like(
            '%' + nome.upper().split()[0] + '%')).first() if nome.split() else None
    return None


def _ordini_per_numero(numero, data_rif=None, cliente_id=None):
    """Ordini cliente con quel numero (cifre di '293/IS' = '293'): se piu' d'uno, il piu' vicino
    (precedente) alla data del documento e dello stesso cliente."""
    cifre = _cifre(numero)
    if not cifre:
        return []
    candidati = [o for o in ClOrdine.query.filter(ClOrdine.stato != 'ANNULLATO').all()
                 if _cifre(o.numero_ordine) == cifre and (not cliente_id or o.cliente_id == cliente_id)]
    if len(candidati) <= 1 or not data_rif:
        return candidati[:1]
    prima = [o for o in candidati if o.data_documento and o.data_documento <= data_rif]
    return [max(prima, key=lambda o: o.data_documento)] if prima else candidati[:1]


@cl_bp.post('/api/triple-watch/importa')
def api_tw_importa():
    """Uno o piu' PDF (campo 'files'), tipo AUTO|DDT|FATTURA. Senza conferma=true: solo anteprima.
    Con conferma: salva; un documento gia' presente (stesso tipo, numero, anno) viene aggiornato."""
    errore = _schema_tw_mancante()
    if errore:
        return errore
    files = request.files.getlist('files')
    if not files:
        return jsonify(ok=False, error='Scegli uno o piu\' file PDF.'), 400
    tipo_richiesto = (request.form.get('tipo') or 'AUTO').upper()
    conferma = (request.form.get('conferma') or '').lower() == 'true'
    risultati, salvati, aggiornati = [], 0, 0
    for f in files:
        if not f.filename.lower().endswith('.pdf'):
            risultati.append({'file': f.filename, 'errore': 'Non è un PDF.'})
            continue
        raw = f.read()
        if len(raw) > 30 * 1024 * 1024:
            risultati.append({'file': f.filename, 'errore': 'File troppo grande (massimo 30 MB).'})
            continue
        documenti, err = estrai_documenti(raw, tipo_richiesto)
        if err:
            risultati.append({'file': f.filename, 'errore': err})
            continue
        for d in documenti:
            data_doc = _data_it(d['data'])
            anno = data_doc.year if data_doc else 0
            ordini_rif = [o for tipo_rif, n in d['riferimenti'] if tipo_rif == 'ORDINE'
                          for o in _ordini_per_numero(n, data_doc)]
            cliente = _cliente_documento(d, ordini_rif)
            ddt_rif = [n for t, n in d['riferimenti'] if t == 'DDT']
            ddt_trovati = [n for n in ddt_rif if ClTwDocumento.query.filter_by(tipo='DDT', numero=n).first()]
            voce = {'file': f.filename, 'tipo': d['tipo'], 'numero': d['numero'], 'data': d['data'],
                    'pagine': d['pagine'], 'cliente': cliente.ragione_sociale if cliente else d['cliente_nome'],
                    'cliente_riconosciuto': bool(cliente), 'importo': d['importo'],
                    'ordini_rif': [n for t, n in d['riferimenti'] if t == 'ORDINE'],
                    'ordini_trovati': [o.numero_ordine for o in ordini_rif],
                    'ddt_rif': ddt_rif, 'ddt_trovati': ddt_trovati, 'n_righe': len(d['righe']),
                    'righe': d['righe'][:40]}
            if d.get('tipo_diverso'):
                voce['errore'] = ('Questo PDF sembra una FATTURA: caricalo con "Carica fatture".'
                                  if d['tipo_diverso'] == 'FATTURA' else
                                  'Questo PDF sembra un DDT di vendita: caricalo con "Carica DDT di vendita".')
                risultati.append(voce)
                continue
            if not d['tipo'] or not d['numero']:
                voce['errore'] = 'Documento non riconosciuto (né DDT né fattura) o numero non letto.'
                risultati.append(voce)
                continue
            esistente = ClTwDocumento.query.filter_by(tipo=d['tipo'], numero=d['numero'], anno=anno).first()
            voce['gia_presente'] = bool(esistente)
            if conferma:
                doc = esistente or ClTwDocumento(tipo=d['tipo'], numero=d['numero'], anno=anno)
                if esistente:
                    doc.riferimenti.clear()
                    doc.righe.clear()
                    aggiornati += 1
                else:
                    db.session.add(doc)
                    salvati += 1
                doc.data_documento = data_doc
                doc.cliente_id = cliente.id if cliente else None
                doc.cliente_nome = (d['cliente_nome'] or (cliente.ragione_sociale if cliente else ''))[:200]
                doc.importo = d['importo']
                doc.filename = f.filename[:255]
                doc.testo_grezzo = d['testo']
                for t, n in dict.fromkeys(d['riferimenti']):
                    doc.riferimenti.append(ClTwRiferimento(tipo_rif=t, numero_rif=n[:30]))
                for r in d['righe']:
                    doc.righe.append(ClTwRiga(numero_ordine_rif=(r.get('ordine_rif') or '')[:30],
                                              codice=r['codice'][:100], descrizione=r['descrizione'][:300],
                                              quantita=r['quantita']))
                db.session.flush()
            risultati.append(voce)
    if conferma:
        _audit('TRIPLE_WATCH_IMPORT', 'cl_tw_documento', '', dopo={'nuovi': salvati, 'aggiornati': aggiornati,
                                                                   'file': [f.filename[:100] for f in files]})
        db.session.commit()
    return jsonify(ok=True, anteprima=not conferma, documenti=risultati, nuovi=salvati, aggiornati=aggiornati)


def _doc_tw_dict(d):
    return {'id': d.id, 'tipo': d.tipo, 'numero': d.numero, 'anno': d.anno,
            'data': d.data_documento.strftime('%d/%m/%Y') if d.data_documento else '',
            'cliente': d.cliente.ragione_sociale if d.cliente else d.cliente_nome,
            'importo': float(d.importo) if d.importo is not None else None, 'filename': d.filename}


@cl_bp.get('/api/triple-watch/quadro')
def api_tw_quadro():
    """Per ogni ordine cliente: DDT di vendita e fatture collegati, quantita' spedite e esito.
    ?cliente_id= filtra; ?mesi=N (default 12) limita agli ordini recenti."""
    errore = _schema_tw_mancante()
    if errore:
        return errore
    cliente_id = request.args.get('cliente_id', type=int)
    docs = ClTwDocumento.query.options(db.joinedload(ClTwDocumento.riferimenti), db.joinedload(ClTwDocumento.righe),
                                       db.joinedload(ClTwDocumento.cliente)).all()
    ddt = [d for d in docs if d.tipo == 'DDT']
    fatture = [d for d in docs if d.tipo == 'FATTURA']
    ordini = (ClOrdine.query.filter(ClOrdine.stato != 'ANNULLATO')
              .options(db.joinedload(ClOrdine.cliente), db.joinedload(ClOrdine.righe).joinedload(ClOrdineRiga.articolo))
              .all())
    per_cifre = {}
    for o in ordini:
        per_cifre.setdefault(_cifre(o.numero_ordine), []).append(o)

    def ordine_di(numero, data_doc, cli_id):
        cand = [o for o in per_cifre.get(_cifre(numero), []) if not cli_id or o.cliente_id == cli_id] or \
            per_cifre.get(_cifre(numero), [])
        if len(cand) <= 1 or not data_doc:
            return cand[:1]
        prima = [o for o in cand if o.data_documento and o.data_documento <= data_doc]
        return [max(prima, key=lambda o: o.data_documento)] if prima else cand[:1]

    ddt_ordini = {}            # ddt.id -> [ordine.id]
    for d in ddt:
        ids = []
        for r in d.riferimenti:
            if r.tipo_rif == 'ORDINE':
                ids += [o.id for o in ordine_di(r.numero_rif, d.data_documento, d.cliente_id)]
        ddt_ordini[d.id] = list(dict.fromkeys(ids))
    ddt_per_cifre = {}
    for d in ddt:
        ddt_per_cifre.setdefault(_cifre(d.numero), []).append(d)
    fatt_ddt, fatt_ordini, ddt_fatture = {}, {}, {}
    for f in fatture:
        legati = []
        for r in f.riferimenti:
            if r.tipo_rif == 'DDT':
                cand = ddt_per_cifre.get(_cifre(r.numero_rif), [])
                if len(cand) > 1 and f.data_documento:
                    prima = [d for d in cand if d.data_documento and d.data_documento <= f.data_documento]
                    cand = [max(prima, key=lambda d: d.data_documento)] if prima else cand[:1]
                legati += cand[:1]
        fatt_ddt[f.id] = list(dict.fromkeys(d.id for d in legati))
        ids = [oid for d in legati for oid in ddt_ordini.get(d.id, [])]
        for r in f.riferimenti:
            if r.tipo_rif == 'ORDINE':
                ids += [o.id for o in ordine_di(r.numero_rif, f.data_documento, f.cliente_id)]
        fatt_ordini[f.id] = list(dict.fromkeys(ids))
        for did in fatt_ddt[f.id]:
            ddt_fatture.setdefault(did, []).append(f.id)

    ddt_by_id = {d.id: d for d in ddt}
    fat_by_id = {f.id: f for f in fatture}
    usati_ddt, usati_fat, quadro = set(), set(), []
    for o in sorted(ordini, key=lambda o: (o.data_documento or datetime.min.date(), o.id), reverse=True):
        if cliente_id and o.cliente_id != cliente_id:
            continue
        miei_ddt = [d for d in ddt if o.id in ddt_ordini.get(d.id, [])]
        mie_fat = [f for f in fatture if o.id in fatt_ordini.get(f.id, [])]
        if o.stato == 'BOZZA' and not miei_ddt and not mie_fat:
            continue
        usati_ddt.update(d.id for d in miei_ddt)
        usati_fat.update(f.id for f in mie_fat)
        cifre = _cifre(o.numero_ordine)
        codici_ordine = {r.articolo.codice.upper() for r in o.righe if r.articolo}
        spedito = 0.0
        for d in miei_ddt:
            solo_questo = len(ddt_ordini.get(d.id, [])) == 1
            for r in d.righe:
                if (_cifre(r.numero_ordine_rif) == cifre or (solo_questo and not r.numero_ordine_rif)) \
                        and (r.codice or '').upper() in codici_ordine:
                    spedito += float(r.quantita or 0)
        ordinato = sum(float(r.quantita or 0) for r in o.righe)
        ddt_senza_fattura = [d for d in miei_ddt if not ddt_fatture.get(d.id)]
        if not miei_ddt and not mie_fat:
            esito = 'NESSUN_DDT'
        elif ddt_senza_fattura or not mie_fat:
            esito = 'DA_FATTURARE'
        elif spedito + 1e-6 < ordinato:
            esito = 'PARZIALE'
        else:
            esito = 'COMPLETO'
        quadro.append({'ordine_id': o.id, 'numero_ordine': o.numero_ordine, 'rif_cliente': o.rif_cliente,
                       'cliente': o.cliente.ragione_sociale if o.cliente else '', 'stato': o.stato,
                       'data': o.data_documento.strftime('%d/%m/%Y') if o.data_documento else '',
                       'ordinato': round(ordinato, 3), 'spedito': round(spedito, 3),
                       'ddt': [{**_doc_tw_dict(d), 'fatturato': bool(ddt_fatture.get(d.id))} for d in miei_ddt],
                       'fatture': [_doc_tw_dict(f) for f in mie_fat], 'esito': esito})
    # Riepiloghi dal database (niente rilettura dei PDF): tutti i DDT di vendita e tutte le fatture
    ord_by_id = {o.id: o for o in ordini}

    def righe_doc(d):
        return [{'ordine_rif': r.numero_ordine_rif, 'codice': r.codice, 'descrizione': r.descrizione,
                 'quantita': float(r.quantita) if r.quantita is not None else None} for r in d.righe]
    elenco_ddt = [{**_doc_tw_dict(d), 'ordini_rif': [r.numero_rif for r in d.riferimenti if r.tipo_rif == 'ORDINE'],
                   'ordini': [ord_by_id[i].numero_ordine for i in ddt_ordini.get(d.id, []) if i in ord_by_id],
                   'fatture': [fat_by_id[i].numero for i in ddt_fatture.get(d.id, [])],
                   'pezzi': round(sum(float(r.quantita or 0) for r in d.righe), 3), 'righe': righe_doc(d)}
                  for d in sorted(ddt, key=lambda d: (d.data_documento or datetime.min.date(), d.id), reverse=True)
                  if not cliente_id or d.cliente_id == cliente_id]
    elenco_fatture = [{**_doc_tw_dict(f), 'ddt_rif': [r.numero_rif for r in f.riferimenti if r.tipo_rif == 'DDT'],
                       'ddt_trovati': [ddt_by_id[i].numero for i in fatt_ddt.get(f.id, [])],
                       'ordini': [ord_by_id[i].numero_ordine for i in fatt_ordini.get(f.id, []) if i in ord_by_id],
                       'pezzi': round(sum(float(r.quantita or 0) for r in f.righe), 3), 'righe': righe_doc(f)}
                      for f in sorted(fatture, key=lambda f: (f.data_documento or datetime.min.date(), f.id), reverse=True)
                      if not cliente_id or f.cliente_id == cliente_id]
    orfani_ddt = [d for d in ddt if d.id not in usati_ddt and (not cliente_id or d.cliente_id == cliente_id)]
    orfane_fat = [f for f in fatture if f.id not in usati_fat and (not cliente_id or f.cliente_id == cliente_id)]
    return jsonify(ok=True, ordini=quadro, ddt_tutti=elenco_ddt, fatture_tutte=elenco_fatture,
                   ddt_senza_ordine=[{**_doc_tw_dict(d), 'ordini_rif': [r.numero_rif for r in d.riferimenti
                                                                        if r.tipo_rif == 'ORDINE']} for d in orfani_ddt],
                   fatture_senza_abbinamento=[{**_doc_tw_dict(f), 'riferimenti': [f'{r.tipo_rif} {r.numero_rif}'
                                                                                 for r in f.riferimenti]}
                                              for f in orfane_fat])


@cl_bp.get('/api/triple-watch/documenti/<int:did>')
def api_tw_documento(did):
    errore = _schema_tw_mancante()
    if errore:
        return errore
    d = db.session.get(ClTwDocumento, did) or abort(404)
    return jsonify(ok=True, documento={**_doc_tw_dict(d),
                   'riferimenti': [{'tipo': r.tipo_rif, 'numero': r.numero_rif} for r in d.riferimenti],
                   'righe': [{'ordine_rif': r.numero_ordine_rif, 'codice': r.codice, 'descrizione': r.descrizione,
                              'quantita': float(r.quantita) if r.quantita is not None else None} for r in d.righe],
                   'testo_grezzo': d.testo_grezzo})


@cl_bp.delete('/api/triple-watch/documenti/<int:did>')
def api_tw_elimina_documento(did):
    errore = _schema_tw_mancante()
    if errore:
        return errore
    d = db.session.get(ClTwDocumento, did) or abort(404)
    _audit('TRIPLE_WATCH_DOC_ELIMINATO', 'cl_tw_documento', d.id, prima=_doc_tw_dict(d))
    db.session.delete(d)
    db.session.commit()
    return jsonify(ok=True)


# ── Ordine inserito A MANO (Mauri, 28/11/2026): quando non si fa in tempo a
# metterlo in Zucchetti e stampare il PDF. Solo Angelo. Quando poi arriva il PDF
# con lo stesso numero, l'import lo aggancia a questo ordine invece di duplicarlo.
ORDINE_A_MANO = 'INSERITO A MANO'


@cl_bp.post('/api/ordini/manuale')
@solo_angelo
def api_ordine_manuale():
    d = request.get_json(silent=True) or {}
    cliente = db.session.get(ClCliente, d.get('cliente_id') or 0)
    if not cliente:
        return jsonify(ok=False, error='Scegli il cliente.'), 400
    numero = str(d.get('numero_ordine') or '').strip().upper()[:50]
    if not numero:
        return jsonify(ok=False, error='Scrivi il numero ordine (es. il numero che avrà in Zucchetti, 300/IS).'), 400
    righe = []
    for r in d.get('righe') or []:
        codice = str(r.get('codice') or '').strip().upper()[:100]
        if not codice:
            continue
        try:
            q = float(r.get('quantita'))
        except (TypeError, ValueError):
            return jsonify(ok=False, error=f'Quantità non valida per {codice}.'), 400
        if q < 0:
            return jsonify(ok=False, error=f'Quantità negativa per {codice}.'), 400
        righe.append((codice, str(r.get('descrizione') or '').strip()[:300], q))
    if not righe:
        return jsonify(ok=False, error="Aggiungi almeno una riga (codice e quantità)."), 400
    try:
        esistenti = {a.codice: a for a in ClArticoloCliente.query.filter(
            ClArticoloCliente.cliente_id == cliente.id, ClArticoloCliente.codice.in_([r[0] for r in righe])).all()}
        creati = []
        for codice, descr, _ in righe:
            if codice not in esistenti:
                a = ClArticoloCliente(cliente_id=cliente.id, codice=codice, descrizione=descr, udm='PZ',
                                      tracciamento='LOTTO', attivo=True)
                db.session.add(a)
                db.session.flush()
                esistenti[codice] = a
                creati.append(codice)
        conferma = d.get('conferma_subito', True) is not False
        o = ClOrdine(cliente_id=cliente.id, numero_ordine=numero, rif_cliente=str(d.get('rif_cliente') or '')[:50],
                     data_documento=_data_ordine(d.get('data_documento') or '') or datetime.now().date(),
                     stato='CONFERMATO' if conferma else 'BOZZA', filename=ORDINE_A_MANO,
                     note=str(d.get('note') or '')[:2000], confermato_il=_adesso() if conferma else None)
        db.session.add(o)
        db.session.flush()
        for n, (codice, descr, q) in enumerate(righe, start=1):
            db.session.add(ClOrdineRiga(ordine_id=o.id, n_riga=n, articolo_id=esistenti[codice].id,
                                        descrizione=descr or esistenti[codice].descrizione, quantita=q))
        _audit('ORDINE_A_MANO', 'cl_ordine', o.id, dopo={'numero_ordine': numero, 'righe': len(righe),
                                                         'codici_creati': creati, 'confermato': conferma})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error='Esiste già un ordine con questo numero per questo cliente.'), 409
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True), codici_creati=creati)


def _numero_opzionale(d, chiave, etichetta):
    """Legge un numero opzionale dal payload: chiave assente = non toccare
    (None, sentinella 'non passato'); chiave presente ma vuota/null = azzera
    esplicitamente (0.0, usato per quantita/evasa) — vedi i due chiamanti per
    come trattano questi due casi diversamente da 'numero non valido'."""
    if chiave not in d:
        return None
    v = d.get(chiave)
    if v in (None, ''):
        return 0.0
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f'{etichetta} non valida.')
    if v < 0:
        raise ValueError(f'{etichetta} non può essere negativa.')
    return v


@cl_bp.put('/api/ordini/<int:oid>/righe/<int:rid>')
def api_modifica_riga_ordine(oid, rid):
    """
    Corregge una riga ordine — richiesta di Mauri (02/10/2026): NON più solo
    in BOZZA, sempre (un CONFERMATO può aver bisogno di una correzione tanto
    quanto una bozza). Tre campi indipendenti, tutti opzionali — si manda solo
    quello che si vuole cambiare:
      - 'quantita': l'Ordinata.
      - 'quantita_prodotta': correzione manuale della Prodotta — stringa
        vuota/null per TOGLIERE la correzione e tornare al calcolo automatico
        da MasterWork (quantita_prodotta_manuale = NULL), un numero per
        impostarla.
      - 'quantita_evasa': colonna vera dalla 0004 (prima sempre 0.0).
    """
    o = db.session.get(ClOrdine, oid) or abort(404)
    r = next((x for x in o.righe if x.id == rid), None) or abort(404)
    d = request.get_json(silent=True) or {}
    prima = {'quantita': float(r.quantita), 'quantita_evasa': float(r.quantita_evasa or 0),
             'quantita_prodotta_manuale': float(r.quantita_prodotta_manuale) if r.quantita_prodotta_manuale is not None else None}
    dopo = {}
    try:
        if 'quantita' in d:
            try:
                nuova_qta = float(d.get('quantita'))
            except (TypeError, ValueError):
                raise ValueError('Quantità ordinata non valida.')
            if nuova_qta < 0:
                raise ValueError('La quantità ordinata non può essere negativa.')
            r.quantita = nuova_qta
            dopo['quantita'] = nuova_qta
        if 'quantita_evasa' in d:
            nuova_evasa = _numero_opzionale(d, 'quantita_evasa', 'Quantità evasa')
            r.quantita_evasa = nuova_evasa
            dopo['quantita_evasa'] = nuova_evasa
        if 'quantita_prodotta' in d:
            v = d.get('quantita_prodotta')
            if v in (None, ''):
                r.quantita_prodotta_manuale = None
                dopo['quantita_prodotta_manuale'] = None
            else:
                try:
                    nuova_prodotta = float(v)
                except (TypeError, ValueError):
                    raise ValueError('Quantità prodotta non valida.')
                if nuova_prodotta < 0:
                    raise ValueError('La quantità prodotta non può essere negativa.')
                r.quantita_prodotta_manuale = nuova_prodotta
                dopo['quantita_prodotta_manuale'] = nuova_prodotta
    except ValueError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    if not dopo:
        return jsonify(ok=False, error='Nessun campo da modificare.'), 400
    _audit('ORDINE_RIGA_MODIFICATA', 'cl_ordine_riga', r.id, prima=prima, dopo=dopo)
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True))


@cl_bp.post('/api/ordini/<int:oid>/righe')
def api_aggiungi_riga_ordine(oid):
    """
    Aggiunge una riga a un ordine esistente — richiesta di Mauri
    (02/10/2026), stesso "menù intelligente" usato per gli altri codici
    cliente: si manda 'codice' (obbligatorio, maiuscolo) scelto dal catalogo
    del cliente (/api/articoli?cliente_id=...) oppure digitato nuovo — in tal
    caso va passata anche 'descrizione' e viene creato il codice di
    magazzino (ClArticoloCliente) al volo, stesso comportamento
    dell'importazione PDF. 'quantita' obbligatoria, 'prezzo_unitario'
    opzionale. Nessun vincolo di stato: si può aggiungere una riga a un
    ordine in qualunque stato, anche CONFERMATO (correzione).
    """
    o = db.session.get(ClOrdine, oid) or abort(404)
    d = request.get_json(silent=True) or {}
    try:
        codice = _testo(d, 'codice', 100, True).upper()
        quantita = float(d.get('quantita'))
    except (ValueError, TypeError):
        return jsonify(ok=False, error='Codice e quantità sono obbligatori.'), 400
    if quantita < 0:
        return jsonify(ok=False, error='La quantità non può essere negativa.'), 400
    prezzo_unitario = None
    if d.get('prezzo_unitario') not in (None, ''):
        try:
            prezzo_unitario = float(d['prezzo_unitario'])
        except (TypeError, ValueError):
            return jsonify(ok=False, error='Prezzo unitario non valido.'), 400

    articolo = ClArticoloCliente.query.filter_by(cliente_id=o.cliente_id, codice=codice).first()
    nuovo_codice = False
    if not articolo and not angelo_attivo():
        return jsonify(ok=False, angelo=False, error=f'Il codice "{codice}" non esiste ancora: un codice nuovo a mano '
                                                     f'lo può creare solo Angelo (🔒 Accesso Angelo).'), 403
    if not articolo:
        descrizione = _testo(d, 'descrizione', 300)
        if not descrizione:
            return jsonify(ok=False, error=f'Codice "{codice}" non ancora a catalogo per questo cliente — indica anche la descrizione per crearlo.'), 400
        udm = (d.get('udm') or 'PZ').upper()
        if udm not in UDM:
            udm = 'PZ'
        articolo = ClArticoloCliente(cliente_id=o.cliente_id, codice=codice, descrizione=descrizione,
                                     udm=udm, tracciamento='LOTTO', attivo=True)
        db.session.add(articolo)
        db.session.flush()
        nuovo_codice = True

    prossimo_n = (max((r.n_riga for r in o.righe), default=0)) + 1
    riga = ClOrdineRiga(ordine_id=o.id, n_riga=prossimo_n, articolo_id=articolo.id,
                        descrizione=d.get('descrizione') or articolo.descrizione,
                        quantita=quantita, prezzo_unitario=prezzo_unitario)
    db.session.add(riga)
    _audit('ORDINE_RIGA_AGGIUNTA', 'cl_ordine_riga', '', dopo={'ordine_id': o.id, 'codice': codice,
                                                               'quantita': quantita, 'codice_nuovo': nuovo_codice})
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True)), 201


@cl_bp.delete('/api/ordini/<int:oid>/righe/<int:rid>')
def api_elimina_riga_ordine(oid, rid):
    """Toglie una riga dall'ordine — richiesta di Mauri (02/10/2026): NON più
    solo in BOZZA, sempre. Resta il vincolo che non può essere l'ultima (un
    ordine senza righe non ha senso: si elimina l'intero ordine, se serve)."""
    o = db.session.get(ClOrdine, oid) or abort(404)
    r = next((x for x in o.righe if x.id == rid), None) or abort(404)
    if len(o.righe) <= 1:
        return jsonify(ok=False, error='Un ordine deve avere almeno una riga — elimina l\'intero ordine, se serve.'), 409
    _audit('ORDINE_RIGA_ELIMINATA', 'cl_ordine_riga', r.id, prima={'codice': r.articolo.codice if r.articolo else '',
                                                                    'quantita': float(r.quantita)})
    db.session.delete(r)
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine_dict(o, con_righe=True))


@cl_bp.delete('/api/ordini/<int:oid>')
def api_elimina_ordine(oid):
    """
    Elimina un intero ordine — richiesta di Mauri (02/10/2026): PRIMA solo se
    ancora in BOZZA, ora consentito qualunque sia lo stato (anche
    CONFERMATO) PURCHÉ non sia già movimentato (vedi _ordine_movimentato:
    nessuna riga con Prodotta o Evasa sopra zero) — un ordine con produzione
    o evasione reale sopra non si tocca più da qui, si annullerebbe un dato
    che altrove (OrdineProduzione/DDT) resta vero."""
    o = db.session.get(ClOrdine, oid) or abort(404)
    if _ordine_movimentato(o):
        return jsonify(ok=False, error='Ordine già movimentato (produzione o evasione registrata) — non eliminabile.'), 409
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
    stati = STATI_ATTIVI if versione_schema() >= VERSIONE_SCHEMA_ANOMALIE else ('CONFERMATO',)
    ordini = (ClOrdine.query.filter(ClOrdine.stato.in_(stati))
             .options(db.joinedload(ClOrdine.cliente),
                      db.joinedload(ClOrdine.righe).joinedload(ClOrdineRiga.articolo))
             .order_by(ClOrdine.cliente_id, ClOrdine.numero_ordine).all())
    if not ordini:
        return jsonify([])
    articolo_ids = {r.articolo_id for o in ordini for r in o.righe}
    con_foto = {aid for (aid,) in db.session.query(ClFotoArticolo.articolo_id)
               .filter(ClFotoArticolo.articolo_id.in_(articolo_ids)).all()} if articolo_ids else set()
    # Vedi _prodotta_per_codici: una sola query invece di una per riga — con
    # NESSUN limite sul numero di ordini CONFERMATO (a differenza di
    # /api/ordini), questo endpoint era il più esposto al bug "Errore di
    # rete" per troppe query.
    mappa_prodotta = _prodotta_per_codici(r.articolo.codice for o in ordini for r in o.righe if r.articolo)

    revisioni = _revisioni(r.id for o in ordini for r in o.righe)
    gruppi = {}
    for o in ordini:
        g = gruppi.setdefault(o.cliente_id, {'cliente_id': o.cliente_id,
                                             'cliente': o.cliente.ragione_sociale if o.cliente else '', 'ordini': []})
        righe = []
        for r in o.righe:
            codice = r.articolo.codice if r.articolo else ''
            ordinata = float(r.quantita)
            prodotta = _prodotta_riga(r, mappa_prodotta)
            saldo = round(ordinata - prodotta, 3)
            pct = round(min(100, prodotta / ordinata * 100)) if ordinata > 0 else 100
            righe.append({'riga_id': r.id, 'revisione': _revisione_dict(revisioni.get(r.id)),
                          'articolo_id': r.articolo_id, 'codice': codice, 'descrizione': r.descrizione,
                          'quantita': ordinata, 'quantita_prodotta': prodotta,
                          'quantita_evasa': float(r.quantita_evasa or 0),
                          'saldo_da_produrre': saldo, 'pct': pct, 'ha_foto': r.articolo_id in con_foto})
        g['ordini'].append({'id': o.id, 'numero_ordine': o.numero_ordine, 'rif_cliente': o.rif_cliente,
                            'sospeso': o.stato == 'SOSPESO', 'righe': righe})
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
    evase = {}
    if versione_schema() >= VERSIONE_SCHEMA_ORDINI:
        righe_ordine = (db.session.query(ClOrdineRiga.articolo_id, db.func.coalesce(db.func.sum(ClOrdineRiga.quantita), 0))
                        .join(ClOrdine, ClOrdine.id == ClOrdineRiga.ordine_id)
                        .filter(ClOrdine.stato.in_(STATI_ATTIVI), ClOrdineRiga.articolo_id.in_(articolo_ids))
                        .group_by(ClOrdineRiga.articolo_id).all())
        ordinate = dict(righe_ordine)
    if versione_schema() >= VERSIONE_SCHEMA_RIGHE_MODIFICABILI:
        righe_evase = (db.session.query(ClOrdineRiga.articolo_id, db.func.coalesce(db.func.sum(ClOrdineRiga.quantita_evasa), 0))
                       .join(ClOrdine, ClOrdine.id == ClOrdineRiga.ordine_id)
                       .filter(ClOrdine.stato.in_(STATI_ATTIVI), ClOrdineRiga.articolo_id.in_(articolo_ids))
                       .group_by(ClOrdineRiga.articolo_id).all())
        evase = dict(righe_evase)

    con_foto = set()
    if versione_schema() >= VERSIONE_SCHEMA_LIVE:
        con_foto = {aid for (aid,) in db.session.query(ClFotoArticolo.articolo_id)
                   .filter(ClFotoArticolo.articolo_id.in_(articolo_ids)).all()}

    # Vedi _prodotta_per_codici: una sola query invece di una per articolo.
    mappa_prodotta = _prodotta_per_codici(a.codice for a in articoli)

    gruppi = {}
    for a in articoli:
        g = gruppi.setdefault(a.cliente_id, {'cliente_id': a.cliente_id,
                                             'cliente': a.cliente.ragione_sociale if a.cliente else '', 'articoli': []})
        ordinata = float(ordinate.get(a.id, 0) or 0)
        prodotta = mappa_prodotta.get(a.codice, 0.0)
        g['articoli'].append({'articolo_id': a.id, 'codice': a.codice, 'descrizione': a.descrizione, 'udm': a.udm,
                              'giacenza': float(giacenze.get(a.id, 0) or 0), 'quantita_ordinata': ordinata,
                              'quantita_prodotta': prodotta, 'quantita_evasa': float(evase.get(a.id, 0) or 0),
                              'saldo_da_produrre': round(ordinata - prodotta, 3), 'ha_foto': a.id in con_foto})
    return jsonify(sorted(gruppi.values(), key=lambda g: g['cliente']))
