import os, json
from datetime import datetime, date, timedelta
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

FASI_DISPONIBILI = ["taglio","sgola","piega","saldatura","finitura","verniciatura","collaudo"]
STATI_FASE       = ["non_prevista","da_fare","in_corso","completata","esternalizzata"]
STATI_COMMESSA   = ["APERTA","IN_PRODUZIONE","IN_ATTESA","IN_SALDATURA","POSTAZIONE","COMPLETATA","SPEDITA","ANNULLATA"]
SEZIONI_MONITOR  = ["lavorazione","da_iniziare","in_attesa","in_saldatura","postazione","terminati"]

PIN_ADMIN = "1234"   # PIN autorizzativo per operazioni protette

class Cliente(db.Model):
    __tablename__ = "clienti"
    id       = db.Column(db.Integer, primary_key=True)
    nome     = db.Column(db.String(200), nullable=False)
    email    = db.Column(db.String(200), default="")
    telefono = db.Column(db.String(50),  default="")
    note     = db.Column(db.Text,        default="")
    creato_il = db.Column(db.DateTime,   default=datetime.utcnow)

class Prodotto(db.Model):
    __tablename__ = "prodotti"
    id          = db.Column(db.Integer, primary_key=True)
    codice      = db.Column(db.String(100), nullable=False, unique=True)
    descrizione = db.Column(db.String(300), default="")
    categoria   = db.Column(db.String(100), default="")
    fasi_json   = db.Column(db.Text, default='["taglio","piega","saldatura","finitura"]')
    note        = db.Column(db.Text, default="")
    @property
    def fasi(self):
        try: return json.loads(self.fasi_json or "[]")
        except: return []

class Commessa(db.Model):
    __tablename__ = "commesse"
    id                  = db.Column(db.Integer, primary_key=True)
    numero              = db.Column(db.String(50),  nullable=False, unique=True)
    cliente_nome        = db.Column(db.String(200), default="")
    data_ordine         = db.Column(db.String(20),  default="")
    data_consegna       = db.Column(db.String(20),  default="")
    priorita            = db.Column(db.Integer,     default=5)
    stato               = db.Column(db.String(50),  default="APERTA")
    ref_masterlogistic  = db.Column(db.String(100), default="")
    note                = db.Column(db.Text,        default="")
    creato_il           = db.Column(db.DateTime,    default=datetime.utcnow)
    aggiornato_il       = db.Column(db.DateTime,    default=datetime.utcnow)
    righe               = db.relationship("RigaCommessa", backref="commessa", lazy=True, cascade="all, delete-orphan")

class RigaCommessa(db.Model):
    __tablename__ = "righe_commessa"
    id          = db.Column(db.Integer, primary_key=True)
    commessa_id = db.Column(db.Integer, db.ForeignKey("commesse.id"), nullable=False)
    codice      = db.Column(db.String(100), nullable=False)
    descrizione = db.Column(db.String(300), default="")
    qta_totale  = db.Column(db.Integer, default=0)
    qta_prodotta= db.Column(db.Integer, default=0)
    note        = db.Column(db.Text, default="")
    fasi        = db.relationship("FaseRiga", backref="riga", lazy=True, cascade="all, delete-orphan")
    @property
    def percentuale(self):
        return round(self.qta_prodotta/self.qta_totale*100) if self.qta_totale > 0 else 0
    @property
    def saldo(self):
        return max(0, self.qta_totale - self.qta_prodotta)

class FaseRiga(db.Model):
    __tablename__ = "fasi_riga"
    id           = db.Column(db.Integer, primary_key=True)
    riga_id      = db.Column(db.Integer, db.ForeignKey("righe_commessa.id"), nullable=False)
    fase         = db.Column(db.String(50),  nullable=False)
    stato        = db.Column(db.String(50),  default="da_fare")
    assegnato_a  = db.Column(db.String(100), default="")
    qta_fatto    = db.Column(db.Integer,     default=0)
    note         = db.Column(db.Text,        default="")
    aggiornato_il= db.Column(db.DateTime,    default=datetime.utcnow)

class Terzista(db.Model):
    __tablename__ = "terzisti"
    id       = db.Column(db.Integer, primary_key=True)
    nome     = db.Column(db.String(200), nullable=False)
    email    = db.Column(db.String(200), default="")
    telefono = db.Column(db.String(50),  default="")
    tipo     = db.Column(db.String(100), default="")
    note     = db.Column(db.Text, default="")

