from flask import Blueprint, render_template, jsonify, request
from models import (db, KanbanProdotto, KanbanGruppo, KanbanCiclo, FaseWip,
                    kanban_to_dict, log, get_kanban_gruppi,
                    registra_ciclo_se_necessario, calcola_analisi_takt, PIN_ADMIN)
from datetime import datetime
import re

kanban_bp = Blueprint('kanban', __name__)

def _url_key_from_label(label):
    key = re.sub(r'[^\w\s⌀]', '', label).strip()
    key = re.sub(r'\s+', '_', key)
    return key

def _gruppo_by_url_key(url_key):
    g = KanbanGruppo.query.filter_by(url_key=url_key).first()
    if g:
        return g, url_key.replace('_', ' ')
    return None, url_key

def _wip_snapshot():
    """Ritorna {fase: {'attivi':N, 'limit':M, 'pct':P, 'colore':C}} per la tabella WIP."""
    fasi = FaseWip.query.order_by(FaseWip.id).all()
    result = {}
    for fw in fasi:
        attivi = db.session.query(db.func.count(KanbanProdotto.id)).filter(
            db.or_(
                KanbanProdotto.in_vern > 0 if fw.fase == 'verniciatura' else db.false(),
                KanbanProdotto.in_prod > 0 if fw.fase in ('saldatura','taglio','piega','sgola','finitura','collaudo') else db.false(),
            )
        ).scalar() or 0
        lim = fw.wip_limit or 0
        pct = round(attivi / lim * 100) if lim > 0 else 0
        if lim == 0:       colore = 'grigio'
        elif pct >= fw.soglia_rosso:  colore = 'rosso'
        elif pct >= fw.soglia_giallo: colore = 'giallo'
        else:              colore = 'verde'
        result[fw.fase] = {
            'label': fw.label, 'attivi': attivi, 'limit': lim,
            'limite_giornaliero': fw.limite_giornaliero,
            'pct': pct, 'colore': colore
        }
    return result

