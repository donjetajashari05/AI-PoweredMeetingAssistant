# Meeting Memory

Asistent për mbledhje me memorie afatgjatë: transkripton mbledhjen, nxjerr vendimet dhe detyrat,
i krahason me tiketat ekzistuese në Jira ose Trello që të mos krijohen dublikata, dhe u përgjigjet
pyetjeve mbi mbledhjet e kaluara.

## Struktura

```
meeting-assistant/
├── app/                   # aplikacioni web (FastAPI + faqja)
│   ├── main.py            # API
│   ├── pipeline.py        # përpunimi në sfond: transkriptim → folës → analiza → kontrolli i dublikimeve
│   ├── trackers.py        # Jira dhe Trello pas një ndërfaqeje të përbashkët (modeli Adapter)
│   ├── dedup.py           # kontrolli i dublikimeve në dy nivele (kandidatët + vendimi)
│   ├── memory.py          # historiku i detyrave, sinkronizimi i statusit, konteksti për mbledhjen e radhës
│   ├── jira.py            # dërgimi i tiketave të konfirmuara në platformë
│   ├── assistant.py       # "Pyet arkivin" (plani i kërkimit → burimet → përgjigje me burime)
│   ├── db.py              # SQLite: mbledhjet, vendimet, detyrat, indeksi i kërkimit
│   └── static/            # ndërfaqja (HTML, CSS, JS)
├── scripts/
│   ├── transcribe.py      # transkriptimi me faster-whisper
│   ├── diarize.py         # kush po flet (pyannote.audio)
│   ├── extract.py         # përmbledhja, vendimet, detyrat me Claude
│   ├── evaluate_wer.py    # matja e saktësisë (WER)
│   └── evaluate_dedup.py  # matja e kontrollit të dublikimeve (P/R/F1, pragu, konfigurimet)
├── data/
│   ├── audio/             # regjistrimet e mbledhjeve
│   ├── reference/         # transkriptet manuale (të sakta)
│   ├── samples/           # transkript shembull për provë
│   ├── uploads/           # skedarët e ngarkuar në aplikacion (jo në git)
│   └── app.db             # databaza e aplikacionit (jo në git)
├── output/                # rezultatet e transkriptimit
└── requirements.txt
```

## Aplikacioni web

```bash
python -m app
```

Pastaj hap http://127.0.0.1:8000 në shfletues. Serveri dëgjon vetëm në këtë kompjuter.

- **Mbledhje e re**: ngarko audio (mp3, wav, m4a, webm…), regjistro direkt nga mikrofoni,
  ose ngarko një transkript `.json` (p.sh. `data/samples/mbledhja_shembull.json`).
- Përpunimi bëhet në sfond: transkriptimi → folësit → vendimet dhe detyrat.
- **Detyrat**: shëno si të kryera; raporti në panel tregon sa janë kryer dhe sa janë me vonesë.
- **Pyet arkivin**: kërkon në të gjitha mbledhjet dhe përgjigjet me burimin (mbledhja + minuta).
- **Ekipi**: punonjësit, ekipet dhe projektet (një punonjës mund të jetë në disa). Shtoji një nga
  një ose importo me CSV (`Emri, Email, Roli, Ekipet, Projektet`, disa grupe ndahen me `;`).
  Kur mbledhja lidhet me një ekip/projekt, Claude i merr emrat e anëtarëve dhe detyrat lidhen
  me punonjësit (edhe sipas nofkave). Përgjegjësi mund të ndërrohet me dorë te detyra.

### AI: Claude ose Groq (falas, për testim)

Analiza, korrigjimi dhe “Pyet arkivin” punojnë me njërin nga këta (`scripts/llm.py`):

| `.env` | Ofruesi |
|--------|---------|
| `ANTHROPIC_API_KEY=sk-ant-…` | Claude (përdoret kur ekziston) |
| `GROQ_API_KEY=gsk_…` | Groq, falas, me `GROQ_MODEL` (parazgjedhur `openai/gpt-oss-120b`) |

