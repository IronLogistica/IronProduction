import os
from datetime import datetime
from flask import Flask, redirect, url_for, render_template
from config import Config
from models import db, init_db, inizializza_schema_pp, get_kanban_gruppi, get_macchine_monitor, migra_schede_lavorazione_unificate, assicura_lunghezze_barra_default, assicura_unita_misura_articoli, assicura_finiti_is_kanban, assicura_lead_time_esterno_centri, assicura_ordinato_cliente_wms, assicura_operatore_evento_consuntivo, assicura_contestuale_distinta_base, assicura_event_id_varianza_produzione, assicura_contapieghe_matrici, migra_parametri_lavorazione_flat, assicura_bandiera_stato_op, assicura_import_consumabili_sicurezza, assicura_categoria_acquisto_config, assicura_lotto_trasferimento_minimo, assicura_ddt_carico_confermato, assicura_mappa_mw_fase, assicura_origine_ordine_acquisto_wood, assicura_campi_stampa_ordine_fornitore

LAUNCHPAD_GRUPPI = [
    {'nome': 'Produzione', 'icona': '🏭', 'colore': '#1e6fa5', 'voci': [
        {'label': 'Ordini Produzione', 'icona': '🏭', 'url': '/ordini-produzione'},
        {'label': 'Ordini di Lavoro', 'icona': '📋', 'url': '/liste-lavoro'},
        {'label': 'Situazione (card)', 'icona': '🗂️', 'url': '/ordini-produzione/situazione'},
        {'label': 'Dichiarazione Produzione', 'icona': '✅', 'url': '/dichiarazione-produzione'},
        {'label': 'Totem Alessandro', 'icona': '🖥️', 'url': '/totem/alessandro'},
        {'label': 'Rietichettatura', 'icona': '🏷️', 'url': '/rietichettatura'},
        {'label': 'Documentazione Articolo', 'icona': '📚', 'url': '/documentazione-articolo'},
        {'label': 'Monitor', 'icona': '⚙️', 'url': '/monitor'},
    ]},
    {'nome': 'Pianificazione & KPI', 'icona': '📈', 'colore': '#2589c7', 'voci': [
        {'label': 'Cruscotto KPI', 'icona': '📈', 'url': '/kpi'},
        {'label': 'Gantt Centri di Costo', 'icona': '📊', 'url': '/gantt-centri-costo'},
        {'label': 'Pianificazione Generale', 'icona': '🗓️', 'url': '/gantt-generale'},
        {'label': 'Alert Scorte Codici Padre', 'icona': '🚨', 'url': '/alert-scorte'},
    ]},
    {'nome': 'Magazzino', 'icona': '📦', 'colore': '#1a9e5c', 'voci': [
        {'label': 'Magazzino', 'icona': '📦', 'url': '/magazzino'},
        {'label': 'Giacenza Iron Wood', 'icona': '📊', 'url': '/giacenza-wood'},
        {'label': 'Inventario', 'icona': '📋', 'url': '/inventario'},
        {'label': 'Kanban Inventario', 'icona': '🗂️', 'url': '/inventario/kanban'},
    ]},
    {'nome': 'Acquisti', 'icona': '🛒', 'colore': '#e6b800', 'voci': [
        {'label': 'Ordini di Acquisto', 'icona': '🛒', 'url': '/ordini-acquisto-wood'},
        {'label': 'Acquisti da Fabbisogno', 'icona': '🧮', 'url': '/acquisti-da-fabbisogno'},
        {'label': 'Materiale in Arrivo', 'icona': '🚛', 'url': '/materiale-in-arrivo'},
        {'label': 'Anagrafica Iron Wood', 'icona': '🏢', 'url': '/anagrafica-azienda-wood'},
        {'label': 'Prezzi Storici', 'icona': '💰', 'url': '/prezzi-storici-wood'},
        {'label': 'Acquisti Consumabili', 'icona': '🧰', 'url': '/acquisti-consumabili'},
        {'label': 'Acquisti Sicurezza', 'icona': '🦺', 'url': '/acquisti-sicurezza'},
        {'label': 'Soglie / Conti Contabili', 'icona': '🧾', 'url': '/impostazioni-contabili'},
    ]},
    {'nome': 'Terzisti', 'icona': '🔨', 'colore': '#e8760a', 'voci': [
        {'label': 'Terzisti', 'icona': '🔨', 'url': '/terzisti'},
        {'label': 'Spedizioni Terzisti', 'icona': '🚚', 'url': '/terzisti/spedizioni'},
        {'label': 'Materiali da Trattare', 'icona': '📋', 'url': '/terzisti/da-trattare'},
    ]},
    {'nome': 'Ingegneria & Costi', 'icona': '⚙️', 'colore': '#7a58c9', 'voci': [
        {'label': 'Centri di Costo', 'icona': '🏭', 'url': '/centri-costo-wood'},
        {'label': 'Costo Standard', 'icona': '💰', 'url': '/costo-standard-wood'},
        {'label': 'Parametri di Lavorazione', 'icona': '⚙️', 'url': '/parametri-lavorazione-wood'},
        {'label': 'Contapieghe', 'icona': '🔧', 'url': '/contapieghe-wood'},
        {'label': 'Esploratore Prodotto', 'icona': '🌳', 'url': '/esploratore-prodotto'},
        {'label': 'Matrice Competenze', 'icona': '🧩', 'url': '/matrice-competenze'},
    ]},
    {'nome': 'Commerciale & Post-Vendita', 'icona': '📋', 'colore': '#c0392b', 'voci': [
        {'label': 'Commesse', 'icona': '📑', 'url': '/commesse'},
        {'label': 'Disponibilità (Commerciale)', 'icona': '📦', 'url': '/commerciale/disponibilita'},
        {'label': 'Situazione (Post-Vendita)', 'icona': '📋', 'url': '/postvendita/situazione'},
    ]},
    {'nome': 'Sistema', 'icona': '🧹', 'colore': '#6b8aaa', 'voci': [
        {'label': 'Manutenzioni di Sistema', 'icona': '🧹', 'url': '/manutenzione-sistema'},
    ]},
]


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    from blueprints.monitor.routes import monitor_bp
    from blueprints.kanban.routes import kanban_bp
    from blueprints.kpi.routes import kpi_bp
    from blueprints.terzisti.routes import terzisti_bp
    from blueprints.magazzino.routes import magazzino_bp
    from blueprints.produzione_pp.routes import pp_bp
    from blueprints.acquisti_wood.routes import acquisti_wood_bp
    from blueprints.commesse.routes import commesse_bp

    app.register_blueprint(monitor_bp)
    app.register_blueprint(kanban_bp)
    app.register_blueprint(kpi_bp)
    app.register_blueprint(terzisti_bp)
    app.register_blueprint(magazzino_bp)
    app.register_blueprint(pp_bp)
    app.register_blueprint(acquisti_wood_bp)
    app.register_blueprint(commesse_bp)

    @app.context_processor
    def inject_globals():
        try:
            gruppi = get_kanban_gruppi()
        except Exception:
            gruppi = []
        try:
            macchine = get_macchine_monitor()
        except Exception:
            macchine = []
        return {'now': datetime.now().strftime('%d/%m/%y'), 'kanban_gruppi': gruppi, 'macchine_monitor': macchine,
                'sidebar_gruppi': LAUNCHPAD_GRUPPI}


    @app.route('/launchpad')
    def launchpad():
        return render_template('launchpad/index.html', active='launchpad', gruppi=LAUNCHPAD_GRUPPI)

    @app.route('/')
    def index():
        return redirect(url_for('launchpad'))

    with app.app_context():
        db.create_all(bind_key=None)   # solo il DB locale — mai il bind 'masterlogistic'
        init_db()
        inizializza_schema_pp()
        # DEVE girare PRIMA di migra_schede_lavorazione_unificate(): quella
        # interroga SchedaLavorazioneWood via ORM, che ora include
        # contromatrice_id nel modello Python — su un DB dove la colonna
        # non esiste ancora, QUALSIASI query su quella tabella fallisce con
        # UndefinedColumn (visto in produzione: il worker non partiva più).
        assicura_contapieghe_matrici()
        migra_schede_lavorazione_unificate()
        migra_parametri_lavorazione_flat()
        assicura_lunghezze_barra_default()
        assicura_unita_misura_articoli()
        assicura_finiti_is_kanban()
        assicura_lead_time_esterno_centri()
        assicura_ordinato_cliente_wms()
        assicura_operatore_evento_consuntivo()
        assicura_contestuale_distinta_base()
        assicura_event_id_varianza_produzione()
        assicura_bandiera_stato_op()
        assicura_lotto_trasferimento_minimo()
        assicura_origine_ordine_acquisto_wood()
        assicura_campi_stampa_ordine_fornitore()
        assicura_ddt_carico_confermato()
        assicura_mappa_mw_fase()
        assicura_import_consumabili_sicurezza()
        assicura_categoria_acquisto_config()

    return app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
