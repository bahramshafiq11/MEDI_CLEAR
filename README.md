# MediClear

**Turns a doctor-patient conversation (audio, video or text) into a clinical note, ICD-10 codes and a draft bill, all on your own computer, for free.**

```
Recording / video / transcript
   → Speech to text (Whisper)
   → Who is speaking: Doctor / Patient (AI)
   → Medical information: symptoms, diagnoses, medicines, services (AI, fixed answer format)
   → Misheard drug names fixed + hallucination check (medicine list, fuzzy matching)
   → Medical words explained in plain language (glossary)
   → ICD-10 codes, matched by meaning (code list + fuzzy matching)
   → SOAP note + simple patient summary, built from the extracted facts (template, no AI)
   → Draft bill from services performed (fee schedule)
   → Doctor reviews, edits and approves
```

Everything runs offline after the first setup. No patient data leaves the computer.

---

## 1. What you need

- A Windows, Mac or Linux computer with **at least 8 GB RAM** (16 GB is much smoother)
- About 10 GB of free disk space
- Internet **only for the first setup** (to download Python packages and the AI models)

## 2. Setup (one time)

### Step 1: Install Python
Download **Python 3.11 or 3.12** from https://www.python.org/downloads/
On Windows, **tick "Add Python to PATH"** on the first screen of the installer.

### Step 2: Install Ollama (runs the AI model locally)
1. Download and install from https://ollama.com/download
2. Open a terminal (Windows: press Start, type `cmd`, press Enter) and run:
   ```
   ollama pull qwen2.5:7b
   ```
   This downloads the AI model (about 4.7 GB). On a PC with 8 GB RAM, use the smaller one instead:
   `ollama pull qwen2.5:3b` (and see "Settings" below to switch to it).

### Step 3: Install the project
In the terminal, go into the project folder and run these one by one:

**Windows**
```
cd path\to\mediclear
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**Mac / Linux**
```
cd path/to/mediclear
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Run it

Every time you want to use the app:
```
cd path\to\mediclear
venv\Scripts\activate          (Mac/Linux: source venv/bin/activate)
uvicorn app.main:app --reload
```
Then open **http://localhost:8000** in your browser.

The badge in the top-right corner should say **"AI ready"**. If it says something else, see "Common problems" below.

### Try it in 1 minute
1. Choose **"Load a sample conversation…"** and pick one of the three samples
2. Press **Process consultation**
3. Look through the tabs: Summary, Conversation, SOAP note, Simple view, Codes, Bill

The three samples show different situations:
| Sample | What it tests |
|---|---|
| jargon htn dm | Doctor uses abbreviations (HTN, DM, OD, TDS PRN) |
| everyday suspected typhoid | Everyday language, small talk, a diagnosis that is only *suspected* |
| procedure wound dressing | Procedures done in the clinic (injection, stitches, dressing) get billed |

**Audio and video:** upload an mp3, wav, m4a, mp4, mov, webm, etc. The **first** upload downloads the Whisper speech model (about 500 MB) once. After that it works offline. The recording is deleted after it's turned into text.

---

## 4. Settings

Open `app/config.py` to change:

