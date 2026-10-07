"""
Regressione (segnalata da Mauri, 07/10/2026, commessa 26100072 / PINX110):
l'Ordine di Lavoro stampato (_lista_lavoro_op) e il Monitor Live
(_righe_macchina) davano numeri DIVERSI per la stessa commessa:
  1) OL Segatrice: PINXTT110 'Da produrre 84' (già saldati altrove) mentre
     il Monitor risultava chiuso — all'OL mancava il credito "da valle".
  2) OL Saldatura: 'Prodotti' 130 per PINX110 invece di 84 — all'OL
     mancava il filtro sugli eventi APPROVATI (stand-by MasterWork).
  3) Totem Saldatura: 'Prodotti' 252 su 'Pz Necessari' 287 — la colonna
     sommava le tre fasi, le altre due erano già sui soli finiti.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP)
from blueprints.monitor.routes import _righe_macchina, _raggruppa_per_op
from blueprints.produzione_pp.routes import _lista_lavoro_op

OP = 'OP-2026-000072'


def _ev(n, componente, buoni, approvato=True, fase='Saldatura'):
    return EventoConsuntivoPP(event_id=f'e{n}', op_code=OP, fase=fase, componente=componente,
                              timestamp_evento=datetime.utcnow(), pezzi_buoni=buoni, pezzi_scarto=0,
                              tempo_minuti=0, approvato_direzione=approvato)


class TestListaLavoroAllineataAlMonitor(unittest.TestCase):
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
            for m in (EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            seg, sal = CentroCostoWood(nome='Segatrice'), CentroCostoWood(nome='Saldatura')
            db.session.add_all([seg, sal])
            db.session.flush()
            self.seg_id, self.sal_id = seg.id, sal.id
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-B', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                CicloLavoroWood(codice='PINXTT110', sequenza=1, centro_costo_id=self.seg_id),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=self.sal_id),
                CicloLavoroWood(codice='PINX110-B', sequenza=1, centro_costo_id=self.sal_id),
                CicloLavoroWood(codice='PINX110', sequenza=1, centro_costo_id=self.sal_id),
                GiacenzaWood(codice='PINXTT110', quantita=203),
                OrdineProduzione(codice=OP, codice_articolo='PINX110', qta_pianificata=287, qta_buona=84,
                                 stato='Rilasciato', priorita=1),
                _ev(1, 'PINX110-A', 84), _ev(2, 'PINX110-B', 84), _ev(3, None, 84),
            ])
            db.session.commit()

    def _lista(self, centro_id):
        op = OrdineProduzione.query.filter_by(codice=OP).one()
        return _lista_lavoro_op(op, db.session.get(CentroCostoWood, centro_id), assegna_numero=False)

    def test_ol_segatrice_non_chiede_i_pezzi_gia_saldati(self):
        with self.app.app_context():
            riga = self._lista(self.seg_id)['gruppi'][0]['righe'][0]
            self.assertEqual(riga['codice'], 'PINXTT110')
            self.assertEqual(riga['saldo'], 0, "203 a magazzino + 84 già saldati = 287: niente da tagliare")
            r = next(x for s in _righe_macchina(db.session.get(CentroCostoWood, self.seg_id)).values()
                     for x in s if x['op_codice'] == OP)
            self.assertEqual(riga['saldo'], r['saldo'], "OL e Monitor Live devono coincidere")

    def test_ol_saldatura_ignora_le_dichiarazioni_in_stand_by(self):
        with self.app.app_context():
            db.session.add(_ev(4, None, 46, approvato=False))     # MasterWork, non ancora approvata
            db.session.commit()
            righe = {r['codice']: r for g in self._lista(self.sal_id)['gruppi'] for r in g['righe']}
            self.assertEqual(righe['PINX110']['pezzi_fatti'], 84, "prima della fix: 130")
            self.assertEqual(righe['PINX110']['saldo'], 203)

    def test_totem_saldatura_prodotti_coerenti_con_necessari(self):
        with self.app.app_context():
            from blueprints.monitor.routes import _contesto_totem
            with self.app.test_request_context():
                ctx = _contesto_totem(db.session.get(CentroCostoWood, self.sal_id))
            g = ctx['gruppi'][0]
            self.assertEqual((g['totale_totale'], g['pezzi_fatti_totale'], g['saldo_totale']), (287, 84, 203))
            self.assertEqual(g['pct_aggregato'], 29)


if __name__ == '__main__':
    unittest.main()


class TestDiagnosticaOp(unittest.TestCase):
    def test_endpoint_diagnostico_risponde_e_trova_anomalie(self):
        base = TestListaLavoroAllineataAlMonitor
        base.setUpClass()
        t = base('test_ol_segatrice_non_chiede_i_pezzi_gia_saldati')
        t.setUp()
        from flask import Flask
        from blueprints.produzione_pp.routes import pp_bp
        app = base.app
        app.register_blueprint(pp_bp)
        with app.app_context():
            db.session.add(_ev(9, None, 46, approvato=False))
            db.session.commit()
        r = app.test_client().get('/api/diagnostica/op/' + OP)
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['errori_diagnostica'], [])
        self.assertTrue(any('STAND-BY' in a for a in d['anomalie_trovate']))
        self.assertIn('Saldatura', d['per_reparto'])
        self.assertEqual(app.test_client().get('/api/diagnostica/op/NOPE').status_code, 404)


class TestEliminaMovimentoProtetto(unittest.TestCase):
    """OP-2026-000063: i movimenti nati da dichiarazioni/storni non si cancellano a mano."""

    def test_movimento_storno_e_consuntivo_rifiutati_altri_eliminabili(self):
        from models import MovimentoGiacenzaWood
        app = Flask(__name__, template_folder='../templates')
        app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                          SQLALCHEMY_TRACK_MODIFICATIONS=False, CAPO_PIN='1234')
        db.init_app(app)
        from blueprints.produzione_pp.routes import pp_bp
        app.register_blueprint(pp_bp)
        with app.app_context():
            db.create_all(bind_key=None)
            db.session.add(EventoConsuntivoPP(event_id='x', op_code=OP, fase='Segatrice', componente='A',
                                              timestamp_evento=datetime.utcnow(), pezzi_buoni=1, pezzi_scarto=0,
                                              tempo_minuti=0, approvato_direzione=True))
            ms = [MovimentoGiacenzaWood(codice='A', tipo='rettifica_import', quantita=5, riferimento=OP,
                                        note='STORNO consuntivo x'),
                  MovimentoGiacenzaWood(codice='A', tipo='carico_produzione', quantita=5, riferimento=OP,
                                        note='Consuntivo 5 pz buoni'),
                  MovimentoGiacenzaWood(codice='A', tipo='carico_manuale', quantita=5, note='a mano')]
            db.session.add_all(ms)
            db.session.commit()
            ids = [m.id for m in ms]
            c = app.test_client()
            for i in ids[:2]:
                self.assertEqual(c.post(f'/api/dichiarazione-produzione/movimenti/{i}/elimina',
                                        json={'pin': '1234'}).status_code, 409)
            self.assertEqual(c.post(f'/api/dichiarazione-produzione/movimenti/{ids[2]}/elimina',
                                    json={'pin': '1234'}).status_code, 200)