`LLM_PROVIDER=groq` ose `anthropic` e detyron njërin. Me Groq transkriptet dërgohen te serverat e Groq;
përdore vetëm për testim, jo për mbledhje konfidenciale.

### Hyrja dhe rolet

Hyrja bëhet me emailin e punës: shkruan emailin, merr një kod 6-shifror (vlen 10 minuta).
Pa SMTP të konfiguruar, kodi shfaqet në terminalin e serverit.

| Roli | Kush | Çka mund të bëjë |
|------|------|------------------|
| Administrator | emailet te `ADMIN_EMAILS` ose të shënuar “Administrator” | gjithçka: ekipet, projektet, punonjësit, të gjitha mbledhjet, njoftimet |
| Project manager | PM-ja i caktuar te projekti (një për projekt) | shton mbledhje në projektet e veta, cakton detyra, konfirmon kontrollin e dublikimeve, i dërgon në Jira/Trello; merr me email përmbledhjet dhe përkujtuesin ditor |
| Punonjës | të tjerët | sheh detyrat e veta dhe mbledhjet e projekteve ku është anëtar; shënon detyrat e veta si të kryera |

- **Email (Gmail / Google Workspace)**: `SMTP_USER` + `SMTP_PASSWORD` (App password i Google).
  Pa to, asgjë s’dërgohet: emailet shihen te faqja “Njoftimet”.
- **Jira ose Trello**: te çdo projekt zgjidhet platforma (Jira me çelësin e projektit, p.sh. `PAY`,
  ose Trello me kodin e tabelës nga `trello.com/b/<kodi>/…`). Secili PM e lidh llogarinë e vet te faqja
  “Jira & Trello”; token-i ruhet i enkriptuar me `APP_SECRET`. Pa llogari të PM-së përdoren vlerat
  `JIRA_*` / `TRELLO_*` në `.env`. Me `JIRA_MOCK=1` nuk thirret asnjë platformë: tiketat ruhen te
  `data/tracker_mock.json` (aty mund të shtosh tiketa “të krijuara me dorë” për testim).

### Kontrolli i dublikimeve dhe memoria e detyrave

Pas analizës, çdo detyrë krahasohet me punën ekzistuese:

1. **Kandidatët**: detyrat e hapura dhe të mbyllura së fundmi nga mbledhjet e mëparshme të projektit
   dhe tiketat ekzistuese në Jira/Trello (të lexuara përmes API-së). Ngjashmëria llogaritet me
   embeddings shumëgjuhëshe (`sentence-transformers`) dhe me fjalë kyçe / kode tiketash (`PAY-12`).
2. **Vendimi**: modeli gjuhësor zgjedh *detyrë e re*, *dublikatë*, *përditësim* (afat/person i ri)
   ose *rihapje* (tiketa ishte mbyllur).

Asgjë nuk ndryshon në platformë pa konfirmimin e PM-së te karta “Existing work” në faqen e mbledhjes.
Përditësimi ndryshon afatin/personin dhe shton koment me lidhjen te mbledhja; rihapja e kthen
tiketën në status të hapur (Jira: tranzicioni i workflow-it; Trello: kartela kthehet nga lista “Done”).
Çdo ndryshim ruhet në historikun e detyrës, dhe statusi sinkronizohet me platformën çdo `SYNC_MINUTES`.

Për modelin e embeddings duhen rreth 0.5 GB; me `HF_HOME` mund ta vendosësh në një disk tjetër.

Matja (kriteri 2 te 4.6), me anotimet në formatin e `data/eval/dedup_pairs.json`:

```bash
python scripts/evaluate_dedup.py data/eval/dedup_pairs.json
```

