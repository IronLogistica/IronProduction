import os, json
from datetime import datetime, date, timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

FASI_DISPONIBILI = ["taglio","sgola","piega","saldatura","finitura","verniciatura","collaudo"]
STATI_FASE       = ["non_prevista","da_fare","in_corso","completata","esternalizzata"]
STATI_COMMESSA   = ["APERTA","IN_PRODUZIONE","IN_ATTESA","IN_SALDATURA","POSTAZIONE","COMPLETATA","SPEDITA","ANNULLATA"]
SEZIONI_MONITOR  = ["lavorazione","da_iniziare","in_attesa","in_saldatura","postazione","terminati"]

PIN_ADMIN = "1234"   # PIN autorizzativo per operazioni protette

# ═══════════════════════════════════════════════════════════════════════════════
#  ARTICOLI — SPECCHIO IN SOLA LETTURA DI MASTERLOGISTIC
#  Stesso progetto Railway, database Postgres separato (bind 'masterlogistic',
#  vedi config.py / SQLALCHEMY_BINDS). Mappa 1:1 la tabella 'articoli' che
#  MasterLogistic già possiede e gestisce — NON è un doppione: IronProduction
#  non scrive né altera mai questa tabella (nessuna INSERT/UPDATE/DELETE da
#  qui, e db.create_all(bind_key=None) in app.py non tocca questo bind).
# ═══════════════════════════════════════════════════════════════════════════════
class ArticoloML(db.Model):
    __bind_key__   = 'masterlogistic'
    __tablename__  = 'articoli'
    id             = db.Column(db.Integer, primary_key=True)
    sku            = db.Column(db.String(50), unique=True, nullable=False)
    codice_esterno = db.Column(db.String(50), default='')
    descrizione    = db.Column(db.String(200), default='')
    stock          = db.Column(db.Integer, default=0)
    ordinati       = db.Column(db.Integer, default=0)
    incoming       = db.Column(db.Integer, default=0)
    fornitore      = db.Column(db.String(100), default='N/D')
    ordine_n       = db.Column(db.String(50),  default='N/D')
    data_evasione  = db.Column(db.String(20),  default='')
    scorta_minima  = db.Column(db.Integer, default=5)

    @property
    def stato(self):
        if (self.stock or 0) <= 0:                     return "esaurito"
        if (self.stock or 0) <= (self.scorta_minima or 0): return "sottoscorta"
        return "ok"


class DistintaBaseML(db.Model):
    """
    Specchio in sola lettura della Distinta Base (BOM) di MasterLogistic —
    stesso bind 'masterlogistic', stessa logica di ArticoloML: nessuna
    scrittura da qui, MasterLogistic resta l'unica fonte di verità.
    """
    __bind_key__  = 'masterlogistic'
    __tablename__ = 'distinta_base'
    id            = db.Column(db.Integer, primary_key=True)
    codice_padre  = db.Column(db.String(50), nullable=False)
    codice_figlio = db.Column(db.String(50), nullable=False)
    quantita      = db.Column(db.Float, default=1.0)
    livello       = db.Column(db.Integer, default=1)
    note          = db.Column(db.String(200), default='')
    creato_il     = db.Column(db.DateTime)


DRIVER_ATTIVITA_WOOD = ['ora_macchina', 'ora_reparto', 'ora_uomo', 'pezzo', 'tariffa_fissa']
VOCI_COSTO_PIANIFICATO_WOOD = ['ammortamento', 'manutenzione', 'energia', 'consumabili',
                               'affitti', 'supervisione', 'altro', 'manodopera_diretta']


class CentroCostoWood(db.Model):
    """
    Centro di costo / reparto di produzione Iron Wood — macchine come
    piegatrice, sega, foratura, oppure lavorazioni esterne (verniciatura
    esterna, piega esterna). Prerequisito dei Cicli di Lavoro: ogni riga
    di CicloLavoroWood punta a uno di questi come "reparto" della fase.

    'costo_orario' resta il campo che il motore di Costo Standard legge per
    il costo macchina/reparto (SENZA manodopera) — può essere scritto a mano
    (rapido) oppure calcolato dalla configurazione dettagliata sotto
    (Costi Pianificati / Capacità) tramite "Calcola e salva tariffa": in
    entrambi i casi resta l'unico valore usato a valle, per compatibilità.
    'tariffa_manodopera_diretta_oraria' è la SUA controparte per la manodopera
    diretta, tenuta volutamente separata (non sommata qui dentro).
    """
    __tablename__ = 'centri_costo_wood'
    id                                  = db.Column(db.Integer, primary_key=True)
    nome                                = db.Column(db.String(100), nullable=False, unique=True)
    esterno                             = db.Column(db.Boolean, default=False)   # True = lavorazione esterna (es. verniciatura esterna)
    costo_orario                        = db.Column(db.Float, default=0)          # €/h macchina/reparto, SENZA manodopera — vedi nota classe
    note                                = db.Column(db.String(300), default='')
    creato_il                           = db.Column(db.DateTime, default=datetime.utcnow)
    # ── Anagrafica estesa ──
    reparto_gruppo                      = db.Column(db.String(100), default='')   # raggruppamento libero (es. più macchine sotto lo stesso reparto)
    attivo                              = db.Column(db.Boolean, default=True)
    fornitore_esterno                   = db.Column(db.String(200), default='')   # solo se esterno=True
    tariffa_esterna                     = db.Column(db.Float, nullable=True)      # €/h o €/pz pattuito col fornitore esterno, se noto
    # ── Capacità e driver ──
    driver_attivita                     = db.Column(db.String(20), default='ora_reparto')  # vedi DRIVER_ATTIVITA_WOOD
    n_risorse_equivalenti               = db.Column(db.Float, default=1)          # es. 3 macchine identiche nello stesso centro
    ore_teoriche_periodo                = db.Column(db.Float, default=0)          # ore teoriche disponibili nel periodo, PER SINGOLA risorsa
    pct_efficienza                      = db.Column(db.Float, default=100)        # % di utilizzo pianificato (assenze, setup, manutenzione...)
    periodo_riferimento                 = db.Column(db.String(10), default='mensile')  # 'mensile' o 'annuale' — unità delle ore teoriche sopra
    tariffa_manodopera_diretta_oraria   = db.Column(db.Float, default=0)          # €/h manodopera diretta, calcolata separatamente da costo_orario
    # True = questo reparto/macchina NON compare nei Monitor/Totem di produzione
    # di IronProduction (es. la Saldatura è già programmata e consuntivata in
    # MasterWork) — resta comunque un centro di costo valido per Cicli di
    # Lavoro e Costo Standard, semplicemente non genera una coda/totem qui.
    escluso_da_monitor_produzione       = db.Column(db.Boolean, default=False)


class CostoPianificatoCentroWood(db.Model):
    """
    Voce di costo pianificato di un Centro di Costo (ammortamento,
    manutenzione, energia, ...), storicizzata per data di validità: quando si
    modifica l'importo di una voce, la riga precedente viene chiusa
    (valido_al = oggi) e se ne apre una nuova (valido_dal = oggi) — non si
    sovrascrive mai un valore storico. La riga "corrente" di una voce è
    sempre quella con valido_al IS NULL.
    """
    __tablename__ = 'costi_pianificati_centro_wood'
    id               = db.Column(db.Integer, primary_key=True)
    centro_costo_id  = db.Column(db.Integer, db.ForeignKey('centri_costo_wood.id'), nullable=False)
    voce             = db.Column(db.String(30), nullable=False)   # vedi VOCI_COSTO_PIANIFICATO_WOOD
    importo          = db.Column(db.Float, nullable=False, default=0)
    valido_dal       = db.Column(db.Date, nullable=False)
    valido_al        = db.Column(db.Date, nullable=True)          # NULL = tuttora valida
    note             = db.Column(db.String(300), default='')
    creato_il        = db.Column(db.DateTime, default=datetime.utcnow)
    centro_costo     = db.relationship('CentroCostoWood')


class CicloLavoroWood(db.Model):
    """
    Ciclo di lavoro (routing) Iron Wood — sequenza di reparti attraversati
    da un codice (padre O figlio: un sottocomponente può avere il proprio
    ciclo, indipendente da quello del padre), con la produttività oraria
    specifica per quel codice in quel reparto.
    Una riga = una fase: (codice, sequenza) individua univocamente il suo
    posto nell'ordine del ciclo; più righe con lo stesso codice e sequenza
    crescente = reparti in serie.
    """
    __tablename__ = 'ciclo_lavoro_wood'
    id                  = db.Column(db.Integer, primary_key=True)
    codice              = db.Column(db.String(50), nullable=False)   # codice padre o figlio della distinta Iron Wood
    sequenza            = db.Column(db.Integer, nullable=False)      # ordine del reparto nel ciclo (1,2,3...)
    centro_costo_id     = db.Column(db.Integer, db.ForeignKey('centri_costo_wood.id'), nullable=False)
    produttivita_oraria = db.Column(db.Float, default=0)             # pezzi/ora per QUESTO codice in QUESTO reparto
    # % massima di scarto fisiologicamente ammessa per QUESTO codice in QUESTO
    # reparto (es. 2.0 = 2%) — standard comunicato all'operatore sul totem,
    # non un vincolo bloccante: None = nessuno standard comunicato.
    scarto_max_pct      = db.Column(db.Float, nullable=True)
    note                = db.Column(db.String(300), default='')
    creato_il           = db.Column(db.DateTime, default=datetime.utcnow)
    centro_costo        = db.relationship('CentroCostoWood')
    __table_args__ = (db.UniqueConstraint('codice', 'sequenza', name='_codice_sequenza_uc'),)


class GiacenzaWood(db.Model):
    """
    Giacenza fisica LOCALE Iron Wood (materie prime, componenti d'acquisto,
    laserati, semilavorati) — non più una lettura di MasterLogistic: qui è
    la fonte di verità per governare materiali/impegni/fabbisogno delle
    produzioni Iron Wood, aggiornata da carico iniziale, rettifiche manuali
    e scarico automatico a consuntivo produzione.
    """
    __tablename__ = 'giacenza_wood'
    codice        = db.Column(db.String(50), primary_key=True)
    quantita      = db.Column(db.Float, default=0)
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow)


