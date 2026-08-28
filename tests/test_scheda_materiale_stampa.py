"""Test del flusso etichette multi-fase della produzione."""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood,
                    OrdineProduzione)
from blueprints.produzione_pp.routes import pp_bp


class TestSchedaMaterialeStampa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (CicloLavoroWood, DistintaBaseWood,
                            OrdineProduzione, CentroCostoWood):
                db.session.query(modello).delete()

            taglio = CentroCostoWood(nome='TAGLIO')
            satinatura = CentroCostoWood(nome='SATINATURA')
            assemblaggio = CentroCostoWood(nome='ASSEMBLAGGIO PADRE')
            db.session.add_all([taglio, satinatura, assemblaggio])
            db.session.flush()

            db.session.add_all([
                CicloLavoroWood(codice='PINXTT110', sequenza=1,
                                centro_costo_id=taglio.id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2,
                                centro_costo_id=satinatura.id),
                CicloLavoroWood(codice='PINX110A', sequenza=1,
                                centro_costo_id=assemblaggio.id),
                # La radice dell'OP è volutamente sopra PINX110A: la scheda
                # deve mostrare il padre immediato, non la radice generica.
                DistintaBaseWood(codice_padre='PRODOTTO-FINITO',
                                 codice_figlio='PINX110A'),
                DistintaBaseWood(codice_padre='PINX110A',
                                 codice_figlio='PINXTT110'),
                # Relazioni di altre distinte: non devono contaminare il padre
                # determinato nel contesto dell'OP corrente.
                DistintaBaseWood(codice_padre='ALTRO-PADRE',
                                 codice_figlio='PINXTT110'),
                DistintaBaseWood(codice_padre='PADRE-ESTERNO',
                                 codice_figlio='PRODOTTO-FINITO'),
                OrdineProduzione(codice='OP-TEST',
                                 codice_articolo='PRODOTTO-FINITO'),
            ])
            db.session.commit()
        self.client = self.app.test_client()

    def _get(self, fase=None):
        query = {
            'codice': 'PINXTT110',
            'descrizione': 'Componente test',
            'quantita': '10',
            'op_code': 'OP-TEST',
        }
        if fase is not None:
            query['fase_sequenza'] = fase
        return self.client.get('/scheda-materiale-stampa', query_string=query)

    def test_codice_multifase_richiede_scelta_etichetta(self):
        risposta = self._get()
        testo = risposta.get_data(as_text=True)
        self.assertEqual(risposta.status_code, 200)
        self.assertIn('Quale etichetta vuoi stampare?', testo)
        self.assertIn('TAGLIO', testo)
        self.assertIn('SATINATURA', testo)

    def test_prima_fase_mostra_quella_successiva(self):
        testo = self._get(fase=1).get_data(as_text=True)
        self.assertIn('Lavorazione Eseguita', testo)
        self.assertIn('TAGLIO', testo)
        self.assertIn('SATINATURA', testo)
        self.assertNotIn('ASSEMBLAGGIO PADRE', testo)

    def test_ultima_fase_mostra_padre_immediato_e_sua_prima_lavorazione(self):
        testo = self._get(fase=2).get_data(as_text=True)
        self.assertIn('SATINATURA', testo)
        self.assertIn('CODICE PADRE: PINX110A', testo)
        self.assertIn('ASSEMBLAGGIO PADRE', testo)
        self.assertNotIn('CODICE PADRE: PRODOTTO-FINITO', testo)
        self.assertNotIn('CODICE PADRE: ALTRO-PADRE', testo)

    def test_fase_inesistente_riporta_alla_selezione(self):
        risposta = self._get(fase=999)
        testo = risposta.get_data(as_text=True)
        self.assertEqual(risposta.status_code, 400)
        self.assertIn('La fase richiesta non esiste', testo)
        self.assertIn('TAGLIO', testo)
        self.assertIn('SATINATURA', testo)


if __name__ == '__main__':
    unittest.main()
