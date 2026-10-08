"""
Segnalato da Angelo (07/10/2026, Z01 di OP-2026-000063): stornare una
dichiarazione lasciava il WIP per fase avanzato. 50 piegati (Curvatubi),
400 forati a Trapani (senza che esistessero), storno, altri 400 forati ->
WIP Trapani 800 invece di 400.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, GiacenzaWood,
                     WipFaseWood, OrdineProduzione, EventoConsuntivoPP)
from blueprints.produzione_pp.routes import _registra_evento_consuntivo, _storna_evento_consuntivo


class TestStornoWip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (EventoConsuntivoPP, WipFaseWood, DistintaBaseWood, GiacenzaWood,
                      OrdineProduzione, CicloLavoroWood, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            c, t, s = CentroCostoWood(nome='Curvatubi'), CentroCostoWood(nome='Trapani'), CentroCostoWood(nome='Saldatura')
            db.session.add_all([c, t, s])
            db.session.flush()
            self.c_id, self.t_id = c.id, t.id
            db.session.add_all([
                CicloLavoroWood(codice='Z01', sequenza=1, centro_costo_id=c.id),
                CicloLavoroWood(codice='Z01', sequenza=2, centro_costo_id=t.id),
                CicloLavoroWood(codice='ZT', sequenza=1, centro_costo_id=s.id),
                DistintaBaseWood(codice_padre='ZT', codice_figlio='Z01', quantita=1.0),
                OrdineProduzione(codice='OP-1', codice_articolo='ZT', qta_pianificata=810,
                                 stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def _dichiara(self, n, fase, buoni):
        o = OrdineProduzione.query.filter_by(codice='OP-1').one()
        _registra_evento_consuntivo(o, fase, datetime.utcnow(), good=buoni, scrap=0, tempo=0,
                                    event_id=f'ev{n}', componente='Z01', approvato_direzione=True)
        db.session.commit()

    def _storna(self, event_id):
        o = OrdineProduzione.query.filter_by(codice='OP-1').one()
        e = EventoConsuntivoPP.query.filter_by(event_id=event_id).one()
        _storna_evento_consuntivo(e, o)
        db.session.commit()

    def _wip(self, centro_id):
        r = WipFaseWood.query.filter_by(codice='Z01', centro_costo_id=centro_id).first()
        return r.quantita if r else 0

    def test_scenario_angelo_wip_trapani_non_raddoppia(self):
        with self.app.app_context():
            self._dichiara(1, 'Curvatubi', 50)
            self._dichiara(2, 'Trapani', 400)
            self.assertEqual((self._wip(self.c_id), self._wip(self.t_id)), (0, 400))
            self._storna('ev2')
            self.assertEqual(self._wip(self.t_id), 0, "lo storno deve togliere i 400 da Trapani")
            self.assertEqual(self._wip(self.c_id), 50, "e riportare i 50 piegati ancora a Curvatubi")
            self._dichiara(3, 'Trapani', 400)
            self.assertEqual(self._wip(self.t_id), 400, "prima della fix: 800")

    def test_storno_normale_ripristina_fase_precedente(self):
        with self.app.app_context():
            self._dichiara(1, 'Curvatubi', 100)
            self._dichiara(2, 'Trapani', 40)
            self.assertEqual((self._wip(self.c_id), self._wip(self.t_id)), (60, 40))
            self._storna('ev2')
            self.assertEqual((self._wip(self.c_id), self._wip(self.t_id)), (100, 0))


class TestBloccoFasePrecedente(TestStornoWip):
    """Non si dichiara una fase successiva se la precedente non ha WIP sufficiente."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from blueprints.produzione_pp.routes import pp_bp
        cls.app.register_blueprint(pp_bp)

    def _post(self, centro_id, buoni, scarto=0):
        return self.app.test_client().post('/api/dichiarazione-produzione', json={
            'op_code': 'OP-1', 'centro_id': centro_id, 'componente': 'Z01',
            'pezzi_buoni': buoni, 'pezzi_scarto': scarto, 'tempo_minuti': 0})

    def test_blocca_senza_wip_poi_consente_nei_limiti(self):
        with self.app.app_context():
            r = self._post(self.t_id, 400)
            self.assertEqual(r.status_code, 409)
            self.assertTrue(r.get_json()['wip_insufficiente'])
            self.assertEqual(EventoConsuntivoPP.query.count(), 0, "nessun evento, nessun movimento di magazzino")
            self.assertEqual(self._post(self.c_id, 50).status_code, 200)       # prima fase: sempre libera
            r = self._post(self.t_id, 51)
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()['disponibile'], 50)
            self.assertEqual(self._post(self.t_id, 50).status_code, 200)
            self.assertEqual((self._wip(self.c_id), self._wip(self.t_id)), (0, 50))
            self.assertEqual(self._post(self.t_id, 1).status_code, 409, "i 50 sono gia' passati a Trapani")