Del P/R/F1 për *vetëm fjalë kyçe*, *vetëm semantike* dhe *të dyja*, për çdo prag, veçmas për
detyrat në të njëjtën gjuhë dhe në gjuhë të ndryshme, si dhe pragu më i mirë.

Çelësat vendosen në `.env` (shih `.env.example`), pastaj rinise serverin:

| Çelësi | Për çka | Pa të |
|--------|---------|-------|
| `ANTHROPIC_API_KEY` | përmbledhja, vendimet, detyrat, përgjigjet | vetëm transkripti dhe kërkimi i thjeshtë |
| `HF_TOKEN` | kush po flet (Folësi 1, 2…) | transkripti pa emrat e folësve |

## Instalimi

Kërkohet Python 3.10 ose më i ri.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

Për GPU NVIDIA duhen CUDA 12 dhe cuDNN 9. Pa GPU, skripta punon automatikisht në CPU.

## Përdorimi

1. Vendos regjistrimin në `data/audio/`, p.sh. `mbledhja1.mp3`.

2. Transkriptoje:
   ```bash
   python scripts/transcribe.py data/audio/mbledhja1.mp3
   ```
   Pa GPU, përdor modelin `medium` ose `small`, sepse `large-v3` në CPU është shumë i ngadalshëm:
   ```bash
   python scripts/transcribe.py data/audio/mbledhja1.mp3 --model medium
   ```

3. Shkruaj manualisht transkriptin e saktë në `data/reference/mbledhja1.txt`.

4. Mate saktësinë:
   ```bash
   python scripts/evaluate_wer.py data/reference/mbledhja1.txt output/mbledhja1.txt
   ```

## Shqipja e folur: fjalori dhe korrigjimi

Transkriptimi i shqipes përmirësohet në tri shtresa:

1. **Fjalori** (`data/fjalori.txt`): fjalët e dialektit, zhargonit, termat në anglisht dhe emrat.
   Whisper i merr si `initial_prompt` (stili + emrat) dhe `hotwords` (fjalët, për gjithë regjistrimin).
   Me `fjala = kuptimi`, Claude e kupton edhe kuptimin.
2. **Korrigjimi me Claude** (`scripts/correct.py`): ndreq vetëm fjalët e dëgjuara gabim sipas kontekstit,
   pa e kthyer dialektin në gjuhë letrare. Origjinali ruhet (`raw`) dhe shihet te faqja e mbledhjes.
3. **Analiza** i kupton fjalët jozyrtare dhe i shkruan detyrat në shqip standard.

Për ta matur efektin (WER) për tezën:

```bash
python scripts/transcribe.py data/audio/mbledhja1.mp3 --no-vocab --out output/pa_fjalor
python scripts/transcribe.py data/audio/mbledhja1.mp3 --out output/me_fjalor
python scripts/correct.py output/me_fjalor/mbledhja1.json --out output/me_fjalor
python scripts/evaluate_wer.py data/reference/mbledhja1.txt output/pa_fjalor/mbledhja1.txt
python scripts/evaluate_wer.py data/reference/mbledhja1.txt output/me_fjalor/mbledhja1.txt
python scripts/evaluate_wer.py data/reference/mbledhja1.txt output/me_fjalor/mbledhja1_corrected.txt
```

| Mbledhja | Modeli | Pa fjalor | Me fjalor | Me fjalor + korrigjim |
|----------|--------|-----------|-----------|-----------------------|
| mbledhja2 (sq) | large-v3 | | | |
| mbledhja3 (sq + en) | large-v3 | | | |

## Eksperimentet e fazës 1

Ruaj rezultatet në një tabelë, se do të të duhen për kapitullin e rezultateve:

| Mbledhja | Gjuha | Modeli | WER | Koha e përpunimit |
|----------|-------|--------|-----|-------------------|
| mbledhja1 | en | large-v3 | | |
| mbledhja2 | sq | large-v3 | | |
| mbledhja3 | sq + en | large-v3 | | |
