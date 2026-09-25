import os

class Config:
    _db = os.environ.get('DATABASE_URL', 'sqlite:///masterproduction.db')
    if _db.startswith('postgres://'):
        _db = _db.replace('postgres://', 'postgresql://', 1)
    # BUG REALE TROVATO E CORRETTO (programma in crash totale al boot:
    # "ModuleNotFoundError: No module named 'psycopg'"): Railway ha
    # ricominciato a fornire DATABASE_URL con lo schema
    # "postgresql+psycopg://" (driver psycopg v3, esplicito), invece del
    # semplice "postgresql://" di sempre — ma qui è installato solo
    # psycopg2-binary (v2), mai psycopg v3. SQLAlchemy legge lo schema
    # dell'URL e prova a importare ESATTAMENTE il driver richiesto lì
    # dentro, quindi con "+psycopg" tenta "import psycopg" (v3) e fallisce
    # sempre, ovunque, ad ogni avvio — non un problema di dati, un
    # mismatch tra cosa chiede l'URL e cosa è davvero installato. Si
    # forza lo schema a puntare sempre al driver psycopg2 già installato
    # e già funzionante, indipendentemente da quale schema Railway decida
    # di fornire in futuro.
    if _db.startswith('postgresql+psycopg://') or _db.startswith('postgresql+psycopg3://'):
        _db = _db.replace('postgresql+psycopg://', 'postgresql+psycopg2://', 1).replace('postgresql+psycopg3://', 'postgresql+psycopg2://', 1)
    SQLALCHEMY_DATABASE_URI = _db
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mes-carpenteria-dev-2024')

    # ══════════════════════════════════════════════════════════════════════
    #  BIND VERSO IL DB DI MASTERLOGISTIC (stesso progetto Railway, Postgres
    #  separato). Sola lettura — IronProduction non crea/altera nulla lì
    #  (vedi ArticoloML in models.py e db.create_all(bind_key=None) in app.py).
    #  Su Railway: variabile MASTERLOGISTIC_DATABASE_URL = connection string
    #  INTERNA (*.railway.internal) del plugin Postgres di MasterLogistic —
    #  si trova tra le variabili d'ambiente del servizio MasterLogistic.
    # ══════════════════════════════════════════════════════════════════════
    _ml_db = os.environ.get('MASTERLOGISTIC_DATABASE_URL', '')
    if _ml_db.startswith('postgres://'):
        _ml_db = _ml_db.replace('postgres://', 'postgresql://', 1)
    # Stessa normalizzazione psycopg v3 -> v2 di sopra, applicata anche qui:
    # stesso identico crash si presenterebbe altrimenti anche su questo bind.
    if _ml_db.startswith('postgresql+psycopg://') or _ml_db.startswith('postgresql+psycopg3://'):
        _ml_db = _ml_db.replace('postgresql+psycopg://', 'postgresql+psycopg2://', 1).replace('postgresql+psycopg3://', 'postgresql+psycopg2://', 1)
    SQLALCHEMY_BINDS = {'masterlogistic': _ml_db} if _ml_db else {}

    # Token Bearer obbligatorio per le API PP; lasciare vuoto disabilita le API.
    PP_API_TOKEN = os.environ.get('PP_API_TOKEN', '')

    # URL di MasterWork, per la direzione OPPOSTA rispetto a PP_API_TOKEN qui
    # sopra (quello autentica le chiamate IN INGRESSO da MasterWork verso
    # IronProduction — /api/pp/events). Questo serve per chiamare MasterWork
    # da IronProduction (es. ricerca nel catalogo articoli per il widget di
    # Corrispondenze Codici MasterWork) — riusa lo STESSO PP_API_TOKEN come
    # segreto condiviso, nessun token nuovo da configurare.
    MASTERWORK_URL = os.environ.get('MASTERWORK_URL', '')

    # Token Bearer per l'integrazione con MasterLedger (Iron Segnaletica):
    # riceve qui il carico magazzino per i materiali "da officina interna"
    # (ferro, filo di saldatura, DPI...) quando arriva un'Entrata Merci.
    # Separato da PP_API_TOKEN apposta: integrazioni diverse, chiavi diverse,
    # revocabili indipendentemente. Vuoto = endpoint disabilitato.
    MASTERLEDGER_API_TOKEN = os.environ.get('MASTERLEDGER_API_TOKEN', '')

    # URL + token per notificare a MasterLogistic-WMS il carico di prodotto
    # finito (es. transenne appena verniciate, pronte alla vendita) — vedi
    # services/masterlogistic_client.py, chiamato da blueprints/kanban/
    # routes.py quando 'verniciati' aumenta. Diverso da
    # MASTERLOGISTIC_DATABASE_URL qui sopra: quello è un bind diretto al DB
    # (sola lettura, mai scrittura); questo è l'endpoint HTTP di WMS che fa
    # l'incremento atomico e ricalcola gli stati fascicoli — la scrittura
    # passa sempre da lì, mai dal bind diretto.
    MASTERLOGISTIC_URL = os.environ.get('MASTERLOGISTIC_URL', '')
    MASTERLOGISTIC_API_TOKEN = os.environ.get('MASTERLOGISTIC_API_TOKEN', '')
    # Mappatura fisica del magazzino / WIP (fase 2) — vedi masterlogistic_client.py.
    # Stesso nome ESATTO su entrambi i Railway (diverso dalla coppia sopra,
    # che invece ha nomi diversi sui due lati).
    WAREHOUSE_API_TOKEN = os.environ.get('WAREHOUSE_API_TOKEN', '')

    # URL + token per interrogare in sola lettura MasterLedgerLight
    # dall'interrogazione automatica del Kanban Gruppi (masterledgerlight_client.py,
    # chiamato da blueprints/kanban/routes.py::api_interroga_codice). Il
    # token deve combaciare con KANBAN_API_TOKEN impostato lato MasterLedgerLight.
    MASTERLEDGERLIGHT_URL = os.environ.get('MASTERLEDGERLIGHT_URL', '')
    MASTERLEDGERLIGHT_API_TOKEN = os.environ.get('MASTERLEDGERLIGHT_API_TOKEN', '')

    # PIN del CAPO REPARTO per sbloccare "Storico Correzioni" in Dichiarazione
    # di Produzione — cambialo impostando la variabile Railway CAPO_PIN se
    # vuoi un valore diverso da quello di default.
    CAPO_PIN = os.environ.get('CAPO_PIN', '1234')