class LavorazioneTerzista(db.Model):
    __tablename__ = "lavorazioni_terziste"
    id               = db.Column(db.Integer, primary_key=True)
    riga_id          = db.Column(db.Integer, db.ForeignKey("righe_commessa.id"), nullable=False)
    terzista_id      = db.Column(db.Integer, db.ForeignKey("terzisti.id"),       nullable=False)
    fase             = db.Column(db.String(50),  default="")
    qta              = db.Column(db.Integer,     default=0)
    data_uscita      = db.Column(db.String(20),  default="")
    data_rientro_prev= db.Column(db.String(20),  default="")
    data_rientro     = db.Column(db.String(20),  default="")
    stato            = db.Column(db.String(50),  default="ATTESA_RIENTRO")
    costo            = db.Column(db.Float,       default=0.0)
    ddt_uscita       = db.Column(db.String(100), default="")
    ddt_rientro      = db.Column(db.String(100), default="")
    note             = db.Column(db.Text,        default="")

class MaterialePrimo(db.Model):
    __tablename__ = "materiali_primi"
    id          = db.Column(db.Integer, primary_key=True)
    codice      = db.Column(db.String(100), nullable=False, unique=True)
    descrizione = db.Column(db.String(300), default="")
    unita_misura= db.Column(db.String(20),  default="pz")
    stock       = db.Column(db.Float,       default=0)
    scorta_min  = db.Column(db.Float,       default=0)
    fornitore   = db.Column(db.String(200), default="")
    note        = db.Column(db.Text,        default="")
    @property
    def stato(self):
        if self.stock <= 0:             return "esaurito"
        if self.stock <= self.scorta_min: return "sottoscorta"
        return "ok"

class MovimentoMagazzino(db.Model):
    __tablename__ = "movimenti_magazzino"
    id           = db.Column(db.Integer, primary_key=True)
    materiale_id = db.Column(db.Integer, db.ForeignKey("materiali_primi.id"), nullable=False)
    tipo         = db.Column(db.String(20),  default="carico")
    qta          = db.Column(db.Float,       default=0)
    causale      = db.Column(db.String(200), default="")
    commessa_ref = db.Column(db.String(100), default="")
    data_mov     = db.Column(db.DateTime,    default=datetime.utcnow)
    note         = db.Column(db.Text,        default="")

# ═══════════════════════════════════════════════════════════════════════════════
# KANBAN PRODOTTO
# ═══════════════════════════════════════════════════════════════════════════════
class KanbanProdotto(db.Model):
    __tablename__ = "kanban_prodotti"
    id           = db.Column(db.Integer, primary_key=True)
    categoria    = db.Column(db.String(100), nullable=False)
    sheet_key    = db.Column(db.String(100), default="")
    icona        = db.Column(db.String(10),  default="📦")
    prodotto     = db.Column(db.String(200), nullable=False)
    lotto        = db.Column(db.Integer, default=0)
    riserva      = db.Column(db.Integer, default=0)
    riservato    = db.Column(db.Integer, default=0)
    grezzi       = db.Column(db.Integer, default=0)
    verniciati   = db.Column(db.Integer, default=0)
    in_vern      = db.Column(db.Integer, default=0)
    in_prod      = db.Column(db.Integer, default=0)
    val_medio    = db.Column(db.Float,   default=0.0)
    lavorazioni  = db.Column(db.String(300), default="")
    sort_order   = db.Column(db.Integer, default=0)
    aggiornato_il= db.Column(db.DateTime, default=datetime.utcnow)
    # ── Parametri Lean ──────────────────────────────────────────────────────
    scorta_sicurezza    = db.Column(db.Float, default=0.15)   # SS — 15% default
    lead_time_giorni    = db.Column(db.Float, default=7.0)    # RT — lead time rimpiazzo
    takt_time_min       = db.Column(db.Float, default=None)   # minuti/pezzo — NULL=non impostato
    domanda_giornaliera = db.Column(db.Float, default=0.0)    # D — aggiornata automaticamente
    n_kanban_suggerito  = db.Column(db.Integer, default=0)    # N calcolato

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
    def n_cicli_chiusi(self):
        try:
            return KanbanCiclo.query.filter(
                KanbanCiclo.kanban_id == self.id,
                KanbanCiclo.data_fine.isnot(None)
            ).count()
        except Exception:
            return 0

    @property
    def n_cicli(self):
        try:
            return KanbanCiclo.query.filter_by(kanban_id=self.id).count()
        except Exception:
            return 0

    @property
    def buffer_pct(self):
        """Percentuale buffer: saldo / riserva × 100. None se riserva=0."""
        if self.riserva <= 0: return None
        return round(self.saldo_contabile / self.riserva * 100, 1)

    @property
    def buffer_colore(self):
        pct = self.buffer_pct
        if pct is None: return 'grigio'
        if pct >= 100:  return 'verde'
        if pct >= 50:   return 'giallo'
        return 'rosso'

    @property
    def val_pv(self):
        return round((self.saldo_contabile + self.in_prod) * self.val_medio, 2)

    def calcola_n_kanban(self):
        """Formula N = D × RT × (1+SS) / C — ritorna float o None se dati mancanti."""
        D  = self.domanda_giornaliera or 0
        RT = self.lead_time_giorni    or 0
        SS = self.scorta_sicurezza    or 0.15
        C  = self.lotto               or 1
        if D <= 0 or RT <= 0: return None
        return (D * RT * (1 + SS)) / C

