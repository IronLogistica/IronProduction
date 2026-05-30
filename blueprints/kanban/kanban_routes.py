from flask import Blueprint, render_template, jsonify, request, current_app
from models import db, KanbanProdotto, KanbanGruppo, kanban_to_dict, log, get_kanban_gruppi
from datetime import datetime
import re, os, requests as http_requests

kanban_bp = Blueprint('kanban', __name__)

# ── URL MasterLogistic (variabile d'ambiente Railway) ────────────────────────
# Su Railway: Settings → Variables → MASTERLOGISTIC_URL = https://tuo-app.railway.app
def _ml_url():
    return os.environ.get('MASTERLOGISTIC_URL', '').rstrip('/')

def _url_key_from_label(label):
    key = re.sub(r'[^\w\s⌀]', '', label).strip()
    key = re.sub(r'\s+', '_', key)
    return key

def _gruppo_by_url_key(url_key):
    g = KanbanGruppo.query.filter_by(url_key=url_key).first()
    if g:
        return g, url_key.replace('_', ' ')
    return None, url_key


# ── PAGINA KANBAN ────────────────────────────────────────────────────────────
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

    tot    = len(prodotti)
    ok     = sum(1 for p in prodotti if p.stato == 'OK')
    warn   = tot - ok
    valore = sum(p.val_pv for p in prodotti)

    ml_connesso = bool(_ml_url())

    return render_template('kanban/index.html',
        active=f'kb-{url_key}',
        topbar_title=f"{info['icona']} {info['label']}",
        topbar_badge='Kanban Gruppi',
        info=info, url_key=url_key, sheet_key=sheet_key,
        prodotti=prodotti,
        stats={'tot': tot, 'ok': ok, 'warn': warn, 'valore': valore},
        ml_connesso=ml_connesso)


# ── API: SCHEDA KANBAN COMPLETA (dati WMS in tempo reale) ───────────────────
@kanban_bp.route('/api/kanban-scheda/<int:kid>')
def api_scheda(kid):
    """
    Ritorna la scheda completa di un prodotto Kanban:
    - dati locali IronProduction (programmi in corso, in verniciatura, ecc.)
    - dati live da MasterLogistic WMS (stock, riservato clienti, ordini, date)
    """
    p = KanbanProdotto.query.get_or_404(kid)

    # ── Dati locali ──
    scheda = {
        'id':           p.id,
        'prodotto':     p.prodotto,
        'sku_vern':     p.sku_verniciato or '',
        'sku_grezzo':   p.sku_grezzo or '',
        'lotto':        p.lotto,
        'riserva':      p.riserva,
        'in_prod':      p.in_prod,
        'in_vern':      p.in_vern,
        'lavorazioni':  p.lavorazioni or '',
        'val_medio':    p.val_medio,
        # WMS placeholders (riempiti sotto)
        'wms_ok':           False,
        'wms_errore':       '',
        'stock_grezzi':     0,
        'stock_verniciati': 0,
        'riservato_clienti':0,
        'ordinati':         0,
        'saldo_contabile':  0,
        'saldo_disponibile':0,
        'saldo_dopo_vern':  0,
        'saldo_grezzi_vern':0,
        'ordini_clienti':   [],   # [{conferma, cliente, qta_ord, qta_evasa, qta_residua, data_consegna}]
        'programmi':        [],   # [{nr, in_produzione, evasi, saldo}]  ← da IronProduction commesse future
    }

    # ── Dati da MasterLogistic ──
    base = _ml_url()
    if base:
        try:
            # Chiama la route dedicata che aggiungiamo a MasterLogistic
            sku_v = p.sku_verniciato.strip() if p.sku_verniciato else ''
            sku_g = p.sku_grezzo.strip()     if p.sku_grezzo     else ''

            if sku_v or sku_g:
                params = {}
                if sku_v: params['sku_vern']  = sku_v
                if sku_g: params['sku_grezzo'] = sku_g

                resp = http_requests.get(
                    f"{base}/api/kanban-stock",
                    params=params,
                    timeout=8
                )
                if resp.status_code == 200:
                    wms = resp.json()
                    scheda['wms_ok']             = True
                    scheda['stock_verniciati']   = wms.get('stock_verniciati', 0)
                    scheda['stock_grezzi']       = wms.get('stock_grezzi', 0)
                    scheda['riservato_clienti']  = wms.get('riservato_clienti', 0)
                    scheda['ordinati']           = wms.get('ordinati', 0)
                    scheda['ordini_clienti']     = wms.get('ordini_clienti', [])

                    # Calcoli saldi (come nello screenshot)
                    vern  = scheda['stock_verniciati']
                    grezz = scheda['stock_grezzi']
                    ris   = scheda['riservato_clienti']
                    iv    = p.in_vern

                    scheda['saldo_contabile']   = vern - ris
                    scheda['saldo_disponibile'] = vern - ris           # identico senza impegni interni
                    scheda['saldo_dopo_vern']   = vern + iv - ris
                    scheda['saldo_grezzi_vern'] = vern + grezz + iv - ris
                else:
                    scheda['wms_errore'] = f'HTTP {resp.status_code}'
            else:
                scheda['wms_errore'] = 'SKU non configurati — modifica il prodotto per collegarlo al WMS'
        except http_requests.exceptions.ConnectionError:
            scheda['wms_errore'] = 'MasterLogistic non raggiungibile'
        except http_requests.exceptions.Timeout:
            scheda['wms_errore'] = 'Timeout connessione MasterLogistic'
        except Exception as e:
            scheda['wms_errore'] = str(e)
    else:
        scheda['wms_errore'] = 'MASTERLOGISTIC_URL non configurata'

    return jsonify(scheda)