# ── PAGINA KANBAN ────────────────────────────────────────────────────────────
@kanban_bp.route('/kanban/<path:url_key>')
def index(url_key):
    g, sheet_key = _gruppo_by_url_key(url_key)
    if g:
        info = {'label': g.label, 'icona': g.icona, 'url_key': g.url_key, 'id': g.id}
    else:
        info = {'label': url_key.replace('_',' '), 'icona': '📋', 'url_key': url_key, 'id': None}

    prodotti = KanbanProdotto.query.filter(
        db.or_(KanbanProdotto.sheet_key == sheet_key,
               KanbanProdotto.sheet_key == url_key)
    ).order_by(KanbanProdotto.sort_order, KanbanProdotto.prodotto).all()
    prodotti = [p for p in prodotti if p.prodotto not in ('Totali',) and not p.prodotto.isdigit()]

    tot    = len(prodotti)
    ok     = sum(1 for p in prodotti if p.stato == 'OK')
    warn   = tot - ok
    valore = sum(p.val_pv for p in prodotti)

    wip = _wip_snapshot()

    return render_template('kanban/index.html',
        active=f'kb-{url_key}',
        topbar_title=f"{info['icona']} {info['label']}",
        topbar_badge='Kanban Gruppi',
        info=info, url_key=url_key, sheet_key=sheet_key,
        prodotti=prodotti,
        stats={'tot': tot, 'ok': ok, 'warn': warn, 'valore': valore},
        wip=wip)


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
        stato_prima = p.stato
        d = request.get_json(force=True)
        for campo in ['lotto','riserva','riservato','grezzi','verniciati','in_vern','in_prod']:
            if campo in d: setattr(p, campo, int(d[campo]))
        if 'val_medio'   in d: p.val_medio   = float(d['val_medio'])
        if 'lavorazioni' in d: p.lavorazioni = d['lavorazioni']
        p.aggiornato_il = datetime.utcnow()
        stato_dopo = p.stato
        # ── Accumulazione dati cicli per Takt Time ──
        registra_ciclo_se_necessario(p, stato_prima, stato_dopo)
        log(f'Kanban: aggiornato {p.prodotto}')
        db.session.commit()
        return jsonify({'ok': True, **kanban_to_dict(p)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban', methods=['POST'])
def api_crea():
    """Crea nuovo prodotto Kanban — richiede PIN autorizzativo."""
    try:
        d = request.get_json(force=True)
        # Verifica PIN
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        prodotto = d.get('prodotto','').strip()
        if not prodotto:
            return jsonify({'ok': False, 'error': 'Nome prodotto obbligatorio'}), 400
        p = KanbanProdotto(
            prodotto=prodotto,
            categoria=d.get('categoria', d.get('sheet_key','').replace('_',' ')),
            sheet_key=d.get('sheet_key',''),
            icona=d.get('icona','📦'),
            lotto=int(d.get('lotto',0)),
            riserva=int(d.get('riserva',0)),
            val_medio=float(d.get('val_medio',0)),
            lavorazioni=d.get('lavorazioni',''),
            scorta_sicurezza=float(d.get('scorta_sicurezza',0.15)),
            lead_time_giorni=float(d.get('lead_time_giorni',7.0)),
        )
        db.session.add(p)
        log(f'Kanban: creato prodotto {prodotto} (PIN autorizzato)')
        db.session.commit()
        return jsonify({'ok': True, 'id': p.id, **kanban_to_dict(p)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>', methods=['DELETE'])
def api_elimina(kid):
    try:
        # PIN può arrivare come query param (DELETE non supporta body in Flask di default)
        pin = request.args.get('pin', '') or (request.get_json(force=True, silent=True) or {}).get('pin', '')
        if pin != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        p = KanbanProdotto.query.get_or_404(kid)
        nome = p.prodotto
        db.session.delete(p)
        log(f'Kanban: eliminato {nome} (PIN autorizzato)')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>/sposta', methods=['POST'])
def api_sposta(kid):
    """Sposta un prodotto in un altro gruppo Kanban (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin', '') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        nuovo_sheet_key = d.get('nuovo_sheet_key', '').strip()
        if not nuovo_sheet_key:
            return jsonify({'ok': False, 'error': 'Gruppo destinazione obbligatorio'}), 400
        p = KanbanProdotto.query.get_or_404(kid)
        vecchio = p.sheet_key
        # Trova il gruppo destinazione per ricavare label e categoria
        g = KanbanGruppo.query.filter_by(url_key=nuovo_sheet_key).first()
        p.sheet_key = nuovo_sheet_key
        p.categoria = g.label if g else nuovo_sheet_key.replace('_', ' ')
        log(f'Kanban: spostato {p.prodotto} da "{vecchio}" a "{nuovo_sheet_key}" (PIN autorizzato)')
        db.session.commit()
        return jsonify({'ok': True, 'nuovo_sheet_key': nuovo_sheet_key})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── API TAKT TIME / ANALISI CICLI ────────────────────────────────────────────
@kanban_bp.route('/api/kanban/<int:kid>/analisi-takt')
def api_analisi_takt(kid):
    """Calcola e suggerisce Takt Time basandosi sui cicli accumulati."""
    return jsonify(calcola_analisi_takt(kid))

@kanban_bp.route('/api/kanban/<int:kid>/imposta-takt', methods=['POST'])
def api_imposta_takt(kid):
    """Imposta ufficialmente il Takt Time (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        p = KanbanProdotto.query.get_or_404(kid)
        p.takt_time_min = float(d.get('takt_time_min', 0)) or None
        if 'lead_time_giorni'  in d: p.lead_time_giorni  = float(d['lead_time_giorni'])
        if 'scorta_sicurezza'  in d: p.scorta_sicurezza  = float(d['scorta_sicurezza'])
        db.session.commit()
        log(f'Kanban: Takt Time {p.prodotto} impostato a {p.takt_time_min} min/pz (PIN)')
        return jsonify({'ok': True, 'takt_time_min': p.takt_time_min})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>/cicli')
def api_cicli(kid):
    """Lista ultimi 50 cicli registrati per questo prodotto."""
    cicli = KanbanCiclo.query.filter_by(kanban_id=kid)\
        .order_by(KanbanCiclo.data_inizio.desc()).limit(50).all()
    return jsonify([{
        'id': c.id,
        'data_inizio': c.data_inizio.strftime('%d/%m/%Y %H:%M') if c.data_inizio else '—',
        'data_fine':   c.data_fine.strftime('%d/%m/%Y %H:%M')   if c.data_fine   else '—',
        'lead_time_ore': c.lead_time_ore,
        'qta_prodotta':  c.qta_prodotta,
        'aperto': c.aperto,
    } for c in cicli])


# ── API WIP SNAPSHOT ─────────────────────────────────────────────────────────
@kanban_bp.route('/api/wip')
def api_wip():
    return jsonify(_wip_snapshot())

@kanban_bp.route('/api/wip', methods=['PUT'])
def api_wip_aggiorna():
    """Aggiorna limiti WIP per una fase (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        fase = d.get('fase','').strip()
        fw = FaseWip.query.filter_by(fase=fase).first()
        if not fw:
            return jsonify({'ok': False, 'error': f'Fase "{fase}" non trovata'}), 404
        if 'wip_limit'          in d: fw.wip_limit          = int(d['wip_limit'])
        if 'limite_giornaliero' in d: fw.limite_giornaliero = int(d['limite_giornaliero'])
        if 'soglia_giallo'      in d: fw.soglia_giallo      = int(d['soglia_giallo'])
        if 'soglia_rosso'       in d: fw.soglia_rosso       = int(d['soglia_rosso'])
        db.session.commit()
        log(f'FaseWip: aggiornata {fase} limit={fw.wip_limit} giorn={fw.limite_giornaliero}')
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

@kanban_bp.route('/api/kanban-gruppi/<path:url_key>', methods=['PUT'])
def api_gruppi_rinomina(url_key):
    """Rinomina label e/o icona di un gruppo (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        g = KanbanGruppo.query.filter_by(url_key=url_key).first()
        if not g:
            return jsonify({'ok': False, 'error': 'Gruppo non trovato'}), 404
        if 'label' in d and d['label'].strip():
            g.label = d['label'].strip()
        if 'icona' in d and d['icona'].strip():
            g.icona = d['icona'].strip()
        db.session.commit()
        log(f'KanbanGruppo: rinominato "{url_key}" → "{g.label}"')
        return jsonify({'ok': True, 'label': g.label, 'icona': g.icona})
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
            db.or_(KanbanProdotto.sheet_key == sheet_k,
                   KanbanProdotto.sheet_key == url_key)
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

# ── API WMS ARTICOLI (autocomplete da MasterLogistic) ────────────────────────
@kanban_bp.route('/api/wms-articoli')
def api_wms_articoli():
    import os, requests as http_req
    base = os.environ.get('MASTERLOGISTIC_URL', '').rstrip('/')
    if not base:
        return jsonify([])
    try:
        resp = http_req.get(f"{base}/api/articoli-lista", timeout=8)
        if resp.status_code == 200:
            return jsonify(resp.json())
        return jsonify([])
    except Exception:
        return jsonify([])