# ═══════════════════════════════════════════════════════════════════════════════
# KANBAN CICLI — accumulo dati per Takt Time futuro
# ═══════════════════════════════════════════════════════════════════════════════
class KanbanCiclo(db.Model):
    """
    Registra ogni 'giro' di un Kanban: da quando scatta DA_PRODURRE a quando
    torna OK. Fonte dati per calcolo automatico di Takt Time, Lead Time, Domanda.
    """
    __tablename__ = "kanban_cicli"
    id            = db.Column(db.Integer, primary_key=True)
    kanban_id     = db.Column(db.Integer, db.ForeignKey("kanban_prodotti.id", ondelete="CASCADE"))
    prodotto      = db.Column(db.String(200), default="")   # snapshot nome
    data_inizio   = db.Column(db.DateTime, nullable=False)  # quando scatta DA_PRODURRE
    data_fine     = db.Column(db.DateTime, default=None)    # quando torna OK
    lead_time_ore = db.Column(db.Float,    default=None)    # calcolato auto
    qta_prodotta  = db.Column(db.Integer,  default=0)       # lotto al momento del ritorno OK
    note          = db.Column(db.Text,     default="")

    @property
    def aperto(self):
        return self.data_fine is None

# ═══════════════════════════════════════════════════════════════════════════════
# FASI WIP — limiti e parametri per reparto
# ═══════════════════════════════════════════════════════════════════════════════
class FaseWip(db.Model):
    __tablename__ = "fasi_wip"
    id                 = db.Column(db.Integer, primary_key=True)
    fase               = db.Column(db.String(50), nullable=False, unique=True)
    label              = db.Column(db.String(80), default="")
    wip_limit          = db.Column(db.Integer, default=0)   # 0 = illimitato
    limite_giornaliero = db.Column(db.Integer, default=0)   # pezzi/giorno max; 0=nessun limite
    soglia_giallo      = db.Column(db.Integer, default=70)  # % WIP → giallo
    soglia_rosso       = db.Column(db.Integer, default=90)  # % WIP → rosso

FASI_WIP_DEFAULT = [
    {'fase':'taglio',       'label':'Taglio',       'wip_limit':0,'limite_giornaliero':0},
    {'fase':'sgola',        'label':'Sgolatura',    'wip_limit':0,'limite_giornaliero':0},
    {'fase':'piega',        'label':'Piega',        'wip_limit':0,'limite_giornaliero':0},
    {'fase':'saldatura',    'label':'Saldatura',    'wip_limit':0,'limite_giornaliero':0},
    {'fase':'finitura',     'label':'Finitura',     'wip_limit':0,'limite_giornaliero':0},
    {'fase':'verniciatura', 'label':'Verniciatura', 'wip_limit':0,'limite_giornaliero':0},
    {'fase':'collaudo',     'label':'Collaudo',     'wip_limit':0,'limite_giornaliero':0},
]

# ═══════════════════════════════════════════════════════════════════════════════
# KANBAN GRUPPI
# ═══════════════════════════════════════════════════════════════════════════════
class KanbanGruppo(db.Model):
    __tablename__ = "kanban_gruppi"
    id         = db.Column(db.Integer, primary_key=True)
    label      = db.Column(db.String(100), nullable=False)
    icona      = db.Column(db.String(10),  default="📦")
    url_key    = db.Column(db.String(120), nullable=False, unique=True)
    sort_order = db.Column(db.Integer,     default=0)
    creato_il  = db.Column(db.DateTime,    default=datetime.utcnow)

