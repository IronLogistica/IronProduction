from flask import Blueprint, render_template, jsonify, request
from models import ArticoloML, DistintaBaseML

magazzino_bp = Blueprint('magazzino', __name__)


@magazzino_bp.route('/magazzino')
def index():
    return render_template('magazzino/index.html', active='magazzino')


# ══════════════════════════════════════════════════════════════════════════════
#  Nessuna anagrafica locale: si legge in tempo reale la tabella 'articoli'
#  di MasterLogistic tramite il bind Postgres 'masterlogistic' (stesso
#  progetto Railway, DB separato — vedi config.py e models.ArticoloML).
#  Sola lettura: nessuna route qui crea/modifica/elimina articoli.
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/api/materiali')
def api_lista():
    try:
        articoli = ArticoloML.query.order_by(ArticoloML.sku).all()
    except Exception as e:
        return jsonify({
            'errore': True,
            'messaggio': f'Connessione al database di MasterLogistic non disponibile ({e})',
        }), 503

    return jsonify([{
        'codice':         a.sku,
        'codice_esterno': a.codice_esterno,
        'descrizione':    a.descrizione,
        'stock':          a.stock or 0,
        'ordinati':       a.ordinati or 0,
        'incoming':       a.incoming or 0,
        'scorta_min':     a.scorta_minima or 0,
        'fornitore':      a.fornitore,
        'ordine_n':       a.ordine_n,
        'stato':          a.stato,
    } for a in articoli])


# ══════════════════════════════════════════════════════════════════════════════
#  DISTINTA BASE (BOM) — anch'essa letta in sola lettura da MasterLogistic
#  (tabella distinta_base, bind 'masterlogistic' — vedi models.DistintaBaseML).
#  Esplosione ricorsiva multilivello con protezione anti-ciclo e profondità
#  massima; nessuna scrittura, MasterLogistic resta l'unica fonte di verità.
# ══════════════════════════════════════════════════════════════════════════════
def _esplodi_bom(codice, qta=1.0, _visitati=None, _profondita=0, _max_profondita=12):
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita:
        return []
    _visitati = _visitati | {codice}

    righe = DistintaBaseML.query.filter_by(codice_padre=codice).order_by(DistintaBaseML.livello).all()
    componenti = []
    for r in righe:
        art = ArticoloML.query.filter_by(sku=r.codice_figlio).first()
        qta_totale = (r.quantita or 1.0) * qta
        componenti.append({
            'codice':            r.codice_figlio,
            'descrizione':       art.descrizione if art else '',
            'quantita_unitaria': r.quantita,
            'quantita_totale':   round(qta_totale, 3),
            'stock':             art.stock if art else None,
            'fornitore':         art.fornitore if art else None,
            'note':              r.note or '',
            'figli':             _esplodi_bom(r.codice_figlio, qta_totale, _visitati, _profondita + 1, _max_profondita),
        })
    return componenti


@magazzino_bp.route('/api/distinta_base/<codice>')
def api_distinta_base(codice):
    try:
        art = ArticoloML.query.filter_by(sku=codice).first()
        componenti = _esplodi_bom(codice)
    except Exception as e:
        return jsonify({
            'errore': True,
            'messaggio': f'Connessione al database di MasterLogistic non disponibile ({e})',
        }), 503

    return jsonify({
        'codice':      codice,
        'descrizione': art.descrizione if art else '',
        'trovato':     art is not None,
        'ha_bom':      len(componenti) > 0,
        'componenti':  componenti,
    })
