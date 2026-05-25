from flask import Blueprint, render_template, jsonify, request
from models import db, KanbanProdotto, kanban_to_dict, log
from datetime import datetime

kanban_bp = Blueprint('kanban', __name__)

CATEGORIE = {
    '1 Cavalletti':              {'label': 'Cavalletti',       'icona': '🏗️', 'url_key': '1_Cavalletti'},
    '2 Transenne':               {'label': 'Transenne',        'icona': '🚧', 'url_key': '2_Transenne'},
    '3 Archetti':                {'label': 'Archetti',         'icona': '🔲', 'url_key': '3_Archetti'},
    '4 Paletti ⌀48':             {'label': 'Paletti ⌀48',      'icona': '📍', 'url_key': '4_Paletti_48'},
    '5 Paletti ⌀60':             {'label': 'Paletti ⌀60',      'icona': '📌', 'url_key': '5_Paletti_60'},
    '6 Paletti Vari':            {'label': 'Paletti Vari',     'icona': '🗂️', 'url_key': '6_Paletti_Vari'},
    '7 Parapetti':               {'label': 'Parapetti',        'icona': '🛡️', 'url_key': '7_Parapetti'},
    '8 Rastrelliere':            {'label': 'Rastrelliere',     'icona': '🚲', 'url_key': '8_Rastrelliere'},
    '9 Tubi Scanalati':          {'label': 'Tubi Scanalati',   'icona': '🔩', 'url_key': '9_Tubi_Scanalati'},
    '10 Staffe e NJ':            {'label': 'Staffe e NJ',      'icona': '🔧', 'url_key': '10_Staffe_e_NJ'},
    '11 Barriere':               {'label': 'Barriere',         'icona': '🚦', 'url_key': '11_Barriere'},
    '12 Varie Altre Produzioni': {'label': 'Varie Altre',      'icona': '📦', 'url_key': '12_Varie_Altre_Produzioni'},
    'Pannelli Transenne':        {'label': 'Pannelli',         'icona': '🪟', 'url_key': 'Pannelli_Transenne'},
}

# mappa da url_key → sheet_key DB
URL_TO_CAT = {v['url_key']: k for k, v in CATEGORIE.items()}

@kanban_bp.route('/kanban/<path:url_key>')
def index(url_key):
    # accetta sia url_key (1_Cavalletti) sia sheet_key diretto (1 Cavalletti)
    cat_key = URL_TO_CAT.get(url_key, url_key)
    info = CATEGORIE.get(cat_key, {'label': cat_key, 'icona': '📋', 'url_key': url_key})

    prodotti = KanbanProdotto.query.filter_by(sheet_key=cat_key)\
        .order_by(KanbanProdotto.sort_order, KanbanProdotto.prodotto).all()

    # filtra righe "Totali" spazzatura dal vecchio seed
    prodotti = [p for p in prodotti if p.prodotto not in ('Totali',) and not p.prodotto.isdigit()]

    tot = len(prodotti)
    ok = sum(1 for p in prodotti if p.stato == 'OK')
    warn = tot - ok
    valore = sum(p.val_pv for p in prodotti)

    return render_template('kanban/index.html',
        active=f'kb-{url_key}',
        active_page=f'kb-{url_key}',
        topbar_title=f"{info['icona']} {info['label']}",
        topbar_badge='Kanban Prodotti',
        info=info, cat_key=cat_key, url_key=url_key,
        prodotti=prodotti,
        stats={'tot': tot, 'ok': ok, 'warn': warn, 'valore': valore})

# ── API REST ─────────────────────────────────────────────────────────────────
@kanban_bp.route('/api/kanban')
def api_lista():
    categoria = request.args.get('categoria', '')
    if categoria:
        prodotti = KanbanProdotto.query.filter_by(sheet_key=categoria)\
            .order_by(KanbanProdotto.sort_order, KanbanProdotto.prodotto).all()
    else:
        prodotti = KanbanProdotto.query.order_by(KanbanProdotto.categoria, KanbanProdotto.prodotto).all()
    return jsonify([kanban_to_dict(p) for p in prodotti])

@kanban_bp.route('/api/kanban/<int:kid>', methods=['PUT'])
def api_aggiorna(kid):
    try:
        p = KanbanProdotto.query.get_or_404(kid)
        d = request.get_json(force=True)
        for campo in ['lotto','riserva','riservato','grezzi','verniciati','in_vern','in_prod']:
            if campo in d: setattr(p, campo, int(d[campo]))
        if 'val_medio' in d: p.val_medio = float(d['val_medio'])
        if 'lavorazioni' in d: p.lavorazioni = d['lavorazioni']
        p.aggiornato_il = datetime.utcnow()
        log(f'Kanban: aggiornato {p.prodotto}')
        db.session.commit()
        return jsonify({'ok': True, **kanban_to_dict(p)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        prodotto = d.get('prodotto','').strip()
        if not prodotto:
            return jsonify({'ok': False, 'error': 'Prodotto obbligatorio'}), 400
        p = KanbanProdotto(
            prodotto=prodotto, categoria=d.get('categoria',''),
            sheet_key=d.get('sheet_key',''), icona=d.get('icona','📦'),
            lotto=int(d.get('lotto',0)), riserva=int(d.get('riserva',0)),
            riservato=int(d.get('riservato',0)), grezzi=int(d.get('grezzi',0)),
            verniciati=int(d.get('verniciati',0)), in_vern=int(d.get('in_vern',0)),
            in_prod=int(d.get('in_prod',0)), val_medio=float(d.get('val_medio',0)),
            lavorazioni=d.get('lavorazioni',''),
        )
        db.session.add(p)
        log(f'Kanban: aggiunto prodotto {prodotto}')
        db.session.commit()
        return jsonify({'ok': True, 'id': p.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>', methods=['DELETE'])
def api_elimina(kid):
    try:
        p = KanbanProdotto.query.get_or_404(kid)
        nome = p.prodotto
        db.session.delete(p)
        log(f'Kanban: eliminato {nome}')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