GRUPPI_DEFAULT = [
    {'label':'Cavalletti',    'icona':'🏗️','url_key':'1_Cavalletti',            'sort_order':1},
    {'label':'Transenne',     'icona':'🚧','url_key':'2_Transenne',             'sort_order':2},
    {'label':'Archetti',      'icona':'🔲','url_key':'3_Archetti',              'sort_order':3},
    {'label':'Paletti ⌀48',   'icona':'📍','url_key':'4_Paletti_48',            'sort_order':4},
    {'label':'Paletti ⌀60',   'icona':'📌','url_key':'5_Paletti_60',            'sort_order':5},
    {'label':'Paletti Vari',  'icona':'🗂️','url_key':'6_Paletti_Vari',          'sort_order':6},
    {'label':'Parapetti',     'icona':'🛡️','url_key':'7_Parapetti',             'sort_order':7},
    {'label':'Rastrelliere',  'icona':'🚲','url_key':'8_Rastrelliere',          'sort_order':8},
    {'label':'Tubi Scanalati','icona':'🔩','url_key':'9_Tubi_Scanalati',        'sort_order':9},
    {'label':'Staffe e NJ',   'icona':'🔧','url_key':'10_Staffe_e_NJ',          'sort_order':10},
    {'label':'Barriere',      'icona':'🚦','url_key':'11_Barriere',             'sort_order':11},
    {'label':'Varie Altre',   'icona':'📦','url_key':'12_Varie_Altre_Produzioni','sort_order':12},
    {'label':'Pannelli',      'icona':'🪟','url_key':'Pannelli_Transenne',       'sort_order':13},
]

def get_kanban_gruppi():
    gruppi = KanbanGruppo.query.order_by(KanbanGruppo.sort_order, KanbanGruppo.label).all()
    result = []
    for g in gruppi:
        sheet_k = g.url_key.replace('_', ' ')
        n = KanbanProdotto.query.filter(
            db.or_(KanbanProdotto.sheet_key == sheet_k,
                   KanbanProdotto.sheet_key == g.url_key)
        ).count()
        result.append({'id': g.id, 'label': g.label, 'icona': g.icona,
                       'url_key': g.url_key, 'n_prodotti': n})
    return result

# ═══════════════════════════════════════════════════════════════════════════════
# MONITOR / LOG
# ═══════════════════════════════════════════════════════════════════════════════
class RigaMonitor(db.Model):
    __tablename__ = "monitor_righe"
    id           = db.Column(db.Integer, primary_key=True)
    sezione      = db.Column(db.String(50),  default="lavorazione")
    comm_id      = db.Column(db.String(50),  default="")
    codice       = db.Column(db.String(100), default="")
    descrizione  = db.Column(db.String(300), default="")
    totale       = db.Column(db.Integer,     default=0)
    saldo        = db.Column(db.Integer,     default=0)
    pct          = db.Column(db.Integer,     default=0)
    priority     = db.Column(db.String(10),  default="")
    ordine       = db.Column(db.Integer,     default=0)
    taglio       = db.Column(db.String(10),  default="nd")
    sgola        = db.Column(db.String(10),  default="nd")
    piega        = db.Column(db.String(10),  default="nd")
    saldatura    = db.Column(db.String(10),  default="nd")
    aggiornato_il= db.Column(db.DateTime,    default=datetime.utcnow)

MonitorRiga = RigaMonitor

class LogOperazione(db.Model):
    __tablename__ = "log_operazioni"
    id        = db.Column(db.Integer, primary_key=True)
    testo     = db.Column(db.Text, nullable=False)
    utente    = db.Column(db.String(100), default="sistema")
    creato_il = db.Column(db.DateTime,   default=datetime.utcnow)

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def log(testo, utente="sistema"):
    try:
        db.session.add(LogOperazione(testo=testo, utente=utente))
    except Exception:
        pass

