"""
Regressione (Mauri, 27/11/2026 — T200DT OP-2026-000109, commessa 26110109): al Curvatubi la riga TR2001
non compariva in Dichiarazione Produzione perche' 300 TR2001 curvati fermi in WIP Station (impegnati da
altre commesse) venivano contati per intero anche per questo OP. L'Ordine di Lavoro diceva giustamente
"Da produrre 120". Ora i pezzi WIP valgono per un OP solo fino alla giacenza che gli spetta per priorita'.
"""
import unittest
from datetime import datetime

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP, WipFaseWood)
from blueprints.produzione_pp.routes import pp_bp


class TestWipRipartitoPerPriorita(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(pp_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (EventoConsuntivoPP, WipFaseWood, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            cur, tra = CentroCostoWood(nome='Curvatubi'), CentroCostoWood(nome='Trapani')
            db.session.add_all([cur, tra])
            db.session.flush()
            self.cur_id = cur.id
            db.session.add_all([
                DistintaBaseWood(codice_padre='T200DT', codice_figlio='TR2001', quantita=1.0),
                CicloLavoroWood(codice='TR2001', sequenza=1, centro_costo_id=cur.id),
                CicloLavoroWood(codice='TR2001', sequenza=2, centro_costo_id=tra.id),
                GiacenzaWood(codice='TR2001', quantita=300),
                WipFaseWood(codice='TR2001', centro_costo_id=cur.id, quantita=300),   # curvati, da forare
                OrdineProduzione(codice='OP-109', codice_articolo='T200DT', commessa='26110109',
                                 qta_pianificata=120, qta_buona=0, stato='Rilasciato', priorita=5,
                                 data_rilascio=datetime(2026, 11, 20)),
            ])
            db.session.commit()

    def _saldo(self, op):
        dati = self.app.test_client().get(f'/api/dichiarazione-produzione/{self.cur_id}/op-aperti').get_json()
        for g in dati or []:
            if g['codice'] == op:
                for c in g['componenti']:
                    if c['codice_lavorato'] == 'TR2001':
                        return c['saldo']
        return 0

    def test_wip_impegnato_da_un_op_prioritario_non_chiude_il_curvatubi(self):
        with self.app.app_context():
            db.session.add(OrdineProduzione(codice='OP-100', codice_articolo='T200DT', commessa='26110100',
                                            qta_pianificata=300, qta_buona=0, stato='Rilasciato', priorita=1,
                                            data_rilascio=datetime(2026, 11, 10)))   # rilasciato prima: impegna per primo
            db.session.commit()
            self.assertEqual(self._saldo('OP-109'), 120, 'prima della fix: riga assente (WIP 300 contato per intero)')
            self.assertEqual(self._saldo('OP-100'), 0, "i 300 curvati coprono l'OP prioritario")

    def test_wip_libero_copre_ancora_l_op(self):
        with self.app.app_context():
            self.assertEqual(self._saldo('OP-109'), 0, 'i 300 curvati sono liberi: nulla da curvare')


if __name__ == '__main__':
    unittest.main()


class TestAvanzamentoCommesseWipEMateriale(TestWipRipartitoPerPriorita):
    """Stessa regola nella tabella Avanzamento Commesse (Cruscotto) + colonna Materiale a 3 stati."""

    def _riga(self, op):
        from blueprints.produzione_pp.avanzamento import calcola_avanzamento_commesse
        righe, _segmenti = calcola_avanzamento_commesse()
        return next(r for r in righe if r['op_codice'] == op)

    def test_curvatura_al_100_solo_per_chi_ha_davvero_i_pezzi(self):
        with self.app.app_context():
            db.session.add(OrdineProduzione(codice='OP-100', codice_articolo='T200DT', commessa='26110100',
                                            qta_pianificata=300, qta_buona=0, stato='Rilasciato', priorita=1,
                                            data_rilascio=datetime(2026, 11, 10)))
            db.session.commit()
            self.assertEqual(self._riga('OP-100')['zone']['curvatura'], 100)   # i 300 curvati sono suoi
            self.assertEqual(self._riga('OP-109')['zone']['curvatura'], 0, 'prima della fix: 100% come OP-100')

    def test_materiale_ordinato_o_da_ordinare(self):
        from models import OrdineAcquistoWood, RigaOrdineAcquistoWood
        with self.app.app_context():
            db.session.add(DistintaBaseWood(codice_padre='TR2001', codice_figlio='TPD32L3440', quantita=1.0))
            db.session.add(CicloLavoroWood(codice='T200DT', sequenza=1, centro_costo_id=self.cur_id))
            GiacenzaWood.query.filter_by(codice='TR2001').delete()
            WipFaseWood.query.delete()
            db.session.commit()
            self.assertEqual(self._riga('OP-109')['stato_materiale'], 'da_ordinare')
            oa = OrdineAcquistoWood(ordine_n='99', fornitore='Tubi Srl', stato_label='ORDINE_INVIATO', filename='oa99.pdf')
            db.session.add(oa)
            db.session.flush()
            db.session.add(RigaOrdineAcquistoWood(ordine_id=oa.id, codice='TPD32L3440', qta_originale=120, qta_ricevuta=0))
            db.session.commit()
            self.assertEqual(self._riga('OP-109')['stato_materiale'], 'ordinato')
