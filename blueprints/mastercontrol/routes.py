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
MAX_RIGHE = 15                 # righe di dettaglio mandate per ogni riquadro (popup di MASTERCONTROL)


def _oggi():
    return date.today()


def _gg(d):
    return d.strftime('%d/%m') if d else 's.d.'


def _oa_txt(o, extra=''):
    return f"{o.fornitore or 'Fornitore ?'} – ordine n. {o.ordine_n or 'N/D'}" + (f" – {extra}" if extra else '')


# ── ACQUISTI PRODUZIONE (Ordini Acquisto Iron Wood) ──────────────────────

def _oa_aperti():
    return OrdineAcquistoWood.query.filter(OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all()


def conta_oa_da_confermare(ctx):
    return [_oa_txt(o, 'lettura PDF da verificare' if o.stato_label == 'LETTURA_DA_VERIFICARE' else 'senza conferma')
            for o in _oa_aperti() if o.stato_label in ('DA_CONFERMARE', 'LETTURA_DA_VERIFICARE')]


def conta_oa_in_arrivo(ctx):
    """Confermati, non ancora ricevuti e NON in ritardo (quelli in ritardo hanno il loro riquadro)."""
    oggi = _oggi()
    righe = [o for o in _oa_aperti()
             if o.stato_label in ('ORDINE_CONFERMATO', 'ORDINE_IN_ARRIVO')
             and not (o.data_consegna and o.data_consegna < oggi)]
    righe.sort(key=lambda o: o.data_consegna or date.max)
    return [_oa_txt(o, f"consegna {_gg(o.data_consegna)}") for o in righe]


def conta_oa_da_ritirare(ctx):
    return [_oa_txt(o, f"pronto dal {_gg(o.data_consegna)}") for o in _oa_aperti() if o.stato_label == 'ORDINE_DA_RITIRARE']


def conta_oa_in_ritardo(ctx):
    """Stessa regola della dashboard/Totem Acquisti: non ricevuti con data consegna passata."""
    oggi = _oggi()
    righe = sorted((o for o in _oa_aperti() if o.data_consegna and o.data_consegna < oggi), key=lambda o: o.data_consegna)
    return [_oa_txt(o, f"doveva arrivare il {_gg(o.data_consegna)} (+{(oggi - o.data_consegna).days} gg)") for o in righe]


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
    righe = sorted((p for p in _kanban_hpi(ctx) if p.get('sotto_scorta')), key=lambda p: p.get('saldo_c_scorta') or 0)
    return [f"{(p.get('nome') or '?').split(' — ')[0]} – saldo c/scorta {p.get('saldo_c_scorta') or 0:g}"
            + (f" – riservato clienti {p['riservato_clienti']}" if p.get('riservato_clienti') else '') for p in righe]


def conta_op_materiali_da_ordinare(ctx):
    """OP aperti con almeno un materiale DA ORDINARE (Lista Fabbisogni) ancora mancante per loro."""
    op = {}
    for r in _fabbisogni(ctx):
        if r.get('stato') != 'DA_ORDINARE':
            continue
        for i in r.get('impegni', []):
            if (i.get('mancante') or 0) > 0:
                op.setdefault(i['op_code'], {'prodotto': i.get('prodotto') or '', 'mancano': []})['mancano'].append(r['codice'])
    return [f"{codice} {v['prodotto']} – da ordinare: {', '.join(v['mancano'][:4])}{' …' if len(v['mancano']) > 4 else ''}"
            for codice, v in sorted(op.items())]


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
    righe = sorted((o for o in candidati if not eventi.get(o.codice)), key=lambda o: o.data_rilascio)
    return [f"{o.codice} {o.codice_articolo} – commessa {o.commessa or '-'} – rilasciato il {_gg(o.data_rilascio)}, "
            f"nessuna fase iniziata da {(oggi - o.data_rilascio.date()).days} gg" for o in righe]


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
    righe = []
    for o in aperti:
        ev = eventi.get(o.codice, [])
        prima = [e for e in ev if not _e_saldatura(e.fase)]
        if not prima:
            continue          # le fasi prima della Saldatura non sono neanche iniziate
        saldati = sum(e.pezzi_buoni or 0 for e in ev if _e_saldatura(e.fase) and e.componente is None)
        if saldati >= (o.qta_pianificata or 0):
            continue          # Saldatura completata
        ultima = max(e.timestamp_evento for e in prima)
        if ultima < limite:
            righe.append((ultima, f"{o.codice} {o.codice_articolo} – commessa {o.commessa or '-'} – fasi prima della "
                                  f"Saldatura ferme dal {_gg(ultima)}, saldati {saldati}/{o.qta_pianificata or 0}"))
    return [t for _u, t in sorted(righe)]


# ── MAGAZZINO PRODUZIONE ─────────────────────────────────────────────────

def _sottoscorta(ctx):
    return [r for r in _fabbisogni(ctx) if (r.get('sottoscorta') or 0) > 0]


def _txt_sottoscorta(r):
    um = r.get('unita_misura') or ''
    return (f"{r['codice']} {r.get('descrizione') or ''}".strip()
            + f" – disponibile {r.get('disponibilita', 0):g} su scorta {r.get('scorta_minima', 0):g} {um}".rstrip()
            + (' – già ordinato' if r.get('stato') == 'COPERTO_DA_ORDINI' else ''))


def conta_mp_sottoscorta(ctx):
    return [_txt_sottoscorta(r) for r in _sottoscorta(ctx) if r.get('tipo') == 'MATERIA_PRIMA_FORNITORE']


def conta_laserati_sottoscorta(ctx):
    return [_txt_sottoscorta(r) for r in _sottoscorta(ctx) if r.get('tipo') == 'LASERATO']


def conta_altri_sottoscorta(ctx):
    return [_txt_sottoscorta(r) for r in _sottoscorta(ctx) if r.get('tipo') not in ('MATERIA_PRIMA_FORNITORE', 'LASERATO')]


def _grezzo_iw(ctx):
    if 'grezzo' not in ctx:
        from blueprints.magazzino.routes import _grezzo_iw_per_codici
        codici = [r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
        ctx['grezzo'] = _grezzo_iw_per_codici(codici) if codici else {}
    return ctx['grezzo']


def conta_giacenza_negativa(ctx):
    """Codici con giacenza negativa: Finiti IW / magazzino (GiacenzaWood) oppure Grezzo IW."""
    negativi = {}
    for g in GiacenzaWood.query.filter(GiacenzaWood.quantita < 0).all():
        if g.codice:
            negativi[g.codice.upper()] = f"{g.codice} – giacenza magazzino {g.quantita:g}"
    for c, q in _grezzo_iw(ctx).items():
        if (q or 0) < 0:
            testo = f"{c} – Grezzo IW {q:g}"
            negativi[c.upper()] = negativi[c.upper()] + f", Grezzo IW {q:g}" if c.upper() in negativi else testo
    return [negativi[k] for k in sorted(negativi)]


def conta_da_trattare(ctx):
    """Stessa fonte di Terzisti → Materiali da trattare: Grezzo IW > 0."""
    return [f"{c} – {q:g} pz grezzi da mandare in trattamento"
            for c, q in sorted(_grezzo_iw(ctx).items(), key=lambda kv: -(kv[1] or 0)) if (q or 0) > 0]


def conta_in_trattamento(ctx):
    """Codici con pezzi ancora presso il terzista (spediti e non rientrati)."""
    from blueprints.magazzino.routes import _in_trattamento_per_codici
    codici = [r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
    return [f"{c} – {q:g} pz presso il terzista"
            for c, q in sorted(_in_trattamento_per_codici(codici).items(), key=lambda kv: -(kv[1] or 0)) if (q or 0) > 0]


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
            risultato = fn(ctx)
            # una funzione può restituire l'elenco delle righe (numero = quante sono,
            # prime MAX_RIGHE nel popup di MASTERCONTROL) oppure solo il numero
            if isinstance(risultato, list):
                valore, items = len(risultato), [{'testo': str(t)} for t in risultato[:MAX_RIGHE]]
            else:
                valore, items = int(risultato), []
        except Exception:
            # un contatore rotto non deve bloccare gli altri: MASTERCONTROL lo mostra grigio
            log.exception('Contatore MASTERCONTROL %s in errore', code)
            db.session.rollback()
            continue
        out.append({'code': code, 'area': area, 'label': label, 'value': valore, 'url': base + path, 'items': items})
    # Indirizzi degli altri programmi che IronProduction già conosce: servono a
    # MASTERCONTROL per collegarli da solo, senza inserire gli URL a mano.
    fonti = {}
    if os.environ.get('MASTERLOGISTIC_URL', '').strip():
        fonti['masterlogistic'] = os.environ['MASTERLOGISTIC_URL'].strip().rstrip('/')
    return jsonify(counters=out, fonti=fonti)