def kanban_to_dict(p):
    saldo = p.saldo_contabile
    n_cicli = KanbanCiclo.query.filter_by(kanban_id=p.id).count()
    n_cicli_chiusi = KanbanCiclo.query.filter(
        KanbanCiclo.kanban_id == p.id,
        KanbanCiclo.data_fine.isnot(None)
    ).count()
    return {
        'id': p.id, 'prodotto': p.prodotto, 'categoria': p.categoria,
        'sheet_key': p.sheet_key, 'icona': p.icona,
        'lotto': p.lotto, 'riserva': p.riserva, 'riservato': p.riservato,
        'grezzi': p.grezzi, 'verniciati': p.verniciati,
        'in_vern': p.in_vern, 'in_prod': p.in_prod, 'val_medio': p.val_medio,
        'saldo': saldo, 'stato': p.stato, 'val_pv': p.val_pv,
        'lavorazioni': p.lavorazioni or '',
        'buffer_pct': p.buffer_pct, 'buffer_colore': p.buffer_colore,
        'scorta_sicurezza': p.scorta_sicurezza,
        'lead_time_giorni': p.lead_time_giorni,
        'takt_time_min': p.takt_time_min,
        'domanda_giornaliera': p.domanda_giornaliera,
        'n_kanban_suggerito': p.n_kanban_suggerito,
        'n_cicli': n_cicli,
        'n_cicli_chiusi': n_cicli_chiusi,
    }

def _aggiorna_domanda_media(p):
    """
    Ricalcola domanda_giornaliera su ultimi 90gg di cicli chiusi.
    Chiama dopo aver chiuso un ciclo, prima del commit.
    """
    try:
        da = datetime.utcnow() - timedelta(days=90)
        cicli = KanbanCiclo.query.filter(
            KanbanCiclo.kanban_id == p.id,
            KanbanCiclo.data_fine.isnot(None),
            KanbanCiclo.data_inizio >= da
        ).all()
        if not cicli:
            return
        tot_qta  = sum(c.qta_prodotta or 0 for c in cicli)
        giorni   = 90.0
        p.domanda_giornaliera = round(tot_qta / giorni, 2)
        # Ricalcola N suggerito
        n = p.calcola_n_kanban()
        if n is not None:
            p.n_kanban_suggerito = max(1, round(n))
    except Exception:
        pass

def registra_ciclo_se_necessario(p, stato_prima, stato_dopo):
    """
    Chiamare dentro api_aggiorna, PRIMA del commit.
    - OK → DA_PRODURRE : apre nuovo ciclo
    - DA_PRODURRE → OK : chiude ciclo aperto
    """
    try:
        ora = datetime.utcnow()
        era_ok         = (stato_prima == 'OK')
        era_da_prod    = ('PROGRAMMARE' in (stato_prima or ''))
        ora_ok         = (stato_dopo  == 'OK')
        ora_da_prod    = ('PROGRAMMARE' in (stato_dopo  or ''))

        if era_ok and ora_da_prod:
            # Apri nuovo ciclo
            db.session.add(KanbanCiclo(
                kanban_id=p.id, prodotto=p.prodotto,
                data_inizio=ora, qta_prodotta=p.lotto
            ))

        elif era_da_prod and ora_ok:
            # Chiudi ciclo aperto
            ciclo = KanbanCiclo.query.filter_by(
                kanban_id=p.id, data_fine=None
            ).order_by(KanbanCiclo.data_inizio.desc()).first()
            if ciclo:
                ciclo.data_fine     = ora
                ciclo.qta_prodotta  = p.lotto
                delta = (ora - ciclo.data_inizio).total_seconds()
                ciclo.lead_time_ore = round(delta / 3600, 2)
                _aggiorna_domanda_media(p)
    except Exception:
        pass

