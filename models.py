import os, json
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

FASI_DISPONIBILI = ["taglio","sgola","piega","saldatura","finitura","verniciatura","collaudo"]
STATI_FASE = ["non_prevista","da_fare","in_corso","completata","esternalizzata"]
STATI_COMMESSA = ["APERTA","IN_PRODUZIONE","IN_ATTESA","IN_SALDATURA","POSTAZIONE","COMPLETATA","SPEDITA","ANNULLATA"]
SEZIONI_MONITOR = ["lavorazione","da_iniziare","in_attesa","in_saldatura","postazione","terminati"]

class Cliente(db.Model):
    __tablename__ = "clienti"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), default="")
    telefono = db.Column(db.String(50), default="")
    note = db.Column(db.Text, default="")
    creato_il = db.Column(db.DateTime, default=datetime.utcnow)

class Prodotto(db.Model):
    __tablename__ = "prodotti"
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(100), nullable=False, unique=True)
    descrizione = db.Column(db.String(300), default="")
    categoria = db.Column(db.String(100), default="")
    fasi_json = db.Column(db.Text, default='["taglio","piega","saldatura","finitura"]')
    note = db.Column(db.Text, default="")
    @property
    def fasi(self):
        try: return json.loads(self.fasi_json or "[]")
        except: return []

class Commessa(db.Model):
    __tablename__ = "commesse"
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(50), nullable=False, unique=True)
    cliente_nome = db.Column(db.String(200), default="")
    data_ordine = db.Column(db.String(20), default="")
    data_consegna = db.Column(db.String(20), default="")
    priorita = db.Column(db.Integer, default=5)
    stato = db.Column(db.String(50), default="APERTA")
    ref_masterlogistic = db.Column(db.String(100), default="")
    note = db.Column(db.Text, default="")
    creato_il = db.Column(db.DateTime, default=datetime.utcnow)
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow)
    righe = db.relationship("RigaCommessa", backref="commessa", lazy=True, cascade="all, delete-orphan")

class RigaCommessa(db.Model):
    __tablename__ = "righe_commessa"
    id = db.Column(db.Integer, primary_key=True)
    commessa_id = db.Column(db.Integer, db.ForeignKey("commesse.id"), nullable=False)
    codice = db.Column(db.String(100), nullable=False)
    descrizione = db.Column(db.String(300), default="")
    qta_totale = db.Column(db.Integer, default=0)
    qta_prodotta = db.Column(db.Integer, default=0)
    note = db.Column(db.Text, default="")
    fasi = db.relationship("FaseRiga", backref="riga", lazy=True, cascade="all, delete-orphan")
    @property
    def percentuale(self):
        return round(self.qta_prodotta/self.qta_totale*100) if self.qta_totale > 0 else 0
    @property
    def saldo(self):
        return max(0, self.qta_totale - self.qta_prodotta)

class FaseRiga(db.Model):
    __tablename__ = "fasi_riga"
    id = db.Column(db.Integer, primary_key=True)
    riga_id = db.Column(db.Integer, db.ForeignKey("righe_commessa.id"), nullable=False)
    fase = db.Column(db.String(50), nullable=False)
    stato = db.Column(db.String(50), default="da_fare")
    assegnato_a = db.Column(db.String(100), default="")
    qta_fatto = db.Column(db.Integer, default=0)
    note = db.Column(db.Text, default="")
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow)

class Terzista(db.Model):
    __tablename__ = "terzisti"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), default="")
    telefono = db.Column(db.String(50), default="")
    tipo = db.Column(db.String(100), default="")
    note = db.Column(db.Text, default="")

class LavorazioneTerzista(db.Model):
    __tablename__ = "lavorazioni_terziste"
    id = db.Column(db.Integer, primary_key=True)
    riga_id = db.Column(db.Integer, db.ForeignKey("righe_commessa.id"), nullable=False)
    terzista_id = db.Column(db.Integer, db.ForeignKey("terzisti.id"), nullable=False)
    fase = db.Column(db.String(50), default="")
    qta = db.Column(db.Integer, default=0)
    data_uscita = db.Column(db.String(20), default="")
    data_rientro_prev = db.Column(db.String(20), default="")
    data_rientro = db.Column(db.String(20), default="")
    stato = db.Column(db.String(50), default="ATTESA_RIENTRO")
    costo = db.Column(db.Float, default=0.0)
    ddt_uscita = db.Column(db.String(100), default="")
    ddt_rientro = db.Column(db.String(100), default="")
    note = db.Column(db.Text, default="")

