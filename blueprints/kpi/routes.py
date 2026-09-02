from flask import Blueprint, render_template, jsonify, request
from models import db, Commessa, KanbanProdotto, RigaMonitor, calcola_kpi, OrdineProduzione, SequenzaAvanzamentoKPI
from datetime import datetime, date, timedelta
from blueprints.produzione_pp.avanzamento import calcola_avanzamento_commesse

kpi_bp = Blueprint('kpi', __name__)

CATEGORIE_MAP = [
    ('1 Cavalletti',            '🏗️ Cavalletti'),
    ('2 Transenne',             '🚧 Transenne'),
    ('3 Archetti',              '🔲 Archetti'),
    ('4 Paletti ⌀48',           '📍 Paletti ⌀48'),
    ('5 Paletti ⌀60',           '📌 Paletti ⌀60'),
    ('6 Paletti Vari',          '🗂️ Paletti Vari'),
    ('7 Parapetti',             '🛡️ Parapetti'),
    ('8 Rastrelliere',          '🚲 Rastrelliere'),
    ('9 Tubi Scanalati',        '🔩 Tubi Scanalati'),
    ('10 Staffe e NJ',          '🔧 Staffe e NJ'),
    ('11 Barriere',             '🚦 Barriere'),
    ('12 Varie Altre Produzioni','📦 Varie Altre'),
    ('Pannelli Transenne',      '🪟 Pannelli'),
]

@kpi_bp.route('/kpi')
def index():
    kpi = calcola_kpi()
    cat_rows = []
    for sheet_key, cat_label in CATEGORIE_MAP:
        prods = KanbanProdotto.query.filter_by(sheet_key=sheet_key).all()
        prods = [p for p in prods if p.prodotto not in ('Totali',) and not p.prodotto.isdigit()]
        tot = len(prods)
        warn = sum(1 for p in prods if p.stato == 'PROGRAMMARE PRODUZIONE')
        ok = tot - warn
        pct = round(ok / tot * 100) if tot > 0 else 100
        color = '#27ae60' if pct >= 80 else '#f39c12' if pct >= 50 else '#c0392b'
        cat_rows.append({'label': cat_label, 'tot': tot, 'warn': warn, 'ok': ok, 'pct': pct, 'color': color})

    return render_template('kpi/index.html',
        active='kpi', active_page='cruscotto',
        topbar_title='📈 Cruscotto KPI',
        topbar_badge='Dashboard',
        kpi=kpi, cat_rows=cat_rows)


@kpi_bp.route('/kpi/embed')
def kpi_embed():
    """
    Copia 'nuda' del Cruscotto KPI (Gantt + Avanzamento Commesse + In Attesa
    di Rilascio, stessa API — sempre gli stessi dati) SENZA sidebar/topbar
    né nessun link verso il resto del programma: pensata per essere mostrata
    dentro un overlay (es. Totem Live) senza mai far uscire l'utente dalla
    pagina che stava guardando.
    """
    return render_template('kpi/embed.html')

@kpi_bp.route('/api/kpi')
def api_kpi():
    return jsonify(calcola_kpi())

@kpi_bp.route('/api/kpi/avanzamento-commesse')
def api_avanzamento_commesse():
    risultati, _segmenti_per_centro = calcola_avanzamento_commesse()
    return jsonify(risultati)

@kpi_bp.route('/api/kpi/avanzamento-commesse/riordina', methods=['POST'])
def api_riordina_avanzamento_commesse():
    """
    Salva l'ordine scelto A MANO da Angelo (trascina/rilascia) nella
    tabella 'Avanzamento Commesse' — un reset completo a ogni chiamata:
    riceve la lista COMPLETA dei codici OP nel nuovo ordine desiderato e
    la sostituisce tutta, non un aggiustamento parziale. Editabile solo
    da qui (pagina principale /kpi di Angelo) — la copia mostrata nei
    monitor/totem dell'officina (/kpi/embed) non ha nessun pulsante o
    funzione che chiami questa route.
    """
    d = request.get_json(force=True)
    codici_ordine = d.get('op_codici') or []
    if not codici_ordine:
        return jsonify(ok=False, error='Elenco vuoto'), 400
    ordini = {o.codice: o.id for o in OrdineProduzione.query.filter(OrdineProduzione.codice.in_(codici_ordine)).all()}
    SequenzaAvanzamentoKPI.query.delete()
    for posizione, codice in enumerate(codici_ordine):
        oid = ordini.get(codice)
        if oid is not None:
            db.session.add(SequenzaAvanzamentoKPI(ordine_produzione_id=oid, posizione=posizione))
    db.session.commit()
    return jsonify(ok=True)