class ScortaMinimaWood(db.Model):
    """
    Scorta minima configurabile per codice Iron Wood — soglia sotto la quale
    scatta il fabbisogno (metodologia MasterLogistic-WMS: Disponibile
    Contabile confrontato con questa soglia). Riga assente = soglia 0.
    """
    __tablename__ = 'scorte_minime_wood'
    codice         = db.Column(db.String(50), primary_key=True)
    scorta_minima  = db.Column(db.Float, default=0)
    aggiornato_il  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MovimentoGiacenzaWood(db.Model):
    """Storico di ogni variazione della giacenza Iron Wood (carico/scarico), per audit."""
    __tablename__ = 'movimenti_giacenza_wood'
    id             = db.Column(db.Integer, primary_key=True)
    codice         = db.Column(db.String(50), nullable=False, index=True)
    tipo           = db.Column(db.String(30), nullable=False)  # carico_iniziale | carico_manuale | scarico_manuale | scarico_produzione | carico_produzione | rettifica_import
    quantita       = db.Column(db.Float, nullable=False)        # positivo=carico, negativo=scarico
    costo_unitario = db.Column(db.Float, nullable=True)         # €/pz — valorizzato solo per carico_produzione (costo standard al momento del carico)
    valore         = db.Column(db.Float, nullable=True)         # costo_unitario × |quantita|
    riferimento    = db.Column(db.String(100), default='')      # es. codice OP che ha generato lo scarico/carico
    note           = db.Column(db.String(300), default='')
    creato_il      = db.Column(db.DateTime, default=datetime.utcnow)


class ImpostazioneCostoWood(db.Model):
    """
    Impostazioni globali per il calcolo del costo standard Iron Wood (riga
    singola) — logica SAP a due basi separate, non una percentuale unica:
    'Overhead materiali' si applica SOLO ai materiali, 'Overhead di
    produzione' si applica SOLO a lavorazione + manodopera diretta. Il vecchio
    campo aliquota_overhead_pct resta per compatibilità di lettura di
    versioni salvate prima di questa modifica, ma non è più scritto.
    """
    __tablename__ = 'impostazioni_costo_wood'
    id                              = db.Column(db.Integer, primary_key=True)
    aliquota_overhead_pct           = db.Column(db.Float, default=0)   # LEGACY, non più usato per nuovi calcoli
    aliquota_overhead_materiali_pct = db.Column(db.Float, default=0)   # % su costo_materiali
    aliquota_overhead_produzione_pct = db.Column(db.Float, default=0)  # % su (costo_lavorazione + costo_manodopera)
    aggiornato_il                   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CostoStandardVersioneWood(db.Model):
    """
    Snapshot storico e VERSIONATO del costo standard di un codice — a
    differenza di CostoStandardWood (che tiene solo l'ultimo calcolo, per la
    vista rapida), qui ogni "Ricalcola e salva" crea una NUOVA riga con
    versione incrementale per quel codice, mai sovrascritta. Un Ordine di
    Produzione, al rilascio, si aggancia alla versione più recente disponibile
    in quel momento (vedi LegameCostoStandardOrdineWood) — così un ricalcolo
    successivo del costo standard non altera retroattivamente cosa "era" lo
    standard quando l'ordine è partito (indispensabile per calcolare le
    varianze in modo onesto).
    """
    __tablename__ = 'costo_standard_versioni_wood'
    id                       = db.Column(db.Integer, primary_key=True)
    codice                   = db.Column(db.String(50), nullable=False, index=True)
    versione                 = db.Column(db.Integer, nullable=False)   # 1, 2, 3... incrementale per codice
    costo_materiali          = db.Column(db.Float, default=0)
    costo_lavorazione        = db.Column(db.Float, default=0)   # SOLO macchina/reparto (costo_orario, no manodopera)
    costo_manodopera         = db.Column(db.Float, default=0)   # manodopera diretta, tariffa separata (tariffa_manodopera_diretta_oraria)
    costo_overhead           = db.Column(db.Float, default=0)   # overhead_materiali + overhead_produzione, totale
    costo_overhead_materiali = db.Column(db.Float, default=0)
    costo_overhead_produzione = db.Column(db.Float, default=0)
    costo_totale             = db.Column(db.Float, default=0)
    ore_manodopera_standard  = db.Column(db.Float, default=0)
    overhead_pct_usata       = db.Column(db.Float, default=0)   # LEGACY
    overhead_materiali_pct_usata  = db.Column(db.Float, default=0)  # aliquota materiali applicata in QUESTO calcolo
    overhead_produzione_pct_usata = db.Column(db.Float, default=0)  # aliquota produzione applicata in QUESTO calcolo
    completo                 = db.Column(db.Boolean, default=True)
    codici_senza_costo       = db.Column(db.Text, default='')
    calcolato_il             = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('codice', 'versione', name='_costo_std_codice_versione_uc'),)


class LegameCostoStandardOrdineWood(db.Model):
    """
    Aggancia un Ordine di Produzione (per codice OP, non per id — coerente
    con come il resto del programma referenzia gli OP nei movimenti/eventi)
    alla versione di Costo Standard valida al momento del suo RILASCIO.
    L'analisi varianze usa sempre questa versione congelata, mai l'ultima
    disponibile — altrimenti un ricalcolo successivo falserebbe le varianze
    di ordini già in corso.
    """
    __tablename__ = 'legame_costo_standard_ordine_wood'
    op_code                     = db.Column(db.String(20), primary_key=True)
    costo_standard_versione_id  = db.Column(db.Integer, db.ForeignKey('costo_standard_versioni_wood.id'), nullable=True)
    agganciato_il                = db.Column(db.DateTime, default=datetime.utcnow)
    versione = db.relationship('CostoStandardVersioneWood')


class CostoStandardVersioneDettaglioWood(db.Model):
    """
    Dettaglio PER COMPONENTE di una versione di Costo Standard — quantità e
    prezzo unitario congelati al momento di QUEL calcolo, per ogni codice
    toccato dall'esplosione della BOM. Senza questo dettaglio, l'analisi
    varianze per componente dovrebbe usare prezzi/quantità CORRENTI come
    proxy dello standard, disallineandosi dal totale congelato (incoerenza
    seria per un controllo di gestione serio) — con questo dettaglio la
    varianza prezzo/quantità per componente riconcilia esattamente col totale.
    """
    __tablename__ = 'costo_standard_versione_dettaglio_wood'
    id                       = db.Column(db.Integer, primary_key=True)
    versione_id              = db.Column(db.Integer, db.ForeignKey('costo_standard_versioni_wood.id'), nullable=False, index=True)
    codice_componente        = db.Column(db.String(50), nullable=False)
    quantita_standard        = db.Column(db.Float, default=0)   # per 1 pz del codice padre di quella versione
    prezzo_standard_unitario = db.Column(db.Float, nullable=True)  # None = componente senza prezzo al momento del calcolo
    tipo                     = db.Column(db.String(20), default='')  # ACQUISTO / SEMILAVORATO / DA_CLASSIFICARE
    versione = db.relationship('CostoStandardVersioneWood', backref='dettagli')


class CostoStandardVersioneFaseWood(db.Model):
    """
    Snapshot PER FASE di routing (Ciclo di Lavoro) del codice calcolato in
    una versione di Costo Standard — reparto, produttività e TARIFFE (macchina
    e manodopera) congelate a quel momento. Senza questo, la varianza di
    lavorazione/manodopera a consuntivo può misurare solo tempo/efficienza
    (la tariffa usata per "standard" e "reale" sarebbe sempre la stessa,
    quella corrente): con le tariffe congelate qui, si può isolare anche una
    vera varianza di TARIFFA se il costo del centro cambia tra il rilascio
    dell'ordine e il consuntivo.
    """
    __tablename__ = 'costo_standard_versione_fase_wood'
    id                              = db.Column(db.Integer, primary_key=True)
    versione_id                     = db.Column(db.Integer, db.ForeignKey('costo_standard_versioni_wood.id'), nullable=False, index=True)
    sequenza                        = db.Column(db.Integer, default=1)
    nome_reparto                    = db.Column(db.String(100), nullable=False)   # denormalizzato: resta leggibile anche se il centro viene rinominato/eliminato
    produttivita_oraria_congelata   = db.Column(db.Float, default=0)
    costo_orario_congelato          = db.Column(db.Float, default=0)   # tariffa macchina/reparto al momento del calcolo
    tariffa_manodopera_congelata    = db.Column(db.Float, default=0)   # tariffa manodopera diretta al momento del calcolo
    versione = db.relationship('CostoStandardVersioneWood', backref='fasi')


class CostoStandardWood(db.Model):
    """
    Costo standard calcolato (e salvato) per un codice Iron Wood, secondo la
    logica SAP: Materiali (BOM ricorsiva) + Lavorazione (Routing/Ciclo di
    Lavoro) + Overhead (% su materiali+lavorazione). Ricalcolato su richiesta
    (bottone "Ricalcola"), non automaticamente ad ogni modifica di BOM/routing.
    """
    __tablename__ = 'costo_standard_wood'
    codice                    = db.Column(db.String(50), primary_key=True)
    costo_materiali           = db.Column(db.Float, default=0)
    costo_lavorazione         = db.Column(db.Float, default=0)   # SOLO macchina/reparto
    costo_manodopera          = db.Column(db.Float, default=0)   # manodopera diretta, tariffa separata
    costo_overhead            = db.Column(db.Float, default=0)   # totale (materiali + produzione)
    costo_overhead_materiali  = db.Column(db.Float, default=0)
    costo_overhead_produzione = db.Column(db.Float, default=0)
    costo_totale              = db.Column(db.Float, default=0)
    ore_manodopera_standard   = db.Column(db.Float, default=0)  # ore totali standard per pezzo (dirette + di ogni sottocomponente)
    completo                  = db.Column(db.Boolean, default=True)   # False = almeno un componente foglia nell'albero non ha un prezzo configurato: il totale è sottostimato, non affidabile
    codici_senza_costo        = db.Column(db.Text, default='')        # lista codici mancanti separata da virgola, per mostrarli senza dover riesplodere l'albero
    calcolato_il              = db.Column(db.DateTime, default=datetime.utcnow)