class MaterialePrimo(db.Model):
    __tablename__ = "materiali_primi"
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(100), nullable=False, unique=True)
    descrizione = db.Column(db.String(300), default="")
    unita_misura = db.Column(db.String(20), default="pz")
    stock = db.Column(db.Float, default=0)
    scorta_min = db.Column(db.Float, default=0)
    fornitore = db.Column(db.String(200), default="")
    note = db.Column(db.Text, default="")
    @property
    def stato(self):
        if self.stock <= 0: return "esaurito"
        if self.stock <= self.scorta_min: return "sottoscorta"
        return "ok"

class MovimentoMagazzino(db.Model):
    __tablename__ = "movimenti_magazzino"
    id = db.Column(db.Integer, primary_key=True)
    materiale_id = db.Column(db.Integer, db.ForeignKey("materiali_primi.id"), nullable=False)
    tipo = db.Column(db.String(20), default="carico")
    qta = db.Column(db.Float, default=0)
    causale = db.Column(db.String(200), default="")
    commessa_ref = db.Column(db.String(100), default="")
    data_mov = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.Text, default="")

class KanbanProdotto(db.Model):
    """Kanban prodotti finiti. I campi sono compatibili col seed e le route."""
    __tablename__ = "kanban_prodotti"
    id = db.Column(db.Integer, primary_key=True)
    categoria = db.Column(db.String(100), nullable=False)
    sheet_key = db.Column(db.String(100), default="")
    icona = db.Column(db.String(10), default="📦")
    prodotto = db.Column(db.String(200), nullable=False)
    # campi numerici — nomi coerenti con seed_db e kanban/routes.py
    lotto = db.Column(db.Integer, default=0)
    riserva = db.Column(db.Integer, default=0)
    riservato = db.Column(db.Integer, default=0)
    grezzi = db.Column(db.Integer, default=0)
    verniciati = db.Column(db.Integer, default=0)
    in_vern = db.Column(db.Integer, default=0)
    in_prod = db.Column(db.Integer, default=0)
    val_medio = db.Column(db.Float, default=0.0)
    lavorazioni = db.Column(db.String(300), default="")
    sort_order = db.Column(db.Integer, default=0)
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow)
    # SKU MasterLogistic: codice articolo verniciato e grezzo per interrogare il WMS
    sku_verniciato = db.Column(db.String(100), default="")
    sku_grezzo     = db.Column(db.String(100), default="")

    @property
    def saldo_contabile(self):
        return (self.verniciati + self.in_vern) - self.riservato

    @property
    def stato(self):
        saldo = self.saldo_contabile
        if self.riserva > 0:
            return "PROGRAMMARE PRODUZIONE" if saldo < self.riserva else "OK"
        return "OK"

    @property
    def val_pv(self):
        return round((self.saldo_contabile + self.in_prod) * self.val_medio, 2)

class KanbanGruppo(db.Model):
    """Gruppi Kanban — sostituisce il dizionario CATEGORIE hard-coded."""
    __tablename__ = "kanban_gruppi"
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(100), nullable=False)
    icona = db.Column(db.String(10), default="📦")
    url_key = db.Column(db.String(120), nullable=False, unique=True)
    sort_order = db.Column(db.Integer, default=0)
    creato_il = db.Column(db.DateTime, default=datetime.utcnow)

class RigaMonitor(db.Model):
    """Monitor segatrice — una riga per commessa/articolo in lavorazione."""
    __tablename__ = "monitor_righe"
    id = db.Column(db.Integer, primary_key=True)
    sezione = db.Column(db.String(50), default="lavorazione")
    comm_id = db.Column(db.String(50), default="")
    codice = db.Column(db.String(100), default="")
    descrizione = db.Column(db.String(300), default="")
    totale = db.Column(db.Integer, default=0)
    saldo = db.Column(db.Integer, default=0)
    pct = db.Column(db.Integer, default=0)
    priority = db.Column(db.String(10), default="")
    ordine = db.Column(db.Integer, default=0)
    taglio = db.Column(db.String(10), default="nd")
    sgola = db.Column(db.String(10), default="nd")
    piega = db.Column(db.String(10), default="nd")
    saldatura = db.Column(db.String(10), default="nd")
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow)