# ── API: aggiorna SKU sul prodotto ───────────────────────────────────────────
@kanban_bp.route('/api/kanban/<int:kid>/sku', methods=['PUT'])
def api_aggiorna_sku(kid):
    try:
        p = KanbanProdotto.query.get_or_404(kid)
        d = request.get_json(force=True)
        if 'sku_verniciato' in d: p.sku_verniciato = d['sku_verniciato'].strip()
        if 'sku_grezzo'     in d: p.sku_grezzo     = d['sku_grezzo'].strip()
        db.session.commit()
        log(f'Kanban SKU: {p.prodotto} → vern={p.sku_verniciato} gr={p.sku_grezzo}')
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── API: lista articoli WMS (per autocomplete SKU) ───────────────────────────
@kanban_bp.route('/api/wms-articoli')
def api_wms_articoli():
    base = _ml_url()
    if not base:
        return jsonify([])
    try:
        resp = http_requests.get(f"{base}/api/articoli-lista", timeout=8)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify([])
    except Exception:
        return jsonify([])


# ── API KANBAN PRODOTTI (CRUD standard) ──────────────────────────────────────
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
        if 'val_medio'   in d: p.val_medio   = float(d['val_medio'])
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
            prodotto=prodotto,      categoria=d.get('categoria',''),
            sheet_key=d.get('sheet_key',''), icona=d.get('icona','📦'),
            lotto=int(d.get('lotto',0)),         riserva=int(d.get('riserva',0)),
            riservato=int(d.get('riservato',0)), grezzi=int(d.get('grezzi',0)),
            verniciati=int(d.get('verniciati',0)),in_vern=int(d.get('in_vern',0)),
            in_prod=int(d.get('in_prod',0)),     val_medio=float(d.get('val_medio',0)),
            lavorazioni=d.get('lavorazioni',''),
            sku_verniciato=d.get('sku_verniciato',''),
            sku_grezzo=d.get('sku_grezzo',''),
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
        icona   = d.get('icona','📦').strip() or '📦'
        url_key = _url_key_from_label(label)
        base_key, counter = url_key, 2
        while KanbanGruppo.query.filter_by(url_key=url_key).first():
            url_key = f"{base_key}_{counter}"; counter += 1
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
        sheet_k    = url_key.replace('_', ' ')
        n_prodotti = KanbanProdotto.query.filter(
            db.or_(KanbanProdotto.sheet_key == sheet_k, KanbanProdotto.sheet_key == url_key)
        ).count()
        if n_prodotti > 0:
            return jsonify({'ok': False,
                'error': f'Impossibile eliminare: contiene {n_prodotti} prodotti. Rimuovili prima.'}), 400
        nome = g.label
        db.session.delete(g)
        log(f'KanbanGruppo: eliminato "{nome}"')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500
