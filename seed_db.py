#!/usr/bin/env python3
"""
seed_db.py — Carica i dati esatti del mockup mes_carpenteria_v2 nel DB.
Eseguire DOPO il primo avvio: python3 seed_db.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import app
from models import db, KanbanProdotto, RigaMonitor

KANBAN = [
  # ── 1 Cavalletti ──────────────────────────────────────────────────────────
  {"prodotto":"S-14","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":600,"riserva":300,"riservato":70,"grezzi":0,"verniciati":0,"in_vern":161,"in_prod":0,"val_medio":21.0,"lavorazioni":"IN VERNICIATURA"},
  {"prodotto":"S-20","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":360,"riserva":160,"riservato":200,"grezzi":0,"verniciati":52,"in_vern":0,"in_prod":0,"val_medio":33.0,"lavorazioni":"SPEDIZIONE PARZIALE"},
  {"prodotto":"C10","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":600,"riserva":300,"riservato":200,"grezzi":0,"verniciati":19,"in_vern":0,"in_prod":200,"val_medio":9.5,"lavorazioni":"SPEDIZIONE PARZIALE, IN PRODUZIONE"},
  {"prodotto":"C12","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":500,"riserva":400,"riservato":300,"grezzi":0,"verniciati":28,"in_vern":88,"in_prod":212,"val_medio":11.5,"lavorazioni":"SPEDIZIONE PARZIALE, IN VERNICIATURA, IN PRODUZIONE"},
  {"prodotto":"PLBT - PL Baionetta","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":500,"riserva":250,"riservato":0,"grezzi":0,"verniciati":286,"in_vern":0,"in_prod":0,"val_medio":8.5,"lavorazioni":""},
  {"prodotto":"L10","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"L12","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":37,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"S-30","categoria":"1 Cavalletti","sheet_key":"1 Cavalletti","icona":"🏗️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":52,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  # ── 2 Transenne ───────────────────────────────────────────────────────────
  {"prodotto":"Transenna T200","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":400,"riserva":240,"riservato":200,"grezzi":19,"verniciati":53,"in_vern":240,"in_prod":0,"val_medio":34.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN VERNICIATURA, DA VERNICIARE"},
  {"prodotto":"Zampe Normali ZT","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":600,"riserva":0,"riservato":1760,"grezzi":0,"verniciati":1376,"in_vern":0,"in_prod":665,"val_medio":0.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN PRODUZIONE"},
  {"prodotto":"Transenna T250","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":120,"riserva":60,"riservato":320,"grezzi":0,"verniciati":40,"in_vern":0,"in_prod":340,"val_medio":45.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN PRODUZIONE"},
  {"prodotto":"Transenna CATANIA","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":2,"in_vern":0,"in_prod":0,"val_medio":45.0,"lavorazioni":""},
  {"prodotto":"FENCE32","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":25,"riserva":10,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":110.0,"lavorazioni":""},
  {"prodotto":"Cancello Piccolo FENCE","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":5,"riserva":3,"riservato":0,"grezzi":0,"verniciati":3,"in_vern":0,"in_prod":0,"val_medio":220.0,"lavorazioni":""},
  {"prodotto":"Transenna T200RMZF","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":100,"riserva":0,"riservato":20,"grezzi":4,"verniciati":3,"in_vern":0,"in_prod":18,"val_medio":90.0,"lavorazioni":"SPEDIZIONE PARZIALE, DA VERNICIARE, IN PRODUZIONE"},
  {"prodotto":"Zampe ZTRM20","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":200,"riserva":0,"riservato":40,"grezzi":0,"verniciati":50,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":"PRONTI PER SPEDIZIONE"},
  {"prodotto":"Transenna Piede Fisso T200PF","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"Zampe Roma ZTR","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":200,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"T200DT Doppio Trav","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":180,"riserva":0,"riservato":200,"grezzi":4,"verniciati":0,"in_vern":0,"in_prod":196,"val_medio":0.0,"lavorazioni":"DA VERNICIARE, IN PRODUZIONE"},
  {"prodotto":"T250DT Doppio Trav","categoria":"2 Transenne","sheet_key":"2 Transenne","icona":"🚧","lotto":60,"riserva":0,"riservato":110,"grezzi":0,"verniciati":61,"in_vern":49,"in_prod":0,"val_medio":0.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN VERNICIATURA"},
  # ── 3 Archetti ────────────────────────────────────────────────────────────
  {"prodotto":"A60 1210 CT","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":78,"riserva":0,"riservato":4,"grezzi":0,"verniciati":5,"in_vern":0,"in_prod":0,"val_medio":54.0,"lavorazioni":"PRONTI PER SPEDIZIONE"},
  {"prodotto":"A60 1212 CT","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":34.0,"lavorazioni":""},
  {"prodotto":"A60 1210 ST","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":33.0,"lavorazioni":""},
  {"prodotto":"A48 1212 CT","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":34,"in_vern":0,"in_prod":0,"val_medio":20.0,"lavorazioni":""},
  {"prodotto":"A48 1210 ST","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":77,"in_vern":0,"in_prod":0,"val_medio":25.0,"lavorazioni":""},
  {"prodotto":"A60 1212 CT-CP","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":48.0,"lavorazioni":""},
  {"prodotto":"A60 1212 ST","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":20,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":20,"val_medio":41.0,"lavorazioni":"IN PRODUZIONE"},
  {"prodotto":"A48 512 ST","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":20,"grezzi":0,"verniciati":17,"in_vern":0,"in_prod":50,"val_medio":30.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN PRODUZIONE"},
  {"prodotto":"A60 1212 CTD6","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":26,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":26,"val_medio":42.0,"lavorazioni":"IN PRODUZIONE"},
  {"prodotto":"A60 1210 CT-CP","categoria":"3 Archetti","sheet_key":"3 Archetti","icona":"🔲","lotto":0,"riserva":0,"riservato":16,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":16,"val_medio":30.0,"lavorazioni":"IN PRODUZIONE"},
  # ── 4 Paletti 48 ──────────────────────────────────────────────────────────
  {"prodotto":"P48 120 CA - ROSSO CON Anelli","categoria":"4 Paletti ⌀48","sheet_key":"4 Paletti ⌀48","icona":"📍","lotto":200,"riserva":50,"riservato":0,"grezzi":0,"verniciati":139,"in_vern":0,"in_prod":0,"val_medio":8.0,"lavorazioni":""},
  {"prodotto":"P48 120 SA - ROSSO SENZA Anelli","categoria":"4 Paletti ⌀48","sheet_key":"4 Paletti ⌀48","icona":"📍","lotto":300,"riserva":50,"riservato":50,"grezzi":0,"verniciati":150,"in_vern":0,"in_prod":0,"val_medio":10.0,"lavorazioni":"PRONTI PER SPEDIZIONE"},
  # ── 5 Paletti 60 ──────────────────────────────────────────────────────────
  {"prodotto":"P60 120 SA - ROSSO SENZA Anelli","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":200,"riserva":100,"riservato":100,"grezzi":0,"verniciati":179,"in_vern":0,"in_prod":0,"val_medio":9.0,"lavorazioni":"PRONTI PER SPEDIZIONE"},
  {"prodotto":"P60 120 CA - ROSSO CON Anelli","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":100,"riserva":50,"riservato":0,"grezzi":0,"verniciati":182,"in_vern":0,"in_prod":0,"val_medio":12.0,"lavorazioni":""},
  {"prodotto":"P60120SA-BNG","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":4,"in_vern":0,"in_prod":0,"val_medio":15.0,"lavorazioni":""},
  {"prodotto":"P60 120 CS - c/SFERA","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":40,"in_vern":0,"in_prod":0,"val_medio":30.0,"lavorazioni":""},
  {"prodotto":"P60 120 CSA - c/SFERA+ANELLI","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":35.0,"lavorazioni":""},
  {"prodotto":"P60 100 CP - c/PIASTRE","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":7,"in_vern":0,"in_prod":0,"val_medio":15.0,"lavorazioni":""},
  {"prodotto":"PINX60 CP - c/PIASTRE","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":4,"in_vern":0,"in_prod":0,"val_medio":40.0,"lavorazioni":""},
  {"prodotto":"P60CNT-LUC","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":10,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"P60CNT","categoria":"5 Paletti ⌀60","sheet_key":"5 Paletti ⌀60","icona":"📌","lotto":0,"riserva":0,"riservato":12,"grezzi":12,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":"DA VERNICIARE"},
  # ── 6 Paletti Vari ────────────────────────────────────────────────────────
  {"prodotto":"PCFI Chiodo Fiorentino","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":30,"in_vern":0,"in_prod":0,"val_medio":95.0,"lavorazioni":""},
  {"prodotto":"Cassette Chiodo Fiorentino","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":30,"in_vern":0,"in_prod":0,"val_medio":30.0,"lavorazioni":""},
  {"prodotto":"PINX100 - Paletto Inox ⌀100","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":59,"in_vern":0,"in_prod":0,"val_medio":90.0,"lavorazioni":""},
  {"prodotto":"P80120CSA - Paletto ⌀80 C/Sfera + Anelli","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":45.0,"lavorazioni":""},
  {"prodotto":"PRGR - Palo Rotante GR","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":18,"in_vern":0,"in_prod":0,"val_medio":70.0,"lavorazioni":""},
  {"prodotto":"PDG100 - Delineatore per Galleria","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":44,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"PINX110 - Paletto Inox ⌀100 sp3","categoria":"6 Paletti Vari","sheet_key":"6 Paletti Vari","icona":"🗂️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  # ── 7 Parapetti ───────────────────────────────────────────────────────────
  {"prodotto":"5040MOD - TRAMVIA Milano","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":60,"riserva":30,"riservato":829,"grezzi":0,"verniciati":-22,"in_vern":0,"in_prod":256,"val_medio":130.0,"lavorazioni":"IN PRODUZIONE"},
  {"prodotto":"5040TER M/F - TRAMVIA Milano","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":60,"riserva":30,"riservato":50,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":96,"val_medio":85.0,"lavorazioni":"IN PRODUZIONE"},
  {"prodotto":"5031MOD - Inox Scolastica Modulare","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":130,"in_vern":0,"in_prod":0,"val_medio":150.0,"lavorazioni":""},
  {"prodotto":"5032TER-M -Terminale Inox MASCHIO","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":10,"in_vern":0,"in_prod":0,"val_medio":80.0,"lavorazioni":""},
  {"prodotto":"5032TER-F -Terminale Inox FEMMINA","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":10,"in_vern":0,"in_prod":0,"val_medio":80.0,"lavorazioni":""},
  {"prodotto":"PNPV - Parapetto Naviglio Pavese","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":190.0,"lavorazioni":""},
  {"prodotto":"PNPV-PL - Piastra Laser","categoria":"7 Parapetti","sheet_key":"7 Parapetti","icona":"🛡️","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":110,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  # ── 8 Rastrelliere ────────────────────────────────────────────────────────
  {"prodotto":"RTFIGR - Archetto Grande Rastrelliera","categoria":"8 Rastrelliere","sheet_key":"8 Rastrelliere","icona":"🚲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":3,"in_vern":0,"in_prod":0,"val_medio":83.0,"lavorazioni":""},
  {"prodotto":"RTFIPC - Archetto Piccolo Rastrelliera","categoria":"8 Rastrelliere","sheet_key":"8 Rastrelliere","icona":"🚲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":96,"in_vern":0,"in_prod":0,"val_medio":50.0,"lavorazioni":""},
  {"prodotto":"RTCF150 - Canotto per Rastrelliera","categoria":"8 Rastrelliere","sheet_key":"8 Rastrelliere","icona":"🚲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":499,"in_vern":0,"in_prod":0,"val_medio":7.0,"lavorazioni":""},
  {"prodotto":"RT1400 - Binario L.1.400 base 2 archi","categoria":"8 Rastrelliere","sheet_key":"8 Rastrelliere","icona":"🚲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":68,"in_vern":0,"in_prod":0,"val_medio":19.0,"lavorazioni":""},
  {"prodotto":"RT1500 - Binario L.1.500 base 3 archi","categoria":"8 Rastrelliere","sheet_key":"8 Rastrelliere","icona":"🚲","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":164,"in_vern":0,"in_prod":0,"val_medio":20.0,"lavorazioni":""},
  # ── 9 Tubi Scanalati ──────────────────────────────────────────────────────
  {"prodotto":"L70 - Tubo ⌀60 sp.2 L.7.000","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":31.5,"lavorazioni":""},
  {"prodotto":"L37 - Tubo ⌀60 sp.2 L.3.700","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":17.8,"lavorazioni":""},
  {"prodotto":"L33 - Tubo ⌀60 sp.2 L.3.300","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":14.9,"lavorazioni":""},
  {"prodotto":"L35 - Tubo ⌀60 sp.2 L.3.500","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":16.8,"lavorazioni":""},
  {"prodotto":"L35-SAGO - Tubo ⌀60 sp.2 L.3.500 SAGOMATO","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"L60 - Tubo ⌀60 sp.2 L.6.000","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"L40 - Tubo ⌀60 sp.2 L.4.000","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"L30 - Tubo ⌀60 sp.2 L.3.000","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"L20 - Tubo ⌀60 sp.2 L.2.000","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"L15 - Tubo ⌀60 sp.2 L.1.500","categoria":"9 Tubi Scanalati","sheet_key":"9 Tubi Scanalati","icona":"🔩","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  # ── 10 Staffe e NJ ────────────────────────────────────────────────────────
  {"prodotto":"STFU170 - Staffa Universale a U","categoria":"10 Staffe e NJ","sheet_key":"10 Staffe e NJ","icona":"🔧","lotto":200,"riserva":100,"riservato":0,"grezzi":0,"verniciati":160,"in_vern":0,"in_prod":0,"val_medio":14.0,"lavorazioni":""},
  {"prodotto":"CPNJ-SUPP - Supporti","categoria":"10 Staffe e NJ","sheet_key":"10 Staffe e NJ","icona":"🔧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":453,"in_vern":0,"in_prod":0,"val_medio":11.0,"lavorazioni":""},
  {"prodotto":"CPNJ-SCA - Scatolati","categoria":"10 Staffe e NJ","sheet_key":"10 Staffe e NJ","icona":"🔧","lotto":200,"riserva":100,"riservato":0,"grezzi":0,"verniciati":237,"in_vern":0,"in_prod":0,"val_medio":14.0,"lavorazioni":""},
  {"prodotto":"SNJ-CANT14","categoria":"10 Staffe e NJ","sheet_key":"10 Staffe e NJ","icona":"🔧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":38,"in_vern":0,"in_prod":0,"val_medio":15.0,"lavorazioni":""},
  {"prodotto":"SNJ190 - Supporto New Jersey 190","categoria":"10 Staffe e NJ","sheet_key":"10 Staffe e NJ","icona":"🔧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":48,"in_vern":0,"in_prod":0,"val_medio":32.0,"lavorazioni":""},
  {"prodotto":"SGR200","categoria":"10 Staffe e NJ","sheet_key":"10 Staffe e NJ","icona":"🔧","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":155,"in_vern":0,"in_prod":0,"val_medio":9.0,"lavorazioni":""},
  # ── 11 Barriere ───────────────────────────────────────────────────────────
  {"prodotto":"BM24-PS - PIEDISTALLO BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":72,"grezzi":0,"verniciati":2,"in_vern":80,"in_prod":0,"val_medio":140.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN VERNICIATURA"},
  {"prodotto":"BM24-PB - PORTABARRA BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":72,"grezzi":0,"verniciati":2,"in_vern":80,"in_prod":0,"val_medio":90.0,"lavorazioni":"SPEDIZIONE PARZIALE, IN VERNICIATURA"},
  {"prodotto":"BM24-PM - PIEDINO MURARE BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":72,"grezzi":0,"verniciati":0,"in_vern":80,"in_prod":0,"val_medio":0.0,"lavorazioni":"IN VERNICIATURA"},
  {"prodotto":"BM24-PP - PIEDINO PENSILE BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":4,"in_vern":0,"in_prod":0,"val_medio":240.0,"lavorazioni":""},
  {"prodotto":"BM24-PB-CP - COPERCHIO BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":72,"grezzi":0,"verniciati":0,"in_vern":80,"in_prod":0,"val_medio":150.0,"lavorazioni":"IN VERNICIATURA"},
  {"prodotto":"BM24-PST05 - CONTROPIASTRA BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":72,"grezzi":0,"verniciati":80,"in_vern":0,"in_prod":0,"val_medio":35.0,"lavorazioni":"PRONTI PER SPEDIZIONE"},
  {"prodotto":"BM24-PST07 - COPRICUSCINETTO BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":144,"grezzi":0,"verniciati":0,"in_vern":157,"in_prod":0,"val_medio":14.0,"lavorazioni":"IN VERNICIATURA"},
  {"prodotto":"BM24-PM-PN - PERNO BM24","categoria":"11 Barriere","sheet_key":"11 Barriere","icona":"🚦","lotto":0,"riserva":0,"riservato":72,"grezzi":0,"verniciati":0,"in_vern":81,"in_prod":0,"val_medio":30.0,"lavorazioni":"IN VERNICIATURA"},
  # ── 12 Varie Altre ────────────────────────────────────────────────────────
  {"prodotto":"BC60 - Basi Circolari","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":800,"riserva":0,"riservato":160,"grezzi":0,"verniciati":160,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":"PRONTI PER SPEDIZIONE"},
  {"prodotto":"SPSC - Supporto a Muro","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":14.0,"lavorazioni":""},
  {"prodotto":"C120 - Contenitore","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"DBESP-TL - Telaio","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"DBESP-SP - Separatore","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"TOTEM Kit","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":2,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"USOST80 - Sostegno U","categoria":"12 Varie Altre Produzioni","sheet_key":"12 Varie Altre Produzioni","icona":"📦","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  # ── Pannelli Transenne ────────────────────────────────────────────────────
  {"prodotto":"PNT25 - Pannello Polionda 150x20 [FINITO]","categoria":"Pannelli Transenne","sheet_key":"Pannelli Transenne","icona":"🪟","lotto":300,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":6.0,"lavorazioni":""},
  {"prodotto":"PNPO25 - Pannello Polionda 150x20 [POLIONDA]","categoria":"Pannelli Transenne","sheet_key":"Pannelli Transenne","icona":"🪟","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
  {"prodotto":"PU-PNT25-1 - Pellicola B/R 150x20 [PELLICOLA]","categoria":"Pannelli Transenne","sheet_key":"Pannelli Transenne","icona":"🪟","lotto":0,"riserva":0,"riservato":0,"grezzi":0,"verniciati":0,"in_vern":0,"in_prod":0,"val_medio":0.0,"lavorazioni":""},
]

MONITOR = [
  # lavorazione
  {"sezione":"lavorazione","comm_id":"552/26","codice":"A60 1210 CT-CP","descrizione":"ARCHETTO ⌀60 100x120 CON TRAVERSO RETTANGOLARE + PIASTRA FISSAGGIO","totale":16,"saldo":16,"pct":0,"priority":"!","ordine":1,"taglio":"wip","sgola":"na","piega":"wip","saldatura":"todo"},
  {"sezione":"lavorazione","comm_id":"-/26","codice":"Misti Archetti","descrizione":"ARCHETTI PERSONALIZZATI 2 MISURE","totale":2,"saldo":2,"pct":0,"priority":"!","ordine":2,"taglio":"wip","sgola":"na","piega":"wip","saldatura":"todo"},
  {"sezione":"lavorazione","comm_id":"551/26","codice":"A60 1212 CTD6","descrizione":"ARCHETTO ⌀60 120x120 CON TRAVERSO TONDO","totale":26,"saldo":26,"pct":0,"priority":"!","ordine":3,"taglio":"wip","sgola":"wip","piega":"wip","saldatura":"todo"},
  {"sezione":"lavorazione","comm_id":"568/26","codice":"A60 1212 ST","descrizione":"ARCHETTO ⌀60 120x120 SENZA TRAVERSO","totale":20,"saldo":20,"pct":0,"priority":"!","ordine":4,"taglio":"wip","sgola":"na","piega":"wip","saldatura":"na"},
  {"sezione":"lavorazione","comm_id":"581/26","codice":"T250","descrizione":"TRANSENNA 2,5 MT NORMALE x220 CON TARGA AVR","totale":340,"saldo":340,"pct":0,"priority":"1","ordine":5,"taglio":"wip","sgola":"na","piega":"todo","saldatura":"todo"},
  # da_iniziare
  {"sezione":"da_iniziare","comm_id":"587/26","codice":"ZT zampa tipo normale","descrizione":"ZAMPA NORMALE PER TRANSENNA \"V\"","totale":1925,"saldo":665,"pct":65,"priority":"2","ordine":1,"taglio":"todo","sgola":"na","piega":"todo","saldatura":"todo"},
  {"sezione":"da_iniziare","comm_id":"112/OF","codice":"TS60 L35","descrizione":"L.3.500 TUBO SEGNALETICA ⌀60","totale":17,"saldo":17,"pct":0,"priority":"3","ordine":2,"taglio":"todo","sgola":"na","piega":"na","saldatura":"na"},
  {"sezione":"da_iniziare","comm_id":"112/OF","codice":"TS60 L40","descrizione":"L.4.000 TUBO SEGNALETICA ⌀60","totale":30,"saldo":30,"pct":0,"priority":"4","ordine":3,"taglio":"todo","sgola":"na","piega":"na","saldatura":"na"},
  {"sezione":"da_iniziare","comm_id":"537/26","codice":"A48  512 ST","descrizione":"ARCHETTO ⌀48 50x120 SENZA TRAVERSO","totale":50,"saldo":50,"pct":0,"priority":"5","ordine":4,"taglio":"todo","sgola":"na","piega":"todo","saldatura":"na"},
  {"sezione":"da_iniziare","comm_id":"399/26","codice":"5040MOD","descrizione":"PARAPETTO MILANO TRAMVIA","totale":453,"saldo":256,"pct":43,"priority":"6","ordine":5,"taglio":"todo","sgola":"na","piega":"na","saldatura":"todo"},
  # in_attesa
  {"sezione":"in_attesa","comm_id":"-/26","codice":"5040TER","descrizione":"TERMINALE TRAMVIA Maschio / Femmina","totale":96,"saldo":96,"pct":0,"priority":"","ordine":1,"taglio":"todo","sgola":"na","piega":"todo","saldatura":"todo"},
  # in_saldatura
  {"sezione":"in_saldatura","comm_id":"560/26","codice":"T200RMZF","descrizione":"TRANSENNA RETE METALLICA 2 metri","totale":50,"saldo":18,"pct":64,"priority":"","ordine":1,"taglio":"done","sgola":"na","piega":"done","saldatura":"wip"},
  {"sezione":"in_saldatura","comm_id":"531/26","codice":"C12","descrizione":"Cavalletto C12 h.120 NUOVO MODELLO 2026","totale":300,"saldo":212,"pct":29,"priority":"","ordine":2,"taglio":"done","sgola":"na","piega":"na","saldatura":"wip"},
  {"sezione":"in_saldatura","comm_id":"482/26","codice":"T200DT doppio traverso","descrizione":"TRANSENNA 2 MT DOPPIO TRAVERSO","totale":200,"saldo":196,"pct":2,"priority":"","ordine":3,"taglio":"wip","sgola":"wip","piega":"wip","saldatura":"wip"},
  # postazione
  {"sezione":"postazione","comm_id":"530/26","codice":"C10","descrizione":"Cavalletto C10 h.100 NUOVO MODELLO 2026","totale":200,"saldo":200,"pct":0,"priority":"1","ordine":1,"taglio":"done","sgola":"na","piega":"na","saldatura":"todo"},
  # terminati
  {"sezione":"terminati","comm_id":"483/26","codice":"T200","descrizione":"TRANSENNA 2 MT NORMALE","totale":291,"saldo":0,"pct":100,"priority":"","ordine":1,"taglio":"done","sgola":"na","piega":"done","saldatura":"done"},
  {"sezione":"terminati","comm_id":"411/26","codice":"T250DT doppio traverso","descrizione":"TRANSENNA 2,5 MT DOPPIO TRAVERSO","totale":110,"saldo":0,"pct":100,"priority":"","ordine":2,"taglio":"done","sgola":"done","piega":"done","saldatura":"done"},
]

def seed():
    with app.app_context():
        db.create_all(bind_key=None)   # solo il DB locale — mai il bind 'masterlogistic'
        if KanbanProdotto.query.count() == 0:
            print(f"Inserisco {len(KANBAN)} prodotti Kanban...")
            for i, d in enumerate(KANBAN):
                d['sort_order'] = i
                db.session.add(KanbanProdotto(**d))
            db.session.commit()
            print(f"✅ Kanban OK ({KanbanProdotto.query.count()} prodotti)")
        else:
            print(f"⏭  Kanban già popolato ({KanbanProdotto.query.count()} prodotti)")

        if RigaMonitor.query.count() == 0:
            print(f"Inserisco {len(MONITOR)} righe monitor...")
            for d in MONITOR:
                db.session.add(RigaMonitor(**d))
            db.session.commit()
            print(f"✅ Monitor OK ({RigaMonitor.query.count()} righe)")
        else:
            print(f"⏭  Monitor già popolato ({RigaMonitor.query.count()} righe)")

if __name__ == '__main__':
    seed()
    print("✅ Seed completato.")