class LogOperazione(db.Model):
    __tablename__ = "log_operazioni"
    id = db.Column(db.Integer, primary_key=True)
    testo = db.Column(db.Text, nullable=False)
    utente = db.Column(db.String(100), default="sistema")
    creato_il = db.Column(db.DateTime, default=datetime.utcnow)

# ── ALIAS per compatibilità backward ────────────────────────────────────────
MonitorRiga = RigaMonitor  # vecchio nome usato in alcuni file

def log(testo, utente="sistema"):
    try:
        db.session.add(LogOperazione(testo=testo, utente=utente))
    except Exception:
        pass

def kanban_to_dict(p):
    saldo = p.saldo_contabile
    return {
        'id': p.id, 'prodotto': p.prodotto, 'categoria': p.categoria,
        'sheet_key': p.sheet_key, 'icona': p.icona,
        'lotto': p.lotto, 'riserva': p.riserva, 'riservato': p.riservato,
        'grezzi': p.grezzi, 'verniciati': p.verniciati, 'in_vern': p.in_vern,
        'in_prod': p.in_prod, 'val_medio': p.val_medio,
        'saldo': saldo, 'stato': p.stato, 'val_pv': p.val_pv,
        'lavorazioni': p.lavorazioni or ''
    }

def commessa_to_dict(c):
    righe = []
    for r in c.righe:
        fasi_dict = {f.fase: {"stato": f.stato, "id": f.id,
                               "assegnato_a": f.assegnato_a, "qta_fatto": f.qta_fatto} for f in r.fasi}
        righe.append({"id": r.id, "codice": r.codice, "descrizione": r.descrizione,
                      "qta_totale": r.qta_totale, "qta_prodotta": r.qta_prodotta,
                      "saldo": r.saldo, "percentuale": r.percentuale, "note": r.note, "fasi": fasi_dict})
    return {"id": c.id, "numero": c.numero, "cliente_nome": c.cliente_nome,
            "data_ordine": c.data_ordine, "data_consegna": c.data_consegna,
            "priorita": c.priorita, "stato": c.stato, "note": c.note,
            "ref_ml": c.ref_masterlogistic,
            "aggiornato_il": c.aggiornato_il.strftime('%d/%m %H:%M') if c.aggiornato_il else '',
            "righe": righe}

def calcola_kpi():
    oggi = date.today()
    commesse_attive = Commessa.query.filter(Commessa.stato.notin_(['ANNULLATA','SPEDITA'])).all()
    urgenti = sum(1 for c in commesse_attive if c.priorita == 1)
    in_ritardo = 0
    for c in commesse_attive:
        if c.data_consegna:
            try:
                from datetime import datetime as dt
                if dt.strptime(c.data_consegna, '%d/%m/%Y').date() < oggi: in_ritardo += 1
            except: pass
    fasi_count = {}
    for c in commesse_attive:
        for r in c.righe:
            for f in r.fasi:
                if f.stato == 'in_corso':
                    fasi_count[f.fase] = fasi_count.get(f.fase, 0) + 1
    from datetime import datetime as dt2
    da = dt2(oggi.year, oggi.month, 1)
    completate_mese = Commessa.query.filter(
        Commessa.stato.in_(["COMPLETATA","SPEDITA"]),
        Commessa.aggiornato_il >= da
    ).count()
    # kanban stats
    tutti_kb = KanbanProdotto.query.all()
    tot_prodotti = len(tutti_kb)
    da_programmare = sum(1 for p in tutti_kb if p.stato == 'PROGRAMMARE PRODUZIONE')
    valore_magazzino = sum(p.val_pv for p in tutti_kb)
    return {
        'commesse_aperte': len(commesse_attive),
        'urgenti': urgenti,
        'in_ritardo': in_ritardo,
        'completate_mese': completate_mese,
        'saturazione': fasi_count,
        'tot_prodotti_kanban': tot_prodotti,
        'da_programmare': da_programmare,
        'scorte_ok': tot_prodotti - da_programmare,
        'valore_magazzino': round(valore_magazzino, 2),
        'commesse_segatrice': RigaMonitor.query.count(),
        'in_lavorazione': RigaMonitor.query.filter_by(sezione='lavorazione').count(),
        'in_saldatura_mon': RigaMonitor.query.filter_by(sezione='in_saldatura').count(),
    }