class VarianzaProduzioneWood(db.Model):
    """
    Varianza di lavorazione registrata ad ogni consuntivo di produzione:
    confronta il tempo/costo di lavorazione STANDARD atteso (da produttività
    oraria del Ciclo di Lavoro) con quello REALE consuntivato (da tempo_minuti
    dell'evento), quando la fase dell'evento è abbinabile per nome a un
    Centro di Costo — se non abbinabile, nessuna varianza viene registrata.
    Manodopera diretta tracciata in PARALLELO al costo macchina (stesso
    tempo standard/reale, tariffa propria) — mai sommata al costo macchina,
    coerente con la separazione costo_lavorazione/costo_manodopera del
    Costo Standard.
    """
    __tablename__ = 'varianze_produzione_wood'
    id                          = db.Column(db.Integer, primary_key=True)
    op_code                     = db.Column(db.String(20), nullable=False, index=True)
    codice_articolo             = db.Column(db.String(50), nullable=False)
    fase                        = db.Column(db.String(100), default='')
    quantita                    = db.Column(db.Integer, nullable=False)   # pezzi buoni di questo evento
    tempo_standard_minuti       = db.Column(db.Float, default=0)
    tempo_reale_minuti          = db.Column(db.Float, default=0)
    costo_standard_lavorazione  = db.Column(db.Float, default=0)   # SOLO macchina/reparto
    costo_reale_lavorazione     = db.Column(db.Float, default=0)
    costo_standard_manodopera   = db.Column(db.Float, default=0)   # manodopera diretta, tariffa propria
    costo_reale_manodopera      = db.Column(db.Float, default=0)
    varianza                    = db.Column(db.Float, default=0)  # lavorazione TOTALE: reale − standard (positivo = peggio dello standard)
    varianza_manodopera         = db.Column(db.Float, default=0)  # manodopera TOTALE: reale − standard
    varianza_tariffa_lavorazione = db.Column(db.Float, default=0)  # SOLO quota dovuta al cambio tariffa macchina (0 se non congelata)
    varianza_tariffa_manodopera  = db.Column(db.Float, default=0)  # SOLO quota dovuta al cambio tariffa manodopera (0 se non congelata)
    creato_il                   = db.Column(db.DateTime, default=datetime.utcnow)


class SogliaAllarmeVarianzaWood(db.Model):
    """
    Soglie configurabili (riga singola, come ImpostazioneCostoWood) oltre le
    quali il report di commessa segnala automaticamente una varianza come
    da tenere d'occhio — non bloccano nulla, sono solo per l'evidenza visiva
    nel report (vedi 'segnalazioni' in api_analisi_costo_ordine).
    """
    __tablename__ = 'soglie_allarme_varianza_wood'
    id                      = db.Column(db.Integer, primary_key=True)
    soglia_materiali_pct    = db.Column(db.Float, default=5.0)
    soglia_lavorazione_pct  = db.Column(db.Float, default=5.0)
    soglia_manodopera_pct   = db.Column(db.Float, default=5.0)
    soglia_totale_pct       = db.Column(db.Float, default=5.0)
    soglia_scarto_pct       = db.Column(db.Float, default=3.0)
    aggiornato_il           = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Voci contabili standard per cui serve una mappatura verso un conto — vedi
# ContoContabileMappaWood. L'elenco segue esattamente la struttura richiesta
# per la futura integrazione MasterLedger/MasterFico (sez. 6 della specifica
# costo standard): magazzini, produzione in corso, assorbimenti standard e
# varianze separate per tipo.
VOCI_CONTABILI_WOOD = [
    ('MAGAZZINO_MP',                   'Magazzino materie prime'),
    ('MAGAZZINO_SEMILAVORATI',         'Magazzino semilavorati'),
    ('MAGAZZINO_PF',                   'Magazzino prodotti finiti'),
    ('PRODUZIONE_IN_CORSO',            'Produzione in corso (WIP)'),
    ('CONSUMO_MATERIALI_STD',          'Consumo materiali standard'),
    ('ASSORBIMENTO_MANODOPERA_STD',    'Assorbimento manodopera standard'),
    ('ASSORBIMENTO_MACCHINA_STD',      'Assorbimento macchina standard'),
    ('VARIANZA_PREZZO_MATERIALI',      'Varianza prezzo materiali'),
    ('VARIANZA_QUANTITA_MATERIALI',    'Varianza quantità/consumo materiali'),
    ('VARIANZA_EFFICIENZA_MANODOPERA', 'Varianza efficienza manodopera'),
    ('VARIANZA_TARIFFA_MANODOPERA',    'Varianza tariffa manodopera'),
    ('VARIANZA_EFFICIENZA_MACCHINA',   'Varianza efficienza macchina'),
    ('VARIANZA_TARIFFA_MACCHINA',      'Varianza tariffa macchina'),
    ('VARIANZA_PRODUZIONE',            'Varianza di produzione (carico PF)'),
]


class ContoContabileMappaWood(db.Model):
    """
    Mappa CONFIGURABILE voce contabile → conto — una riga per voce di
    VOCI_CONTABILI_WOOD, precreata vuota (conto='') da
    assicura_conti_contabili_wood() e compilabile dall'utente. Usata da
    _genera_movimenti_contabili_ordine per scrivere il conto giusto su ogni
    riga di MovimentoContabileWood.
    """
    __tablename__ = 'conti_contabili_mappa_wood'
    id                = db.Column(db.Integer, primary_key=True)
    voce              = db.Column(db.String(50), nullable=False, unique=True)
    conto             = db.Column(db.String(30), default='')
    descrizione_conto = db.Column(db.String(200), default='')


def assicura_conti_contabili_wood():
    """Crea (se mancanti) le righe della mappa conti per tutte le VOCI_CONTABILI_WOOD, conto vuoto — idempotente."""
    esistenti = {c.voce for c in ContoContabileMappaWood.query.all()}
    for voce, descr in VOCI_CONTABILI_WOOD:
        if voce not in esistenti:
            db.session.add(ContoContabileMappaWood(voce=voce, descrizione_conto=descr))
    db.session.commit()


class MovimentoContabileWood(db.Model):
    """
    Movimento contabile STRUTTURATO in partita doppia, generato su richiesta
    per un Ordine di Produzione (vedi _genera_movimenti_contabili_ordine) a
    partire dagli stessi numeri già mostrati nel report Analisi Costo — MAI
    scritto automaticamente ad ogni evento: si genera quando si vuole
    "chiudere" contabilmente un OP (tipicamente a completamento). Predisposto
    per una futura trasmissione a MasterLedger/MasterFico — 'esportato'
    resta False finché quell'integrazione non esiste davvero; per ora
    l'unica via d'uscita è l'export CSV (vedi /api/movimenti-contabili/export).
    Rigenerare i movimenti di un OP cancella e ricrea SOLO le righe non ancora
    esportate, per non alterare dati già trasmessi.
    """
    __tablename__ = 'movimenti_contabili_wood'
    id                  = db.Column(db.Integer, primary_key=True)
    data                = db.Column(db.Date, nullable=False, default=date.today)
    causale             = db.Column(db.String(150), nullable=False)
    op_code             = db.Column(db.String(20), default='', index=True)
    codice_articolo     = db.Column(db.String(50), default='')
    conto               = db.Column(db.String(30), default='')
    centro_costo        = db.Column(db.String(100), default='')
    dare_avere          = db.Column(db.String(5), nullable=False)   # 'DARE' o 'AVERE'
    importo             = db.Column(db.Float, nullable=False, default=0)
    tipo_varianza       = db.Column(db.String(50), default='')      # '' se non è una riga di varianza (assorbimento/consumo standard)
    esportato           = db.Column(db.Boolean, default=False)
    esportato_il        = db.Column(db.DateTime, nullable=True)
    creato_il           = db.Column(db.DateTime, default=datetime.utcnow)


class DescrizioneCodiceWood(db.Model):
    """
    Descrizione LOCALE di un codice Iron Wood — alimentata dalla colonna
    DESCOM degli export Zucchetti Ad Hoc, usata SOLO come riserva quando
    ArticoloML (il magazzino condiviso di MasterLogistic, di un'altra
    azienda) non ha già una descrizione per quel codice. Non scrive né
    modifica mai ArticoloML.
    """
    __tablename__ = 'descrizione_codice_wood'
    codice      = db.Column(db.String(100), primary_key=True)
    descrizione = db.Column(db.String(300), nullable=False, default='')
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DistintaBaseWood(db.Model):
    """
    Distinta base (BOM) di Iron Wood — COPIA LOCALE, nel database di
    IronProduction (nessun __bind_key__: vive nel bind di default,
    creata da db.create_all(bind_key=None)).

    Perché una copia locale e non la stessa tabella di MasterLogistic:
    'distinta_base' su MasterLogistic non ha nessuna colonna che distingue
    l'azienda ed è pensata per un caricamento riga-per-riga (nessun bulk
    replace nell'app) — un import massivo delle distinte di Iron Wood lì
    rischia di sovrascrivere/confondersi con quelle di Iron Segnaletica.
    Tenendole qui, separate, si evita il problema alla radice.

    Il magazzino (articoli/stock) resta invece UNICO e condiviso su
    MasterLogistic (vedi ArticoloML) — qui dentro, codice_padre e
    codice_figlio fanno riferimento agli stessi SKU di quel magazzino.
    """
    __tablename__ = 'distinta_base_wood'
    id            = db.Column(db.Integer, primary_key=True)
    codice_padre  = db.Column(db.String(50), nullable=False)
    codice_figlio = db.Column(db.String(50), nullable=False)
    quantita      = db.Column(db.Float, default=1.0)
    livello       = db.Column(db.Integer, default=1)
    note          = db.Column(db.String(200), default='')
    # ── Componenti alternativi (es. stesso articolo, a volte barra di ferro
    # da 7m, a volte da 6m): righe con lo stesso (codice_padre, gruppo_alternativa)
    # sono mutuamente esclusive. 'preferita' indica quale delle alternative va
    # usata di default nell'esplosione BOM/costo standard/fabbisogno finché non
    # viene cambiata a mano (vedi _righe_bom_attive in blueprints/magazzino).
    # gruppo_alternativa = None → riga normale, nessuna alternativa, sempre inclusa.
    gruppo_alternativa = db.Column(db.String(50), nullable=True)
    preferita          = db.Column(db.Boolean, default=True)
    # ── Schede di taglio (es. "Padre / Figlio / pz per Barra / Sviluppo / Note"
    # raccolte da Angelo in Excel sparsi): quando il figlio è una barra/profilo
    # grezzo da tagliare, questi due campi conservano il dato ORIGINALE della
    # scheda (quanti pezzi finiti escono da una barra, e la lunghezza di
    # sviluppo del pezzo) — 'quantita' resta comunque calcolata come
    # 1/pezzi_per_barra, per restare compatibile col motore BOM/costo standard
    # esistente, che non cambia e non deve sapere di questi due campi.
    pezzi_per_barra    = db.Column(db.Float, nullable=True)
    sviluppo           = db.Column(db.String(50), nullable=True)   # es. "L. 1.932" — testo libero, alcune schede hanno note tipo "D.V." attaccate
    creato_il     = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('codice_padre', 'codice_figlio', name='_padre_figlio_wood_uc'),)


