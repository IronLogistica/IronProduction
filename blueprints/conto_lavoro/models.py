"""
Modelli del modulo CONTO LAVORO — bind 'conto_lavoro', schema Postgres
'conto_lavoro' (vedi config.py: schema_translate_map). Rispecchiano
ESATTAMENTE sql/0001_up.sql: le tabelle le crea SOLO quello script
(python -m blueprints.conto_lavoro.migra), mai db.create_all() all'avvio.

cl_movimento e cl_audit sono SOLO IN INSERIMENTO: il database rifiuta
UPDATE/DELETE con un trigger — il codice non deve nemmeno provarci.
"""
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB

from models import db

BIND = 'conto_lavoro'
RUOLI = ('MAGAZZINO', 'CAPO_REPARTO', 'AMMINISTRAZIONE', 'DIREZIONE', 'LETTURA')
UDM = ('PZ', 'KG', 'M', 'KIT')
TRACCIAMENTI = ('LOTTO', 'MATRICOLA')
STATI_DDT_IN = ('BOZZA', 'VERIFICATO', 'CONFERMATO', 'ANNULLATO')
STATI_DDT_OUT = ('BOZZA', 'EMESSO', 'ANNULLATO')
CAUSALI_POSITIVE = ('CARICO_DDT', 'PRODUZIONE', 'SCARTO')
CAUSALI_NEGATIVE = ('CONSUMO', 'RESO')
CAUSALI_LIBERE = ('STORNO', 'RETTIFICA')
CAUSALI = CAUSALI_POSITIVE + CAUSALI_NEGATIVE + CAUSALI_LIBERE


def _adesso():
    return datetime.now(timezone.utc)


class ClSchemaVersion(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_schema_version'
    versione = db.Column(db.Integer, primary_key=True, autoincrement=False)
    descrizione = db.Column(db.String(200), nullable=False)
    applicata_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)


class ClUtente(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_utente'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), nullable=False, unique=True)
    nome = db.Column(db.String(120), nullable=False, default='')
    password_hash = db.Column(db.String(255), nullable=False)
    attivo = db.Column(db.Boolean, nullable=False, default=True)
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)


