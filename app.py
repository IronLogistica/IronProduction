import os
from datetime import datetime
from flask import Flask, redirect, url_for
from config import Config
from models import db, init_db, inizializza_schema_pp, get_kanban_gruppi, get_macchine_monitor, migra_schede_lavorazione_unificate, assicura_lunghezze_barra_default, assicura_unita_misura_articoli, assicura_finiti_is_kanban, assicura_lead_time_esterno_centri, assicura_ordinato_cliente_wms, assicura_operatore_evento_consuntivo

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

    app.register_blueprint(monitor_bp)
    app.register_blueprint(kanban_bp)
    app.register_blueprint(kpi_bp)
    app.register_blueprint(terzisti_bp)
    app.register_blueprint(magazzino_bp)
    app.register_blueprint(pp_bp)
    app.register_blueprint(acquisti_wood_bp)

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
        return {'now': datetime.now().strftime('%d/%m/%y'), 'kanban_gruppi': gruppi, 'macchine_monitor': macchine}

    @app.route('/')
    def index():
        return redirect(url_for('monitor.index'))

    with app.app_context():
        db.create_all(bind_key=None)   # solo il DB locale — mai il bind 'masterlogistic'
        init_db()
        inizializza_schema_pp()
        migra_schede_lavorazione_unificate()
        assicura_lunghezze_barra_default()
        assicura_unita_misura_articoli()
        assicura_finiti_is_kanban()
        assicura_lead_time_esterno_centri()
        assicura_ordinato_cliente_wms()
        assicura_operatore_evento_consuntivo()

    return app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
