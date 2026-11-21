"""
Contatori per MASTERCONTROL (plancia punti critici di Iron Segnaletica,
repo IronSegnaletica/MASTERCONTROL).

    GET /api/control/counters
    Header: X-Control-Token: <MASTERCONTROL_TOKEN>

Restituisce SOLO numeri, ognuno con il link alla pagina di IronProduction
dove si vede l'elenco. Sola lettura: non scrive niente nel database.
Dove esiste già un calcolo in IronProduction (Kanban HPI, Lista Fabbisogni,
Grezzo IW, In Trattamento) si usa QUELLO, così il numero in plancia è lo
stesso che si vede qui dentro.

Elenco indicatori deciso da Mauri il 21/11/2026:
  ACQUISTI PRODUZIONE: da confermare, in arrivo, da ritirare, in ritardo
  PRODUZIONE: articoli sottoscorta da pianificare, OP con materiali da
    ordinare, OP da iniziare in ritardo (>10 gg dal rilascio senza nessuna
    fase iniziata), OP da saldare in ritardo (>7 gg dalla fine delle fasi
    prima della Saldatura)
  MAGAZZINO PRODUZIONE: materie prime / laserati / altri articoli
    sottoscorta, giacenza negativa (grezzi IW e finiti IW), articoli da
    trattare (Grezzo IW > 0), articoli in trattamento esterno

Variabile Railway: MASTERCONTROL_TOKEN (stesso valore messo in
MASTERCONTROL → Fonti → ironproduction). Vuota = endpoint disabilitato.
"""
import hmac
import logging
import os
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, jsonify, request

from models import (db, OrdineProduzione, EventoConsuntivoPP, OrdineAcquistoWood,
                    GiacenzaWood, CicloLavoroWood)

mastercontrol_bp = Blueprint('mastercontrol', __name__)
log = logging.getLogger('ironproduction.mastercontrol')

STATI_OP_APERTI = ('Creato', 'Rilasciato', 'In esecuzione')
GIORNI_INIZIO_RITARDO = 10     # OP rilasciato da più di 10 gg e nessuna fase iniziata
GIORNI_SALDATURA_RITARDO = 7   # fasi prima della Saldatura ferme da più di 7 gg, Saldatura non finita


def _oggi():
    return date.today()


# ── ACQUISTI PRODUZIONE (Ordini Acquisto Iron Wood) ──────────────────────