class MatriceWood(db.Model):
    """Anagrafica matrici (piegatrice) — lista da cui pesca il menu a tendina della Scheda Piega."""
    __tablename__ = 'matrici_wood'
    id          = db.Column(db.Integer, primary_key=True)
    codice      = db.Column(db.String(50), nullable=False, unique=True)
    descrizione = db.Column(db.String(200), default='')
    creato_il   = db.Column(db.DateTime, default=datetime.utcnow)


class RulloWood(db.Model):
    """Anagrafica rulli (piegatrice) — lista da cui pesca il menu a tendina della Scheda Piega."""
    __tablename__ = 'rulli_wood'
    id          = db.Column(db.Integer, primary_key=True)
    codice      = db.Column(db.String(50), nullable=False, unique=True)
    descrizione = db.Column(db.String(200), default='')
    creato_il   = db.Column(db.DateTime, default=datetime.utcnow)


class LunghezzaBarraWood(db.Model):
    """
    Anagrafica lunghezze barra disponibili (es. 6 mt, 7 mt) — PARAMETRO DI
    IMPOSTAZIONE INIZIALE configurabile, prima era una lista fissa scritta
    nel codice. Alimenta il menu a tendina "Lunghezza Barra" nei Parametri
    di Lavorazione.
    """
    __tablename__ = 'lunghezze_barra_wood'
    id          = db.Column(db.Integer, primary_key=True)
    valore_mm   = db.Column(db.Float, nullable=False, unique=True)
    etichetta   = db.Column(db.String(50), default='')   # es. "6 mt" — se vuota, si mostra calcolata da valore_mm
    creato_il   = db.Column(db.DateTime, default=datetime.utcnow)


class AvvisoScostamentoWood(db.Model):
    """
    Avviso alla Direzione quando una commessa si chiude fuori tolleranza
    quantità — sopra-produzione rilevata in automatico appena la si supera
    (soglia 10%, oltre la quale è quasi certamente un errore di
    dichiarazione), sotto-produzione rilevata solo quando la commessa viene
    chiusa deliberatamente sotto target (soglia 3%, tolleranza commerciale).
    """
    __tablename__ = 'avviso_scostamento_wood'
    id                = db.Column(db.Integer, primary_key=True)
    op_code           = db.Column(db.String(20), nullable=False)
    codice_articolo   = db.Column(db.String(100), nullable=False)
    tipo              = db.Column(db.String(20), nullable=False)   # 'SOPRA_PRODUZIONE' / 'SOTTO_PRODUZIONE'
    qta_pianificata   = db.Column(db.Float, nullable=False)
    qta_buona         = db.Column(db.Float, nullable=False)
    percentuale       = db.Column(db.Float, nullable=False)        # scostamento %, sempre positivo
    letto             = db.Column(db.Boolean, default=False)
    creato_il         = db.Column(db.DateTime, default=datetime.utcnow)


class SchedaLavorazioneWood(db.Model):
    """
    Scheda di lavorazione Iron Wood UNIFICATA — una riga per coppia
    codice_padre/codice_figlio con TUTTE le specifiche macchina (taglio,
    piega, satinatura insieme), invece di tre tabelle separate. Sostituisce
    le vecchie SchedaTaglioWood/SchedaPiegaWood/SchedaSatinaturaWood — i dati
    già inseriti lì vengono migrati una tantum da
    migra_schede_lavorazione_unificate() al primo avvio dopo l'aggiornamento;
    le vecchie tabelle restano nel DB (non cancellate) ma non più usate.
    """
    __tablename__ = 'schede_lavorazione_wood'
    id                        = db.Column(db.Integer, primary_key=True)
    codice_padre              = db.Column(db.String(50), nullable=False)
    codice_figlio             = db.Column(db.String(50), nullable=False)
    lunghezza_barra_mm        = db.Column(db.Float, nullable=True)          # es. 6000 o 7000
    spessore_mm               = db.Column(db.Float, nullable=True)          # es. 1.5, 2, 3 — spessore del materiale/barra
    pezzi_per_barra           = db.Column(db.Float, nullable=True)
    sviluppo                  = db.Column(db.String(50), default='')        # es. "L. 1.932"
    matrice_id                = db.Column(db.Integer, db.ForeignKey('matrici_wood.id', ondelete='SET NULL'), nullable=True)
    punto_zero                = db.Column(db.String(50), default='')
    indice_assorbimento       = db.Column(db.String(50), default='')
    rullo_id                  = db.Column(db.Integer, db.ForeignKey('rulli_wood.id', ondelete='SET NULL'), nullable=True)
    impostazione_satinatrice  = db.Column(db.String(100), default='')
    note                      = db.Column(db.String(300), default='')
    creato_il                 = db.Column(db.DateTime, default=datetime.utcnow)
    matrice = db.relationship('MatriceWood')
    rullo   = db.relationship('RulloWood')
    __table_args__ = (db.UniqueConstraint('codice_padre', 'codice_figlio', name='_lavorazione_padre_figlio_uc'),)


def assicura_lunghezze_barra_default():
    """UNA TANTUM: se la tabella è vuota, precrea 6 mt e 7 mt (i due valori che prima erano fissi nel codice)."""
    if LunghezzaBarraWood.query.count() > 0:
        return
    db.session.add_all([
        LunghezzaBarraWood(valore_mm=6000, etichetta='6 mt'),
        LunghezzaBarraWood(valore_mm=7000, etichetta='7 mt'),
    ])
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()  # vedi nota in migra_schede_lavorazione_unificate()


def migra_schede_lavorazione_unificate():
    """
    UNA TANTUM: se schede_lavorazione_wood è ancora vuota, importa (unendo per
    codice_padre+codice_figlio) i dati dalle vecchie tabelle
    schede_taglio_wood / schede_piega_wood / schede_satinatura_wood, se
    esistono ancora nel DB. Non cancella né modifica le vecchie tabelle.
    Si ferma da sola appena la tabella unificata ha almeno una riga (anche
    inserita a mano) — non sovrascrive mai dati già presenti lì.
    """
    from sqlalchemy import inspect, text
    if SchedaLavorazioneWood.query.count() > 0:
        return
    insp = inspect(db.engine)
    unite = {}
    if insp.has_table('schede_taglio_wood'):
        for r in db.session.execute(text(
                "SELECT codice_padre, codice_figlio, pezzi_per_barra, sviluppo, lunghezza_barra_mm, note "
                "FROM schede_taglio_wood")):
            unite.setdefault((r.codice_padre, r.codice_figlio), {}).update({
                'pezzi_per_barra': r.pezzi_per_barra, 'sviluppo': r.sviluppo,
                'lunghezza_barra_mm': r.lunghezza_barra_mm, 'note': r.note,
            })
    if insp.has_table('schede_piega_wood'):
        for r in db.session.execute(text(
                "SELECT codice_padre, codice_figlio, sviluppo, matrice_id, punto_zero, indice_assorbimento, rullo_id, note "
                "FROM schede_piega_wood")):
            d = unite.setdefault((r.codice_padre, r.codice_figlio), {})
            if r.sviluppo:
                d['sviluppo'] = r.sviluppo
            d['matrice_id'] = r.matrice_id
            d['punto_zero'] = r.punto_zero
            d['indice_assorbimento'] = r.indice_assorbimento
            d['rullo_id'] = r.rullo_id
            if r.note:
                d['note'] = ((d.get('note') or '') + ' ' + r.note).strip()
    if insp.has_table('schede_satinatura_wood'):
        for r in db.session.execute(text(
                "SELECT codice_padre, codice_figlio, impostazione_satinatrice, note "
                "FROM schede_satinatura_wood")):
            d = unite.setdefault((r.codice_padre, r.codice_figlio), {})
            d['impostazione_satinatrice'] = r.impostazione_satinatrice
            if r.note:
                d['note'] = ((d.get('note') or '') + ' ' + r.note).strip()

    for (padre, figlio), campi in unite.items():
        db.session.add(SchedaLavorazioneWood(codice_padre=padre, codice_figlio=figlio, **campi))
    if unite:
        try:
            db.session.commit()
            log(f"Migrazione schede lavorazione: unificate {len(unite)} righe da taglio/piega/satinatura in una sola tabella")
        except Exception:
            # Difesa in profondità oltre a --preload: se un'altra istanza ha
            # già eseguito questa stessa migrazione nel frattempo (es. durante
            # un deploy con sovrapposizione breve), non deve mai far crashare
            # l'avvio — i dati sono comunque già lì.
            db.session.rollback()


