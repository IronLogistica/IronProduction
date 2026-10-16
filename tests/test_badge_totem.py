"""
Badge operatore sul Totem Alessandro (richiesta Mauri, 16/10/2026): il totem si sblocca
passando il badge QR di un "Dichiaratore"; ogni dichiarazione porta il nome di chi l'ha
fatta. Parte solo quando Angelo lo attiva; riguarda solo il Totem Alessandro.
"""
import unittest

from flask import Flask

from models import (db, OperatoreWood, ImpostazioneTotemWood, CentroCostoWood, CicloLavoroWood, OrdineProduzione,
                    EventoConsuntivoPP, GiacenzaWood, MovimentoGiacenzaWood, AuditPP, DistintaBaseWood)
from blueprints.produzione_pp.routes import pp_bp
from blueprints.produzione_pp.badge_totem import badge_totem_bp

PIN = '1234'
TOTEM = {'X-Totem': 'alessandro'}


class TestBadgeTotem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='test', CAPO_PIN='9999')
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        cls.app.register_blueprint(badge_totem_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (MovimentoGiacenzaWood, GiacenzaWood, AuditPP, EventoConsuntivoPP, CicloLavoroWood, DistintaBaseWood,
                      OrdineProduzione, CentroCostoWood, ImpostazioneTotemWood, OperatoreWood):
                db.session.query(m).delete()
            db.session.add_all([OperatoreWood(nome='MARIO ROSSI'), OperatoreWood(nome='LUCA BIANCHI')])
            db.session.commit()
            self.mario = OperatoreWood.query.filter_by(nome='MARIO ROSSI').one().id
            self.luca = OperatoreWood.query.filter_by(nome='LUCA BIANCHI').one().id
        self.c = self.app.test_client()

    def _dichiaratore(self, oid, si=True):
        return self.c.post(f'/api/operatori/{oid}/dichiaratore', json={'dichiaratore': si}).get_json()

    def _codice(self, oid):
        with self.app.app_context():
            return db.session.get(OperatoreWood, oid).codice_badge

    def _libera(self, token=None, headers=TOTEM):
        body = {'codice': 'TR2003', 'quantita': 5}
        if token:
            body['badge_token'] = token
        return self.c.post('/api/dichiarazione-produzione/libera', json=body, headers=headers)

    def test_qualifica_e_sblocco(self):
        self.assertEqual(self.c.post('/api/totem-badge/sblocca', json={'codice': 'XXX'}).status_code, 404)
        d = self._dichiaratore(self.mario)
        self.assertTrue(d['dichiaratore'] and d['ha_badge'])
        codice = self._codice(self.mario)
        r = self.c.post('/api/totem-badge/sblocca', json={'codice': codice.lower() + ' '}).get_json()
        self.assertEqual(r['nome'], 'MARIO ROSSI')
        self.assertTrue(r['token'])
        self.assertEqual(r['minuti_inattivita'], 2)
        # lettore con tastiera americana su PC italiano: il trattino arriva come apostrofo
        self.assertEqual(self.c.post('/api/totem-badge/sblocca', json={'codice': codice.replace('-', "'")}).get_json()['nome'], 'MARIO ROSSI')
        # tolta la qualifica: il badge non sblocca più
        self._dichiaratore(self.mario, False)
        self.assertEqual(self.c.post('/api/totem-badge/sblocca', json={'codice': codice}).status_code, 403)

    def test_angelo_attiva_con_pin_e_solo_con_dichiaratori(self):
        self.assertEqual(self.c.post('/api/totem-badge/attiva', json={'attivo': True, 'pin': '0'}).status_code, 403)
        r = self.c.post('/api/totem-badge/attiva', json={'attivo': True, 'pin': PIN})
        self.assertEqual(r.status_code, 400, 'senza dichiaratori non si attiva')
        self._dichiaratore(self.mario)
        d = self.c.post('/api/totem-badge/attiva', json={'attivo': True, 'pin': PIN}).get_json()
        self.assertTrue(d['attivo'] and d['attivato_il'])
        self.assertTrue(self.c.get('/api/totem-badge/stato').get_json()['attivo'])

    def test_dichiarazione_dal_totem_porta_il_nome(self):
        self._dichiaratore(self.mario)
        # sistema NON attivo: il totem dichiara come oggi
        self.assertTrue(self._libera().get_json()['ok'])
        self.c.post('/api/totem-badge/attiva', json={'attivo': True, 'pin': PIN})
        r = self._libera()
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()['badge_richiesto'])
        token = self.c.post('/api/totem-badge/sblocca', json={'codice': self._codice(self.mario)}).get_json()['token']
        self.assertTrue(self._libera(token).get_json()['ok'])
        with self.app.app_context():
            note = MovimentoGiacenzaWood.query.order_by(MovimentoGiacenzaWood.id.desc()).first().note
        self.assertIn('MARIO ROSSI', note)
        # le altre pagine di dichiarazione (senza intestazione del totem) non cambiano
        self.assertTrue(self._libera(headers={}).get_json()['ok'])

    def test_dichiarazione_op_salva_operatore(self):
        self._dichiaratore(self.mario)
        with self.app.app_context():
            seg = CentroCostoWood(nome='Segatrice')
            db.session.add(seg)
            db.session.flush()
            db.session.add_all([CicloLavoroWood(codice='TR2003', sequenza=1, centro_costo_id=seg.id),
                                OrdineProduzione(codice='OP-1', codice_articolo='TR2003', commessa='C1',
                                                 qta_pianificata=100, stato='Rilasciato', priorita=1)])
            db.session.commit()
            cid = seg.id
        self.c.post('/api/totem-badge/attiva', json={'attivo': True, 'pin': PIN})
        token = self.c.post('/api/totem-badge/sblocca', json={'codice': self._codice(self.mario)}).get_json()['token']
        body = {'op_code': 'OP-1', 'centro_id': cid, 'pezzi_buoni': 10, 'pezzi_scarto': 0, 'tempo_minuti': 0}
        self.assertEqual(self.c.post('/api/dichiarazione-produzione', json=body, headers=TOTEM).status_code, 401)
        r = self.c.post('/api/dichiarazione-produzione', json=dict(body, badge_token=token), headers=TOTEM)
        self.assertTrue(r.get_json()['ok'], r.get_json())
        with self.app.app_context():
            self.assertEqual(EventoConsuntivoPP.query.one().operatore, 'MARIO ROSSI')
        storico = self.c.get(f'/api/dichiarazione-produzione/{cid}/storico?pin=9999').get_json()
        self.assertEqual(storico['eventi'][0]['operatore'], 'MARIO ROSSI')

    def test_badge_rigenerato_invalida_il_vecchio(self):
        self._dichiaratore(self.mario)
        self.c.post('/api/totem-badge/attiva', json={'attivo': True, 'pin': PIN})
        vecchio = self._codice(self.mario)
        token = self.c.post('/api/totem-badge/sblocca', json={'codice': vecchio}).get_json()['token']
        self.assertEqual(self.c.post(f'/api/operatori/{self.mario}/badge/rigenera', json={'pin': '0'}).status_code, 403)
        self.assertTrue(self.c.post(f'/api/operatori/{self.mario}/badge/rigenera', json={'pin': PIN}).get_json()['ok'])
        self.assertNotEqual(self._codice(self.mario), vecchio)
        self.assertEqual(self.c.post('/api/totem-badge/sblocca', json={'codice': vecchio}).status_code, 404)
        self.assertEqual(self._libera(token).status_code, 401, 'il token del badge vecchio non vale più')

    def test_pagina_badge_e_matrice(self):
        self._dichiaratore(self.mario)
        html = self.c.get('/operatori/badge').get_data(as_text=True)
        self.assertIn('MARIO ROSSI', html)
        self.assertIn('<svg', html)
        self.assertNotIn('LUCA BIANCHI', html, 'solo i dichiaratori')
        ops = {o['nome']: o for o in self.c.get('/api/matrice-competenze').get_json()['operatori']}
        self.assertTrue(ops['MARIO ROSSI']['dichiaratore'])
        self.assertFalse(ops['LUCA BIANCHI']['dichiaratore'])


if __name__ == '__main__':
    unittest.main()
