"""
Regressione (26/10/2026, OP-2026-000090): eliminare una commessa trascinata nel cruscotto KPI dava
"The string did not match the expected pattern" su Safari: la riga di posizione manuale
(SequenzaAvanzamentoKPI) puntava all'OP con chiave esterna e Postgres rifiutava la cancellazione (500 HTML).
Qui le chiavi esterne sono ATTIVE anche su SQLite, come in produzione.
"""
import unittest

from flask import Flask
from sqlalchemy import event
from sqlalchemy.engine import Engine

from blueprints.produzione_pp.routes import pp_bp
from models import db, OrdineProduzione, SequenzaAvanzamentoKPI, SequenzaMonitorMacchina, CentroCostoWood


@event.listens_for(Engine, 'connect')
def _fk_on(dbapi_conn, _):
    try:
        dbapi_conn.execute('PRAGMA foreign_keys=ON')
    except Exception:
        pass


class TestEliminaOpConPosizioneKpi(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(pp_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            c = CentroCostoWood(nome='Segatrice')
            o = OrdineProduzione(codice='OP-2026-000090', codice_articolo='PINX110', qta_pianificata=35, stato='Rilasciato')
            db.session.add_all([c, o])
            db.session.flush()
            db.session.add_all([SequenzaAvanzamentoKPI(ordine_produzione_id=o.id, posizione=1),
                                SequenzaMonitorMacchina(ordine_produzione_id=o.id, centro_costo_id=c.id, posizione=1)])
            db.session.commit()
            self.oid = o.id

    def test_elimina_op_trascinata_nel_kpi(self):
        r = self.app.test_client().delete(f'/api/ordini-produzione/{self.oid}')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(OrdineProduzione.query.count(), 0)
            self.assertEqual(SequenzaAvanzamentoKPI.query.count(), 0)
            self.assertEqual(SequenzaMonitorMacchina.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
