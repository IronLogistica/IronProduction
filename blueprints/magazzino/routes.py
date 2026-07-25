from flask import Blueprint, render_template, jsonify
from models import ArticoloML

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