def _oa_aperti():
    return OrdineAcquistoWood.query.filter(OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all()


def conta_oa_da_confermare(ctx):
    return sum(1 for o in _oa_aperti() if o.stato_label in ('DA_CONFERMARE', 'LETTURA_DA_VERIFICARE'))


def conta_oa_in_arrivo(ctx):
    """Confermati, non ancora ricevuti e NON in ritardo (quelli in ritardo hanno il loro riquadro)."""
    oggi = _oggi()
    return sum(1 for o in _oa_aperti()
               if o.stato_label in ('ORDINE_CONFERMATO', 'ORDINE_IN_ARRIVO')
               and not (o.data_consegna and o.data_consegna < oggi))


def conta_oa_da_ritirare(ctx):
    return sum(1 for o in _oa_aperti() if o.stato_label == 'ORDINE_DA_RITIRARE')


def conta_oa_in_ritardo(ctx):
    """Stessa regola della dashboard/Totem Acquisti: non ricevuti con data consegna passata."""
    oggi = _oggi()
    return sum(1 for o in _oa_aperti() if o.data_consegna and o.data_consegna < oggi)


# ── PRODUZIONE ───────────────────────────────────────────────────────────

def _kanban_hpi(ctx):
    if 'hpi' not in ctx:
        from blueprints.kanban.routes import api_kanban_hpi_dati
        dati = api_kanban_hpi_dati().get_json()
        ctx['hpi'] = [p for g in dati for p in g.get('prodotti', [])]
    return ctx['hpi']


def _fabbisogni(ctx):
    if 'fabbisogni' not in ctx:
        from blueprints.acquisti_wood.routes import calcola_lista_fabbisogni
        ctx['fabbisogni'] = calcola_lista_fabbisogni()
    return ctx['fabbisogni']


def conta_sottoscorta_da_pianificare(ctx):
    """Prodotti Kanban sotto la scorta minima (stesso calcolo di Kanban HPI)."""
    return sum(1 for p in _kanban_hpi(ctx) if p.get('sotto_scorta'))


def conta_op_materiali_da_ordinare(ctx):
    """OP aperti con almeno un materiale DA ORDINARE (Lista Fabbisogni) ancora mancante per loro."""
    op = set()
    for r in _fabbisogni(ctx):
        if r.get('stato') != 'DA_ORDINARE':
            continue
        for i in r.get('impegni', []):
            if (i.get('mancante') or 0) > 0:
                op.add(i['op_code'])
    return len(op)


def _eventi_per_op(op_codes):
    eventi = {}
    if not op_codes:
        return eventi
    for e in EventoConsuntivoPP.query.filter(EventoConsuntivoPP.op_code.in_(op_codes)).all():
        eventi.setdefault(e.op_code, []).append(e)
    return eventi


def conta_op_inizio_ritardo(ctx, oggi=None):
    """OP rilasciati da più di 10 giorni su cui non è ancora stata dichiarata NESSUNA fase."""
    oggi = oggi or _oggi()
    limite = datetime.combine(oggi, datetime.min.time()) - timedelta(days=GIORNI_INIZIO_RITARDO)
    candidati = OrdineProduzione.query.filter(
        OrdineProduzione.stato.in_(STATI_OP_APERTI),
        OrdineProduzione.data_rilascio.isnot(None),
        OrdineProduzione.data_rilascio < limite,
    ).all()
    eventi = _eventi_per_op([o.codice for o in candidati])
    return sum(1 for o in candidati if not eventi.get(o.codice))


def _e_saldatura(nome_fase):
    return 'sald' in (nome_fase or '').lower()


def conta_op_saldatura_ritardo(ctx, oggi=None):
    """OP con Saldatura nel ciclo: le fasi PRIMA della Saldatura sono ferme da più
    di 7 giorni (ultima dichiarazione) e la Saldatura non è ancora completa."""
    oggi = oggi or _oggi()
    limite = datetime.combine(oggi, datetime.min.time()) - timedelta(days=GIORNI_SALDATURA_RITARDO)
    aperti = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_OP_APERTI)).all()
    if not aperti:
        return 0
    codici = {o.codice_articolo for o in aperti}
    con_saldatura = {c.codice for c in CicloLavoroWood.query.filter(CicloLavoroWood.codice.in_(codici)).all()
                     if c.centro_costo and _e_saldatura(c.centro_costo.nome)}
    aperti = [o for o in aperti if o.codice_articolo in con_saldatura]
    eventi = _eventi_per_op([o.codice for o in aperti])
    n = 0
    for o in aperti:
        ev = eventi.get(o.codice, [])
        prima = [e for e in ev if not _e_saldatura(e.fase)]
        if not prima:
            continue          # le fasi prima della Saldatura non sono neanche iniziate
        saldati = sum(e.pezzi_buoni or 0 for e in ev if _e_saldatura(e.fase) and e.componente is None)
        if saldati >= (o.qta_pianificata or 0):
            continue          # Saldatura completata
        if max(e.timestamp_evento for e in prima) < limite:
            n += 1
    return n


# ── MAGAZZINO PRODUZIONE ─────────────────────────────────────────────────

def _sottoscorta(ctx):
    return [r for r in _fabbisogni(ctx) if (r.get('sottoscorta') or 0) > 0]


def conta_mp_sottoscorta(ctx):
    return sum(1 for r in _sottoscorta(ctx) if r.get('tipo') == 'MATERIA_PRIMA_FORNITORE')


def conta_laserati_sottoscorta(ctx):
    return sum(1 for r in _sottoscorta(ctx) if r.get('tipo') == 'LASERATO')


def conta_altri_sottoscorta(ctx):
    return sum(1 for r in _sottoscorta(ctx) if r.get('tipo') not in ('MATERIA_PRIMA_FORNITORE', 'LASERATO'))


