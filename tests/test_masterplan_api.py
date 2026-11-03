"""MASTERPLAN · AMPS — pagine e API con dati veri in un database in memoria."""
import unittest
from datetime import date, datetime, timedelta

from flask import Flask

from blueprints.masterplan.modelli import MpFamiglia, MpFamigliaCodice, MpImpostazione, MpParametroArticolo, MpPrevisione
from blueprints.masterplan.routes import masterplan_bp
from models import (ArticoloApprovvigionamento, CentroCostoWood, CicloLavoroWood, DistintaBaseWood,
                    OrdineProduzione, Prodotto, db)

PROSSIMO_MESE = (date.today().replace(day=1) + timedelta(days=40)).replace(day=1)


class TestMasterplanApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='t')
        db.init_app(cls.app)
        cls.app.register_blueprint(masterplan_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (MpParametroArticolo, MpPrevisione, MpFamigliaCodice, MpFamiglia, MpImpostazione, CicloLavoroWood, CentroCostoWood,
                      DistintaBaseWood, ArticoloApprovvigionamento, OrdineProduzione, Prodotto):
                db.session.query(m).delete()
            db.session.commit()
        self.c = self.app.test_client()

    def _distinta(self):
        with self.app.app_context():
            db.session.add_all([
                DistintaBaseWood(codice_padre='FIN1', codice_figlio='COMP1', quantita=2),
                DistintaBaseWood(codice_padre='FIN2', codice_figlio='COMP1', quantita=1),
                ArticoloApprovvigionamento(codice='COMP1', tipo_approvvigionamento='DA_CLASSIFICARE'),
                Prodotto(codice='FIN1', categoria='PORTE'), Prodotto(codice='FIN2', categoria='PORTE')])
            db.session.commit()

    def test_pagine_rispondono(self):
        for u in ('/masterplan', '/masterplan/qualita-dati', '/masterplan/famiglie', '/masterplan/previsioni',
                  '/masterplan/domanda', '/masterplan/impostazioni'):
            self.assertEqual(self.c.get(u).status_code, 200, u)

    def test_qualita_segnala_foglia_da_classificare_e_senza_ciclo(self):
        self._distinta()
        q = self.c.get('/api/masterplan/qualita').get_json()
        tipi = {(p['tipo'], p['codice']) for p in q['problemi']}
        self.assertIn(('DA_CLASSIFICARE', 'COMP1'), tipi)
        self.assertIn(('SENZA_CICLO', 'FIN1'), tipi)
        self.assertEqual(q['sintesi']['prodotti_finiti'], 2)
        self.assertEqual(q['sintesi']['pronti'], 0)

    def test_famiglia_mix_non_100_rifiutato(self):
        r = self.c.post('/api/masterplan/famiglie', json={'codice': 'PORTE', 'codici': [
            {'codice': 'FIN1', 'mix_pct': 60}, {'codice': 'FIN2', 'mix_pct': 30}]})
        self.assertEqual(r.status_code, 400)
        r = self.c.post('/api/masterplan/famiglie', json={'codice': 'PORTE', 'codici': [
            {'codice': 'FIN1', 'mix_pct': 60}, {'codice': 'FIN2', 'mix_pct': 40}]})
        self.assertTrue(r.get_json()['ok'])
        r = self.c.post('/api/masterplan/famiglie', json={'codice': 'ALTRA', 'codici': [{'codice': 'FIN1', 'mix_pct': 100}]})
        self.assertEqual(r.status_code, 400)       # FIN1 è già in PORTE

    def test_solo_approvate_entrano_nel_calcolo(self):
        self._distinta()
        self.c.post('/api/masterplan/previsioni', json={'livello': 'ARTICOLO', 'codice': 'FIN1',
                                                         'mese': PROSSIMO_MESE.isoformat(), 'quantita': 100})
        d = self.c.get('/api/masterplan/domanda').get_json()
        self.assertEqual(d['totali']['domanda'], 0)
        self.c.post('/api/masterplan/previsioni/approva')
        d = self.c.get('/api/masterplan/domanda').get_json()
        self.assertEqual(d['totali']['domanda'], 100)

    def test_modifica_riporta_a_bozza(self):
        body = {'livello': 'ARTICOLO', 'codice': 'FIN1', 'mese': PROSSIMO_MESE.isoformat(), 'quantita': 10}
        self.c.post('/api/masterplan/previsioni', json=body)
        self.c.post('/api/masterplan/previsioni/approva')
        body['quantita'] = 20
        p = self.c.post('/api/masterplan/previsioni', json=body).get_json()['previsione']
        self.assertEqual((p['stato'], p['quantita']), ('BOZZA', 20))
        self.assertEqual(len(self.c.get('/api/masterplan/previsioni').get_json()), 1)

    def test_famiglia_divisa_secondo_mix_e_ordini_consumano(self):
        self._distinta()
        self.c.post('/api/masterplan/famiglie', json={'codice': 'PORTE', 'codici': [
            {'codice': 'FIN1', 'mix_pct': 70}, {'codice': 'FIN2', 'mix_pct': 30}]})
        fid = self.c.get('/api/masterplan/famiglie').get_json()[0]['id']
        self.c.post('/api/masterplan/previsioni', json={'livello': 'FAMIGLIA', 'famiglia_id': fid,
                                                         'mese': PROSSIMO_MESE.isoformat(), 'quantita': 100})
        self.c.post('/api/masterplan/previsioni/approva')
        d = self.c.get('/api/masterplan/domanda').get_json()
        per = {c['codice']: c['tot_domanda'] for c in d['codici']}
        self.assertEqual(per, {'FIN1': 70, 'FIN2': 30})

    def test_ordine_certo_conta_e_a_scorta_no(self):
        with self.app.app_context():
            db.session.add_all([
                OrdineProduzione(codice='OP1', codice_articolo='fin1', qta_pianificata=40, stato='Rilasciato',
                                 data_prevista=date.today() + timedelta(days=21)),
                OrdineProduzione(codice='OP2', codice_articolo='FIN1', qta_pianificata=99, stato='Rilasciato',
                                 a_scorta=True)])
            db.session.commit()
        d = self.c.get('/api/masterplan/domanda').get_json()
        self.assertEqual(d['totali']['ordini'], 40)
        self.assertEqual(d['codici'][0]['codice'], 'FIN1')

    def test_impostazioni_validate(self):
        self.assertEqual(self.c.post('/api/masterplan/impostazioni', json={'orizzonte_settimane': 999}).status_code, 400)
        r = self.c.post('/api/masterplan/impostazioni', json={'orizzonte_settimane': 12}).get_json()
        self.assertEqual(r['valori']['orizzonte_settimane'], 12)


if __name__ == '__main__':
    unittest.main()


class TestMrpApi(unittest.TestCase):
    setUpClass = classmethod(TestMasterplanApi.setUpClass.__func__)
    setUp = TestMasterplanApi.setUp

    def test_esplosione_con_date_giacenza_e_ordini_aperti(self):
        from models import GiacenzaWood
        with self.app.app_context():
            db.session.add_all([
                DistintaBaseWood(codice_padre='FIN1', codice_figlio='COMP1', quantita=2),
                ArticoloApprovvigionamento(codice='COMP1', tipo_approvvigionamento='COMPONENTE_ACQUISTO',
                                           lead_time_fornitura_giorni=10),
                GiacenzaWood(codice='COMP1', quantita=20),
                OrdineProduzione(codice='OP1', codice_articolo='FIN1', qta_pianificata=40, stato='Rilasciato',
                                 data_prevista=date.today() + timedelta(days=28))])
            db.session.commit()
        d = self.c.get('/api/masterplan/mrp').get_json()
        comp = {c['codice']: c for c in d['codici']}['COMP1']
        self.assertEqual(sum(r['lordo'] for r in comp['righe']), 80)          # 40 pz × 2
        self.assertEqual(comp['lead_time_settimane'], 2)                       # 10 gg / 5
        self.assertEqual(sum(o['quantita'] for o in comp['ordini_pianificati']), 60)   # 80 − 20 di giacenza
        self.assertTrue(comp['acquisto'])
        self.assertEqual(self.c.get('/masterplan/mrp').status_code, 200)

    def _mrp_base(self):
        from models import GiacenzaWood
        with self.app.app_context():
            db.session.add_all([
                DistintaBaseWood(codice_padre='FIN1', codice_figlio='COMP1', quantita=1),
                ArticoloApprovvigionamento(codice='COMP1', tipo_approvvigionamento='COMPONENTE_ACQUISTO', lead_time_fornitura_giorni=5),
                OrdineProduzione(codice='OP1', codice_articolo='FIN1', qta_pianificata=47, stato='Rilasciato',
                                 data_prevista=date.today() + timedelta(days=28))])
            db.session.commit()

    def test_lotto_minimo_e_multiplo(self):
        self._mrp_base()
        self.c.post('/api/masterplan/parametri-articolo', json={'codice': 'comp1', 'lotto_minimo': 100, 'multiplo': 50})
        comp = {c['codice']: c for c in self.c.get('/api/masterplan/mrp').get_json()['codici']}['COMP1']
        self.assertEqual([o['quantita'] for o in comp['ordini_pianificati']], [100])      # 47 -> minimo 100
        self.c.post('/api/masterplan/parametri-articolo', json={'codice': 'COMP1', 'lotto_minimo': 0, 'multiplo': 25})
        comp = {c['codice']: c for c in self.c.get('/api/masterplan/mrp').get_json()['codici']}['COMP1']
        self.assertEqual([o['quantita'] for o in comp['ordini_pianificati']], [50])       # 47 -> multiplo 25
        self.assertEqual(len(self.c.get('/api/masterplan/mrp').get_json()['proposte']['acquisto']), 1)

    def test_scenari_pessimista_base_ottimista(self):
        for aff, q in (('CERTA', 100), ('PROBABILE', 100), ('POSSIBILE', 100)):
            self.c.post('/api/masterplan/previsioni', json={'livello': 'ARTICOLO', 'codice': 'FIN1', 'affidabilita': aff,
                                                             'mese': PROSSIMO_MESE.isoformat(), 'quantita': q})
        self.c.post('/api/masterplan/previsioni/approva')
        d = {x['scenario']: x['previsione'] for x in self.c.get('/api/masterplan/scenari').get_json()}
        self.assertEqual(d['pessimista'], 100)
        self.assertEqual(d['base'], 170)           # 100 + 70% di 100
        self.assertEqual(d['ottimista'], 200)      # + 30% di 100
        self.assertEqual(self.c.get('/api/masterplan/domanda?scenario=ottimista').get_json()['scenario'], 'ottimista')

    def test_carico_reparti(self):
        from models import CentroCostoWood, CicloLavoroWood
        self._mrp_base()
        with self.app.app_context():
            cc = CentroCostoWood(nome='TAGLIO', n_risorse_equivalenti=1, pct_efficienza=100)
            db.session.add(cc)
            db.session.flush()
            db.session.add(CicloLavoroWood(codice='FIN1', sequenza=1, centro_costo_id=cc.id, produttivita_oraria=1))
            db.session.commit()
        d = self.c.get('/api/masterplan/mrp').get_json()
        taglio = d['carico'][0]
        self.assertEqual(taglio['centro'], 'TAGLIO')
        self.assertEqual(sum(taglio['ore']), 47.0)                  # 47 pz a 1 pz/h
        self.assertEqual(taglio['capacita_ore'], 40.0)              # 8 h × 5 gg
        self.assertGreater(max(taglio['saturazione']), 100)
        self.assertEqual(d['totali']['reparti_saturi'], 1)

    def test_errore_previsione(self):
        mese_scorso = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
        with self.app.app_context():
            db.session.add(OrdineProduzione(codice='OP9', codice_articolo='FIN1', qta_pianificata=80, stato='Chiuso',
                                            creato_il=datetime.combine(mese_scorso + timedelta(days=3), datetime.min.time())))
            db.session.commit()
        self.c.post('/api/masterplan/previsioni', json={'livello': 'ARTICOLO', 'codice': 'FIN1',
                                                         'mese': mese_scorso.isoformat(), 'quantita': 100})
        self.assertIsNone(self.c.get('/api/masterplan/errore').get_json()['totale'])      # bozza: non conta
        self.c.post('/api/masterplan/previsioni/approva')
        t = self.c.get('/api/masterplan/errore').get_json()['totale']
        self.assertEqual((t['previsto'], t['effettivo'], t['wape'], t['bias']), (100, 80, 25.0, 25.0))
        for u in ('/masterplan/proposte', '/masterplan/scenari', '/masterplan/errore'):
            self.assertEqual(self.c.get(u).status_code, 200)
