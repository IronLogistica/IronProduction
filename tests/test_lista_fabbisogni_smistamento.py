"""Lista Fabbisogni (stile sottoscorta) e Smistamento merce in arrivo da DDT."""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, OrdineProduzione,
                    GiacenzaWood, ArticoloApprovvigionamento, ScortaMinimaWood,
                    DDTCaricoWood, RigaDDTCaricoWood, OrdineAcquistoWood, RigaOrdineAcquistoWood)
from blueprints.acquisti_wood.routes import acquisti_wood_bp


class TestListaFabbisogniSmistamento(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(acquisti_wood_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (RigaDDTCaricoWood, DDTCaricoWood, RigaOrdineAcquistoWood, OrdineAcquistoWood,
                      CicloLavoroWood, DistintaBaseWood, OrdineProduzione, GiacenzaWood,
                      ArticoloApprovvigionamento, ScortaMinimaWood, CentroCostoWood):
                db.session.query(m).delete()
            saldatura = CentroCostoWood(nome='SALDATURA')
            db.session.add(saldatura)
            db.session.flush()
            db.session.add_all([
                # PF1 = 2 x TUBO ; PF2 = 1 x TUBO
                DistintaBaseWood(codice_padre='PF1', codice_figlio='TUBO', quantita=2),
                DistintaBaseWood(codice_padre='PF2', codice_figlio='TUBO', quantita=1),
                CicloLavoroWood(codice='PF1', sequenza=1, centro_costo_id=saldatura.id),
                CicloLavoroWood(codice='PF2', sequenza=1, centro_costo_id=saldatura.id),
                OrdineProduzione(codice='OP1', codice_articolo='PF1', qta_pianificata=10,
                                 stato='Rilasciato', priorita=1, commessa='C1', cliente='ANAS'),
                OrdineProduzione(codice='OP2', codice_articolo='PF2', qta_pianificata=5,
                                 stato='Rilasciato', priorita=2, commessa='C2', cliente='COMUNE'),
                GiacenzaWood(codice='TUBO', quantita=8),
                ArticoloApprovvigionamento(codice='TUBO', tipo_approvvigionamento='MATERIA_PRIMA_FORNITORE'),
                ArticoloApprovvigionamento(codice='VITE', tipo_approvvigionamento='COMPONENTE_ACQUISTO'),
                ScortaMinimaWood(codice='VITE', scorta_minima=100),
                GiacenzaWood(codice='VITE', quantita=30),
            ])
            ddt = DDTCaricoWood(filename='d.pdf', fornitore='Mericat', numero_ddt='9', data_ddt='26/09/2026')
            db.session.add(ddt)
            db.session.flush()
            db.session.add_all([
                RigaDDTCaricoWood(ddt_id=ddt.id, codice='TUBO', quantita=20, ubicazione_allocata='Z01SC110'),
                RigaDDTCaricoWood(ddt_id=ddt.id, codice='VITE', quantita=50),
            ])
            db.session.commit()
            self.ddt_id = ddt.id
        self.client = self.app.test_client()

    def test_lista_fabbisogni(self):
        righe = {r['codice']: r for r in self.client.get('/api/lista_fabbisogni').get_json()['righe']}
        tubo = righe['TUBO']
        # impegnato: OP1 20 + OP2 5 = 25 ; esistenza 8 → disponibilità -17
        self.assertEqual(tubo['impegnato'], 25)
        self.assertEqual(tubo['disponibilita'], -17)
        self.assertEqual(tubo['da_ordinare'], 17)
        self.assertEqual([i['op_code'] for i in tubo['impegni']], ['OP1', 'OP2'])
        self.assertEqual(tubo['impegni'][0]['mancante'], 12)  # OP1 prende gli 8 in giacenza
        vite = righe['VITE']
        self.assertEqual(vite['sottoscorta'], 70)
        self.assertEqual(vite['impegni'], [])

    def test_smistamento_ddt(self):
        from blueprints.acquisti_wood.routes import calcola_smistamento_ddt
        with self.app.app_context():
            schede = {s['codice']: s for s in calcola_smistamento_ddt(db.session.get(DDTCaricoWood, self.ddt_id))}
        tubo = schede['TUBO']
        # 20 arrivati: 12 a OP1 (priorità 1), 5 a OP2, 3 a magazzino
        self.assertEqual([(p['op_code'], p['quantita']) for p in tubo['produzione']], [('OP1', 12), ('OP2', 5)])
        self.assertEqual(tubo['magazzino'], 3)
        self.assertEqual(tubo['produzione'][0]['destinazioni'][0]['reparto'], 'SALDATURA')
        self.assertEqual(schede['VITE']['produzione'], [])
        self.assertEqual(schede['VITE']['magazzino'], 50)
        pagina = self.client.get(f'/ddt_carico_wood/{self.ddt_id}/smistamento').get_data(as_text=True)
        self.assertIn('SMISTAMENTO MERCE IN ARRIVO', pagina)
        self.assertIn('Z01SC110', pagina)

    def test_smistamento_ddt_confermato_non_conta_se_stesso(self):
        from blueprints.acquisti_wood.routes import calcola_smistamento_ddt
        with self.app.app_context():
            ddt = db.session.get(DDTCaricoWood, self.ddt_id)
            ddt.confermato = True
            db.session.get(GiacenzaWood, 'TUBO').quantita = 28  # 8 + 20 già caricati
            db.session.commit()
            schede = {s['codice']: s for s in calcola_smistamento_ddt(ddt)}
        self.assertEqual([(p['op_code'], p['quantita']) for p in schede['TUBO']['produzione']], [('OP1', 12), ('OP2', 5)])


if __name__ == '__main__':
    unittest.main()
