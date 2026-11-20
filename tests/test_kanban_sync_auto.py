"""Riallineamento automatico Kanban da WMS ogni 4 ore (Mauri, 14/10/2026 — T250DT:
impegno inserito in Logistics non visibile nel Kanban HPI senza premere 🔄)."""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from flask import Flask

from models import db, KanbanProdotto, EsecuzioneJobWood
from blueprints.kanban import sync_auto
from blueprints.kanban.routes import kanban_bp
from masterlogistic_client import MasterLogisticError


class TestKanbanSyncAuto(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(kanban_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.query(KanbanProdotto).delete()
            db.session.query(EsecuzioneJobWood).delete()
            db.session.add_all([
                KanbanProdotto(categoria='Transenne', sheet_key='Transenne', prodotto='T250DT — TRANSENNA DOPPIO TRAVERSO', riservato=0),
                KanbanProdotto(categoria='Transenne', sheet_key='Transenne', prodotto='T200 — TRANSENNA', riservato=160),
            ])
            db.session.commit()

    def test_lock_un_solo_giro_ogni_4_ore(self):
        with self.app.app_context():
            self.assertTrue(sync_auto.prenota_giro())
            self.assertFalse(sync_auto.prenota_giro(), 'secondo worker: niente doppio giro')
            job = db.session.get(EsecuzioneJobWood, sync_auto.NOME_JOB)
            job.ultima_fine = datetime.utcnow()
            db.session.commit()
            self.assertFalse(sync_auto.prenota_giro(), 'giro finito da poco: si aspetta le 4 ore')
            self.assertTrue(sync_auto.prenota_giro(forza=True), 'il pulsante manuale parte se nessun giro è in corso')
            job.ultimo_avvio = datetime.utcnow() - timedelta(hours=5)
            job.ultima_fine = job.ultimo_avvio + timedelta(minutes=2)
            db.session.commit()
            self.assertTrue(sync_auto.prenota_giro())

    def test_giro_aggiorna_riservato_e_lascia_invariato_se_wms_non_risponde(self):
        def finto_wms(sku, sku_grezzo='', timeout=8):
            if sku == 'T200':
                raise MasterLogisticError('timeout')
            return {'riservato_clienti': 40, 'stock_verniciati': 0}
        with self.app.app_context(), \
                mock.patch('blueprints.kanban.routes.ottieni_scheda_kanban', side_effect=finto_wms), \
                mock.patch('blueprints.kanban.routes._aggiorna_grezzi_e_trattamento'), \
                mock.patch('blueprints.kanban.routes._aggiorna_residuo_produzione'), \
                mock.patch('blueprints.kanban.routes._sincronizza_finiti_iw_da_magazzino'):
            self.assertTrue(sync_auto.prenota_giro())
            ok, errori = sync_auto.esegui_giro()
            self.assertEqual((ok, errori), (1, 1))
            prodotti = {p.prodotto.split(' — ')[0]: p for p in KanbanProdotto.query.all()}
            self.assertEqual(prodotti['T250DT'].riservato, 40, 'ora il T250DT compare nel Kanban HPI')
            self.assertEqual(prodotti['T200'].riservato, 160, 'WMS non raggiungibile: valore invariato, mai azzerato')
            st = self.app.test_client().get('/api/kanban-hpi/sync-stato').get_json()
            self.assertFalse(st['in_corso'])
            self.assertIn('1 prodotto aggiornato', st['esito'])


if __name__ == '__main__':
    unittest.main()


class TestKanbanHpiSottoScortaWms(unittest.TestCase):
    """Mauri 20/11/2026: S-14 nella Scheda Kanban è sotto scorta (Saldo C/Scorta
    -158, scorta min. WMS 240) senza impegni clienti, ma in Kanban HPI non
    compariva: la lista guardava solo 'riserva' della scheda (0)."""
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(kanban_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.query(KanbanProdotto).delete()
            db.session.add(KanbanProdotto(categoria='Cavalletti', sheet_key='Cavalletti',
                                          prodotto='S-14 — CAVALLETTO S-14 D.90-TR.120',
                                          riserva=0, riservato=0, finiti_is=82))
            db.session.commit()

    def _riga_s14(self):
        with mock.patch('blueprints.kanban.routes._snapshot_campi_kanban_principale', return_value={}):
            dati = self.app.test_client().get('/api/kanban-hpi/dati').get_json()
        return [p for g in dati for p in g['prodotti'] if p['nome'].startswith('S-14')][0]

    def test_scorta_wms_salvata_dal_riallineamento_e_usata_in_hpi(self):
        with self.app.app_context():
            self.assertFalse(self._riga_s14()['sotto_scorta'], 'prima del riallineamento: nessuna scorta nota')
            p = KanbanProdotto.query.first()
            with mock.patch('blueprints.kanban.routes.ottieni_scheda_kanban',
                            return_value={'riservato_clienti': 0, 'stock_verniciati': 82, 'scorta_minima': 240}), \
                    mock.patch('blueprints.kanban.routes._aggiorna_grezzi_e_trattamento'), \
                    mock.patch('blueprints.kanban.routes._aggiorna_residuo_produzione'), \
                    mock.patch('blueprints.kanban.routes._sincronizza_finiti_iw_da_magazzino'):
                from blueprints.kanban.routes import risincronizza_prodotto_da_wms
                self.assertTrue(risincronizza_prodotto_da_wms(p))
            db.session.commit()
            self.assertEqual(KanbanProdotto.query.first().scorta_minima_wms, 240)
            riga = self._riga_s14()
            self.assertTrue(riga['sotto_scorta'])
            self.assertEqual(riga['scorta_minima'], 240)
            self.assertLess(riga['saldo_c_scorta'], 0)
            self.assertEqual(riga['stato_codice'], 'pianifica_scorta', 'compare con "Pianificare (sotto scorta)"')
