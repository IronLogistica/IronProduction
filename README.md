# MES Carpenteria v2 — Blueprint Edition

## Struttura progetto

```
mes_flask_v2/
├── app.py                          # Entry point Flask, registra i blueprint
├── config.py                       # Configurazione DB e variabili ambiente
├── models.py                       # SQLAlchemy models + helper functions
├── seed_db.py                      # Script di seed dati iniziali da Excel
├── requirements.txt
├── Procfile
├── railway.json
├── blueprints/
│   ├── monitor/routes.py           # Monitor Segatrice
│   ├── kanban/routes.py            # Kanban prodotti (tutte le categorie)
│   ├── commesse/routes.py          # Gestione commesse
│   ├── kpi/routes.py               # Cruscotto KPI
│   ├── terzisti/routes.py          # Terzisti e lavorazioni esterne
│   └── magazzino/routes.py         # Magazzino materie prime
└── templates/
    ├── base.html                   # Layout MasterLogistic (sidebar + topbar)
    ├── monitor/index.html
    ├── kanban/index.html
    ├── commesse/index.html
    ├── kpi/index.html
    ├── terzisti/index.html
    └── magazzino/index.html
```

## Deploy su Railway

1. Push su GitHub
2. Collega repo a Railway
3. Aggiungi PostgreSQL plugin → `DATABASE_URL` automatica
4. Deploy

## Primo avvio

```bash
# Prima esecuzione: crea tabelle e carica dati Excel
python3 seed_db.py
```

## Sviluppo locale

```bash
pip install -r requirements.txt
export DATABASE_URL="sqlite:///mes_local.db"
python3 seed_db.py
python3 app.py
```