class ClUtenteRuolo(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_utente_ruolo'
    utente_id = db.Column(db.Integer, db.ForeignKey('cl_utente.id', ondelete='CASCADE'), primary_key=True)
    ruolo = db.Column(db.String(30), primary_key=True)
    __table_args__ = (db.CheckConstraint(f"ruolo IN {RUOLI}", name='ck_cl_utente_ruolo'),)


class ClCliente(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_cliente'
    id = db.Column(db.Integer, primary_key=True)
    ragione_sociale = db.Column(db.String(200), nullable=False)
    piva = db.Column(db.String(20))
    codice_fiscale = db.Column(db.String(20))
    indirizzo = db.Column(db.String(200), nullable=False, default='')
    cap = db.Column(db.String(10), nullable=False, default='')
    citta = db.Column(db.String(100), nullable=False, default='')
    provincia = db.Column(db.String(5), nullable=False, default='')
    nazione = db.Column(db.String(2), nullable=False, default='IT')
    attivo = db.Column(db.Boolean, nullable=False, default=True)
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)


class ClArticoloCliente(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_articolo_cliente'
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cl_cliente.id'), nullable=False)
    codice = db.Column(db.String(100), nullable=False)
    descrizione = db.Column(db.String(300), nullable=False, default='')
    udm = db.Column(db.String(10), nullable=False)
    tracciamento = db.Column(db.String(10), nullable=False, default='LOTTO')
    attivo = db.Column(db.Boolean, nullable=False, default=True)
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    __table_args__ = (
        db.UniqueConstraint('cliente_id', 'codice', name='uq_cl_articolo_cliente_codice'),
        db.UniqueConstraint('id', 'cliente_id', name='uq_cl_articolo_cliente_id_cliente'),
        db.CheckConstraint(f"udm IN {UDM}", name='ck_cl_articolo_udm'),
        db.CheckConstraint(f"tracciamento IN {TRACCIAMENTI}", name='ck_cl_articolo_tracciamento'),
    )
    cliente = db.relationship('ClCliente')


class ClFotoArticolo(db.Model):
    """Foto di riferimento (com'è fatto il pezzo) per un codice articolo
    cliente — stesso pattern di FotoArticolo in IronProduction: base64 nel
    DB, niente filesystem separato da gestire/perdere ai redeploy. Una sola
    foto corrente per articolo (un nuovo caricamento sostituisce la
    precedente); usata dal riquadro immagine del monitor LIVE Conto lavoro."""
    __bind_key__ = BIND
    __tablename__ = 'cl_foto_articolo'
    id = db.Column(db.Integer, primary_key=True)
    articolo_id = db.Column(db.Integer, db.ForeignKey('cl_articolo_cliente.id', ondelete='CASCADE'), nullable=False)
    nome_file = db.Column(db.String(255), nullable=False)
    tipo_mime = db.Column(db.String(100), nullable=False, default='application/octet-stream')
    contenuto_base64 = db.Column(db.Text, nullable=False)
    caricato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    __table_args__ = (db.UniqueConstraint('articolo_id', name='uq_cl_foto_articolo_articolo'),)
    articolo = db.relationship('ClArticoloCliente')


class ClOrdine(db.Model):
    """Ordine del cliente (import PDF): è l'origine del 'codice di magazzino'
    (ClArticoloCliente) — un codice dell'ordine che non esiste ancora per
    quel cliente viene creato automaticamente all'importazione."""
    __bind_key__ = BIND
    __tablename__ = 'cl_ordine'
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cl_cliente.id'), nullable=False)
    numero_ordine = db.Column(db.String(50), nullable=False)
    rif_cliente = db.Column(db.String(50), nullable=False, default='')
    data_documento = db.Column(db.Date)
    stato = db.Column(db.String(20), nullable=False, default='BOZZA')
    filename = db.Column(db.String(255), nullable=False, default='')
    testo_grezzo_pdf = db.Column(db.Text, nullable=False, default='')
    note = db.Column(db.Text, nullable=False, default='')
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    confermato_il = db.Column(db.DateTime(timezone=True))
    __table_args__ = (
        db.UniqueConstraint('cliente_id', 'numero_ordine', name='uq_cl_ordine_cliente_numero'),
        db.CheckConstraint("stato IN ('BOZZA','CONFERMATO','ANNULLATO')", name='ck_cl_ordine_stato'),
    )
    cliente = db.relationship('ClCliente')
    righe = db.relationship('ClOrdineRiga', back_populates='ordine', order_by='ClOrdineRiga.n_riga',
                            cascade='all, delete-orphan')


class ClOrdineRiga(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_ordine_riga'
    id = db.Column(db.Integer, primary_key=True)
    ordine_id = db.Column(db.Integer, db.ForeignKey('cl_ordine.id', ondelete='CASCADE'), nullable=False)
    n_riga = db.Column(db.Integer, nullable=False)
    articolo_id = db.Column(db.Integer, db.ForeignKey('cl_articolo_cliente.id'), nullable=False)
    descrizione = db.Column(db.String(300), nullable=False, default='')
    quantita = db.Column(db.Numeric(14, 3), nullable=False)
    prezzo_unitario = db.Column(db.Numeric(14, 4))
    # Dalla 0004 (richiesta di Mauri, 02/10/2026): "Evasa" era sempre 0.0
    # calcolato al volo (il modulo DDT di uscita non esiste ancora) — ora è
    # una colonna vera, correggibile a mano dal capo nel frattempo.
    quantita_evasa = db.Column(db.Numeric(14, 3), nullable=False, default=0)
    # "Prodotta" resta di norma calcolata in sola lettura da
    # OrdineProduzione.qta_buona (vedi testata di routes.py) — se
    # valorizzata, questa colonna la SOVRASCRIVE per questa riga (correzione
    # manuale quando MasterWork non ha ancora dichiarato o la dichiarazione
    # non corrisponde). NULL = nessuna correzione, resta il calcolo automatico.
    quantita_prodotta_manuale = db.Column(db.Numeric(14, 3), nullable=True)
    __table_args__ = (
        db.UniqueConstraint('ordine_id', 'n_riga', name='uq_cl_ordine_riga'),
        db.CheckConstraint('n_riga > 0', name='ck_cl_ordine_riga_n'),
        db.CheckConstraint('quantita >= 0', name='ck_cl_ordine_riga_qta'),
        db.CheckConstraint('quantita_evasa >= 0', name='ck_cl_ordine_riga_evasa'),
        db.CheckConstraint('quantita_prodotta_manuale IS NULL OR quantita_prodotta_manuale >= 0',
                           name='ck_cl_ordine_riga_prodotta_manuale'),
    )
    ordine = db.relationship('ClOrdine', back_populates='righe')
    articolo = db.relationship('ClArticoloCliente')


class ClFattura(db.Model):
    """Predisposta dalla 0002 per il Triple Watch — non ancora usata finché
    non viene attivato l'interruttore (cl_impostazione.triple_watch_attivo)."""
    __bind_key__ = BIND
    __tablename__ = 'cl_fattura'
    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cl_cliente.id'), nullable=False)
    numero = db.Column(db.String(50), nullable=False)
    data_documento = db.Column(db.Date)
    importo = db.Column(db.Numeric(14, 2))
    filename = db.Column(db.String(255), nullable=False, default='')
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    __table_args__ = (
        db.UniqueConstraint('cliente_id', 'numero', name='uq_cl_fattura_cliente_numero'),
    )
    cliente = db.relationship('ClCliente')


class ClImpostazione(db.Model):
    """Interruttori/impostazioni del modulo (es. triple_watch_attivo: 'on'/'off')."""
    __bind_key__ = BIND
    __tablename__ = 'cl_impostazione'
    chiave = db.Column(db.String(60), primary_key=True)
    valore = db.Column(db.String(200), nullable=False, default='')
    aggiornato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)


class ClDdt(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_ddt'
    id = db.Column(db.Integer, primary_key=True)
    direzione = db.Column(db.String(3), nullable=False)
    cliente_id = db.Column(db.Integer, db.ForeignKey('cl_cliente.id'), nullable=False)
    stato = db.Column(db.String(20), nullable=False, default='BOZZA')
    causale = db.Column(db.String(100), nullable=False, default='')
    numero_cliente = db.Column(db.String(50))
    data_documento = db.Column(db.Date)
    anno = db.Column(db.Integer)
    numero = db.Column(db.Integer)
    note = db.Column(db.Text, nullable=False, default='')
    creato_da = db.Column(db.Integer, db.ForeignKey('cl_utente.id'))
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    confermato_da = db.Column(db.Integer, db.ForeignKey('cl_utente.id'))
    confermato_il = db.Column(db.DateTime(timezone=True))
    annullato_da = db.Column(db.Integer, db.ForeignKey('cl_utente.id'))
    annullato_il = db.Column(db.DateTime(timezone=True))
    motivo_annullamento = db.Column(db.Text)
    # Predisposti dalla 0002 per il Triple Watch (Ordine <-> DDT uscita <-> Fattura),
    # non ancora popolati/usati finché cl_impostazione.triple_watch_attivo = 'off'.
    ordine_id = db.Column(db.Integer, db.ForeignKey('cl_ordine.id'))
    fattura_id = db.Column(db.Integer, db.ForeignKey('cl_fattura.id'))
    __table_args__ = (
        db.CheckConstraint("direzione IN ('IN','OUT')", name='ck_cl_ddt_direzione'),
        db.CheckConstraint(
            "(direzione = 'IN' AND stato IN ('BOZZA','VERIFICATO','CONFERMATO','ANNULLATO')) OR "
            "(direzione = 'OUT' AND stato IN ('BOZZA','EMESSO','ANNULLATO'))", name='ck_cl_ddt_stato'),
        db.CheckConstraint(
            "direzione = 'IN' OR stato = 'BOZZA' OR (anno IS NOT NULL AND numero IS NOT NULL)",
            name='ck_cl_ddt_numero_out'),
    )
    cliente = db.relationship('ClCliente')
    righe = db.relationship('ClDdtRiga', back_populates='ddt', order_by='ClDdtRiga.n_riga',
                            cascade='all, delete-orphan')


class ClDdtRiga(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_ddt_riga'
    id = db.Column(db.Integer, primary_key=True)
    ddt_id = db.Column(db.Integer, db.ForeignKey('cl_ddt.id', ondelete='CASCADE'), nullable=False)
    n_riga = db.Column(db.Integer, nullable=False)
    articolo_id = db.Column(db.Integer, db.ForeignKey('cl_articolo_cliente.id'), nullable=False)
    descrizione = db.Column(db.String(300), nullable=False, default='')
    qta_dichiarata = db.Column(db.Numeric(14, 3), nullable=False)
    qta_verificata = db.Column(db.Numeric(14, 3))
    cassone = db.Column(db.String(50), nullable=False, default='')
    matricola = db.Column(db.String(100))
    lotto_id = db.Column(db.Integer, db.ForeignKey('cl_lotto.id', use_alter=True, name='fk_cl_ddt_riga_lotto'))
    __table_args__ = (
        db.UniqueConstraint('ddt_id', 'n_riga', name='uq_cl_ddt_riga'),
        db.CheckConstraint('n_riga > 0', name='ck_cl_ddt_riga_n'),
        db.CheckConstraint('qta_dichiarata >= 0', name='ck_cl_ddt_riga_dich'),
        db.CheckConstraint('qta_verificata >= 0', name='ck_cl_ddt_riga_ver'),
    )
    ddt = db.relationship('ClDdt', back_populates='righe')
    articolo = db.relationship('ClArticoloCliente')


class ClLotto(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_lotto'
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(30), nullable=False, unique=True)
    cliente_id = db.Column(db.Integer, nullable=False)
    articolo_id = db.Column(db.Integer, nullable=False)
    ddt_riga_origine_id = db.Column(db.Integer, db.ForeignKey('cl_ddt_riga.id'))
    cassone = db.Column(db.String(50), nullable=False, default='')
    matricola = db.Column(db.String(100))
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    __table_args__ = (
        # il lotto è SEMPRE dello stesso cliente del suo articolo (vincolo nel database)
        db.ForeignKeyConstraint(['articolo_id', 'cliente_id'],
                                ['cl_articolo_cliente.id', 'cl_articolo_cliente.cliente_id'],
                                name='fk_cl_lotto_articolo_stesso_cliente'),
    )
    articolo = db.relationship('ClArticoloCliente', foreign_keys=[articolo_id],
                               primaryjoin='ClLotto.articolo_id == ClArticoloCliente.id', viewonly=True)


class ClMovimento(db.Model):
    """Ledger SOLO INSERT: il saldo di un lotto è la somma delle quantità."""
    __bind_key__ = BIND
    __tablename__ = 'cl_movimento'
    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True)
    lotto_id = db.Column(db.Integer, db.ForeignKey('cl_lotto.id'), nullable=False, index=True)
    causale = db.Column(db.String(20), nullable=False)
    quantita = db.Column(db.Numeric(14, 3), nullable=False)
    ddt_riga_id = db.Column(db.Integer, db.ForeignKey('cl_ddt_riga.id'))
    riferimento = db.Column(db.String(100), nullable=False, default='')
    chiave_idempotenza = db.Column(db.String(120), nullable=False, unique=True)
    utente_id = db.Column(db.Integer, db.ForeignKey('cl_utente.id'))
    note = db.Column(db.String(300), nullable=False, default='')
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    __table_args__ = (
        db.CheckConstraint('quantita <> 0', name='ck_cl_movimento_quantita'),
        db.CheckConstraint(
            f"(causale IN {CAUSALI_POSITIVE} AND quantita > 0) OR "
            f"(causale IN {CAUSALI_NEGATIVE} AND quantita < 0) OR "
            f"(causale IN {CAUSALI_LIBERE})", name='ck_cl_movimento_segno'),
    )


class ClAudit(db.Model):
    """SOLO INSERT."""
    __bind_key__ = BIND
    __tablename__ = 'cl_audit'
    id = db.Column(db.BigInteger().with_variant(db.Integer, 'sqlite'), primary_key=True)
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    utente_id = db.Column(db.Integer, db.ForeignKey('cl_utente.id'))
    azione = db.Column(db.String(60), nullable=False)
    entita = db.Column(db.String(40), nullable=False)
    entita_id = db.Column(db.String(40), nullable=False, default='')
    prima = db.Column(db.JSON().with_variant(JSONB(), 'postgresql'))
    dopo = db.Column(db.JSON().with_variant(JSONB(), 'postgresql'))
    ip = db.Column(db.String(45), nullable=False, default='')


class ClAllegato(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_allegato'
    id = db.Column(db.Integer, primary_key=True)
    entita = db.Column(db.String(40), nullable=False)
    entita_id = db.Column(db.String(40), nullable=False)
    nome_file = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(100), nullable=False)
    dimensione = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.CHAR(64), nullable=False)
    contenuto = db.Column(db.LargeBinary, nullable=False)
    creato_da = db.Column(db.Integer, db.ForeignKey('cl_utente.id'))
    creato_il = db.Column(db.DateTime(timezone=True), nullable=False, default=_adesso)
    __table_args__ = (
        db.UniqueConstraint('entita', 'entita_id', 'sha256', name='uq_cl_allegato'),
        db.CheckConstraint('dimensione > 0 AND dimensione <= 20971520', name='ck_cl_allegato_dim'),
    )


class ClSequenza(db.Model):
    __bind_key__ = BIND
    __tablename__ = 'cl_sequenza'
    nome = db.Column(db.String(30), primary_key=True)
    anno = db.Column(db.Integer, primary_key=True, autoincrement=False)
    ultimo = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.CheckConstraint('ultimo >= 0', name='ck_cl_sequenza_ultimo'),)