| Setting | Default | What it does |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5:7b` | AI model. Use `qwen2.5:3b` on weaker PCs |
| `WHISPER_MODEL` | `small` | `base` is faster, `medium` is more accurate |
| `WHISPER_TASK` | `auto` | `auto`: English is written down as spoken, **any other language is translated to English** automatically |
| `WHISPER_BEAM_SIZE` | `1` | `1` is fastest; `5` is slightly more accurate but about twice as slow |
| `CODE_AUTO_ACCEPT` | `85` | Code matches below this score are flagged "check" |
| `CURRENCY` | `Rs.` | Shown on bills |

Data you can edit (in the `data` folder, open with Excel or Notepad):
- `fee_schedule.csv`: your services and prices
- `icd10_common.csv`: diagnosis codes and the different ways people say them (synonyms)
- `glossary.json`: medical words and their plain meanings
- `medicines.txt`: real drug names, used to fix names that speech-to-text misheard
- `whisper_vocabulary.txt`: medical words Whisper should expect while listening
- `samples/`: add your own mock conversations as `.txt` files

---

## 5. Project structure

```
mediclear/
├── app/
│   ├── main.py        Web server and API (FastAPI). API docs at http://localhost:8000/docs
│   ├── pipeline.py    Runs all the steps in order
│   ├── transcribe.py  Audio/video → text (faster-whisper)
│   ├── llm.py         Talks to the local AI model (Ollama)
│   ├── extract.py     AI prompts: who is speaking, medical information
│   ├── notes.py       SOAP note + patient summary from the extracted facts (no AI, instant)
│   ├── medicines.py   Fixes misheard drug names ("pridness alone" → prednisolone)
│   ├── checks.py      Hallucination check: flags medicines/diagnoses not found in the conversation
│   ├── simplify.py    Medical words → plain language (glossary, medicine timings)
│   ├── coding.py      Diagnoses/symptoms → ICD-10 codes
│   ├── billing.py     Services performed → draft bill
│   ├── database.py    Saves consultations (SQLite file: data/mediclear.db)
│   └── config.py      Settings
├── static/            The web page (HTML, CSS, JavaScript)
├── data/              Code list, fee schedule, glossary, sample conversations
├── tests/cases/       Test cases with correct answers (used by evaluate.py)
├── evaluate.py        Runs all test cases and scores the results
└── requirements.txt   Python packages
```

---

## 6. How it solves "every doctor says it differently" (for your report/viva)

1. **The AI normalizes meaning.** It reads "sugar problem", "known case of DM" or "high sugar" and writes a standard medical name ("Type 2 diabetes mellitus"), while keeping the exact words that were said.
2. **Codes come from a fixed list, never from the AI.** `coding.py` compares that name and the original words against every code's description and synonyms using word-overlap and fuzzy matching. So "HTN", "high BP" and "blood pressure is high" all land on **I10**, and the AI can't invent a code.
3. **Uncertainty is visible.** Weak matches are marked "check". If nothing matches well, it goes to "Not coded" for a human coder.

Safety rules built into the logic:
- **Suspected diagnoses are never coded as confirmed.** "Maybe typhoid, let's test" → typhoid is not coded; the symptoms (fever, abdominal pain) are coded instead, which is standard practice.
- **Bills come from services performed, not from the diagnosis.** Prices come only from `fee_schedule.csv`.
- **Tests ordered for later aren't billed** by the clinic; routine things like a BP check are included in the consultation fee.
- **Unknown services get price 0 and a flag**, never a guessed price.
- **Nothing is final until the doctor approves.** Approved records are locked until reopened.
- **Hallucination check:** any medicine or diagnosis that doesn't appear in the conversation is flagged.
- **Drug-name corrections are shown**, e.g. "heard as 'pridness alone'", so the doctor can confirm them.

---

## 7. Testing and scoring (evaluation)

`evaluate.py` runs every test case in `tests/cases/` through the system and checks the results against the
correct answers: diagnosis codes, bill items and total, medicines, allergies, tests ordered and visit type.
For recordings it also measures **word accuracy** of the transcription against the answer key.

```
python evaluate.py                 (all 6 test cases)
python evaluate.py asthma          (only cases with "asthma" in the folder name)
python evaluate.py --no-speakers   (skip the who-is-speaking step)
```
Ollama must be running; the web server doesn't need to be. Results are printed and saved in
`evaluation_results/` as a Markdown table (ready for your report) and a CSV.

**Add your own test case:** make a new folder in `tests/cases/` with `recording.mp3` (or `transcript.txt`),
an optional `answer_key.txt`, and an `expected.json` (copy one from another case and edit it).

---

## 8. Common problems

| Problem | Fix |
|---|---|
| Badge says "AI not running" | Open the Ollama app (Windows/Mac) or run `ollama serve` |
| Badge says "Model missing" | Run `ollama pull qwen2.5:7b` (or the model set in config.py) |
| `'uvicorn' is not recognized` | You forgot to activate the venv (`venv\Scripts\activate`) |
| Processing takes very long | Use `qwen2.5:3b` and `WHISPER_MODEL = "base"`; close other programs |
| "isn't valid JSON" error | Press Process again; if it keeps happening use the 7b model instead of 3b |
| Speakers are labelled wrong | Paste the transcript with `Doctor:` / `Patient:` at the start of lines, or try a bigger model |

---

## 9. Next steps (ideas to grow the project)

1. **Full ICD-10 list:** replace the 57-code starter list with the complete list and add an embeddings search for better meaning-matching.
2. **Ask questions about past visits (RAG):** "What medicines has this patient been on?" across all saved consultations.
3. **Mixed-language conversations:** auto mode translates non-English recordings already; test mixed-language mock recordings and add local-language words to the synonyms and glossary.
4. **Login for doctors and staff**, plus a patient table so visits are linked to one patient.
5. **Evaluation:** record 10–20 mock consultations, write down the correct codes and bill by hand, and measure how often the system matches. This makes a strong results chapter.

> ⚠ **Important:** The ICD-10 codes and prices included are starter/sample data for a student project. Check codes against the official WHO ICD-10 browser (https://icd.who.int/browse10) and use your clinic's real fee list before any real use. Use only mock recordings, or real ones with the patient's consent.