class NumeroListaLavoroWood(db.Model):
    """
    Numero identificativo di ogni Lista di Lavoro stampata (es. CUT/001,
    PRES/001) — assegnato UNA VOLTA per coppia OP+centro di costo (idempotente:
    aprire/stampare di nuovo la stessa lista mostra sempre lo stesso numero,
    non ne genera uno nuovo), progressivo per tipo di macchina (prefisso).
    """
    __tablename__ = 'numero_lista_lavoro_wood'
    id               = db.Column(db.Integer, primary_key=True)
    op_code          = db.Column(db.String(20), nullable=False)
    centro_costo_id  = db.Column(db.Integer, db.ForeignKey('centri_costo_wood.id'), nullable=False)
    prefisso         = db.Column(db.String(10), nullable=False)
    numero           = db.Column(db.Integer, nullable=False)
    creato_il        = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('op_code', 'centro_costo_id', name='_numlista_op_centro_uc'),)


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
    # nullable=True: le lavorazioni da DDT PDF non sono collegate a una riga commessa
    riga_id          = db.Column(db.Integer, db.ForeignKey("righe_commessa.id"), nullable=True)
    # nullable=True: il terzista viene creato automaticamente ma potrebbe mancare
    terzista_id      = db.Column(db.Integer, db.ForeignKey("terzisti.id"),       nullable=True)
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

TIPI_TRATTAMENTO_SCHEDA = {
    'ZINCATURA_CALDO':        {'label': 'Zincatura a Caldo',        'zincatura': True,  'zinc_label': 'CALDO',  'verniciatura': False},
    'ZINCATURA_FREDDO':       {'label': 'Zincatura a Freddo',       'zincatura': True,  'zinc_label': 'FREDDO', 'verniciatura': False},
    'ZINCATURA_VERNICIATURA': {'label': 'Zincatura + Verniciatura', 'zincatura': True,  'zinc_label': '',       'verniciatura': True},
    'VERNICIATURA':           {'label': 'Verniciatura',             'zincatura': False, 'zinc_label': '',       'verniciatura': True},
}

FORNITORI_SCHEDA_DEFAULT = ['TGT', 'NUOVA PLASTIC METAL']


class SchedaTrattamento(db.Model):
    __tablename__ = "schede_trattamenti"
    id               = db.Column(db.Integer, primary_key=True)
    codice_articolo  = db.Column(db.String(100), nullable=False)
    fornitore        = db.Column(db.String(200), nullable=False)
    commessa         = db.Column(db.String(100), default="")
    tipo_trattamento = db.Column(db.String(50),  nullable=False)  # legacy, tenuto per compatibilità storico
    colore           = db.Column(db.String(100), default="")
    zincatura        = db.Column(db.Boolean, default=False)
    zincatura_tipo   = db.Column(db.String(10), default="")   # 'CALDO' / 'FREDDO'
    verniciatura     = db.Column(db.Boolean, default=False)
    creato_il        = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def numero_scheda(self):
        return f"ST-{self.id:05d}"

    @property
    def ha_zincatura(self):
        if self.zincatura:
            return True
        # fallback per schede create prima dell'introduzione dei flag
        return TIPI_TRATTAMENTO_SCHEDA.get(self.tipo_trattamento, {}).get('zincatura', False)

    @property
    def zincatura_label(self):
        if self.zincatura and self.zincatura_tipo:
            return self.zincatura_tipo
        return TIPI_TRATTAMENTO_SCHEDA.get(self.tipo_trattamento, {}).get('zinc_label', '')

    @property
    def ha_verniciatura(self):
        if self.verniciatura:
            return True
        return TIPI_TRATTAMENTO_SCHEDA.get(self.tipo_trattamento, {}).get('verniciatura', False)

    @property
    def info_trattamento(self):
        """Compatibilità con il vecchio codice che leggeva info.label/zincatura/verniciatura/zinc_label."""
        zinc = self.ha_zincatura
        vern = self.ha_verniciatura
        if zinc and vern:   label = "Zincatura + Verniciatura"
        elif zinc:          label = f"Zincatura {self.zincatura_label}".strip()
        elif vern:          label = "Verniciatura"
        else:               label = TIPI_TRATTAMENTO_SCHEDA.get(self.tipo_trattamento, {}).get('label', self.tipo_trattamento)
        return {
            'label':       label,
            'zincatura':   zinc,
            'zinc_label':  self.zincatura_label,
            'verniciatura': vern,
        }


class MaterialePrimo(db.Model):
    """
    DEPRECATO: prima della connessione a MasterLogistic, era l'anagrafica
    materiali locale di IronProduction. Ora l'anagrafica/giacenze vive in
    MasterLogistic e si legge tramite ArticoloML — questa tabella resta
    definita (per non rompere eventuali dati già presenti) ma non è più
    alimentata dal blueprint magazzino.
    """
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
TIPI_APPROVVIGIONAMENTO = {
    'MATERIA_PRIMA_FORNITORE',
    'COMPONENTE_ACQUISTO',
    'LASERATO',
    'DA_CLASSIFICARE',
}

class ArticoloApprovvigionamento(db.Model):
    """Classificazione acquisti per ogni SKU, indipendente dalle schede Kanban."""
    __tablename__ = 'articoli_approvvigionamento'
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(100), nullable=False, unique=True, index=True)
    tipo_approvvigionamento = db.Column(db.String(40), default='DA_CLASSIFICARE', nullable=False)
    lead_time_fornitura_giorni = db.Column(db.Float, default=None)
    costo_acquisto_standard = db.Column(db.Float, default=None)  # €/pz — NULL = non configurato (non 0!). Base del costo standard per i codici foglia (materia prima/componente acquisto/laserato): niente BOM/routing sotto, il costo standard è questo prezzo
    # UoM canonica locale per SKU: la stessa usata da distinta, parametri e giacenza.
    unita_misura = db.Column(db.String(20), default='', nullable=False)
    aggiornato_il = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def assicura_unita_misura_articoli():
    """Migrazione compatibile con DB già esistenti: aggiunge la UoM senza ricreare tabelle."""
    db_url = os.environ.get('DATABASE_URL', '')
    if 'postgresql' in db_url or 'postgres' in db_url:
        try:
            db.session.execute(text("ALTER TABLE articoli_approvvigionamento ADD COLUMN IF NOT EXISTS unita_misura VARCHAR(20) NOT NULL DEFAULT ''"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    else:
        colonne = {c['name'] for c in inspect(db.engine).get_columns('articoli_approvvigionamento')}
        if 'unita_misura' not in colonne:
            db.session.execute(text("ALTER TABLE articoli_approvvigionamento ADD COLUMN unita_misura VARCHAR(20) NOT NULL DEFAULT ''"))
            db.session.commit()

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
    # Approvvigionamento: il laserato è un acquisto con lead time proprio, non una fase terzista.
    tipo_approvvigionamento = db.Column(db.String(40), default='DA_CLASSIFICARE', nullable=False)
    lead_time_fornitura_giorni = db.Column(db.Float, default=None)
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
        # Per gli articoli acquistati usa il lead time del fornitore, se impostato.
        RT = self.lead_time_fornitura_giorni if self.lead_time_fornitura_giorni is not None else self.lead_time_giorni
        RT = RT or 0
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
# STORICO PRODUZIONE — per articolo Kanban, per mese/anno
# Alimentato da: import CSV manuale + accumulo automatico dal 01/07/2026
# ═══════════════════════════════════════════════════════════════════════════════
class StoricoProduzione(db.Model):
    __tablename__ = "storico_produzione"
    id           = db.Column(db.Integer, primary_key=True)
    kanban_id    = db.Column(db.Integer, db.ForeignKey("kanban_prodotti.id", ondelete="CASCADE"), nullable=False)
    anno         = db.Column(db.Integer, nullable=False)   # es. 2025, 2026
    mese         = db.Column(db.Integer, nullable=False)   # 1-12
    qta_import   = db.Column(db.Integer, default=0)        # inserita manualmente via CSV
    qta_auto     = db.Column(db.Integer, default=0)        # accumulata automaticamente dal sistema
    aggiornato_il= db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('kanban_id', 'anno', 'mese', name='uq_storico_produzione'),)

    @property
    def qta_totale(self):
        return (self.qta_import or 0) + (self.qta_auto or 0)

def storico_aggiungi_auto(kanban_id, delta_pezzi, anno=None, mese=None):
    """
    Chiamare ogni volta che verniciati aumenta su un KanbanProdotto.
    delta_pezzi = differenza positiva (nuovi pezzi prodotti finiti).
    Accumula in qta_auto per il mese/anno corrente.
    """
    if delta_pezzi <= 0:
        return
    ora = datetime.utcnow()
    anno = anno or ora.year
    mese = mese or ora.month
    try:
        riga = StoricoProduzione.query.filter_by(
            kanban_id=kanban_id, anno=anno, mese=mese
        ).first()
        if riga:
            riga.qta_auto = (riga.qta_auto or 0) + delta_pezzi
            riga.aggiornato_il = ora
        else:
            db.session.add(StoricoProduzione(
                kanban_id=kanban_id, anno=anno, mese=mese,
                qta_import=0, qta_auto=delta_pezzi
            ))
    except Exception:
        pass

def storico_get(kanban_id):
    """
    Ritorna dict strutturato per la scheda:
    {
      'anno_corrente': int,
      'anno_precedente': int,
      'mesi_corrente': [{'mese':1,'qta':N}, ...],   # tutti 12 mesi anche se 0
      'tot_corrente': N,
      'tot_precedente': N,
    }
    """
    ora = datetime.utcnow()
    anno_c = ora.year
    anno_p = ora.year - 1
    mesi_labels = ['Gen','Feb','Mar','Apr','Mag','Giu','Lug','Ago','Set','Ott','Nov','Dic']

    righe_c = {r.mese: r.qta_totale for r in
               StoricoProduzione.query.filter_by(kanban_id=kanban_id, anno=anno_c).all()}
    righe_p = {r.mese: r.qta_totale for r in
               StoricoProduzione.query.filter_by(kanban_id=kanban_id, anno=anno_p).all()}

    mesi_corrente = [
        {'mese': m, 'label': mesi_labels[m-1], 'qta': righe_c.get(m, 0)}
        for m in range(1, 13)
    ]
    mesi_precedente = [
        {'mese': m, 'label': mesi_labels[m-1], 'qta': righe_p.get(m, 0)}
        for m in range(1, 13)
    ]
    return {
        'anno_corrente':   anno_c,
        'anno_precedente': anno_p,
        'mesi_corrente':   mesi_corrente,
        'mesi_precedente': mesi_precedente,
        'tot_corrente':    sum(v['qta'] for v in mesi_corrente),
        'tot_precedente':  sum(v['qta'] for v in mesi_precedente),
    }

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


class SequenzaMonitorMacchina(db.Model):
    """
    Posizione scelta A MANO dal capo per un Ordine di Produzione nella coda di
    UNA specifica macchina (centro di costo) — i Monitor Macchina (vedi
    blueprints/monitor) NON sono più righe inserite a mano: gli OP e il loro
    stato di avanzamento vengono estratti da OrdineProduzione + CicloLavoroWood
    + EventoConsuntivoPP. Questa tabella serve SOLO per lasciare al capo la
    possibilità di riordinare manualmente la coda di una macchina (es. mettere
    in cima un OP urgente) — se un OP non ha una riga qui, va in coda con un
    ordine di default (priorità OP, poi data di consegna).
    """
    __tablename__ = 'sequenza_monitor_macchina'
    id                    = db.Column(db.Integer, primary_key=True)
    ordine_produzione_id  = db.Column(db.Integer, db.ForeignKey('ordini_produzione_pp.id'), nullable=False)
    centro_costo_id       = db.Column(db.Integer, db.ForeignKey('centri_costo_wood.id'), nullable=False)
    posizione             = db.Column(db.Integer, default=0)   # più basso = più in alto in coda
    aggiornato_il         = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('ordine_produzione_id', 'centro_costo_id', name='_op_centro_seq_uc'),)