class TestBloccoSemilavoratiDistinta(TestStornoWip):
    """TR2006-L (pressopiegato) dichiarato senza TR2006 (punzonato) disponibili: bloccato."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from blueprints.produzione_pp.routes import pp_bp
        if 'produzione_pp' not in cls.app.blueprints:
            cls.app.register_blueprint(pp_bp)

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            p, z = CentroCostoWood(nome='Pressopiegatrice'), CentroCostoWood(nome='Punzonatrice')
            db.session.add_all([p, z]); db.session.flush()
            self.p_id = p.id
            db.session.add_all([
                CicloLavoroWood(codice='TR2006-L', sequenza=1, centro_costo_id=p.id),
                CicloLavoroWood(codice='TR2006', sequenza=1, centro_costo_id=z.id),
                DistintaBaseWood(codice_padre='TR2006-L', codice_figlio='TR2006', quantita=1.0),
                DistintaBaseWood(codice_padre='TR2006', codice_figlio='TONDO8M', quantita=0.15),
                GiacenzaWood(codice='TONDO8M', quantita=100),
                OrdineProduzione(codice='OP-L', codice_articolo='TR2006-L', qta_pianificata=50,
                                 stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def _post(self, buoni):
        return self.app.test_client().post('/api/dichiarazione-produzione', json={
            'op_code': 'OP-L', 'centro_id': self.p_id, 'componente': '',
            'pezzi_buoni': buoni, 'pezzi_scarto': 0, 'tempo_minuti': 0})

    def test_blocca_se_manca_il_semilavorato_poi_consente(self):
        with self.app.app_context():
            r = self._post(10)
            self.assertEqual(r.status_code, 409)
            d = r.get_json()
            self.assertTrue(d['wip_insufficiente'])
            self.assertIn('TR2006: disponibili 0, servono 10', d['error'])
            self.assertEqual(EventoConsuntivoPP.query.count(), 0)
            db.session.add(GiacenzaWood(codice='TR2006', quantita=10)); db.session.commit()
            self.assertEqual(self._post(10).status_code, 200)
            self.assertEqual(GiacenzaWood.query.get('TR2006').quantita, 0)
            self.assertEqual(self._post(1).status_code, 409, "consumati tutti")

    def test_materia_prima_in_negativo_bloccata_chiama_angelo(self):
        with self.app.app_context():
            db.session.add(OrdineProduzione(codice='OP-T', codice_articolo='TR2006', qta_pianificata=2000,
                                            stato='Rilasciato', priorita=1))
            db.session.commit()
            z = CentroCostoWood.query.filter_by(nome='Punzonatrice').one()
            c = self.app.test_client()
            body = {'op_code': 'OP-T', 'centro_id': z.id, 'componente': '', 'pezzi_scarto': 0, 'tempo_minuti': 0}
            r = c.post('/api/dichiarazione-produzione', json={**body, 'pezzi_buoni': 1000})   # servono 150, ce ne sono 100
            self.assertEqual(r.status_code, 409)
            d = r.get_json()
            self.assertTrue(d['chiama_angelo'])
            self.assertIn('CHIAMA ANGELO', d['error'])
            self.assertEqual(GiacenzaWood.query.get('TONDO8M').quantita, 100, "nessuno scarico")
            self.assertEqual(c.post('/api/dichiarazione-produzione', json={**body, 'pezzi_buoni': 500}).status_code, 200)


class TestSaldoTrapaniZ01(TestStornoWip):
    """Z01 di OP-2026-000063 (Mauri, 08/10/2026): 660 fatti a Trapani su 810, 150 ancora a
    Curvatubi: Dichiarazione, Ordine di Lavoro e Live devono dire tutti saldo 150."""

    def test_ol_live_e_dichiarazione_coincidono(self):
        from blueprints.produzione_pp.routes import _lista_lavoro_op
        from blueprints.monitor.routes import _righe_macchina
        with self.app.app_context():
            db.session.add_all([GiacenzaWood(codice='Z01', quantita=810),
                                WipFaseWood(codice='Z01', centro_costo_id=self.c_id, quantita=150),
                                WipFaseWood(codice='Z01', centro_costo_id=self.t_id, quantita=660)])
            for n, (fase, q) in enumerate((('Curvatubi', 810), ('Trapani', 660))):
                db.session.add(EventoConsuntivoPP(event_id=f'e{n}', op_code='OP-1', fase=fase, componente='Z01',
                                                  timestamp_evento=datetime.utcnow(), pezzi_buoni=q, pezzi_scarto=0,
                                                  tempo_minuti=0, approvato_direzione=True))
            db.session.commit()
            trapani = db.session.get(CentroCostoWood, self.t_id)
            op = OrdineProduzione.query.filter_by(codice='OP-1').one()
            riga = _lista_lavoro_op(op, trapani, assegna_numero=False)['gruppi'][0]['righe'][0]
            self.assertEqual((riga['codice'], riga['saldo']), ('Z01', 150), "prima della fix: 0")
            r = next(x for sez in _righe_macchina(trapani).values() for x in sez if x['op_codice'] == 'OP-1')
            self.assertEqual(r['saldo'], 150)
            self.assertNotEqual(r.get('completato'), True)
