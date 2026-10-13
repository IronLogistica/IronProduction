"""Totem Ufficio Acquisti e Logistica (13/10/2026): pagina e KPI di tutti i reparti."""
import json
import unittest
from datetime import date, datetime, timedelta

from flask import Flask

from models import (db, OrdineProduzione, GiacenzaWood, LavorazioneTerzista, Terzista, OrdineAcquistoWood,
                    EventoConsuntivoPP, KanbanProdotto, KanbanGruppo, GruppoInventarioWood)
from blueprints.totem_acquisti.routes import totem_acquisti_bp
from blueprints.kanban.routes import kanban_bp


class TestTotemAcquisti(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(totem_acquisti_bp)
        cls.app.register_blueprint(kanban_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
            oggi = date.today()
            t = Terzista(nome='T.G.T.')
            db.session.add(t)
            db.session.flush()
            db.session.add_all([
                LavorazioneTerzista(terzista_id=t.id, fase='ZINCATURA', qta=65, ddt_uscita='607',
                                    data_rientro_prev=(oggi - timedelta(days=2)).strftime('%d/%m/%Y'),
                                    stato='ATTESA_RIENTRO', note=json.dumps({'codice': 'T200'})),
                LavorazioneTerzista(terzista_id=t.id, fase='ZINCATURA', qta=10, ddt_uscita='600',
                                    data_rientro_prev=(oggi - timedelta(days=9)).strftime('%d/%m/%Y'),
                                    stato='RIENTRATA', note=json.dumps({'codice': 'T200'})),
                OrdineAcquistoWood(filename='a.pdf', fornitore='Acciai', ordine_n='1',
                                   data_consegna=oggi - timedelta(days=1), stato_label='CONFERMATO'),
                GiacenzaWood(codice='T200', quantita=-65),
                OrdineProduzione(codice='OP-1', codice_articolo='T250', qta_pianificata=10, stato='Rilasciato',
                                 priorita=1, data_prevista=oggi - timedelta(days=1)),
                EventoConsuntivoPP(event_id='e1', op_code='OP-1', fase='Saldatura', timestamp_evento=datetime.utcnow(),
                                   pezzi_buoni=1, pezzi_scarto=0, tempo_minuti=0, approvato_direzione=False),
            ])
            db.session.add_all([
                GruppoInventarioWood(nome='Imballo', frequenza_giorni=21,
                                     ultimo_inventario_il=datetime.utcnow() - timedelta(days=30)),
                GruppoInventarioWood(nome='Vernici Strada', frequenza_giorni=15),
                GruppoInventarioWood(nome='Gas e Filo', frequenza_giorni=8,
                                     ultimo_inventario_il=datetime.utcnow() - timedelta(days=6)),
                GruppoInventarioWood(nome='Senza frequenza'),
            ])
            db.session.commit()

    def test_inventari_kanban_scaduti(self):
        d = self.app.test_client().get('/api/totem-acquisti/kpi').get_json()
        kpi = {k['chiave']: k for k in d['kpi']}
        self.assertEqual(kpi['inventari_scaduti']['valore'], 2)
        self.assertIn('Vernici Strada (mai contato)', kpi['inventari_scaduti']['dettaglio'])
        self.assertIn('Imballo (scaduto da 9 gg)', kpi['inventari_scaduti']['dettaglio'])
        self.assertEqual(kpi['inventari_in_scadenza']['valore'], 1)
        self.assertTrue(any(n['chiave'] == 'inventari_scaduti' for n in d['notifiche']))

    def test_kpi_e_notifiche(self):
        d = self.app.test_client().get('/api/totem-acquisti/kpi').get_json()
        kpi = {k['chiave']: k for k in d['kpi']}
        self.assertEqual(kpi['terzisti_ritardo']['valore'], 1, 'la lavorazione RIENTRATA non conta')
        self.assertEqual(kpi['oa_ritardo']['valore'], 1)
        self.assertEqual(kpi['giacenze_negative']['valore'], 1)
        self.assertEqual(kpi['op_ritardo']['valore'], 1)
        self.assertEqual(kpi['dichiarazioni_da_approvare']['valore'], 1)
        self.assertIn('1 ordine fornitore oltre', kpi['oa_ritardo']['testo'])
        self.assertEqual(d['notifiche'][0]['livello'], 'rosso', 'le urgenze rosse vengono per prime')

    def test_pagina(self):
        r = self.app.test_client().get('/totem/acquisti-logistica')
        self.assertEqual(r.status_code, 200)
        self.assertIn('TOTEM UFFICIO', r.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
