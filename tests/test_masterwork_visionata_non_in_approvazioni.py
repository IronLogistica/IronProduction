"""Angelo 08/10/2026: la stessa dichiarazione MasterWork compariva in 'Produzioni dichiarate in officina'
e in 'Area Direzione - approvazioni'. Una volta 'visionata e registrata' deve sparire da entrambe."""
import unittest
from datetime import datetime

from flask import Flask

from models import db, EventoConsuntivoPP
from blueprints.produzione_pp.routes import pp_bp, PIN_DIREZIONE


class TestVisionataFuoriDalleApprovazioni(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder='../templates')
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(pp_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            for i, op in enumerate(('OP-2026-000026', 'OP-2026-000072'), start=1):
                db.session.add(EventoConsuntivoPP(event_id=f'E{i}', op_code=op, fase='Saldatura',
                                                  timestamp_evento=datetime.utcnow(), pezzi_buoni=40 + i))
            db.session.commit()
            self.ids = [e.id for e in EventoConsuntivoPP.query.order_by(EventoConsuntivoPP.id)]
        self.c = self.app.test_client()

    def _approvazioni(self):
        return self.c.get(f'/api/dichiarazione-produzione/approvazioni?pin={PIN_DIREZIONE}').get_json()['eventi']

    def test_visionata_sparisce_da_entrambi_gli_elenchi(self):
        self.assertEqual(len(self._approvazioni()), 2)
        self.assertEqual(len(self.c.get('/api/dichiarazione-produzione/masterwork-pendenti').get_json()['eventi']), 2)
        r = self.c.post(f'/api/dichiarazione-produzione/eventi/{self.ids[0]}/visiona', json={'visionato': True})
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual([e['op_code'] for e in self._approvazioni()], ['OP-2026-000072'])
        self.assertEqual(len(self.c.get('/api/dichiarazione-produzione/masterwork-pendenti').get_json()['eventi']), 1)
        # tolta la spunta, torna in entrambe
        self.c.post(f'/api/dichiarazione-produzione/eventi/{self.ids[0]}/visiona', json={'visionato': False})
        self.assertEqual(len(self._approvazioni()), 2)


if __name__ == '__main__':
    unittest.main()