@kpi_bp.route('/api/kpi/in-attesa-rilascio')
def api_in_attesa_rilascio():
    """
    Tutti gli Ordini di Produzione inseriti ma NON ANCORA rilasciati (stato
    'Creato') — promemoria trasversale a tutti i centri di costo, così non
    sparisce dal radar un OP inserito e mai rilasciato in produzione.
    """
    from models import OrdineProduzione
    ordini = (OrdineProduzione.query.filter_by(stato='Creato')
              .order_by(OrdineProduzione.priorita.asc(), OrdineProduzione.id.asc()).all())
    return jsonify([{
        'id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
        'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione or '',
        'qta_pianificata': o.qta_pianificata, 'priorita': o.priorita,
        'data_prevista': o.data_prevista.strftime('%d/%m/%Y') if o.data_prevista else None,
    } for o in ordini])


@kpi_bp.route('/api/kpi/appena-terminate')
def api_appena_terminate():
    """
    Ordini 'Tecnicamente completato' la cui ULTIMA dichiarazione di
    produzione risale a non più di 3 giorni fa — promemoria temporaneo,
    trasversale a tutti i centri di costo come 'In Attesa di Rilascio':
    appena finita una commessa resta visibile qui per un po' (spedizione,
    controllo qualità, fatturazione...) invece di sparire subito dal
    Cruscotto KPI non appena l'ultimo pezzo è dichiarato — ma passati 3
    giorni senza altre dichiarazioni, esce da sola, senza doverla
    rimuovere a mano.
    """
    from models import OrdineProduzione, EventoConsuntivoPP
    limite = datetime.utcnow() - timedelta(days=3)
    ordini = OrdineProduzione.query.filter_by(stato='Tecnicamente completato').all()
    if not ordini:
        return jsonify([])
    op_codes = [o.codice for o in ordini]
    ultima_dichiarazione_per_op = dict(
        db.session.query(EventoConsuntivoPP.op_code, db.func.max(EventoConsuntivoPP.timestamp_evento))
        .filter(EventoConsuntivoPP.op_code.in_(op_codes))
        .group_by(EventoConsuntivoPP.op_code).all())

    risultato = []
    for o in ordini:
        ultima = ultima_dichiarazione_per_op.get(o.codice)
        if not ultima or ultima < limite:
            continue   # nessuna dichiarazione, o troppo vecchia — non più 'appena terminata'
        risultato.append({
            'id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
            'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione or '',
            'cliente': o.cliente or '', 'qta_pianificata': o.qta_pianificata, 'qta_buona': o.qta_buona,
            'ultima_dichiarazione': ultima.strftime('%d/%m/%Y %H:%M'),
        })
    risultato.sort(key=lambda r: r['ultima_dichiarazione'], reverse=True)
    return jsonify(risultato)


@kpi_bp.route('/api/kpi/storico')
def api_storico():
    oggi = date.today()
    da = datetime(oggi.year, oggi.month, 1)
    completate = Commessa.query.filter(
        Commessa.stato.in_(['COMPLETATA','SPEDITA']),
        Commessa.aggiornato_il >= da
    ).all()
    ritardi = []
    for c in Commessa.query.filter(
        Commessa.stato.notin_(['COMPLETATA','SPEDITA','ANNULLATA']),
        Commessa.data_consegna != ''
    ).all():
        try:
            d = datetime.strptime(c.data_consegna, '%d/%m/%Y').date()
            if d < oggi:
                ritardi.append({'numero': c.numero, 'cliente': c.cliente_nome,
                                'giorni': (oggi - d).days, 'data_cons': c.data_consegna})
        except: pass
    ritardi.sort(key=lambda x: x['giorni'], reverse=True)
    return jsonify({'completate_mese': len(completate), 'ritardi': ritardi[:10]})