def get_macchine_monitor():
    """
    Macchine (centri di costo) da mostrare nel Monitor: solo quelle REALMENTE
    usate in almeno un Ciclo di Lavoro (righe di CicloLavoroWood) e non
    esterne — una lavorazione esterna non è una macchina che il capo mette
    in coda internamente. Alimenta sia la sidebar sia la pagina /monitor.
    """
    righe = (db.session.query(CentroCostoWood.id, CentroCostoWood.nome)
             .join(CicloLavoroWood, CicloLavoroWood.centro_costo_id == CentroCostoWood.id)
             .filter(CentroCostoWood.esterno == False, CentroCostoWood.escluso_da_monitor_produzione == False)
             .distinct().order_by(CentroCostoWood.nome).all())
    return [{'id': r.id, 'nome': r.nome} for r in righe]


class SessioneLavoroMacchina(db.Model):
    """
    Sessione di lavoro INIZIO/FINE avviata dall'operatore dal totem a bordo
    macchina. Una sola sessione APERTA (terminata_il IS NULL) per centro di
    costo alla volta: una macchina fisica lavora un OP alla volta. Alla
    chiusura genera l'EventoConsuntivoPP corrispondente (stesso motore usato
    dall'integrazione MasterWork in /api/pp/events, via _registra_evento_consuntivo).
    """
    __tablename__ = 'sessioni_lavoro_macchina'
    id                    = db.Column(db.Integer, primary_key=True)
    ordine_produzione_id  = db.Column(db.Integer, db.ForeignKey('ordini_produzione_pp.id'), nullable=False)
    centro_costo_id       = db.Column(db.Integer, db.ForeignKey('centri_costo_wood.id'), nullable=False)
    # Codice del componente specifico lavorato in questa sessione — NULL =
    # prodotto finito/assieme finale dell'OP (comportamento storico). Serve
    # perché lo stesso OP può avere PIÙ componenti diversi che passano dalla
    # STESSA macchina prima dell'assemblaggio finale (vedi EventoConsuntivoPP.componente).
    componente            = db.Column(db.String(50), nullable=True)
    iniziata_il           = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    terminata_il          = db.Column(db.DateTime, nullable=True)
    pezzi_buoni           = db.Column(db.Integer, nullable=True)
    pezzi_scarto          = db.Column(db.Integer, nullable=True)
    event_id_generato     = db.Column(db.String(100), nullable=True)   # collega all'EventoConsuntivoPP creato alla chiusura
    ordine_produzione     = db.relationship('OrdineProduzione')
    centro_costo          = db.relationship('CentroCostoWood')


class DocumentoTecnicoArticolo(db.Model):
    """
    Documentazione tecnica (disegno, istruzione di lavoro, PDF) legata a un
    codice articolo — mostrata sul totem quando quell'articolo è in
    lavorazione. Il file è salvato come base64 direttamente nel DB (niente
    filesystem separato da gestire/perdere ai redeploy).
    """
    __tablename__ = 'documenti_tecnici_articolo'
    id                = db.Column(db.Integer, primary_key=True)
    codice_articolo   = db.Column(db.String(100), nullable=False, index=True)
    nome_file         = db.Column(db.String(255), nullable=False)
    tipo_mime         = db.Column(db.String(100), default='application/octet-stream')
    contenuto_base64  = db.Column(db.Text, nullable=False)
    note              = db.Column(db.String(300), default='')
    caricato_il       = db.Column(db.DateTime, default=datetime.utcnow)


class FotoArticolo(db.Model):
    """
    Foto di RIFERIMENTO del prodotto (com'è fatto, non come sta andando una
    lavorazione specifica) — legata solo al codice_articolo, non a un OP:
    resta archiviata e consultabile sempre, indipendentemente da quale OP è
    in corso. Diversa da FotoLavorazioneMacchina (quella è per OP+macchina,
    documenta l'andamento di UN lotto specifico).
    """
    __tablename__ = 'foto_articolo'
    id                = db.Column(db.Integer, primary_key=True)
    codice_articolo   = db.Column(db.String(100), nullable=False, index=True)
    nome_file         = db.Column(db.String(255), nullable=False)
    contenuto_base64  = db.Column(db.Text, nullable=False)
    note              = db.Column(db.String(300), default='')
    caricato_il       = db.Column(db.DateTime, default=datetime.utcnow)


