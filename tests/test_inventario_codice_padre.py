"""
"Inventario Codice Padre" (richiesta Mauri, 05/10/2026): a differenza
dell'Inventario Magazzino mensile (per gruppo merceologico), qui si sceglie
UN codice padre e si inventariano solo i codici della SUA distinta base a
cascata — utile prima di lanciare una commessa su quel codice o subito dopo
averla chiusa. Per i codici con Ciclo di Lavoro a più fasi, l'inventario è
anche per fase/centro di costo (stesso meccanismo di WIP Station).

Scenario reale riusato: PINX110 (finito) -> PINXTT110 (semilavorato, ciclo
Segatrice->Satinatrice, 287 pezzi di cui 96 fermi dopo la Segatrice e 191
già oltre la Satinatrice) -> BARRA-GREZZA (materia prima, mono-fase).
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, DistintaBaseWood, GiacenzaWood,
                     WipFaseWood, DescrizioneCodiceWood, ArticoloApprovvigionamento,
                     MovimentoGiacenzaWood)
from blueprints.magazzino.routes import magazzino_bp


class TestInventarioCodicePadre(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                              SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(magazzino_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)
        cls.client = cls.app.test_client()

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for modello in (MovimentoGiacenzaWood, WipFaseWood, DistintaBaseWood, CicloLavoroWood,
                            GiacenzaWood, DescrizioneCodiceWood, ArticoloApprovvigionamento, CentroCostoWood):
                db.session.query(modello).delete()
            db.session.commit()

            segatrice = CentroCostoWood(nome='Segatrice')
            satinatrice = CentroCostoWood(nome='Satinatrice')
            db.session.add_all([segatrice, satinatrice])
            db.session.flush()
            self.segatrice_id, self.satinatrice_id = segatrice.id, satinatrice.id

            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINXTT110', quantita=1.0),
                DistintaBaseWood(codice_padre='PINXTT110', codice_figlio='BARRA-GREZZA', quantita=1.0),
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.segatrice_id),
                CicloLavoroWood(codice='PINXTT110', sequenza=2, centro_costo_id=self.satinatrice_id),
                GiacenzaWood(codice='PINX110', quantita=0),
                GiacenzaWood(codice='PINXTT110', quantita=287),
                GiacenzaWood(codice='BARRA-GREZZA', quantita=500),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.segatrice_id, quantita=96),
                WipFaseWood(codice='PINXTT110', centro_costo_id=self.satinatrice_id, quantita=191),
                DescrizioneCodiceWood(codice='PINXTT110', descrizione='Profilo semilavorato TT110'),
                DescrizioneCodiceWood(codice='BARRA-GREZZA', descrizione='Barra grezza alluminio'),
                ArticoloApprovvigionamento(codice='BARRA-GREZZA', tipo_approvvigionamento='MATERIA_PRIMA',
                                            unita_misura='n.'),
            ])
            db.session.commit()

    # ------------------------------------------------------------------
    # Esplosione a cascata
    # ------------------------------------------------------------------
    def test_esplode_tutta_la_cascata_padre_compreso(self):
        r = self.client.get('/api/inventario-codice-padre/PINX110')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d['trovato'])
        codici = [riga['codice'] for riga in d['righe']]
        self.assertEqual(codici, ['PINX110', 'PINXTT110', 'BARRA-GREZZA'],
                          "deve comparire il padre, poi a cascata ogni componente, ognuno una sola volta")

    def test_codice_multi_fase_porta_le_sue_fasi_wip(self):
        r = self.client.get('/api/inventario-codice-padre/PINX110')
        righe = {riga['codice']: riga for riga in r.get_json()['righe']}
        pinxtt110 = righe['PINXTT110']
        self.assertTrue(pinxtt110['multi_fase'])
        fasi = {f['centro_nome']: f['quantita_wip_attuale'] for f in pinxtt110['fasi']}
        self.assertEqual(fasi, {'Segatrice': 96, 'Satinatrice': 191})

    def test_codice_mono_fase_non_porta_fasi(self):
        r = self.client.get('/api/inventario-codice-padre/PINX110')
        righe = {riga['codice']: riga for riga in r.get_json()['righe']}
        barra = righe['BARRA-GREZZA']
        self.assertFalse(barra['multi_fase'])
        self.assertEqual(barra['fasi'], [])
        self.assertEqual(barra['unita_misura'], 'n.')
        self.assertEqual(barra['giacenza_attuale'], 500)

    def test_codice_padre_inesistente(self):
        r = self.client.get('/api/inventario-codice-padre/NONESISTE')
        d = r.get_json()
        self.assertFalse(d['trovato'])
        self.assertEqual(d['righe'], [])

    # ------------------------------------------------------------------
    # Foglio stampabile VUOTO
    # ------------------------------------------------------------------
    def test_foglio_stampa_mostra_le_quantita_attuali(self):
        r = self.client.get('/inventario-codice-padre-stampa/PINX110')
        self.assertEqual(r.status_code, 200)
        t = r.get_data(as_text=True)
        self.assertIn('PINXTT110', t)
        self.assertIn('BARRA-GREZZA', t)
        self.assertIn('fermi A «Segatrice»', t)
        self.assertIn('fermi A «Satinatrice»', t)
        # colonna «Attuale» (richiesta Mauri 08/10/2026): giacenza e WIP come nella schermata
        self.assertIn('Attuale', t)
        for q in ('>287<', '>500<', '>96<', '>191<'):
            self.assertIn(q, t.replace(' ', '').replace('\n', ''))

    # ------------------------------------------------------------------
    # Salvataggio conteggi
    # ------------------------------------------------------------------
    def test_salva_rettifica_giacenza_piana_come_movimento_audit(self):
        r = self.client.post('/api/inventario-codice-padre/salva', json={
            'codice_padre': 'PINX110',
            'conteggi': {'BARRA-GREZZA': 480},
            'conteggi_fasi': {},
        })
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['aggiornati'], 1)
        with self.app.app_context():
            g = GiacenzaWood.query.get('BARRA-GREZZA')
            self.assertEqual(g.quantita, 480)
            mov = MovimentoGiacenzaWood.query.filter_by(codice='BARRA-GREZZA').first()
            self.assertIsNotNone(mov, "la rettifica deve lasciare un movimento tracciato, non una sovrascrittura cieca")
            self.assertEqual(mov.tipo, 'rettifica_inventario')
            self.assertEqual(mov.quantita, -20)

    def test_salva_aggiorna_wip_per_fase_del_codice_multi_fase(self):
        # Conteggio fisico reale diverso da quello automatico: 100 fermi
        # dopo la Segatrice, 187 già oltre la Satinatrice (somma 287, invariata).
        r = self.client.post('/api/inventario-codice-padre/salva', json={
            'codice_padre': 'PINX110',
            'conteggi': {},
            'conteggi_fasi': {'PINXTT110': {str(self.segatrice_id): 100, str(self.satinatrice_id): 187}},
        })
        d = r.get_json()
        self.assertTrue(d['ok'])
        self.assertEqual(d['fasi_aggiornate'], 2)
        with self.app.app_context():
            righe = {w.centro_costo_id: w.quantita for w in WipFaseWood.query.filter_by(codice='PINXTT110').all()}
            self.assertEqual(righe[self.segatrice_id], 100)
            self.assertEqual(righe[self.satinatrice_id], 187)

    def test_salva_fase_a_zero_cancella_la_riga_wip(self):
        r = self.client.post('/api/inventario-codice-padre/salva', json={
            'codice_padre': 'PINX110',
            'conteggi': {},
            'conteggi_fasi': {'PINXTT110': {str(self.segatrice_id): 0}},
        })
        self.assertTrue(r.get_json()['ok'])
        with self.app.app_context():
            self.assertIsNone(WipFaseWood.query.filter_by(
                codice='PINXTT110', centro_costo_id=self.segatrice_id).first())
            # la fase Satinatrice, non inviata, resta invariata
            satinatrice = WipFaseWood.query.filter_by(
                codice='PINXTT110', centro_costo_id=self.satinatrice_id).first()
            self.assertEqual(satinatrice.quantita, 191)

    def test_salva_centro_costo_non_del_ciclo_di_questo_codice_viene_ignorato(self):
        """Un centro_costo_id che non è nel Ciclo di Lavoro del codice (es.
        errore di invio dal client) non deve creare righe WIP spurie."""
        centro_estraneo_id = self.segatrice_id + 9999
        r = self.client.post('/api/inventario-codice-padre/salva', json={
            'codice_padre': 'PINX110',
            'conteggi': {},
            'conteggi_fasi': {'PINXTT110': {str(centro_estraneo_id): 50}},
        })
        self.assertTrue(r.get_json()['ok'])
        with self.app.app_context():
            self.assertIsNone(WipFaseWood.query.filter_by(
                codice='PINXTT110', centro_costo_id=centro_estraneo_id).first())

    # ------------------------------------------------------------------
    # Widget di ricerca intelligente (richiesta Mauri, 05/10/2026)
    # ------------------------------------------------------------------
    def test_ricerca_codice_padre_trova_per_codice_o_descrizione(self):
        r = self.client.get('/api/inventario-codice-padre/ricerca?q=PINXTT')
        d = r.get_json()
        self.assertEqual([x['codice'] for x in d], ['PINXTT110'])

        r = self.client.get('/api/inventario-codice-padre/ricerca?q=semilavorato')
        d = r.get_json()
        self.assertEqual([x['codice'] for x in d], ['PINXTT110'])

    def test_ricerca_segnala_chi_ha_davvero_una_distinta_base(self):
        r = self.client.get('/api/inventario-codice-padre/ricerca?q=BARRA')
        d = r.get_json()
        self.assertEqual(len(d), 1)
        self.assertFalse(d[0]['ha_distinta'], "BARRA-GREZZA non ha figli in distinta base")

        r = self.client.get('/api/inventario-codice-padre/ricerca?q=PINXTT')
        self.assertTrue(r.get_json()[0]['ha_distinta'], "PINXTT110 ha BARRA-GREZZA come figlio")

    def test_ricerca_con_query_troppo_corta_non_cerca(self):
        r = self.client.get('/api/inventario-codice-padre/ricerca?q=P')
        self.assertEqual(r.get_json(), [])

    def test_salva_senza_conteggi_ritorna_errore(self):
        r = self.client.post('/api/inventario-codice-padre/salva', json={
            'codice_padre': 'PINX110', 'conteggi': {}, 'conteggi_fasi': {},
        })
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])


if __name__ == '__main__':
    unittest.main()
