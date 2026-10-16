"""
BADGE OPERATORE SUL TOTEM ALESSANDRO (richiesta Mauri, 16/10/2026).

Come alle casse del supermercato: il totem è bloccato e si sblocca passando il badge
(QR) sul lettore collegato al PC. Ogni dichiarazione di produzione fatta dal totem
porta il nome di chi l'ha fatta, visibile in "Area Capo — storico e correzioni".

- Solo gli operai segnati "Dichiaratore" nella Matrice Competenze hanno un badge valido.
- Il totem si richiude da solo dopo 2 minuti senza attività (o con "Esci").
- Angelo decide DA QUANDO parte il nuovo sistema (interruttore sul Cruscotto KPI, PIN
  Direzione). Prima di allora il totem funziona come oggi.
- Riguarda SOLO il Totem Alessandro e SOLO la fase di dichiarazione: le altre pagine
  e i monitor LIVE non cambiano.

Il lettore USB di codici a barre/QR funziona come una tastiera: "scrive" il codice e
preme Invio. Il codice si può anche digitare a mano.
"""
import io
import os
import secrets
from datetime import datetime

from flask import Blueprint, current_app, jsonify, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from models import db, OperatoreWood, ImpostazioneTotemWood

badge_totem_bp = Blueprint('badge_totem', __name__)

TOTEM_HEADER = 'X-Totem'
TOTEM_NOME = 'alessandro'
DURATA_TOKEN_S = 12 * 3600          # il blocco a 2 minuti è sul totem; questo è solo un tetto di sicurezza
MINUTI_INATTIVITA = 2


def _pin_ok(pin):
    return str(pin or '').strip() == os.environ.get('PIN_DIREZIONE', '1234')


def impostazione():
    imp = ImpostazioneTotemWood.query.first()
    if imp is None:
        imp = ImpostazioneTotemWood(badge_attivo=False)
        db.session.add(imp)
        db.session.flush()
    return imp


def badge_attivo():
    try:
        imp = ImpostazioneTotemWood.query.first()
        return bool(imp and imp.badge_attivo)
    except Exception:
        db.session.rollback()
        return False


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='badge-totem-alessandro')


def _solo_alfanumerico(testo):
    return ''.join(ch for ch in str(testo or '').upper() if ch.isascii() and ch.isalnum())


def nuovo_codice_badge():
    while True:
        codice = 'IRW-' + secrets.token_hex(5).upper()
        if not OperatoreWood.query.filter_by(codice_badge=codice).first():
            return codice


def operatore_da_token(token):
    """Operatore valido per il token, oppure None (scaduto, badge rigenerato, non più dichiaratore)."""
    if not token:
        return None
    try:
        dati = _serializer().loads(token, max_age=DURATA_TOKEN_S)
    except (BadSignature, SignatureExpired):
        return None
    o = db.session.get(OperatoreWood, dati.get('id'))
    if not o or not o.attivo or not o.dichiaratore or o.codice_badge != dati.get('badge'):
        return None
    return o


def operatore_dichiarazione(d):
    """Chiamata dalle API di dichiarazione. Ritorna (nome_operatore | None, errore | None).
    Vale solo per le richieste del Totem Alessandro (intestazione X-Totem): le altre pagine
    di dichiarazione non cambiano. Con il sistema attivo, senza badge valido si rifiuta."""
    if (request.headers.get(TOTEM_HEADER) or '').lower() != TOTEM_NOME:
        return None, None
    o = operatore_da_token((d or {}).get('badge_token'))
    if o:
        return o.nome, None
    if badge_attivo():
        return None, '🔒 Totem bloccato: passa il badge per dichiarare.'
    return None, None


# ── Totem ────────────────────────────────────────────────────────────────────
@badge_totem_bp.get('/api/totem-badge/stato')
def api_stato():
    imp = ImpostazioneTotemWood.query.first()
    return jsonify(attivo=bool(imp and imp.badge_attivo),
                   attivato_il=imp.attivato_il.isoformat() if imp and imp.attivato_il else None,
                   minuti_inattivita=MINUTI_INATTIVITA)