class FotoLavorazioneMacchina(db.Model):
    """
    Foto scattate dall'operatore dal totem durante la lavorazione (come da
    cellulare) — legate a un OP + centro di costo specifico. Salvate come
    base64 nel DB per lo stesso motivo di DocumentoTecnicoArticolo sopra.
    """
    __tablename__ = 'foto_lavorazione_macchina'
    id                    = db.Column(db.Integer, primary_key=True)
    ordine_produzione_id  = db.Column(db.Integer, db.ForeignKey('ordini_produzione_pp.id'), nullable=False, index=True)
    centro_costo_id       = db.Column(db.Integer, db.ForeignKey('centri_costo_wood.id'), nullable=False)
    nome_file             = db.Column(db.String(255), nullable=False)
    contenuto_base64      = db.Column(db.Text, nullable=False)
    caricato_il           = db.Column(db.DateTime, default=datetime.utcnow)

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
        'tipo_approvvigionamento': p.tipo_approvvigionamento or 'DA_CLASSIFICARE',
        'lead_time_fornitura_giorni': p.lead_time_fornitura_giorni,
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
    db.create_all(bind_key=None)   # solo il DB locale — mai il bind 'masterlogistic'
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
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS tipo_approvvigionamento VARCHAR(40) DEFAULT 'DA_CLASSIFICARE'",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS lead_time_fornitura_giorni FLOAT",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS takt_time_min FLOAT",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS domanda_giornaliera FLOAT DEFAULT 0",
        "ALTER TABLE kanban_prodotti ADD COLUMN IF NOT EXISTS n_kanban_suggerito INTEGER DEFAULT 0",
        "ALTER TABLE monitor_righe ADD COLUMN IF NOT EXISTS ordine INTEGER DEFAULT 0",
        "ALTER TABLE commesse ADD COLUMN IF NOT EXISTS ref_masterlogistic VARCHAR(100) DEFAULT ''",
        "ALTER TABLE lavorazioni_terziste ADD COLUMN IF NOT EXISTS costo FLOAT DEFAULT 0",
        # ── Rende riga_id e terzista_id nullable (DDT senza commessa collegata) ──
        "ALTER TABLE lavorazioni_terziste ALTER COLUMN riga_id DROP NOT NULL",
        "ALTER TABLE lavorazioni_terziste ALTER COLUMN terzista_id DROP NOT NULL",
        "CREATE TABLE IF NOT EXISTS storico_produzione (id SERIAL PRIMARY KEY, kanban_id INTEGER REFERENCES kanban_prodotti(id) ON DELETE CASCADE, anno INTEGER NOT NULL, mese INTEGER NOT NULL, qta_import INTEGER DEFAULT 0, qta_auto INTEGER DEFAULT 0, aggiornato_il TIMESTAMP, UNIQUE(kanban_id, anno, mese))",
        # ── Scheda Trattamento: flag indipendenti zincatura/verniciatura (sostituiscono il vecchio menu a tendina unico) ──
        "ALTER TABLE schede_trattamenti ADD COLUMN IF NOT EXISTS zincatura BOOLEAN DEFAULT FALSE",
        "ALTER TABLE schede_trattamenti ADD COLUMN IF NOT EXISTS zincatura_tipo VARCHAR(10) DEFAULT ''",
        "ALTER TABLE schede_trattamenti ADD COLUMN IF NOT EXISTS verniciatura BOOLEAN DEFAULT FALSE",
        "UPDATE schede_trattamenti SET zincatura=TRUE, zincatura_tipo='CALDO' WHERE tipo_trattamento='ZINCATURA_CALDO' AND zincatura IS NOT TRUE",
        "UPDATE schede_trattamenti SET zincatura=TRUE, zincatura_tipo='FREDDO' WHERE tipo_trattamento='ZINCATURA_FREDDO' AND zincatura IS NOT TRUE",
        "UPDATE schede_trattamenti SET zincatura=TRUE, verniciatura=TRUE WHERE tipo_trattamento='ZINCATURA_VERNICIATURA' AND (zincatura IS NOT TRUE OR verniciatura IS NOT TRUE)",
        "UPDATE schede_trattamenti SET verniciatura=TRUE WHERE tipo_trattamento='VERNICIATURA' AND verniciatura IS NOT TRUE",
        # ── Centro di Costo Iron Wood: costo orario macchina/reparto (senza manodopera) ──
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS costo_orario DOUBLE PRECISION DEFAULT 0",
        # ── Costo Standard Iron Wood: prezzo acquisto per i codici foglia, valorizzazione carichi/scarichi ──
        "ALTER TABLE articoli_approvvigionamento ADD COLUMN IF NOT EXISTS costo_acquisto_standard DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE movimenti_giacenza_wood ADD COLUMN IF NOT EXISTS costo_unitario DOUBLE PRECISION",
        "ALTER TABLE movimenti_giacenza_wood ADD COLUMN IF NOT EXISTS valore DOUBLE PRECISION",
        "ALTER TABLE costo_standard_wood ADD COLUMN IF NOT EXISTS ore_manodopera_standard DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_wood ADD COLUMN IF NOT EXISTS completo BOOLEAN DEFAULT TRUE",
        "ALTER TABLE costo_standard_wood ADD COLUMN IF NOT EXISTS codici_senza_costo TEXT DEFAULT ''",
        # ── Costo Standard: manodopera diretta separata + overhead a due basi (stile SAP) ──
        "ALTER TABLE impostazioni_costo_wood ADD COLUMN IF NOT EXISTS aliquota_overhead_materiali_pct DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE impostazioni_costo_wood ADD COLUMN IF NOT EXISTS aliquota_overhead_produzione_pct DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_wood ADD COLUMN IF NOT EXISTS costo_manodopera DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_wood ADD COLUMN IF NOT EXISTS costo_overhead_materiali DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_wood ADD COLUMN IF NOT EXISTS costo_overhead_produzione DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_versioni_wood ADD COLUMN IF NOT EXISTS costo_manodopera DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_versioni_wood ADD COLUMN IF NOT EXISTS costo_overhead_materiali DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_versioni_wood ADD COLUMN IF NOT EXISTS costo_overhead_produzione DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_versioni_wood ADD COLUMN IF NOT EXISTS overhead_materiali_pct_usata DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE costo_standard_versioni_wood ADD COLUMN IF NOT EXISTS overhead_produzione_pct_usata DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE varianze_produzione_wood ADD COLUMN IF NOT EXISTS costo_standard_manodopera DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE varianze_produzione_wood ADD COLUMN IF NOT EXISTS costo_reale_manodopera DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE varianze_produzione_wood ADD COLUMN IF NOT EXISTS varianza_manodopera DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE varianze_produzione_wood ADD COLUMN IF NOT EXISTS varianza_tariffa_lavorazione DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE varianze_produzione_wood ADD COLUMN IF NOT EXISTS varianza_tariffa_manodopera DOUBLE PRECISION DEFAULT 0",
        # ── Centro di Costo: anagrafica estesa e capacità/driver (Configura) ──
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS reparto_gruppo VARCHAR(100) DEFAULT ''",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS attivo BOOLEAN DEFAULT TRUE",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS fornitore_esterno VARCHAR(200) DEFAULT ''",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS tariffa_esterna DOUBLE PRECISION",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS driver_attivita VARCHAR(20) DEFAULT 'ora_reparto'",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS n_risorse_equivalenti DOUBLE PRECISION DEFAULT 1",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS ore_teoriche_periodo DOUBLE PRECISION DEFAULT 0",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS pct_efficienza DOUBLE PRECISION DEFAULT 100",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS periodo_riferimento VARCHAR(10) DEFAULT 'mensile'",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS tariffa_manodopera_diretta_oraria DOUBLE PRECISION DEFAULT 0",
        # ── Distinta Base Wood: componenti alternativi (es. barra 7m/6m) ──
        "ALTER TABLE distinta_base_wood ADD COLUMN IF NOT EXISTS gruppo_alternativa VARCHAR(50)",
        "ALTER TABLE distinta_base_wood ADD COLUMN IF NOT EXISTS preferita BOOLEAN DEFAULT TRUE",
        "ALTER TABLE distinta_base_wood ADD COLUMN IF NOT EXISTS pezzi_per_barra DOUBLE PRECISION",
        "ALTER TABLE distinta_base_wood ADD COLUMN IF NOT EXISTS sviluppo VARCHAR(50)",
        # ── Ordini di Acquisto Wood: prezzo estratto dal PDF + codice fornitore originale (prima della mappa) ──
        "ALTER TABLE righe_ordine_acquisto_wood ADD COLUMN IF NOT EXISTS prezzo_unitario DOUBLE PRECISION",
        "ALTER TABLE righe_ordine_acquisto_wood ADD COLUMN IF NOT EXISTS codice_fornitore_originale VARCHAR(100) DEFAULT ''",
        "ALTER TABLE ciclo_lavoro_wood ADD COLUMN IF NOT EXISTS scarto_max_pct DOUBLE PRECISION",
        "ALTER TABLE centri_costo_wood ADD COLUMN IF NOT EXISTS escluso_da_monitor_produzione BOOLEAN DEFAULT FALSE",
        # ── Consuntivi per singolo componente della distinta base (non solo per l'assieme finale dell'OP) ──
        "ALTER TABLE pp_eventi_consuntivi ADD COLUMN IF NOT EXISTS componente VARCHAR(50)",
        "ALTER TABLE sessioni_lavoro_macchina ADD COLUMN IF NOT EXISTS componente VARCHAR(50)",
        "ALTER TABLE schede_lavorazione_wood ADD COLUMN IF NOT EXISTS spessore_mm DOUBLE PRECISION",
        # ── Dichiarazione di Produzione: approvazione Direzione ──
        "ALTER TABLE pp_eventi_consuntivi ADD COLUMN IF NOT EXISTS approvato_direzione BOOLEAN DEFAULT FALSE",
        "ALTER TABLE pp_eventi_consuntivi ADD COLUMN IF NOT EXISTS approvato_il TIMESTAMP",
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


# ── Ordini di produzione / integrazione MasterWork ───────────────────────────
STATI_ORDINE_PP = ["Creato", "Rilasciato", "In esecuzione", "Tecnicamente completato", "Chiuso CO"]
ASA_MASTERWORK = "Carpenteria Propria"

class SequenzaOrdineProduzione(db.Model):
    __tablename__ = "pp_sequenze"
    anno = db.Column(db.Integer, primary_key=True)
    ultimo_numero = db.Column(db.Integer, nullable=False, default=0)

class OrdineProduzione(db.Model):
    __tablename__ = "ordini_produzione_pp"
    id = db.Column(db.Integer, primary_key=True)
    codice = db.Column(db.String(20), nullable=False, unique=True, index=True)
    codice_articolo = db.Column(db.String(100), nullable=False)
    descrizione = db.Column(db.String(300), default="")
    cliente = db.Column(db.String(200), default="")
    commessa = db.Column(db.String(100), default="")
    # Retrocompatibilità con l'anteprima P0: valore leggibile da vecchi client.
    cliente_commessa_esterna = db.Column(db.String(300), default="")
    qta_pianificata = db.Column(db.Integer, nullable=False, default=0)
    qta_buona = db.Column(db.Integer, nullable=False, default=0)
    qta_scarto = db.Column(db.Integer, nullable=False, default=0)
    tempo_consuntivo_minuti = db.Column(db.Integer, nullable=False, default=0)
    stato = db.Column(db.String(40), nullable=False, default="Creato")
    asa = db.Column(db.String(100), nullable=False, default="")
    priorita = db.Column(db.Integer, nullable=False, default=5)
    data_inizio = db.Column(db.Date, nullable=True)
    data_prevista = db.Column(db.Date, nullable=True)  # consegna/fine pianificata
    data_rilascio = db.Column(db.DateTime, nullable=True)
    data_completamento = db.Column(db.DateTime, nullable=True)
    data_chiusura_co = db.Column(db.DateTime, nullable=True)
    creato_il = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    aggiornato_il = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class EventoConsuntivoPP(db.Model):
    __tablename__ = "pp_eventi_consuntivi"
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
    op_code = db.Column(db.String(20), nullable=False, index=True)
    fase = db.Column(db.String(100), nullable=False)
    # Codice del componente REALMENTE lavorato in questa fase — NULL (o uguale
    # al codice_articolo dell'OP) = si tratta del prodotto finito/assieme
    # finale dell'OP, comportamento storico invariato. Se l'OP passa da più
    # macchine PRIMA dell'assemblaggio finale (es. 3 componenti diversi
    # lavorati alla segatrice prima di diventare T200), qui c'è il codice di
    # quel componente — permette al Monitor di distinguere l'avanzamento
    # dei singoli componenti dello stesso OP sulla stessa macchina.
    componente = db.Column(db.String(50), nullable=True)
    timestamp_evento = db.Column(db.DateTime, nullable=False)
    pezzi_buoni = db.Column(db.Integer, nullable=False, default=0)
    pezzi_scarto = db.Column(db.Integer, nullable=False, default=0)
    tempo_minuti = db.Column(db.Integer, nullable=False, default=0)
    ricevuto_il = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Approvazione Direzione (PIN separato da quello del capo reparto) — la
    # dichiarazione viene SEMPRE registrata subito negli ordini/giacenza
    # (comportamento invariato), questo flag è solo un controllo successivo
    # che la Direzione deve dare: non blocca né ritarda la produzione.
    approvato_direzione = db.Column(db.Boolean, nullable=False, default=False)
    approvato_il = db.Column(db.DateTime, nullable=True)

class AuditPP(db.Model):
    __tablename__ = "pp_audit"
    id = db.Column(db.Integer, primary_key=True)
    op_code = db.Column(db.String(20), default="")
    event_id = db.Column(db.String(100), default="")
    azione = db.Column(db.String(80), nullable=False)
    dettaglio = db.Column(db.Text, default="")
    creato_il = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

def prossimo_codice_ordine_pp():
    """Restituisce OP-YYYY-000001. Su PostgreSQL serializza anche la prima riga annuale."""
    anno = datetime.utcnow().year
    if db.engine.dialect.name == "postgresql":
        # lock transazionale deterministico: evita la gara sull'INSERT della prima sequenza.
        db.session.execute(db.text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 505000000 + anno})
    seq = SequenzaOrdineProduzione.query.filter_by(anno=anno).with_for_update().first()
    if seq is None:
        seq = SequenzaOrdineProduzione(anno=anno, ultimo_numero=0)
        db.session.add(seq)
        db.session.flush()
    seq.ultimo_numero += 1
    db.session.flush()
    return f"OP-{anno}-{seq.ultimo_numero:06d}"