GRUPPI_DEFAULT = [
    {'label':'Cavalletti','icona':'🏗️','url_key':'1_Cavalletti','sort_order':1},
    {'label':'Transenne','icona':'🚧','url_key':'2_Transenne','sort_order':2},
    {'label':'Archetti','icona':'🔲','url_key':'3_Archetti','sort_order':3},
    {'label':'Paletti ⌀48','icona':'📍','url_key':'4_Paletti_48','sort_order':4},
    {'label':'Paletti ⌀60','icona':'📌','url_key':'5_Paletti_60','sort_order':5},
    {'label':'Paletti Vari','icona':'🗂️','url_key':'6_Paletti_Vari','sort_order':6},
    {'label':'Parapetti','icona':'🛡️','url_key':'7_Parapetti','sort_order':7},
    {'label':'Rastrelliere','icona':'🚲','url_key':'8_Rastrelliere','sort_order':8},
    {'label':'Tubi Scanalati','icona':'🔩','url_key':'9_Tubi_Scanalati','sort_order':9},
    {'label':'Staffe e NJ','icona':'🔧','url_key':'10_Staffe_e_NJ','sort_order':10},
    {'label':'Barriere','icona':'🚦','url_key':'11_Barriere','sort_order':11},
    {'label':'Varie Altre','icona':'📦','url_key':'12_Varie_Altre_Produzioni','sort_order':12},
    {'label':'Pannelli','icona':'🪟','url_key':'Pannelli_Transenne','sort_order':13},
]

def get_kanban_gruppi():
    """Ritorna tutti i gruppi ordinati, con n_prodotti calcolato."""
    gruppi = KanbanGruppo.query.order_by(KanbanGruppo.sort_order, KanbanGruppo.label).all()
    result = []
    for g in gruppi:
        # cerca sheet_key sia come url_key che come label originale
        url_k = g.url_key
        sheet_k = url_k.replace('_', ' ')  # es. 1_Cavalletti → 1 Cavalletti
        n = KanbanProdotto.query.filter(
            db.or_(KanbanProdotto.sheet_key == sheet_k, KanbanProdotto.sheet_key == url_k)
        ).count()
        result.append({'id': g.id, 'label': g.label, 'icona': g.icona,
                       'url_key': g.url_key, 'n_prodotti': n})
    return result

def init_db():
    """Crea tabelle e applica migration sicure."""
    migs_pg = [
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS lotto INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS riserva INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS riservato INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS grezzi INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS verniciati INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS in_vern INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS in_prod INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS val_medio FLOAT DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS sku_verniciato VARCHAR(100) DEFAULT ''",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS sku_grezzo VARCHAR(100) DEFAULT ''",
        "ALTER TABLE monitor_righe ADD COLUMN IF NOT EXISTS ordine INTEGER DEFAULT 0",
        "ALTER TABLE commesse ADD COLUMN IF NOT EXISTS ref_masterlogistic VARCHAR(100) DEFAULT ''",
        "ALTER TABLE lavorazioni_terziste ADD COLUMN IF NOT EXISTS costo FLOAT DEFAULT 0",
    ]
    db_url = os.environ.get('DATABASE_URL', '')
    if 'postgresql' in db_url or 'postgres' in db_url:
        for sql in migs_pg:
            try: db.session.execute(db.text(sql))
            except: db.session.rollback()
        try: db.session.commit()
        except: db.session.rollback()
    # Seed gruppi default se tabella vuota
    try:
        if KanbanGruppo.query.count() == 0:
            for g in GRUPPI_DEFAULT:
                db.session.add(KanbanGruppo(**g))
            db.session.commit()
    except Exception:
        db.session.rollback()
