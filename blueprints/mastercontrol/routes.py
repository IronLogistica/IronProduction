"""
Contatori per MASTERCONTROL (plancia punti critici di Iron Segnaletica,
repo IronSegnaletica/MASTERCONTROL).

    GET /api/control/counters
    Header: X-Control-Token: <MASTERCONTROL_TOKEN>

Restituisce SOLO numeri (quanti OP in ritardo, quante dichiarazioni da
approvare, ...), ognuno con il link alla pagina di IronProduction dove si
vede l'elenco. Sola lettura: non scrive niente nel database.
I filtri sono gli STESSI già usati dalla dashboard/totem di IronProduction,
così il numero in plancia non può mai essere diverso da quello che si vede
qui dentro.

Variabile Railway: MASTERCONTROL_TOKEN (stesso valore messo in
MASTERCONTROL → Fonti → ironproduction). Vuota = endpoint disabilitato.
"""
import hmac
import logging
import os
from datetime import date, datetime

from flask import Blueprint, abort, jsonify, request

from models import (db, OrdineProduzione, EventoConsuntivoPP, LavorazioneTerzista,
                    OrdineAcquistoWood, GiacenzaWood, ModuloNonConformita8D)

mastercontrol_bp = Blueprint('mastercontrol', __name__)
log = logging.getLogger('ironproduction.mastercontrol')

STATI_OP_APERTI = ('Creato', 'Rilasciato', 'In esecuzione')
STATI_OP_CHIUSI = ('Tecnicamente completato', 'Chiuso CO')
STATI_TERZISTI_APERTI = ('ATTESA_RIENTRO', 'IN_RITARDO', 'PARZIALE')


# ── Funzioni di conteggio (ognuna ritorna un intero) ─────────────────────

def conta_op_in_ritardo(oggi=None):
    """OP ancora aperti con data prevista di fine già passata."""
    oggi = oggi or date.today()
    return OrdineProduzione.query.filter(
        OrdineProduzione.stato.in_(STATI_OP_APERTI),
        OrdineProduzione.data_prevista.isnot(None),
        OrdineProduzione.data_prevista < oggi,
    ).count()


def conta_op_sospesi():
    """OP con bandiera SOSPESA (fermi). ATTESA_MATERIALI NON conta: lì il
    materiale è già in arrivo, non c'è niente da sollecitare."""
    return OrdineProduzione.query.filter(
        OrdineProduzione.bandiera_stato == 'SOSPESA',
        OrdineProduzione.stato.notin_(STATI_OP_CHIUSI),
    ).count()


def conta_dichiarazioni_da_approvare():
    """Stesso filtro della tile lampeggiante (api_masterwork_pendenti_conteggio)."""
    return EventoConsuntivoPP.query.filter_by(visionato=False, approvato_direzione=False).count()


def conta_moduli_8d_in_attesa():
    return ModuloNonConformita8D.query.filter_by(stato='IN_ATTESA').count()


def conta_terzisti_in_ritardo(oggi=None):
    """Stessa logica della dashboard: lavorazioni aperte con rientro previsto (gg/mm/aaaa) già passato."""
    oggi = oggi or date.today()
    n = 0
    for lav in LavorazioneTerzista.query.filter(LavorazioneTerzista.stato.in_(STATI_TERZISTI_APERTI)).all():
        if not lav.data_rientro_prev:
            continue
        try:
            prevista = datetime.strptime(lav.data_rientro_prev.strip(), '%d/%m/%Y').date()
        except ValueError:
            continue
        if prevista < oggi:
            n += 1
    return n


def conta_oa_non_confermati():
    return OrdineAcquistoWood.query.filter(
        OrdineAcquistoWood.stato_label.in_(('DA_CONFERMARE', 'LETTURA_DA_VERIFICARE'))).count()


def conta_oa_da_ritirare():
    return OrdineAcquistoWood.query.filter_by(stato_label='ORDINE_DA_RITIRARE').count()


def conta_oa_in_ritardo(oggi=None):
    """Stessa logica della dashboard: ordini non ricevuti con data consegna passata."""
    oggi = oggi or date.today()
    return OrdineAcquistoWood.query.filter(
        OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO',
        OrdineAcquistoWood.data_consegna.isnot(None),
        OrdineAcquistoWood.data_consegna < oggi,
    ).count()


def conta_giacenze_negative():
    return GiacenzaWood.query.filter(GiacenzaWood.quantita < 0).count()


def conta_codici_padre_sotto_scorta():
    from blueprints.magazzino.routes import calcola_alert_fabbisogno_codici_padre
    return len(calcola_alert_fabbisogno_codici_padre())


# code, area, label, funzione, pagina di dettaglio
CONTATORI = [
    ('PRODUZIONE_OP_RITARDO', 'PRODUZIONE', 'OP in ritardo', conta_op_in_ritardo, '/ordini-produzione'),
    ('PRODUZIONE_OP_FERMI', 'PRODUZIONE', 'OP sospesi', conta_op_sospesi, '/kpi'),
    ('PRODUZIONE_DICHIARAZIONI_DA_APPROVARE', 'PRODUZIONE', 'Dichiarazioni da approvare',
     conta_dichiarazioni_da_approvare, '/dichiarazione-produzione'),
    ('PRODUZIONE_TERZISTI_RITARDO', 'PRODUZIONE', 'Rientri terzisti in ritardo', conta_terzisti_in_ritardo, '/terzisti'),
    ('PRODUZIONE_8D_IN_ATTESA', 'PRODUZIONE', 'Moduli 8D da decidere', conta_moduli_8d_in_attesa, '/dichiarazione-produzione'),

    ('ACQUISTI_IW_NON_CONFERMATI', 'ACQUISTI', 'Ordini fornitori IW non confermati', conta_oa_non_confermati, '/ordini-acquisto-wood'),
    ('ACQUISTI_IW_DA_RITIRARE', 'ACQUISTI', 'Ordini fornitori IW da ritirare', conta_oa_da_ritirare, '/ordini-acquisto-wood'),
    ('ACQUISTI_IW_RITARDO', 'ACQUISTI', 'Ordini fornitori IW in ritardo', conta_oa_in_ritardo, '/ordini-acquisto-wood'),

    ('MAGAZZINO_IW_GIACENZE_NEGATIVE', 'MAGAZZINO', 'Giacenze negative IW', conta_giacenze_negative, '/magazzino'),
    ('MAGAZZINO_IW_PADRI_SOTTOSCORTA', 'MAGAZZINO', 'Codici padre sotto scorta', conta_codici_padre_sotto_scorta, '/alert-scorte'),
]


@mastercontrol_bp.get('/api/control/counters')
def api_control_counters():
    atteso = os.environ.get('MASTERCONTROL_TOKEN', '')
    if not atteso or not hmac.compare_digest(request.headers.get('X-Control-Token', ''), atteso):
        abort(403)
    base = (os.environ.get('PUBLIC_URL') or request.host_url).rstrip('/')
    if base.startswith('http://') and 'localhost' not in base and '127.0.0.1' not in base:
        base = 'https://' + base[len('http://'):]  # Railway: dietro proxy arriva come http
    out = []
    for code, area, label, fn, path in CONTATORI:
        try:
            valore = int(fn())
        except Exception:
            # un contatore rotto non deve bloccare gli altri: MASTERCONTROL lo mostra grigio
            log.exception('Contatore MASTERCONTROL %s in errore', code)
            db.session.rollback()
            continue
        out.append({'code': code, 'area': area, 'label': label, 'value': valore, 'url': base + path})
    return jsonify(counters=out)