class OrdineAcquistoWood(db.Model):
    """
    Ordine di acquisto a un fornitore per materiali Iron Wood (materia
    prima, componenti d'acquisto, laserati...) — caricato da PDF fornitore
    e parsato automaticamente (clone del sistema di MasterLogistic-WMS,
    adattato). Vive nel DB locale (Postgres): il PDF stesso è salvato come
    blob in 'pdf_bytes', NON su file — su Railway i file locali vengono
    cancellati ad ogni redeploy, un DB no.
    """
    __tablename__ = 'ordini_acquisto_wood'
    id                = db.Column(db.Integer, primary_key=True)
    filename          = db.Column(db.String(300), nullable=False, unique=True)  # nome originale del PDF caricato
    pdf_bytes         = db.Column(db.LargeBinary, nullable=True)   # contenuto del PDF, per riaprirlo/riscaricarlo
    fornitore         = db.Column(db.String(200), default='Sconosciuto')
    ordine_n          = db.Column(db.String(50), default='N/D')
    rif_fornitore     = db.Column(db.String(100), default='')
    data_ordine       = db.Column(db.Date, nullable=True)
    data_consegna     = db.Column(db.Date, nullable=True)    # data comune di evasione prevista (modificabile a mano)
    ritiro_proprio    = db.Column(db.Boolean, default=False)  # True = ritiriamo noi, False = consegnano loro
    confermato        = db.Column(db.Boolean, default=False)
    stato_label       = db.Column(db.String(30), default='DA_CONFERMARE')
    # DA_CONFERMARE / ORDINE_CONFERMATO / ORDINE_IN_ARRIVO / ORDINE_DA_RITIRARE / ORDINE_RICEVUTO
    nota_interna      = db.Column(db.String(500), default='')
    testo_grezzo_pdf  = db.Column(db.Text, default='')   # testo estratto da PyPDF2, per debug/riparsing futuro
    caricato_il       = db.Column(db.DateTime, default=datetime.utcnow)
    aggiornato_il     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RigaOrdineAcquistoWood(db.Model):
    """Riga articolo di un Ordine di Acquisto Iron Wood, con quantità ordinata e ricevuta."""
    __tablename__ = 'righe_ordine_acquisto_wood'
    id             = db.Column(db.Integer, primary_key=True)
    ordine_id      = db.Column(db.Integer, db.ForeignKey('ordini_acquisto_wood.id'), nullable=False, index=True)
    codice         = db.Column(db.String(100), nullable=False)   # codice interno, dopo risoluzione via MappaCodiceFornitoreWood (se mappato)
    codice_fornitore_originale = db.Column(db.String(100), default='')  # codice così come scritto sul PDF, PRIMA della risoluzione — vuoto se coincide con 'codice' (nessuna mappa trovata/necessaria)
    descrizione    = db.Column(db.String(300), default='')
    unita_misura   = db.Column(db.String(10), default='')
    qta_originale  = db.Column(db.Float, default=0)
    qta_ricevuta   = db.Column(db.Float, default=0)
    prezzo_unitario = db.Column(db.Float, nullable=True)   # €/unità estratto dal PDF ordine — None = non trovato dal parsing, va inserito a mano
    data_evasione  = db.Column(db.String(20), default='')   # come estratta dal PDF, stringa gg/mm/aaaa
    ordine = db.relationship('OrdineAcquistoWood', backref='righe')


class MappaCodiceFornitoreWood(db.Model):
    """
    Mappa codice-articolo-del-fornitore -> codice interno IronProduction.
    Serve perché ogni fornitore scrive sui propri PDF (ordini/DDT) il PROPRIO
    codice articolo, non necessariamente lo SKU interno (ArticoloML). Se per
    una coppia (fornitore, codice_fornitore) esiste una riga qui, il parsing
    usa 'codice_interno' al posto del codice grezzo letto dal PDF; altrimenti
    (nessuna riga trovata) si usa il codice grezzo così com'è — comportamento
    identico a prima, nessuna rottura per i fornitori non ancora mappati.
    """
    __tablename__ = 'mappa_codici_fornitore_wood'
    id               = db.Column(db.Integer, primary_key=True)
    fornitore        = db.Column(db.String(200), nullable=False)
    codice_fornitore = db.Column(db.String(100), nullable=False)
    codice_interno   = db.Column(db.String(50), nullable=False)
    note             = db.Column(db.String(200), default='')
    creato_il        = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('fornitore', 'codice_fornitore', name='_fornitore_codice_uc'),)


class ManodoperaRealeWood(db.Model):
    """
    PLACEHOLDER — manodopera reale consuntivata per Ordine di Produzione.
    In attesa dell'integrazione con MasterWork (che ha i consuntivi orari
    reali): 'ore_reali' resta NULL e 'fonte' resta 'placeholder' finché
    l'integrazione non viene collegata. Creato già ora per avere la tabella
    pronta ed evitare un'altra migrazione quando si collega MasterWork.
    """
    __tablename__ = 'manodopera_reale_wood'
    id                   = db.Column(db.Integer, primary_key=True)
    ordine_produzione_id = db.Column(db.Integer, db.ForeignKey('ordini_produzione_pp.id'), nullable=False, index=True)
    ore_reali            = db.Column(db.Float, nullable=True)   # None = non ancora valorizzato
    fonte                = db.Column(db.String(30), default='placeholder')  # 'placeholder' / 'masterwork' (quando collegato davvero)
    note                 = db.Column(db.String(200), default='')
    creato_il            = db.Column(db.DateTime, default=datetime.utcnow)
    ordine = db.relationship('OrdineProduzione')


class DDTCaricoWood(db.Model):
    """
    DDT di carico (arrivo merce) da fornitore per materiali Iron Wood —
    caricato da PDF e parsato automaticamente. Ogni riga viene abbinata, se
    possibile, alla riga corrispondente di un OrdineAcquistoWood (tramite il
    numero d'ordine di riferimento presente nel DDT), aggiornando la
    quantità ricevuta e caricando la Giacenza Iron Wood in automatico.
    """
    __tablename__ = 'ddt_carico_wood'
    id               = db.Column(db.Integer, primary_key=True)
    filename         = db.Column(db.String(300), nullable=False, unique=True)
    pdf_bytes        = db.Column(db.LargeBinary, nullable=True)
    fornitore        = db.Column(db.String(200), default='')
    numero_ddt       = db.Column(db.String(50), default='')
    data_ddt         = db.Column(db.String(20), default='')   # gg/mm/aaaa come estratta dal PDF
    testo_grezzo_pdf = db.Column(db.Text, default='')
    caricato_il      = db.Column(db.DateTime, default=datetime.utcnow)


class RigaDDTCaricoWood(db.Model):
    """Riga articolo di un DDT di carico — collegata (se trovata) a una riga di Ordine di Acquisto."""
    __tablename__ = 'righe_ddt_carico_wood'
    id                    = db.Column(db.Integer, primary_key=True)
    ddt_id                = db.Column(db.Integer, db.ForeignKey('ddt_carico_wood.id'), nullable=False, index=True)
    ordine_n_riferimento  = db.Column(db.String(50), default='')   # da "Ns. doc.(ORDFO) n.:" nel PDF
    ordine_acquisto_id    = db.Column(db.Integer, db.ForeignKey('ordini_acquisto_wood.id'), nullable=True)
    codice                = db.Column(db.String(100), nullable=False)
    descrizione           = db.Column(db.String(300), default='')
    quantita              = db.Column(db.Float, default=0)
    abbinata              = db.Column(db.Boolean, default=False)   # True = trovata una riga OA corrispondente, aggiornata
    ddt = db.relationship('DDTCaricoWood', backref='righe')
    ordine_acquisto = db.relationship('OrdineAcquistoWood')


class SequenzaCommessa(db.Model):
    """Contatore separato da SequenzaOrdineProduzione: numera la COMMESSA
    (non il codice OP interno), formato YYMMNNNN — vedi prossimo_numero_commessa()."""
    __tablename__ = "pp_sequenze_commessa"
    anno = db.Column(db.Integer, primary_key=True)
    ultimo_numero = db.Column(db.Integer, nullable=False, default=0)


def prossimo_numero_commessa():
    """
    Restituisce il numero commessa progressivo nel formato YYMMNNNN, es. 26070001:
    - YY = ultime 2 cifre dell'anno di apertura
    - MM = mese di apertura (informativo, non azzera il contatore)
    - NNNN = progressivo a 4 cifre, che riparte da 0001 solo a inizio anno nuovo
    """
    ora = datetime.utcnow()
    anno = ora.year
    if db.engine.dialect.name == "postgresql":
        db.session.execute(db.text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 506000000 + anno})
    seq = SequenzaCommessa.query.filter_by(anno=anno).with_for_update().first()
    if seq is None:
        seq = SequenzaCommessa(anno=anno, ultimo_numero=0)
        db.session.add(seq)
        db.session.flush()
    seq.ultimo_numero += 1
    db.session.flush()
    return f"{anno % 100:02d}{ora.month:02d}{seq.ultimo_numero:04d}"

def inizializza_schema_pp():
    """Completa in avvio le colonne introdotte dopo P0; non richiede migration manuale."""
    # create_all è già eseguito dall'app. Questi ALTER servono solo DB già avviati con P0.
    additions = {
        "cliente": "VARCHAR(200) DEFAULT ''", "commessa": "VARCHAR(100) DEFAULT ''",
        "tempo_consuntivo_minuti": "INTEGER DEFAULT 0", "priorita": "INTEGER DEFAULT 5",
        "data_inizio": "DATE", "data_chiusura_co": "TIMESTAMP",
    }
    event_additions = {"tempo_minuti": "INTEGER DEFAULT 0"}
    dialect = db.engine.dialect.name
    for table, cols in (("ordini_produzione_pp", additions), ("pp_eventi_consuntivi", event_additions)):
        for col, definition in cols.items():
            try:
                if dialect == "postgresql":
                    db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition}"))
                else:  # SQLite: ADD COLUMN IF NOT EXISTS non è disponibile in tutte le versioni
                    db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))
                db.session.commit()
            except Exception:
                db.session.rollback()  # colonna già presente o DB non ancora inizializzato

