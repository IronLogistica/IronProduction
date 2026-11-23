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


def _n(x):
    """Numero in formato italiano: 1250 -> '1.250', 1.5 -> '1,5'."""
    x = round(float(x or 0), 2)
    if x == int(x):
        return f"{int(x):,}".replace(',', '.')
    return f"{x:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.').rstrip('0').rstrip(',')


def _oa_riga(o, extra=None):
    """OA N° | Fornitore | Articoli | Qtà da ricevere | Data prev. [| Ritardo]"""
    righe = list(o.righe or [])
    qta = sum(max(0, (r.qta_originale or 0) - (r.qta_ricevuta or 0)) for r in righe)
    um = {r.unita_misura for r in righe if r.unita_misura}
    r = [f"OA {o.ordine_n or 'N/D'}", o.fornitore or '', len(righe),
         _n(qta) + (f" {um.pop()}" if len(um) == 1 else ''), _gg(o.data_consegna) if o.data_consegna else '—']
    return r + ([extra] if extra is not None else [])


# ── ACQUISTI PRODUZIONE (Ordini Acquisto Iron Wood) ──────────────────────

def _oa_aperti():
    return OrdineAcquistoWood.query.filter(OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all()


def conta_oa_da_confermare(ctx):
    return [_oa_riga(o, 'PDF da verificare' if o.stato_label == 'LETTURA_DA_VERIFICARE' else 'senza conferma')
            for o in _oa_aperti() if o.stato_label in ('DA_CONFERMARE', 'LETTURA_DA_VERIFICARE')]


def conta_oa_in_arrivo(ctx):
    """Confermati, non ancora ricevuti e NON in ritardo (quelli in ritardo hanno il loro riquadro)."""
    oggi = _oggi()
    righe = [o for o in _oa_aperti()
             if o.stato_label in ('ORDINE_CONFERMATO', 'ORDINE_IN_ARRIVO')
             and not (o.data_consegna and o.data_consegna < oggi)]
    righe.sort(key=lambda o: o.data_consegna or date.max)
    return [_oa_riga(o) for o in righe]


def conta_oa_da_ritirare(ctx):
    return [_oa_riga(o) for o in _oa_aperti() if o.stato_label == 'ORDINE_DA_RITIRARE']


def conta_oa_in_ritardo(ctx):
    """Stessa regola della dashboard/Totem Acquisti: non ricevuti con data consegna passata."""
    oggi = _oggi()
    righe = sorted((o for o in _oa_aperti() if o.data_consegna and o.data_consegna < oggi), key=lambda o: o.data_consegna)
    return [_oa_riga(o, f"+{(oggi - o.data_consegna).days} gg") for o in righe]


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
    out = []
    for p in righe:
        codice, _, descr = (p.get('nome') or '?').partition(' — ')
        out.append([codice, descr, _n(p.get('scorta_minima')), _n(p.get('saldo_c_scorta')), _n(p.get('riservato_clienti')),
                    _n(p.get('pronti_a_magazzino')), _n(p.get('in_produzione'))])
    return out


def conta_op_materiali_da_ordinare(ctx):
    """OP aperti con almeno un materiale DA ORDINARE (Lista Fabbisogni) ancora mancante per loro."""
    op = {}
    for r in _fabbisogni(ctx):
        if r.get('stato') != 'DA_ORDINARE':
            continue
        for i in r.get('impegni', []):
            if (i.get('mancante') or 0) > 0:
                op.setdefault(i['op_code'], {'prodotto': i.get('prodotto') or '', 'commessa': i.get('commessa') or '',
                                             'mancano': []})['mancano'].append(
                    f"{r['codice']} {_n(i['mancante'])}{(' ' + r['unita_misura']) if r.get('unita_misura') else ''}")
    qta_op = {o.codice: o.qta_pianificata for o in OrdineProduzione.query.filter(OrdineProduzione.codice.in_(list(op))).all()} if op else {}
    return [[codice, v['prodotto'], _n(qta_op.get(codice)), v['commessa'] or '-',
             ', '.join(v['mancano'][:3]) + (' …' if len(v['mancano']) > 3 else '')]
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
    return [[o.codice, o.codice_articolo, _n(o.qta_pianificata), o.commessa or '-', _gg(o.data_rilascio),
             f"{(oggi - o.data_rilascio.date()).days} gg"] for o in righe]


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
            righe.append((ultima, [o.codice, o.codice_articolo, _n(o.qta_pianificata), _n(saldati),
                                   o.commessa or '-', _gg(ultima), f"{(oggi - ultima.date()).days} gg"]))
    return [r for _u, r in sorted(righe, key=lambda x: x[0])]


# ── MAGAZZINO PRODUZIONE ─────────────────────────────────────────────────

def _sottoscorta(ctx):
    return [r for r in _fabbisogni(ctx) if (r.get('sottoscorta') or 0) > 0]


def _txt_sottoscorta(r):
    """Codice | Descrizione | UM | Disponibile | Scorta | Ordinato | Da ordinare"""
    return [r['codice'], r.get('descrizione') or '', r.get('unita_misura') or '', _n(r.get('disponibilita')),
            _n(r.get('scorta_minima')), _n(r.get('ordinato')), _n(r.get('da_ordinare'))]


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
            negativi.setdefault(g.codice.upper(), [g.codice, '', ''])[1] = _n(g.quantita)
    for c, q in _grezzo_iw(ctx).items():
        if (q or 0) < 0:
            negativi.setdefault(c.upper(), [c, '', ''])[2] = _n(q)
    return [[r[0], r[1] or '—', r[2] or '—'] for _k, r in sorted(negativi.items())]


def conta_da_trattare(ctx):
    """Stessa fonte di Terzisti → Materiali da trattare: Grezzo IW > 0."""
    return [[c, _n(q)] for c, q in sorted(_grezzo_iw(ctx).items(), key=lambda kv: -(kv[1] or 0)) if (q or 0) > 0]


def conta_in_trattamento(ctx):
    """Codici con pezzi ancora presso il terzista (spediti e non rientrati)."""
    from blueprints.magazzino.routes import _in_trattamento_per_codici
    codici = [r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
    return [[c, _n(q)] for c, q in sorted(_in_trattamento_per_codici(codici).items(), key=lambda kv: -(kv[1] or 0)) if (q or 0) > 0]


# intestazioni della tabella del popup di MASTERCONTROL
_COL_OA = ['OA N°', 'Fornitore', 'Articoli', 'Qtà da ricevere', 'Data prev.']
_COL_SCORTA = ['Codice', 'Descrizione', 'UM', 'Disponibile', 'Scorta', 'Ordinato', 'Da ordinare']
COLONNE = {
    'ACQ_PROD_DA_CONFERMARE': _COL_OA + ['Stato'], 'ACQ_PROD_IN_ARRIVO': _COL_OA, 'ACQ_PROD_DA_RITIRARE': _COL_OA,
    'ACQ_PROD_RITARDO': _COL_OA + ['Ritardo'],
    'PROD_SOTTOSCORTA_DA_PIANIFICARE': ['Codice', 'Descrizione', 'Scorta', 'Saldo c/scorta', 'Riservato clienti',
                                        'Pronti', 'In produzione'],
    'PROD_OP_MATERIALI_DA_ORDINARE': ['OP', 'Articolo', 'Qtà', 'Commessa', 'Da ordinare'],
    'PROD_OP_INIZIO_RITARDO': ['OP', 'Articolo', 'Qtà', 'Commessa', 'Rilasciato', 'Fermo da'],
    'PROD_OP_SALDATURA_RITARDO': ['OP', 'Articolo', 'Qtà', 'Saldati', 'Commessa', 'Fasi ferme dal', 'Da'],
    'MAGP_MP_SOTTOSCORTA': _COL_SCORTA, 'MAGP_LASERATI_SOTTOSCORTA': _COL_SCORTA, 'MAGP_ALTRI_SOTTOSCORTA': _COL_SCORTA,
    'MAGP_GIACENZA_NEGATIVA': ['Codice', 'Giacenza magazzino', 'Grezzo IW'],
    'MAGP_DA_TRATTARE': ['Codice', 'Pz grezzi IW'],
    'MAGP_IN_TRATTAMENTO': ['Codice', 'Pz dal terzista'],
}

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
                valore, items = len(risultato), [
                    {'cells': [str(x) for x in t], 'testo': ' – '.join(str(x) for x in t if str(x))}
                    if isinstance(t, (list, tuple)) else {'testo': str(t)} for t in risultato[:MAX_RIGHE]]
            else:
                valore, items = int(risultato), []
        except Exception:
            # un contatore rotto non deve bloccare gli altri: MASTERCONTROL lo mostra grigio
            log.exception('Contatore MASTERCONTROL %s in errore', code)
            db.session.rollback()
            continue
        out.append({'code': code, 'area': area, 'label': label, 'value': valore, 'url': base + path,
                    'columns': COLONNE.get(code, []), 'items': items})
    # Indirizzi degli altri programmi che IronProduction già conosce: servono a
    # MASTERCONTROL per collegarli da solo, senza inserire gli URL a mano.
    fonti = {}
    if os.environ.get('MASTERLOGISTIC_URL', '').strip():
        fonti['masterlogistic'] = os.environ['MASTERLOGISTIC_URL'].strip().rstrip('/')
    return jsonify(counters=out, fonti=fonti)
