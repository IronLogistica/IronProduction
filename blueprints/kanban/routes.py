from flask import Blueprint, render_template, jsonify, request
from models import db, KanbanProdotto, KanbanGruppo, kanban_to_dict, log, get_kanban_gruppi
from datetime import datetime
import re

kanban_bp = Blueprint('kanban', __name__)

def _url_key_from_label(label):
    """Genera url_key da label: 'Miei Prodotti' → 'Miei_Prodotti'"""
    key = re.sub(r'[^\w\s⌀]', '', label).strip()
    key = re.sub(r'\s+', '_', key)
    return key

def _gruppo_by_url_key(url_key):
    g = KanbanGruppo.query.filter_by(url_key=url_key).first()
    if g:
        return g, url_key.replace('_', ' ')
    return None, url_key

@kanban_bp.route('/kanban/<path:url_key>')
def index(url_key):
    g, sheet_key = _gruppo_by_url_key(url_key)
    if g:
        info = {'label': g.label, 'icona': g.icona, 'url_key': g.url_key}
    else:
        info = {'label': url_key.replace('_',' '), 'icona': '📋', 'url_key': url_key}

    prodotti = KanbanProdotto.query.filter(
        db.or_(KanbanProdotto.sheet_key == sheet_key, KanbanProdotto.sheet_key == url_key)
    ).order_by(KanbanProdotto.sort_order, KanbanProdotto.prodotto).all()

    prodotti = [p for p in prodotti if p.prodotto not in ('Totali',) and not p.prodotto.isdigit()]

    tot = len(prodotti)
    ok = sum(1 for p in prodotti if p.stato == 'OK')
    warn = tot - ok
    valore = sum(p.val_pv for p in prodotti)

    return render_template('kanban/index.html',
        active=f'kb-{url_key}',
        active_page=f'kb-{url_key}',
        topbar_title=f"{info['icona']} {info['label']}",
        topbar_badge='Kanban Gruppi',
        info=info, url_key=url_key, sheet_key=sheet_key,
        prodotti=prodotti,
        stats={'tot': tot, 'ok': ok, 'warn': warn, 'valore': valore})


# ── API KANBAN PRODOTTI ──────────────────────────────────────────────────────
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


# ── API KANBAN GRUPPI ────────────────────────────────────────────────────────
@kanban_bp.route('/api/kanban-gruppi', methods=['GET'])
def api_gruppi_lista():
    return jsonify(get_kanban_gruppi())

@kanban_bp.route('/api/kanban-gruppi', methods=['POST'])
def api_gruppi_crea():
    try:
        d = request.get_json(force=True)
        label = d.get('label','').strip()
        if not label:
            return jsonify({'ok': False, 'error': 'Nome gruppo obbligatorio'}), 400
        icona = d.get('icona','📦').strip() or '📦'
        url_key = _url_key_from_label(label)
        # gestisci duplicati url_key
        base_key = url_key
        counter = 2
        while KanbanGruppo.query.filter_by(url_key=url_key).first():
            url_key = f"{base_key}_{counter}"
            counter += 1
        max_order = db.session.query(db.func.max(KanbanGruppo.sort_order)).scalar() or 0
        g = KanbanGruppo(label=label, icona=icona, url_key=url_key, sort_order=max_order+1)
        db.session.add(g)
        log(f'KanbanGruppo: creato "{label}"')
        db.session.commit()
        return jsonify({'ok': True, 'url_key': url_key, 'label': label})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban-gruppi/<path:url_key>', methods=['DELETE'])
def api_gruppi_elimina(url_key):
    try:
        g = KanbanGruppo.query.filter_by(url_key=url_key).first()
        if not g:
            return jsonify({'ok': False, 'error': 'Gruppo non trovato'}), 404
        # Controlla se ha prodotti
        sheet_k = url_key.replace('_', ' ')
        n_prodotti = KanbanProdotto.query.filter(
            db.or_(KanbanProdotto.sheet_key == sheet_k, KanbanProdotto.sheet_key == url_key)
        ).count()
        if n_prodotti > 0:
            return jsonify({
                'ok': False,
                'error': f'Impossibile eliminare: il gruppo contiene {n_prodotti} prodotti Kanban. Rimuovi prima i prodotti.'
            }), 400
        nome = g.label
        db.session.delete(g)
        log(f'KanbanGruppo: eliminato "{nome}"')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
