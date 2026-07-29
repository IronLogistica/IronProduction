from flask import Blueprint, render_template, jsonify, request
from datetime import datetime
from models import db, ArticoloML, DistintaBaseML, DistintaBaseWood

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


# ══════════════════════════════════════════════════════════════════════════════
#  DISTINTA BASE IRON WOOD — copia LOCALE (tabella distinta_base_wood, nel
#  database di IronProduction, vedi models.DistintaBaseWood). A differenza
#  della sezione sopra, qui si legge E si scrive: MasterLogistic non c'entra,
#  gli articoli/stock restano quelli condivisi (ArticoloML), ma le righe di
#  distinta sono gestite qui per non toccare mai la tabella di MasterLogistic.
# ══════════════════════════════════════════════════════════════════════════════
def _esplodi_bom_wood(codice, qta=1.0, _visitati=None, _profondita=0, _max_profondita=12):
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita:
        return []
    _visitati = _visitati | {codice}

    righe = DistintaBaseWood.query.filter_by(codice_padre=codice).order_by(DistintaBaseWood.livello).all()
    componenti = []
    for r in righe:
        art = ArticoloML.query.filter_by(sku=r.codice_figlio).first()
        qta_totale = (r.quantita or 1.0) * qta
        componenti.append({
            'id':                r.id,
            'codice':            r.codice_figlio,
            'descrizione':       art.descrizione if art else '',
            'quantita_unitaria': r.quantita,
            'quantita_totale':   round(qta_totale, 3),
            'stock':             art.stock if art else None,
            'fornitore':         art.fornitore if art else None,
            'note':              r.note or '',
            'figli':             _esplodi_bom_wood(r.codice_figlio, qta_totale, _visitati, _profondita + 1, _max_profondita),
        })
    return componenti


@magazzino_bp.route('/api/distinta_base_wood/<codice>')
def api_distinta_base_wood(codice):
    art = ArticoloML.query.filter_by(sku=codice).first()
    componenti = _esplodi_bom_wood(codice)
    return jsonify({
        'codice':      codice,
        'descrizione': art.descrizione if art else '',
        'trovato':     art is not None,
        'ha_bom':      len(componenti) > 0,
        'componenti':  componenti,
    })


@magazzino_bp.route('/api/distinta_base_wood', methods=['GET'])
def api_lista_distinta_wood():
    """Restituisce tutte le righe della distinta base Iron Wood (per la tabella di gestione)."""
    righe = DistintaBaseWood.query.order_by(DistintaBaseWood.codice_padre, DistintaBaseWood.livello).all()
    return jsonify([{
        'id':            r.id,
        'codice_padre':  r.codice_padre,
        'codice_figlio': r.codice_figlio,
        'quantita':      r.quantita,
        'livello':       r.livello,
        'note':          r.note or '',
    } for r in righe])


@magazzino_bp.route('/api/distinta_base_wood', methods=['POST'])
def api_add_distinta_wood():
    """Aggiunge (o aggiorna se già esiste la stessa coppia padre+figlio) una riga alla distinta base Iron Wood."""
    try:
        data = request.get_json(force=True)
        padre  = (data.get('codice_padre')  or '').strip().upper()
        figlio = (data.get('codice_figlio') or '').strip().upper()
        if not padre or not figlio:
            return jsonify({'errore': True, 'messaggio': 'Codice padre e codice figlio sono obbligatori.'}), 400
        if padre == figlio:
            return jsonify({'errore': True, 'messaggio': 'Un articolo non può essere componente di se stesso.'}), 400

        quantita = float(data.get('quantita') or 1.0)
        livello  = int(data.get('livello') or 1)
        note     = (data.get('note') or '').strip()

        esistente = DistintaBaseWood.query.filter_by(codice_padre=padre, codice_figlio=figlio).first()
        if esistente:
            esistente.quantita = quantita
            esistente.livello  = livello
            esistente.note     = note
        else:
            db.session.add(DistintaBaseWood(
                codice_padre=padre, codice_figlio=figlio,
                quantita=quantita, livello=livello, note=note,
                creato_il=datetime.utcnow()
            ))
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500


@magazzino_bp.route('/api/distinta_base_wood/<int:id_riga>', methods=['DELETE'])
def api_del_distinta_wood(id_riga):
    """Elimina una riga dalla distinta base Iron Wood."""
    try:
        riga = DistintaBaseWood.query.get_or_404(id_riga)
        db.session.delete(riga)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500