def calcola_analisi_takt(kanban_id):
    """
    Calcola e restituisce l'analisi completa per suggerire il Takt Time.
    Richiede almeno 5 cicli chiusi per dare risultati, 30 per essere affidabile.
    """
    MIN_CICLI_UTILI   = 5
    MIN_CICLI_AFFID   = 30

    p = KanbanProdotto.query.get(kanban_id)
    if not p:
        return {'ok': False, 'error': 'Prodotto non trovato'}

    cicli_chiusi = KanbanCiclo.query.filter(
        KanbanCiclo.kanban_id == kanban_id,
        KanbanCiclo.data_fine.isnot(None),
        KanbanCiclo.lead_time_ore.isnot(None),
        KanbanCiclo.qta_prodotta > 0
    ).order_by(KanbanCiclo.data_inizio).all()

    n_tot    = KanbanCiclo.query.filter_by(kanban_id=kanban_id).count()
    n_chiusi = len(cicli_chiusi)

    if n_chiusi < MIN_CICLI_UTILI:
        return {
            'ok': True,
            'affidabile': False,
            'n_cicli': n_tot,
            'n_cicli_chiusi': n_chiusi,
            'min_richiesti': MIN_CICLI_UTILI,
            'min_affidabili': MIN_CICLI_AFFID,
            'messaggio': f'Dati insufficienti: {n_chiusi}/{MIN_CICLI_UTILI} cicli completati. '
                         f'Continua a produrre — i dati si accumulano automaticamente.',
            'takt_suggerito': None,
            'lead_time_medio_ore': None,
            'domanda_giornaliera': p.domanda_giornaliera,
        }

    # Calcoli statistici
    lt_ore   = [c.lead_time_ore   for c in cicli_chiusi]
    qt_list  = [c.qta_prodotta    for c in cicli_chiusi]

    lt_medio = round(sum(lt_ore) / len(lt_ore), 2)
    lt_min   = round(min(lt_ore), 2)
    lt_max   = round(max(lt_ore), 2)

    tot_qta  = sum(qt_list)
    if cicli_chiusi:
        primo   = cicli_chiusi[0].data_inizio
        ultimo  = cicli_chiusi[-1].data_fine
        giorni  = max(1, (ultimo - primo).total_seconds() / 86400)
        domanda = round(tot_qta / giorni, 2)
    else:
        domanda = 0

    # Takt Time: minuti disponibili al giorno / domanda giornaliera
    # Assunzione: 8 ore/giorno = 480 minuti
    minuti_giorno = 480
    takt = round(minuti_giorno / domanda, 2) if domanda > 0 else None

    # N Kanban
    RT = lt_medio / 24   # ore → giorni
    SS = p.scorta_sicurezza or 0.15
    C  = p.lotto or 1
    n_kanban = round((domanda * RT * (1 + SS)) / C, 1) if domanda > 0 else None

    affidabile = n_chiusi >= MIN_CICLI_AFFID

    return {
        'ok': True,
        'affidabile': affidabile,
        'n_cicli': n_tot,
        'n_cicli_chiusi': n_chiusi,
        'min_richiesti': MIN_CICLI_UTILI,
        'min_affidabili': MIN_CICLI_AFFID,
        'takt_suggerito': takt,
        'takt_attuale': p.takt_time_min,
        'lead_time_medio_ore': lt_medio,
        'lead_time_min_ore': lt_min,
        'lead_time_max_ore': lt_max,
        'domanda_giornaliera': domanda,
        'tot_qta_analizzata': tot_qta,
        'n_kanban_suggerito': n_kanban,
        'messaggio': (
            f'✅ Analisi affidabile su {n_chiusi} cicli.' if affidabile
            else f'⚠️ Analisi parziale: {n_chiusi}/{MIN_CICLI_AFFID} cicli. '
                 f'Dati utili ma non ancora statisticamente solidi.'
        )
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
    urgenti    = sum(1 for c in commesse_attive if c.priorita == 1)
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
    tutti_kb = KanbanProdotto.query.all()
    tot_prodotti     = len(tutti_kb)
    da_programmare   = sum(1 for p in tutti_kb if p.stato == 'PROGRAMMARE PRODUZIONE')
    valore_magazzino = sum(p.val_pv for p in tutti_kb)
    return {
        'commesse_aperte': len(commesse_attive), 'urgenti': urgenti,
        'in_ritardo': in_ritardo, 'completate_mese': completate_mese,
        'saturazione': fasi_count,
        'tot_prodotti_kanban': tot_prodotti,
        'da_programmare': da_programmare,
        'scorte_ok': tot_prodotti - da_programmare,
        'valore_magazzino': round(valore_magazzino, 2),
        'commesse_segatrice': RigaMonitor.query.count(),
        'in_lavorazione': RigaMonitor.query.filter_by(sezione='lavorazione').count(),
        'in_saldatura_mon': RigaMonitor.query.filter_by(sezione='in_saldatura').count(),
    }

def init_db():
    db.create_all()
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
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS scorta_sicurezza FLOAT DEFAULT 0.15",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS lead_time_giorni FLOAT DEFAULT 7.0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS takt_time_min FLOAT",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS domanda_giornaliera FLOAT DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS n_kanban_suggerito INTEGER DEFAULT 0",
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
    try:
        if KanbanGruppo.query.count() == 0:
            for g in GRUPPI_DEFAULT:
                db.session.add(KanbanGruppo(**g))
            db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        if FaseWip.query.count() == 0:
            for f in FASI_WIP_DEFAULT:
                db.session.add(FaseWip(**f))
            db.session.commit()
    except Exception:
        db.session.rollback()
