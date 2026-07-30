from datetime import datetime
from flask import Blueprint, render_template, jsonify, request
from models import (db, log, CentroCostoWood, CicloLavoroWood, OrdineProduzione,
                    EventoConsuntivoPP, SequenzaMonitorMacchina, get_macchine_monitor)
from blueprints.magazzino.routes import _giacenza_residua_dopo_impegni, _netta_e_esplodi_wood

monitor_bp = Blueprint('monitor', __name__)

SEZIONI = {
    'in_attesa':   ('🛬 IN ATTESA — Materiale / Fase Precedente', 'in_attesa-hdr'),
    'da_iniziare': ('✅ DA INIZIARE',                              'da_iniziare-hdr'),
    'lavorazione': ('🚧 IN LAVORAZIONE',                           'lavorazione-hdr'),
    'terminati':   ('✅ APPENA TERMINATI (questa fase)',           'terminati-hdr'),
}


def _pezzi_fase(op_code, nome_centro):
    """Pezzi buoni già consuntivati per questo OP in QUESTA fase (per nome centro di costo, case-insensitive)."""
    tot = (db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
           .filter(EventoConsuntivoPP.op_code == op_code,
                   db.func.lower(EventoConsuntivoPP.fase) == nome_centro.lower())
           .scalar())
    return tot or 0


def _materiale_disponibile(o):
    """Stessa logica usata in 'Situazione Ordini di Produzione': True se non manca nessun componente."""
    saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
    if saldo <= 0:
        return True
    righe = {}
    giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=o.id)
    _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, righe)
    return all(r['mancante'] <= 0 for r in righe.values())


def _righe_macchina(centro):
    """
    Estrae, per la macchina (centro di costo) data, tutti gli Ordini di
    Produzione ancora aperti il cui articolo passa da questa macchina nel suo
    Ciclo di Lavoro, e li smista nelle 4 sezioni in base all'avanzamento
    REALE (consuntivi) — nessuna riga inserita a mano: i dati vengono da
    OrdineProduzione + CicloLavoroWood + EventoConsuntivoPP.
    """
    ordini = (OrdineProduzione.query
              .filter(OrdineProduzione.stato != 'Chiuso CO')
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())

    sequenze_manuali = {
        (s.ordine_produzione_id, s.centro_costo_id): s.posizione
        for s in SequenzaMonitorMacchina.query.filter_by(centro_costo_id=centro.id).all()
    }

    righe = {k: [] for k in SEZIONI}
    for o in ordini:
        fasi_ciclo = (CicloLavoroWood.query.filter_by(codice=o.codice_articolo)
                      .order_by(CicloLavoroWood.sequenza).all())
        idx = next((i for i, f in enumerate(fasi_ciclo) if f.centro_costo_id == centro.id), None)
        if idx is None:
            continue  # questo articolo non passa da questa macchina

        pezzi_fase = _pezzi_fase(o.codice, centro.nome)
        saldo_fase = max((o.qta_pianificata or 0) - pezzi_fase, 0)
        pct_fase = round(pezzi_fase / o.qta_pianificata * 100) if o.qta_pianificata else 0

        if saldo_fase <= 0:
            sezione = 'terminati'
        elif pezzi_fase > 0:
            sezione = 'lavorazione'
        else:
            if idx == 0:
                pronto = _materiale_disponibile(o)
            else:
                fase_prec = fasi_ciclo[idx - 1]
                pronto = _pezzi_fase(o.codice, fase_prec.centro_costo.nome) >= (o.qta_pianificata or 0)
            sezione = 'da_iniziare' if pronto else 'in_attesa'

        posizione_manuale = sequenze_manuali.get((o.id, centro.id))
        chiave_ordine = (0, posizione_manuale) if posizione_manuale is not None else (
            1, o.priorita, o.data_prevista or datetime.max.date(), o.id)

        righe[sezione].append({
            'op_id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
            'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione or '',
            'priorita': o.priorita, 'stato_op': o.stato,
            'totale': o.qta_pianificata, 'saldo': saldo_fase, 'pct': pct_fase,
            'posizione_manuale': posizione_manuale,
            'data_prevista': o.data_prevista.isoformat() if o.data_prevista else None,
            '_chiave_ordine': chiave_ordine,
        })

    for sezione in righe:
        righe[sezione].sort(key=lambda r: r['_chiave_ordine'])
        for r in righe[sezione]:
            r.pop('_chiave_ordine')
    return righe


# ── ROUTE: pagina principale — se non specificata una macchina, va sulla prima disponibile ──
@monitor_bp.route('/monitor')
def index():
    macchine = get_macchine_monitor()
    if not macchine:
        return render_template('monitor/nessuna_macchina.html', active='monitor')
    from flask import redirect, url_for
    return redirect(url_for('monitor.macchina', cid=macchine[0]['id']))


# Redirect di cortesia per il vecchio indirizzo /monitor/trapani
@monitor_bp.route('/monitor/trapani')
def index_trapani_legacy():
    from flask import redirect, url_for
    macchine = get_macchine_monitor()
    trapano = next((m for m in macchine if 'trapano' in m['nome'].lower()), None)
    if trapano:
        return redirect(url_for('monitor.macchina', cid=trapano['id']))
    return redirect(url_for('monitor.index'))


@monitor_bp.route('/monitor/macchina/<int:cid>')
def macchina(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    righe_per_sezione = _righe_macchina(centro)
    return render_template('monitor/macchina.html',
        active='monitor', active_page='monitor',
        centro=centro, macchine=get_macchine_monitor(),
        sezioni_map=SEZIONI, righe_per_sezione=righe_per_sezione,
        now=datetime.now().strftime('%d/%m/%Y'))


# ══════════════════════════════════════════════════════════════════════════════
#  API — dati macchina (per refresh AJAX) e riordino manuale della coda
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/api/monitor_macchina/<int:cid>')
def api_righe_macchina(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    return jsonify(_righe_macchina(centro))


@monitor_bp.route('/api/monitor_macchina/<int:cid>/ordina', methods=['POST'])
def api_ordina_macchina(cid):
    """
    Il capo fissa a mano la posizione di un OP nella coda di questa macchina
    (es. per metterlo davanti a tutti perché urgente). Se l'OP non ha ancora
    una riga qui, viene creata; altrimenti la posizione viene aggiornata.
    """
    CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    try:
        op_id = int(d['ordine_produzione_id'])
        posizione = int(d['posizione'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'ordine_produzione_id e posizione sono obbligatori (numerici)'}), 400
    OrdineProduzione.query.get_or_404(op_id)

    riga = SequenzaMonitorMacchina.query.filter_by(ordine_produzione_id=op_id, centro_costo_id=cid).first()
    if riga:
        riga.posizione = posizione
    else:
        db.session.add(SequenzaMonitorMacchina(ordine_produzione_id=op_id, centro_costo_id=cid, posizione=posizione))
    log(f'Monitor macchina: OP #{op_id} posizionato a {posizione} su centro di costo {cid}')
    db.session.commit()
    return jsonify({'ok': True})


@monitor_bp.route('/api/monitor_macchina/<int:cid>/ordina/<int:op_id>', methods=['DELETE'])
def api_reset_ordine_macchina(cid, op_id):
    """Rimuove la posizione manuale: l'OP torna a ordinarsi da solo (priorità/data consegna)."""
    riga = SequenzaMonitorMacchina.query.filter_by(ordine_produzione_id=op_id, centro_costo_id=cid).first()
    if riga:
        db.session.delete(riga)
        db.session.commit()
    return jsonify({'ok': True})
