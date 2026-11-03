"""
MASTERPLAN · AMPS — tabelle del piano previsionale (MPS + MRP).

Tutte con prefisso mp_ e nel database locale di IronProduction (bind di default,
create_all le crea all'avvio): il modulo LEGGE distinte, cicli, giacenze e ordini
esistenti ma scrive SOLO qui dentro — non tocca nessuna tabella operativa.
"""
from datetime import datetime

from models import db

LIVELLI = ('ARTICOLO', 'FAMIGLIA')
AFFIDABILITA = ('CERTA', 'PROBABILE', 'POSSIBILE')
STATI_PREVISIONE = ('BOZZA', 'APPROVATA')
ORIGINI_PREVISIONE = ('MANUALE', 'STORICO', 'IMPORT')


class MpImpostazione(db.Model):
    """Parametri di AMPS (chiave/valore): i valori di partenza stanno in impostazioni.py."""
    __tablename__ = 'mp_impostazioni'
    chiave = db.Column(db.String(60), primary_key=True)
    valore = db.Column(db.String(60), nullable=False)


class MpFamiglia(db.Model):
    """Famiglia di pianificazione: raggruppa prodotti finiti per prevedere senza conoscere i codici esatti."""
    __tablename__ = 'mp_famiglie'
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(60), nullable=False, unique=True)
    descrizione = db.Column(db.String(200), default='')
    origine = db.Column(db.String(20), default='MANUALE')      # CATEGORIA (proposta dal campo categoria) | MANUALE
    creato_il = db.Column(db.DateTime, default=datetime.utcnow)
    codici = db.relationship('MpFamigliaCodice', backref='famiglia', cascade='all, delete-orphan',
                             order_by='MpFamigliaCodice.codice')


class MpFamigliaCodice(db.Model):
    """Un prodotto finito sta in UNA sola famiglia (codice unico) con la sua quota del mix (%)."""
    __tablename__ = 'mp_famiglia_codici'
    id = db.Column(db.Integer, primary_key=True)
    famiglia_id = db.Column(db.Integer, db.ForeignKey('mp_famiglie.id'), nullable=False, index=True)
    codice = db.Column(db.String(100), nullable=False, unique=True)
    mix_pct = db.Column(db.Float, nullable=False, default=0.0)


class MpPrevisione(db.Model):
    """
    Una cella della griglia previsioni: (articolo o famiglia, cliente, affidabilità, mese) -> quantità.
    Solo le celle APPROVATA entrano nel calcolo; ogni modifica riporta la cella a BOZZA.
    """
    __tablename__ = 'mp_previsioni'
    id = db.Column(db.Integer, primary_key=True)
    livello = db.Column(db.String(10), nullable=False)               # ARTICOLO | FAMIGLIA
    codice = db.Column(db.String(100), nullable=True)                # solo livello ARTICOLO
    famiglia_id = db.Column(db.Integer, db.ForeignKey('mp_famiglie.id'), nullable=True, index=True)   # solo livello FAMIGLIA
    chiave_rif = db.Column(db.String(120), nullable=False)           # 'A:CODICE' oppure 'F:<id>' — serve all'unicità
    cliente = db.Column(db.String(200), nullable=False, default='')
    affidabilita = db.Column(db.String(10), nullable=False, default='CERTA')
    mese = db.Column(db.Date, nullable=False, index=True)            # primo giorno del mese
    quantita = db.Column(db.Integer, nullable=False, default=0)
    origine = db.Column(db.String(10), nullable=False, default='MANUALE')
    stato = db.Column(db.String(10), nullable=False, default='BOZZA')
    creato_il = db.Column(db.DateTime, default=datetime.utcnow)
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    famiglia = db.relationship('MpFamiglia')
    __table_args__ = (db.UniqueConstraint('chiave_rif', 'cliente', 'affidabilita', 'mese', name='uq_mp_previsione_cella'),)


class MpParametroArticolo(db.Model):
    """Lotto minimo e multiplo d'ordine per codice (usati nelle proposte di acquisto/produzione)."""
    __tablename__ = 'mp_parametri_articolo'
    codice = db.Column(db.String(100), primary_key=True)
    lotto_minimo = db.Column(db.Float, nullable=False, default=0)
    multiplo = db.Column(db.Float, nullable=False, default=0)
