from flask import Blueprint, render_template, jsonify
from models import db, Commessa, KanbanProdotto, RigaMonitor, calcola_kpi
from datetime import datetime, date
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

@kpi_bp.route('/api/kpi')
def api_kpi():
    return jsonify(calcola_kpi())

@kpi_bp.route('/api/kpi/avanzamento-commesse')
def api_avanzamento_commesse():
    return jsonify(calcola_avanzamento_commesse())

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
