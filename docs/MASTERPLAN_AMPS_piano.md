# MASTERPLAN · AMPS – Pianificazione previsionale MPS + MRP
**Pagina da aggiungere dentro IronProduction · solo piano, niente sviluppo per ora**

> *"Sapere oggi cosa comprare per gli ordini che arriveranno domani."*

---

## 1. Il nome (DECISO)

MASTERPLAN **non è un programma autonomo**: è il nome di una **pagina dentro IronProduction**.

Intestazione della pagina nel programma:
```
MASTERPLAN · AMPS
Pianificazione previsionale MPS + MRP
Advanced Material & Production Scheduling
```
- **MASTERPLAN** = nome della pagina (voce di menu in IronProduction).
- **AMPS – Advanced Material & Production Scheduling** = nome del motore di calcolo (Material = MRP, Production Scheduling = MPS). Si pronuncia come una parola ("amps", gli ampere: l'energia, il cuore pulsante della pianificazione).
- **MPS + MRP** = spiegazione tecnica per chi conosce la materia.

Scartati: AMSP (troppo gergale, "Push" ha connotazione negativa in ottica lean), PMP (si confonde con la certificazione Project Management Professional e "PMP Plan" ripete la parola "piano").

⚠️ AMPS è una sigla già usata in altri settori: come nome di motore interno va bene; non verificata come marchio.

### Note originali sul nome MASTERPLAN

Perché questo nome:
- in italiano "masterplan" vuol dire già "il piano generale": lo capisce subito anche un imprenditore che non sa cosa sia un MRP;
- è coerente con la famiglia MASTER (MASTERCRM, MASTERWORK, MASTERLOGISTIC…);
- contiene la sigla tecnica giusta: **MPS = Master Production Schedule**, cioè "piano principale di produzione";
- è breve e funziona bene come titolo di un post.

Alternative, se preferisci qualcosa di più "commerciale":
| Nome | Pro | Contro |
|---|---|---|
| **MASTERFORECAST** | dice subito "previsione" | in inglese; sembra solo statistica, non dice che produce la lista acquisti |
| **MASTERANTICIPO** | molto italiano, comprensibile, rende l'idea di "giocare d'anticipo" | meno professionale/tecnico |
| **MASTERDEMAND** | termine tecnico corretto | poco chiaro per le PMI |

⚠️ **Non ho verificato marchi né domini**: "masterplan" è una parola di uso comune, quindi difficilmente registrabile da sola. Come nome di modulo interno al tuo ecosistema va bene; se vuoi proteggerlo come marchio, prima serve una ricerca su UIBM/EUIPO.

---

## 2. Cosa c'è GIÀ in IronProduction (verificato leggendo il codice sul ramo main)

Buona notizia: le basi ci sono quasi tutte.

| Elemento necessario | Dove si trova già | Stato |
|---|---|---|
| Distinta base multilivello | tabella `distinta_base_wood` (padre, figlio, quantità, livello, componenti alternativi con "preferita", pezzi per barra) | ✅ c'è |
| Cicli di lavoro | `ciclo_lavoro_wood` (sequenza reparti, produttività oraria per codice e reparto, scarto max, lotto di trasferimento) | ✅ c'è |
| Lead time terzisti | `centri_costo_wood.lead_time_esterno_giorni` | ✅ c'è |
| Lead time fornitori | `articoli_approvvigionamento.lead_time_fornitura_giorni` | ✅ campo presente — da verificare quanto è compilato |
| Tipo approvvigionamento (si compra / si produce) | `articoli_approvvigionamento.tipo_approvvigionamento` | ✅ c'è |
| Giacenza, impegnato, ordinato fornitori | Magazzino + ordini acquisto aperti | ✅ c'è |
| Scorta minima | `scorte_minime_wood` | ✅ c'è |
| OP senza ordine cliente | flag "OP a scorta" | ✅ c'è |
| Lista Fabbisogni | pagina Acquisti → Lista Fabbisogni | ✅ c'è, ma **reattiva** |

**Cosa manca davvero:**
1. **Le previsioni**: oggi il sistema calcola i fabbisogni solo da OP aperti + scorta minima. Non c'è un posto dove scrivere "a marzo mi arriveranno 200 pezzi del codice X".
2. **Le date**: la Lista Fabbisogni attuale dice *quanto* manca, ma non *entro quando* devo ordinare (niente calcolo a ritroso, niente settimane).
3. **Il consumo delle previsioni**: quando arriva l'ordine vero, va tolto dalla previsione, altrimenti si compra due volte.
4. **Le proposte separate**: un posto dove gli acquisti suggeriti "da previsione" stanno separati da quelli "da ordine certo", finché una persona non li conferma.

---

## 3. Due correzioni al testo che mi hai mandato (utili anche per il post)

1. **La formula**. Quella corretta, nel modo in cui ragiona IronProduction, è:
   **Fabbisogno netto = Fabbisogno lordo (ordini + previsioni) + Scorta di sicurezza − Giacenza − Ordinato ai fornitori non ancora arrivato**
   Gli "impegni" degli OP non vanno sottratti: sono già *dentro* il fabbisogno lordo. Se li togli, sbagli il conto.
2. **"MAD" non è il consumo medio.** MAD = *Mean Absolute Deviation*, cioè di quanto sbaglia in media la previsione. Il consumo medio si chiama *run-rate* o *consumo medio periodico*. Il MAD serve invece a dimensionare la scorta di sicurezza: più la previsione sbaglia, più scorta serve.

---

## 4. Come funziona MASTERPLAN (in parole semplici)

```
 PREVISIONI         ORDINI CERTI
 (commerciale       (OP / commesse
  o storico)         clienti)
      \                 /
       \               /
   1) PIANO PRINCIPALE (MPS)  ← la previsione viene "mangiata" dagli ordini veri
              |
   2) ESPLOSIONE DISTINTA BASE  ← prodotto → semilavorati → materie prime
              |
   3) CALCOLO A RITROSO DELLE DATE  ← cicli (ore di reparto) + terzisti + fornitori
              |
   4) NETTO: tolgo giacenza e ordinato, aggiungo scorta
              |
   5) PROPOSTE: "compra 480 m di tubo entro il 12/01"  → conferma umana → Ordine d'acquisto
```

---

## 5. Il piano di lavoro, per fasi

### Fase 0 – Controllo qualità dei dati (sola lettura, nessuna modifica)
Un MRP con dati sporchi produce acquisti sbagliati. Prima di tutto un report che elenca:
- prodotti finiti senza distinta o con distinta incompleta;
- codici prodotti internamente senza ciclo di lavoro o con produttività = 0;
- codici da acquistare senza lead time fornitore;
- codici non classificati (`DA_CLASSIFICARE`);
- unità di misura incoerenti tra distinta, giacenza e acquisti.

**Collaudo:** il report gira senza modificare nulla e indica quanti codici sono "pronti per l'MRP" su quanti in totale.

### Fase 1 – Piano previsionale di vendita (MPS)
**Decisione presa: si gestiscono ENTRAMBE le ipotesi** – previsione per **singolo prodotto finito** e previsione per **famiglia di prodotti**. Possono convivere nello stesso piano.

Nuova tabella **Previsioni** (separata da tutto il resto):
- **livello**: `ARTICOLO` oppure `FAMIGLIA`;
- codice prodotto finito **oppure** codice famiglia (uno dei due, mai entrambi);
- cliente (facoltativo), periodo (settimana o mese), quantità;
- **affidabilità**: Certa / Probabile / Possibile (es. 100% – 70% – 30%);
- origine: inserita dal commerciale oppure proposta dal sistema in base allo storico;
- stato: bozza / approvata / scaduta.

#### 1A – Previsione per singolo prodotto finito
Si scrive direttamente "febbraio: 100 pz del codice A". Va diretta nel piano MPS. È la più precisa; adatta quando si conoscono bene i codici richiesti dai clienti.

#### 1B – Previsione per famiglia di prodotti
Si scrive "febbraio: 300 pz della famiglia RINGHIERE" senza sapere ancora quali codici esatti. Servono due cose nuove:
1. **Anagrafica Famiglie** (tabella nuova): codice famiglia, descrizione, elenco dei prodotti finiti che ne fanno parte. Un prodotto sta in una sola famiglia di pianificazione.
   - In IronProduction oggi non esiste un campo famiglia sui codici delle distinte Wood; esiste solo `categoria` sulla tabella `prodotti`, che si può usare **come suggerimento iniziale**, da confermare a mano.
2. **Mix percentuale** (la "distinta di pianificazione"): per ogni famiglia, quanto pesa ciascun codice. Es. RINGHIERE = A 50% · B 30% · C 20%.
   - il mix si inserisce a mano **oppure** il sistema lo propone dallo storico degli ultimi 6/12 mesi (da approvare);
   - controllo bloccante: la somma delle percentuali deve fare 100%; prodotti senza distinta base o senza ciclo segnalati.

Il motore AMPS **divide la previsione di famiglia sui codici** prima dell'esplosione: 300 pz → A 150 · B 90 · C 60. Gli arrotondamenti vanno sul codice con la percentuale più alta, così il totale resta 300.

#### Regole quando ci sono entrambe sullo stesso periodo
- Se per lo stesso codice esiste una previsione **per articolo**, quella prevale sulla quota che gli arriverebbe dalla famiglia. Il resto della famiglia si ridistribuisce sugli altri codici in proporzione al mix. Es.: famiglia 300, ma per A ho già scritto 200 → A 200; restano 100 da dividere tra B (30/50 = 60) e C (20/50 = 40).
- Se la previsione per articolo supera quella di famiglia, il sistema **avvisa** (incoerenza da correggere), non decide da solo.
- Nella schermata si vede sempre da dove viene ogni numero: "da articolo" o "da famiglia RINGHIERE (30%)".

Tre modi per caricare le previsioni:
1. a mano, in una griglia tipo Excel (righe = codici **o famiglie**, colonne = mesi);
2. import da Excel;
3. **proposta automatica dallo storico** (media degli ultimi 3/6/12 mesi, con correzione stagionale semplice) che però **l'utente deve approvare**: il sistema propone, la persona decide.

**Collaudo:**
- 1A: inserisco 100 pz del prodotto A per febbraio → li ritrovo nel piano, origine "da articolo".
- 1B: famiglia 300 pz con mix 50/30/20 → piano A 150 · B 90 · C 60, totale 300.
- Misto: famiglia 300 + articolo A 200 → A 200 · B 60 · C 40, totale 300.
- Mix che somma 95% → il sistema blocca il calcolo e lo segnala.

### Fase 2 – Consumo della previsione (la parte che evita di comprare doppio)
Regola: per ogni codice e periodo, **domanda = il più alto tra previsione residua e ordini certi**, non la somma.
- Previsione 100, arriva ordine da 60 → domanda 100 (60 certi + 40 ancora previsti).
- Arriva un altro ordine da 50 → domanda 110 (tutto certo, previsione esaurita).
- **Finestra di consumo**: un ordine può "mangiare" la previsione della settimana prima o dopo (parametro, es. ±2 settimane).
- **Zona congelata**: nelle prime N settimane (es. 2) contano solo gli ordini certi; la previsione da sola non fa comprare nulla perché ormai è tardi.
- **Con le famiglie**: l'ordine vero del codice B consuma prima la quota di B; se ordini su B superano la sua quota, l'eccedenza consuma la previsione residua della famiglia (gli altri codici si riducono in proporzione). Così il totale famiglia non si gonfia.

**Collaudo:** il caso numerico sopra dà esattamente 100 → 100 → 110. Famiglia 300 (B quota 90), arriva ordine B da 120 → B 120, A e C ridotti a 180 complessivi in proporzione 50:20 (A 129 · C 51, arrotondamento sul codice con quota più alta), totale 300.

### Fase 3 – Esplosione a cascata con le date
Usa la distinta che c'è già (solo righe "preferite" per i componenti alternativi; quantità per barra già convertite).
- Calcolo **livello per livello**: prima il prodotto finito, poi i semilavorati, poi le materie prime. Ad ogni livello si toglie la giacenza di quel codice (es. semilavorati già fatti con gli "OP a scorta") prima di scendere.
- **Tempo di produzione** ricavato dai cicli: quantità ÷ produttività oraria di ogni reparto, convertito in giorni lavorativi (default 8 ore/giorno, 5 giorni/settimana, modificabile per reparto), più il lead time dei terzisti.
- **Tempo di acquisto**: lead time del fornitore.
- **Calcolo a ritroso**: dalla data in cui il cliente vorrà la merce, si torna indietro fase per fase fino alla data entro cui ordinare il materiale.

**Collaudo (dati sintetici):** prodotto A richiesto il 30/03; montaggio 2 giorni, saldatura 3 giorni, taglio 1 giorno, fornitore tubo 10 giorni lavorativi → il sistema indica "ordinare il tubo entro il …" alla data corretta, calcolata a mano in parallelo.

### Fase 4 – Tabella MRP a settimane (orizzonte 26 settimane)
Per ogni codice, la classica griglia (righe) per settimana (colonne):
| | Sett.1 | Sett.2 | Sett.3 | … |
|---|---|---|---|---|
| Fabbisogno lordo (certo + previsto) | | | | |
| Arrivi da ordini fornitori già fatti | | | | |
| Disponibile previsto a fine settimana | | | | |
| Fabbisogno netto | | | | |
| **Arrivo da pianificare** | | | | |
| **Ordine da emettere (anticipato del lead time)** | | | | |

Più le regole di lotto: lotto minimo e multipli d'acquisto (da verificare se il catalogo fornitori li contiene già; altrimenti campo nuovo).

### Fase 5 – Proposte e conferma umana
- Pagina **"Proposte AMPS"**, separata dagli ordini d'acquisto veri.
- Ogni riga ha un colore: 🟢 da ordine certo · 🟡 da previsione probabile · 🟠 da previsione possibile.
- Per i semilavorati: proposta di **OP a scorta** (che il flag esistente già supporta).
- **Messaggi d'eccezione**: "anticipa", "posticipa", "annulla" quando un ordine già fatto non serve più o serve prima.
- Il pulsante "Trasforma in ordine d'acquisto" esiste solo dopo conferma esplicita. **MASTERPLAN da solo non crea ordini, non muove giacenze, non manda email.**

### Fase 6 – Simulazione "e se…?"
- Tre scenari: prudente / realistico / ottimista (usando l'affidabilità delle previsioni).
- Per ogni scenario: valore in € del materiale da comprare (usa il costo standard d'acquisto già presente) e **rischio di materiale fermo** se l'ordine poi non arriva.

### Fase 7 – Misura dell'errore di previsione
A fine mese: previsto vs venduto davvero, per codice e per cliente (qui entra il MAD). Serve a capire di quali clienti/prodotti ci si può fidare e a correggere la scorta di sicurezza.

---

## 6. Regole di sicurezza dello sviluppo
- Ramo separato, `main` intatto fino alla tua autorizzazione.
- La **Lista Fabbisogni esistente non si tocca**: MASTERPLAN è una pagina nuova accanto, così si possono confrontare i due risultati.
- Nelle prime versioni il motore è **in sola lettura**: legge distinte, cicli e giacenze ma non scrive nulla nelle tabelle operative.
- Test automatici con dati sintetici (come i test già presenti nel repository), nessun dato reale durante lo sviluppo.
- Nessuna attivazione automatica: il calcolo parte quando lo lanci tu.

---

## 7. Decisioni prese (scelte di default, tutte modificabili da parametro)

Le scelte sotto sono **le impostazioni di partenza**. Ognuna è un parametro nella pagina *Impostazioni AMPS*, quindi si può cambiare senza riscrivere il programma.

| # | Tema | Decisione | Perché |
|---|---|---|---|
| 1 | Livello delle previsioni | **Entrambi**: per prodotto e per famiglia; vince il prodotto sulla quota della famiglia | precisione dove si sa, semplicità dove non si sa |
| 2 | Prime famiglie | **Proposte in automatico dal campo `categoria` dei prodotti**; l'utente le conferma, rinomina o accorpa. I prodotti senza categoria finiscono in "DA ASSEGNARE" e sono esclusi dal calcolo per famiglia finché non vengono assegnati | si parte da quello che esiste già, senza inventare famiglie |
| 3 | Percentuali del mix | **Proposte dal sistema dallo storico degli ultimi 12 mesi** (pezzi ordinati per codice nella famiglia), **approvate a mano**. Se un codice non ha storico, quota 0% finché non la imposti tu. Se l'intera famiglia non ha storico, il mix va scritto a mano | i numeri veri battono le stime; la persona resta l'ultima parola |
| 4 | Unità di tempo | **Si inserisce a mesi, si calcola a settimane.** La previsione mensile viene divisa sulle settimane lavorative del mese; ordini certi e lead time restano in giorni | i commerciali ragionano a mesi, gli acquisti a settimane |
| 5 | Orizzonte del piano | **26 settimane** (6 mesi) | copre i lead time più lunghi senza fare fantascienza |
| 6 | Chi compila | **Il sistema propone dallo storico, il commerciale corregge e approva.** Solo le previsioni in stato *approvata* entrano nel calcolo | il sistema non decide da solo; nessuno parte da un foglio bianco |
| 7 | Metodo dello storico | **Media mobile degli ultimi 6 mesi** con correzione stagionale semplice (rapporto mese/anno sui 12 mesi), solo se ci sono almeno 12 mesi di dati; altrimenti media semplice | metodo spiegabile in aula e verificabile a mano |
| 8 | Ore lavorative | **8 ore al giorno, 5 giorni a settimana** per ogni reparto, modificabile per reparto | standard di una PMI manifatturiera |
| 9 | Zona congelata | **2 settimane**: lì contano solo gli ordini certi | entro 2 settimane la previsione non fa più in tempo a diventare acquisto utile |
| 10 | Finestra di consumo | **±2 settimane**: un ordine vero consuma la previsione dello stesso codice/famiglia nelle 2 settimane prima o dopo | ordini anticipati/posticipati non creano doppioni |
| 11 | Affidabilità | Certa 100% · Probabile 70% · Possibile 30%. Il calcolo **base** usa Certe + Probabili; le Possibili entrano solo nello scenario "ottimista" (Fase 6) | evita di riempire il magazzino con "forse" |
| 12 | Scorta di sicurezza | **R1–R3: si usa la scorta minima già presente** in IronProduction. **R4: calcolo suggerito dal MAD** (errore medio di previsione), da approvare | non si rompe nulla di quanto già configurato |
| 13 | Lead time mancanti | Codici senza lead time fornitore: **valore prudente di 15 giorni lavorativi + segnalazione rossa** "lead time da verificare" | il calcolo non si blocca, ma il dubbio resta visibile |
| 14 | Lotti d'acquisto | Lotto minimo e multiplo: **si leggono dal catalogo fornitori se presenti; altrimenti campi nuovi, vuoti = nessun vincolo** | niente regole inventate |
| 15 | Ricalcolo | **Su richiesta** con il pulsante *Avvia calcolo AMPS*, con data/ora e utente salvati. Nessun ricalcolo automatico notturno nella prima versione | sempre chiaro "di quando" sono i numeri |
| 16 | Proposte | Restano **proposte**: diventano ordini/OP solo con conferma esplicita. Le proposte non confermate scadono al ricalcolo successivo | nessuna azione operativa automatica |
| 17 | Nome in interfaccia | Menu **MASTERPLAN · AMPS**; pulsanti *Avvia calcolo AMPS*, *Proposte AMPS* | coerente con quanto deciso |

**Unico punto che resta da verificare sui dati reali (Fase 0):** quanti prodotti hanno `categoria` compilata, quanti codici hanno lead time e quanti mesi di storico ordini ci sono. Se lo storico è troppo corto, la proposta automatica del mix e delle previsioni parte limitata e si compila di più a mano.

---

## 8. Ordine consigliato di rilascio
| Rilascio | Contenuto | Valore per l'azienda |
|---|---|---|
| R1 | Fase 0 + Fase 1 + Fase 2 | si inseriscono le previsioni e si vede la domanda totale senza doppioni |
| R2 | Fase 3 + Fase 4 | si sa **cosa e quando** comprare |
| R3 | Fase 5 | proposte confermabili, collegate agli ordini d'acquisto |
| R4 | Fase 6 + Fase 7 | simulazioni e controllo dell'accuratezza |

---

## 9. Spunto per il post LinkedIn di domani
**Gancio:** *"Il cliente non ti ha ancora mandato l'ordine. Ma il fornitore ci mette 6 settimane. Che fai, aspetti?"*
**Messaggio:** in una PMI che lavora su commessa, aspettare l'ordine certo vuol dire consegnare in ritardo. MASTERPLAN legge distinta base e cicli, trasforma le previsioni in una lista di acquisti con le date, e quando arriva l'ordine vero non compra due volte.
**Chiusura:** il sistema propone, l'imprenditore decide.

*(Da non scrivere nel post finché non è misurato: percentuali di riduzione scorte, ritardi o costi.)*