@badge_totem_bp.post('/api/totem-badge/sblocca')
def api_sblocca():
    codice = _solo_alfanumerico((request.get_json(silent=True) or {}).get('codice'))
    if not codice:
        return jsonify(ok=False, error='Codice vuoto'), 400
    # Il lettore USB "scrive" come una tastiera americana: su un PC con tastiera italiana il
    # trattino del badge può arrivare come un altro simbolo. Si confrontano solo lettere e cifre.
    o = next((x for x in OperatoreWood.query.filter(OperatoreWood.codice_badge.isnot(None)).all()
              if _solo_alfanumerico(x.codice_badge) == codice), None)
    if not o or not o.attivo:
        return jsonify(ok=False, error='Badge non riconosciuto'), 404
    if not o.dichiaratore:
        return jsonify(ok=False, error=f'{o.nome} non è abilitato a dichiarare (qualifica "Dichiaratore" in Matrice Competenze)'), 403
    token = _serializer().dumps({'id': o.id, 'badge': o.codice_badge})
    return jsonify(ok=True, id=o.id, nome=o.nome, token=token, minuti_inattivita=MINUTI_INATTIVITA)


# ── Angelo: da quando parte il nuovo sistema ─────────────────────────────────
@badge_totem_bp.post('/api/totem-badge/attiva')
def api_attiva():
    d = request.get_json(silent=True) or {}
    if not _pin_ok(d.get('pin')):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    attivo = bool(d.get('attivo'))
    if attivo and not OperatoreWood.query.filter_by(dichiaratore=True, attivo=True).count():
        return jsonify(ok=False, error='Nessun operatore "Dichiaratore": prima qualificali in Matrice Competenze e stampa i badge.'), 400
    imp = impostazione()
    if attivo and not imp.badge_attivo:
        imp.attivato_il = datetime.utcnow()
    imp.badge_attivo = attivo
    db.session.commit()
    return jsonify(ok=True, attivo=imp.badge_attivo, attivato_il=imp.attivato_il.isoformat() if imp.attivato_il else None)


# ── Matrice Competenze: qualifica "Dichiaratore" e badge ─────────────────────
@badge_totem_bp.post('/api/operatori/<int:oid>/dichiaratore')
def api_dichiaratore(oid):
    o = OperatoreWood.query.get_or_404(oid)
    o.dichiaratore = bool((request.get_json(silent=True) or {}).get('dichiaratore'))
    if o.dichiaratore and not o.codice_badge:
        o.codice_badge = nuovo_codice_badge()
    db.session.commit()
    return jsonify(ok=True, dichiaratore=o.dichiaratore, ha_badge=bool(o.codice_badge))


@badge_totem_bp.post('/api/operatori/<int:oid>/badge/rigenera')
def api_rigenera_badge(oid):
    """Badge perso o rubato: nuovo codice, quello vecchio non sblocca più niente."""
    if not _pin_ok((request.get_json(silent=True) or {}).get('pin')):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    o = OperatoreWood.query.get_or_404(oid)
    o.codice_badge = nuovo_codice_badge()
    db.session.commit()
    return jsonify(ok=True)


def _qr_svg(testo):
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(testo, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode('utf-8')
    return svg[svg.find('<svg'):]


@badge_totem_bp.get('/operatori/badge')
def pagina_badge():
    """Badge da stampare (formato carta di credito): ?id=3 per uno solo, altrimenti tutti i dichiaratori."""
    oid = request.args.get('id', type=int)
    q = OperatoreWood.query.filter_by(attivo=True, dichiaratore=True)
    if oid:
        q = q.filter(OperatoreWood.id == oid)
    operatori = q.order_by(OperatoreWood.nome).all()
    badge = [{'nome': o.nome, 'codice': o.codice_badge, 'qr': _qr_svg(o.codice_badge)} for o in operatori if o.codice_badge]
    return render_template('produzione_pp/badge_operatori.html', badge=badge)
