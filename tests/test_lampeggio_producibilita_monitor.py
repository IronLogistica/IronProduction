"""
Regressione "lampeggio" Monitor Live (segnalato da Mauri, 05/10/2026,
screenshot "PRODUCTION BUG 2 MONITOR" — commesse 26100069/26100071 ai
Trapani): il lampeggio giallo della riga (classe 'materiale-ok',
templates/monitor/totem_tabella.html e totem_doppio.html) leggeva SOLO
'materiale_disponibile' — il materiale DIRETTO di QUESTO reparto (es. la
barra grezza per forare) — ignorando del tutto se la fase A MONTE
(Segatrice) avesse già prodotto abbastanza. Una riga con materiale diretto
presente ma ancora "0/2 pronti da «Segatrice»" (badge IN ATTESA) lampeggiava
comunque come se fosse pronta a lavorare ORA, fuorviante per l'operaio.

Verifica che il lampeggio ('materiale-ok') segua invece la producibilità
VERA del reparto (stato_producibilita), coerente col badge già mostrato
nella stessa riga.
"""
import unittest

from flask import Flask

from models import db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, DistintaBaseWood, GiacenzaWood
from blueprints.monitor.routes import monitor_bp
from blueprints.produzione_pp.routes import pp_bp


class TestLampeggioProducibilitaMonitor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(monitor_bp)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (DistintaBaseWood, GiacenzaWood, CicloLavoroWood, OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            segatrice = CentroCostoWood(nome='Segatrice')
            trapani = CentroCostoWood(nome='Trapani')
            db.session.add_all([segatrice, trapani])
            db.session.flush()
            self.trapani_id = trapani.id

            # LUC-ARCPAL: ciclo a due fasi, Segatrice poi Trapani — nessuna
            # dichiarazione/WIP ancora alla Segatrice (fase a monte non
            # pronta), ma il materiale DIRETTO per forare (barra grezza) è
            # presente in abbondanza — esattamente il caso dello screenshot:
            # badge IN ATTESA ("0/2 pronti da «Segatrice»"), materiale
            # diretto disponibile.
            db.session.add_all([
                CicloLavoroWood(codice='LUC-ARCPAL', sequenza=1, centro_costo_id=segatrice.id),
                CicloLavoroWood(codice='LUC-ARCPAL', sequenza=2, centro_costo_id=trapani.id),
                DistintaBaseWood(codice_padre='LUC-ARCPAL', codice_figlio='BARRA-GREZZA', quantita=1.0),
                GiacenzaWood(codice='BARRA-GREZZA', quantita=1000),
                OrdineProduzione(codice='26100069', codice_articolo='LUC-ARCPAL',
                                  qta_pianificata=2, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def test_riga_in_attesa_non_lampeggia_pur_con_materiale_diretto_presente(self):
        r = self.client.get(f'/totem/macchina/{self.trapani_id}')
        self.assertEqual(r.status_code, 200)
        t = r.get_data(as_text=True)
        self.assertIn('26100069', t)
        self.assertIn('IN ATTESA', t)
        # La riga della commessa non deve portare la classe che attiva il
        # lampeggio giallo — prima della fix la portava comunque, perché
        # il materiale diretto (barra grezza) era disponibile.
        riga_commessa = t[t.index('<tr', t.index('26100069') - 500):t.index('26100069') + 50]
        self.assertNotIn('materiale-ok', riga_commessa,
                          "la riga lampeggiava come 'pronta' anche con badge IN ATTESA "
                          "(fase a monte — Segatrice — non ancora pronta)")


if __name__ == '__main__':
    unittest.main()
