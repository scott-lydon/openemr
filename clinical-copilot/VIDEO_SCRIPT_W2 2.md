# Week 2 Demo Video Script

> **Length target:** 5 minutes
> **Recording:** OBS Studio for cursor highlight + zoom; export 1080p MP4; upload to YouTube as **Unlisted** (NOT Private).
> **Audience:** the cohort grader, who has read `W2_QUALITY_PLAN.md` and `W2_ARCHITECTURE.md` and wants to see the rubric anchors fire on a live system.

---

## Cold open (0:00 - 0:30)

Visible on screen: the deployed URL `http://5.161.253.237/` with the OpenEMR sign-in page.

Voiceover:

> "This is the Week 2 build of the Clinical Co-Pilot for OpenEMR. The agent extracts structured clinical data from uploaded PDFs, retrieves grounded guideline evidence, and surfaces every claim with a clickable citation back to the source. I'll walk through five rubric anchors in five minutes."

---

## Anchor 1 — Document upload + structured extraction (0:30 - 1:30)

Steps on screen:

1. Sign in `admin` / `pass`. Open Demo Patient chart.
2. Click the Co-Pilot launch.
3. Drop `hba1c_basic.pdf` onto the upload pane.
4. Within ~5 seconds the chat pane shows: "1 lab result extracted. HbA1c 6.8% (normal range 4.0-5.6)."
5. Click the citation chip on "HbA1c 6.8%". The side panel opens with the rendered page and an orange box around the value.

Voiceover (highlight as you go):

> "MIME sniffing, ClamAV scan, PDF sanitization, two-pass VLM extraction, FHIR DocumentReference write, queue insert. The bounding box came back native from the model and renders pixel-accurate against the page."

---

## Anchor 2 — Grounded retrieval with cited claims (1:30 - 2:30)

Steps on screen:

1. In the chat: "Is her diabetes well controlled per ADA?"
2. Response: "HbA1c 6.8% is within the ADA's recommended range for most non-pregnant adults with type 2 diabetes [1] [2]."
3. Click chip [1] — bbox preview opens (the lab citation).
4. Click chip [2] — guideline card opens with the ADA section path and a deep link to the source page.

Voiceover:

> "Hybrid retrieval: Postgres BM25 plus pgvector dense search, fused by Reciprocal Rank Fusion, reranked by Cohere v3. The reranker isolation guard refuses to send patient identifiers; the unit test asserts that contract."

---

## Anchor 3 — Sanitization stack catches a prompt injection (2:30 - 3:30)

Steps on screen:

1. Drop `prompt_injection_in_freetext.pdf` onto the upload pane.
2. Ask: "What is this patient's chief concern?"
3. Response: chief concern reported, NO injected text echoed.
4. Open the trace dump. Highlight `sanitize.layer4.blocked = true`.

Voiceover:

> "Seven layers of defense in depth. Layer 4 is LLM Guard plus Rebuff ensembled; the user-facing response never sees the injection verbatim."

---

## Anchor 4 — Eval suite + hard regression gate (3:30 - 4:30)

Steps on screen:

1. Open a terminal split. Run `pytest evals/golden_w2/ -q`. Show 51 cases passing.
2. Run `python evals/regress_self_test.py`. Show the deliberate regression applied, the test run failing, the file restored, and `SELF TEST PASSED: gate fired on the deliberate regression.`

Voiceover:

> "51 hand-curated cases, every one with a documented failure mode and rationale. The hard gate from the rubric: the eval suite must catch a deliberate regression. The self-test proves it."

---

## Anchor 5 — Operational dashboard + cost ceiling (4:30 - 5:00)

Steps on screen:

1. Open Grafana at `:3000`. Show all 8 panels rendering data: latency, cost rolling 24h/7d, queue depth, eval pass rate, reranker degradation, VLM confidence, sanitization blocks, PHI scrubbed by kind.
2. Show the cost-ceiling soft alert log line in the trace.

Voiceover:

> "Cost ceiling enforces at runtime, not post-hoc. Soft alert at 80% of envelope, hard cutoff at 100% returning a 503. Every span attribute named in the architecture document is visible in the dashboard."

---

## Outro (5:00)

Voiceover:

> "Architecture defense, eval results, and the AI interview prep document are linked in the description. Repository at github.com/scott-lydon/openemr branch w2-quality."

---

## Recording checklist

- [ ] OBS scene set: cursor highlight, key-press overlay, system audio off, mic input level peak around -12 dBFS.
- [ ] Browser zoom 110% so the citation chips are legible on YouTube's mobile player.
- [ ] Database seeded with the demo patient + the 8 seed eval fixtures so each anchor has a clean run.
- [ ] Hetzner instance pre-warmed (run a dummy chat 30 seconds before recording so the spaCy model is loaded).
- [ ] Recording is 1080p, MP4, H.264 baseline, audio AAC 192 kbps.
- [ ] Upload as **Unlisted** (NOT Private). Capture the share URL into `SUBMISSIONS.md`.