def _grezzo_iw(ctx):
    if 'grezzo' not in ctx:
        from blueprints.magazzino.routes import _grezzo_iw_per_codici
        codici = [r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
        ctx['grezzo'] = _grezzo_iw_per_codici(codici) if codici else {}
    return ctx['grezzo']


def conta_giacenza_negativa(ctx):
    """Codici con giacenza negativa: Finiti IW / magazzino (GiacenzaWood) oppure Grezzo IW."""
    negativi = {g.codice.upper() for g in GiacenzaWood.query.filter(GiacenzaWood.quantita < 0).all() if g.codice}
    negativi |= {c.upper() for c, q in _grezzo_iw(ctx).items() if (q or 0) < 0}
    return len(negativi)


def conta_da_trattare(ctx):
    """Stessa fonte di Terzisti → Materiali da trattare: Grezzo IW > 0."""
    return sum(1 for q in _grezzo_iw(ctx).values() if (q or 0) > 0)


def conta_in_trattamento(ctx):
    """Codici con pezzi ancora presso il terzista (spediti e non rientrati)."""
    from blueprints.magazzino.routes import _in_trattamento_per_codici
    codici = [r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
    return sum(1 for q in _in_trattamento_per_codici(codici).values() if (q or 0) > 0)


# code, area, label, funzione, pagina di dettaglio
CONTATORI = [
    ('ACQ_PROD_DA_CONFERMARE', 'ACQUISTI_PRODUZIONE', 'Ordini da confermare', conta_oa_da_confermare, '/ordini-acquisto-wood'),
    ('ACQ_PROD_IN_ARRIVO', 'ACQUISTI_PRODUZIONE', 'Ordini in arrivo', conta_oa_in_arrivo, '/materiale-in-arrivo'),
    ('ACQ_PROD_DA_RITIRARE', 'ACQUISTI_PRODUZIONE', 'Ordini da ritirare', conta_oa_da_ritirare, '/ordini-acquisto-wood'),
    ('ACQ_PROD_RITARDO', 'ACQUISTI_PRODUZIONE', 'Ordini in ritardo', conta_oa_in_ritardo, '/ordini-acquisto-wood'),

    ('PROD_SOTTOSCORTA_DA_PIANIFICARE', 'PRODUZIONE', 'Articoli sottoscorta da pianificare',
     conta_sottoscorta_da_pianificare, '/kanban-hpi'),
    ('PROD_OP_MATERIALI_DA_ORDINARE', 'PRODUZIONE', 'OP con materiali da ordinare',
     conta_op_materiali_da_ordinare, '/lista-fabbisogni'),
    ('PROD_OP_INIZIO_RITARDO', 'PRODUZIONE', 'OP da iniziare in ritardo', conta_op_inizio_ritardo, '/ordini-produzione'),
    ('PROD_OP_SALDATURA_RITARDO', 'PRODUZIONE', 'OP da saldare in ritardo', conta_op_saldatura_ritardo, '/ordini-produzione'),

    ('MAGP_MP_SOTTOSCORTA', 'MAGAZZINO_PRODUZIONE', 'Materie prime sottoscorta', conta_mp_sottoscorta, '/lista-fabbisogni'),
    ('MAGP_LASERATI_SOTTOSCORTA', 'MAGAZZINO_PRODUZIONE', 'Laserati sottoscorta', conta_laserati_sottoscorta, '/lista-fabbisogni'),
    ('MAGP_ALTRI_SOTTOSCORTA', 'MAGAZZINO_PRODUZIONE', 'Altri articoli sottoscorta', conta_altri_sottoscorta, '/lista-fabbisogni'),
    ('MAGP_GIACENZA_NEGATIVA', 'MAGAZZINO_PRODUZIONE', 'Giacenza reale negativa', conta_giacenza_negativa, '/magazzino'),
    ('MAGP_DA_TRATTARE', 'MAGAZZINO_PRODUZIONE', 'Articoli da trattare', conta_da_trattare, '/terzisti/da-trattare'),
    ('MAGP_IN_TRATTAMENTO', 'MAGAZZINO_PRODUZIONE', 'Articoli in trattamento esterno', conta_in_trattamento, '/terzisti'),
]


@mastercontrol_bp.get('/api/control/counters')
def api_control_counters():
    atteso = os.environ.get('MASTERCONTROL_TOKEN', '')
    if not atteso or not hmac.compare_digest(request.headers.get('X-Control-Token', ''), atteso):
        abort(403)
    base = (os.environ.get('PUBLIC_URL') or request.host_url).rstrip('/')
    if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
        base = 'https://' + base[len('http://'):]  # Railway: dietro proxy arriva come http
    ctx = {}   # calcoli pesanti (Kanban HPI, Lista Fabbisogni, Grezzo IW) fatti UNA volta per richiesta
    out = []
    for code, area, label, fn, path in CONTATORI:
        try:
            valore = int(fn(ctx))
        except Exception:
            # un contatore rotto non deve bloccare gli altri: MASTERCONTROL lo mostra grigio
            log.exception('Contatore MASTERCONTROL %s in errore', code)
            db.session.rollback()
            continue
        out.append({'code': code, 'area': area, 'label': label, 'value': valore, 'url': base + path})
    # Indirizzi degli altri programmi che IronProduction già conosce: servono a
    # MASTERCONTROL per collegarli da solo, senza inserire gli URL a mano.
    fonti = {}
    if os.environ.get('MASTERLOGISTIC_URL', '').strip():
        fonti['masterlogistic'] = os.environ['MASTERLOGISTIC_URL'].strip().rstrip('/')
    return jsonify(counters=out, fonti=fonti)
