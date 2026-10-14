"""
Schemi di taglio e Piano di taglio (richiesta Mauri, 14/10/2026). Caso reale:
barra TTD32SP15L7 da 7.000 per la T250DT.
  A = 1 TPD32L3940 (3.940) + 1 TR2502DT (2.460)    scarto 600
  B = 2 TR2502DT (2.460) + 2 TPD32L780 (780)       scarto 520
50 T250DT = 50 TPD32L3940 + 100 TR2502DT -> 50 barre A + 25 barre B = 75 barre,
50 zampe TPD32L780 in piu' a magazzino.
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood, DistintaBaseWood,
                    EventoConsuntivoPP, AuditPP, SchemaTaglioWood, SchemaTaglioRigaWood, CaricoExtraTaglioWood,
                    MovimentoGiacenzaWood, ParametriLavorazioneWood)
from blueprints.produzione_pp.routes import pp_bp, _lista_lavoro_op
from blueprints.produzione_pp.piano_taglio import schemi_taglio_bp, calcola_piano, schemi_per_materiale

PIN = '1234'


def _schema(nome, righe, barra=7000):
    return {'id': nome, 'nome': nome, 'lunghezza_barra_mm': barra, 'scarto_mm': None,
            'righe': [{'codice': c, 'quantita': q, 'lunghezza_mm': l} for c, q, l in righe]}


A = _schema('A', [('TPD32L3940', 1, 3940), ('TR2502DT', 1, 2460)])
B = _schema('B', [('TR2502DT', 2, 2460), ('TPD32L780', 2, 780)])


class TestOttimizzatore(unittest.TestCase):
    def test_caso_t250dt(self):
        p = calcola_piano({'TPD32L3940': 50, 'TR2502DT': 100}, [A, B])
        self.assertEqual(p['barre_totali'], 75, 'contro 100 tagliando un codice alla volta')
        self.assertEqual({r['nome']: r['barre'] for r in p['righe_piano']}, {'A': 50, 'B': 25})
        self.assertEqual(p['extra'], {'TPD32L780': 50})
        self.assertTrue(p['ottimo'])

    def test_ordine_schemi_indifferente(self):
        self.assertEqual(calcola_piano({'TPD32L3940': 50, 'TR2502DT': 100}, [B, A])['barre_totali'], 75)

    def test_numeri_dispari(self):
        p = calcola_piano({'TPD32L3940': 40, 'TR2502DT': 81}, [A, B])
        self.assertEqual(p['barre_totali'], 61)  # es. 40 A + 21 B oppure 41 A + 20 B
        ecc = sum(q for c, q in p['extra'].items() if c != 'TPD32L780')
        self.assertEqual(ecc, 1, "un solo pezzo richiesto in eccesso, che va a magazzino")

    def test_solo_un_codice(self):
        p = calcola_piano({'TPD32L3940': 10}, [A, B])
        self.assertEqual(p['barre_totali'], 10)
        self.assertEqual(p['extra'], {'TR2502DT': 10})

    def test_codice_senza_schema(self):
        p = calcola_piano({'TR2502DT': 4, 'ALTRO': 3}, [A, B])
        self.assertEqual(p['non_coperti'], ['ALTRO'])
        self.assertEqual(p['barre_totali'], 2)

    def test_nessuno_schema_pertinente(self):
        self.assertIsNone(calcola_piano({'ALTRO': 3}, [A, B]))

    def test_tre_schemi_grandi_numeri(self):
        C = _schema('C', [('TPD32L3940', 1, 3940), ('TPD32L780', 3, 780)])
        p = calcola_piano({'TPD32L3940': 500, 'TR2502DT': 900, 'TPD32L780': 200}, [A, B, C])
        # 500 A coprono i TPD32L3940 e 500 TR2502DT; 200 B coprono 400 TR2502DT e 400 zampe
        self.assertEqual(p['barre_totali'], 700)


class TestPianoNellOrdineDiLavoro(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        cls.app.register_blueprint(schemi_taglio_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (CaricoExtraTaglioWood, SchemaTaglioRigaWood, SchemaTaglioWood, MovimentoGiacenzaWood, AuditPP,
                      EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood, ParametriLavorazioneWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg = CentroCostoWood(nome='Segatrice')
            db.session.add(seg)
            db.session.flush()
            self.seg_id = seg.id
            db.session.add_all([
                DistintaBaseWood(codice_padre='T250DT', codice_figlio='TR2501', quantita=1.0),
                DistintaBaseWood(codice_padre='T250DT', codice_figlio='TR2502DT', quantita=2.0),
                DistintaBaseWood(codice_padre='TR2501', codice_figlio='TPD32L3940', quantita=1.0),
                DistintaBaseWood(codice_padre='TPD32L3940', codice_figlio='TTD32SP15L7', quantita=3.94),
                DistintaBaseWood(codice_padre='TR2502DT', codice_figlio='TTD32SP15L7', quantita=2.46),
                CicloLavoroWood(codice='TPD32L3940', sequenza=1, centro_costo_id=seg.id),
                CicloLavoroWood(codice='TR2502DT', sequenza=1, centro_costo_id=seg.id),
                ParametriLavorazioneWood(codice='TPD32L3940', lunghezza_barra_mm=7000, pezzi_per_barra=1, sviluppo='SVIL.3.940'),
                ParametriLavorazioneWood(codice='TR2502DT', lunghezza_barra_mm=7000, pezzi_per_barra=2, sviluppo='L.2.460'),
                GiacenzaWood(codice='TTD32SP15L7', quantita=1000),
                OrdineProduzione(codice='OP-2026-000090', codice_articolo='T250DT', commessa='26100090',
                                 qta_pianificata=50, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()
        self.c = self.app.test_client()
        for nome, righe in (('A', [('TPD32L3940', 1, 3940), ('TR2502DT', 1, 2460)]),
                            ('B', [('TR2502DT', 2, 2460), ('TPD32L780', 2, 780)])):
            r = self.c.post('/api/schemi-taglio', json={'materiale': 'ttd32sp15l7', 'nome': nome, 'lunghezza_barra_mm': 7000,
                                                        'righe': [{'codice': c, 'quantita': q, 'lunghezza_mm': l}
                                                                  for c, q, l in righe]})
            self.assertEqual(r.status_code, 201, r.get_json())

    def _ol(self):
        with self.app.app_context():
            o = OrdineProduzione.query.filter_by(codice='OP-2026-000090').one()
            return _lista_lavoro_op(o, db.session.get(CentroCostoWood, self.seg_id), assegna_numero=False)

    def test_validazione_schema(self):
        r = self.c.post('/api/schemi-taglio', json={'materiale': 'X', 'nome': 'Z', 'lunghezza_barra_mm': 7000,
                                                    'righe': [{'codice': 'P', 'quantita': 2, 'lunghezza_mm': 3600}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn('superano la barra', r.get_json()['error'])
        with self.app.app_context():
            self.assertEqual(schemi_per_materiale('TTD32SP15L7')[0]['scarto_mm'], 600)

    def test_ol_segatrice_usa_il_piano(self):
        g = self._ol()['gruppi'][0]
        self.assertEqual(g['materiale'], 'TTD32SP15L7')
        self.assertEqual(g['nr_barre_totale'], 75, 'senza schemi sarebbero 50 + 50 = 100')
        self.assertEqual(g['quantita_necessaria'], 525.0, '75 barre x 7 m')
        self.assertEqual(g['piano_taglio']['extra'], {'TPD32L780': 50})
        self.assertTrue(all(r.get('nel_piano') for r in g['righe']))
        self.assertEqual(g['extra_taglio'], [{'codice': 'TPD32L780', 'previsti': 50, 'caricati': 0, 'da_caricare': 50}])

    def test_pagina_stampa_mostra_piano(self):
        with self.app.test_request_context():
            from flask import render_template
            dati = self._ol()
            with self.app.app_context():
                html = render_template('produzione_pp/lista_lavoro_stampa.html', dati=dati,
                                       centro=db.session.get(CentroCostoWood, self.seg_id))
        self.assertIn('PIANO DI TAGLIO TTD32SP15L7', html)
        self.assertIn('→ MAGAZZINO', html)
        self.assertIn('carica-extra-taglio', html)

    def test_carico_extra_a_magazzino(self):
        url = f'/api/ordini-produzione/OP-2026-000090/lista-lavoro/{self.seg_id}/carica-extra-taglio'
        self.assertEqual(self.c.post(url, json={'codice': 'TPD32L780', 'quantita': 10, 'pin': '0'}).status_code, 403)
        self.assertEqual(self.c.post(url, json={'codice': 'TR2501', 'quantita': 10, 'pin': PIN}).status_code, 400)
        self.assertEqual(self.c.post(url, json={'codice': 'TPD32L780', 'quantita': 51, 'pin': PIN}).status_code, 400)
        self.assertTrue(self.c.post(url, json={'codice': 'tpd32l780', 'quantita': 30, 'pin': PIN}).get_json()['ok'])
        with self.app.app_context():
            self.assertEqual(db.session.get(GiacenzaWood, 'TPD32L780').quantita, 30)
        e = self._ol()['gruppi'][0]['extra_taglio'][0]
        self.assertEqual((e['caricati'], e['da_caricare']), (30, 20))
        self.assertTrue(self.c.post(url, json={'codice': 'TPD32L780', 'quantita': 20, 'pin': PIN}).get_json()['ok'])
        self.assertEqual(self.c.post(url, json={'codice': 'TPD32L780', 'quantita': 1, 'pin': PIN}).status_code, 400)

    def test_extra_previsti_restano_mentre_si_taglia(self):
        """Gli extra si calcolano su tutto l'OL (fatti + da fare): non spariscono
        quando la Segatrice dichiara i pezzi tagliati."""
        with self.app.app_context():
            from blueprints.produzione_pp.piano_taglio import applica_piano_al_gruppo
            o = OrdineProduzione.query.filter_by(codice='OP-2026-000090').one()
            righe = [{'codice': 'TPD32L3940', 'saldo': 0, 'pezzi_fatti': 50, 'nr_pz_da_fare': 50, 'nr_barre': 50},
                     {'codice': 'TR2502DT', 'saldo': 0, 'pezzi_fatti': 100, 'nr_pz_da_fare': 100, 'nr_barre': 50}]
            piano, extra, fuori = applica_piano_al_gruppo(o, self.seg_id, 'TTD32SP15L7', righe)
            self.assertIsNone(piano, 'niente piu da tagliare')
            self.assertEqual(extra[0]['previsti'], 50)

    def test_senza_schemi_ol_invariato(self):
        with self.app.app_context():
            db.session.query(SchemaTaglioRigaWood).delete()
            db.session.query(SchemaTaglioWood).delete()
            db.session.commit()
        g = self._ol()['gruppi'][0]
        self.assertEqual(g['nr_barre_totale'], 100)
        self.assertIsNone(g['piano_taglio'])


if __name__ == '__main__':
    unittest.main()
