# Predicate rules — choosing the SKOS mapping predicate

> Canonical predicate *definitions* live in `docs/mapping-conventions.md`. This file is the
> **decision heuristic** for picking one, plus worked examples. Experts: add examples as
> calls get settled.

Direction is from the **subject** (the `b2ai:` dataset term) to the **object** (the ontology
term). Get the direction right — it is the most common mistake.

## Decision order (first that applies)

1. **`skos:exactMatch`** — the source term *is* the ontology concept: a verbatim label or an
   **exact** synonym, same granularity. (Not a broad/related synonym — those are not exact.)
2. **`skos:broadMatch`** — the ontology term is **genuinely broader** and *subsumes* the
   source. Typical for questionnaire item → umbrella phenotype (di_air_in "trouble getting air
   in" → Dyspnea).
3. **`skos:narrowMatch`** — the ontology term is a **subtype** of the source (the source is
   broader). E.g. general "pulmonary hypertension" → HPO *Pulmonary arterial hypertension*.
4. **`skos:relatedMatch`** — associated but not a clean hierarchical match; **always add a
   comment saying why.** Use when the source description and the ontology term only partly
   overlap.

When torn between two, pick the **weaker** one and leave a comment. Confidence convention:
exact ≈ 0.95, broad ≈ 0.8, narrow ≈ 0.75, related ≈ 0.6.

## The "A or B" conflation rule (don't invert the direction)

A single column meaning **two distinct concepts** ("pneumothorax **or** atelectasis") must
**not** `broadMatch` to just one of them — one sibling is not the *parent* of the other, so
the ontology term is *narrower* than the column, not broader. Instead:

- **Split** into one `narrowMatch` row per sense (subject repeats, objects differ), **or**
- **Retarget** to an ontology term that truly subsumes both.

Which one? **Check the candidates' parents.** If they share a near parent, retargeting to it is
cleaner (`no_appetite` → *Abnormal eating behavior*). If they sit in different branches, there
is no honest subsumer and you must split: PHQ-9 `feeling_bad_self` conflates worthlessness
(HP:0031469, under *Cognitive distortion*) with guilt (HP:6000011, under *Dysregulated negative
emotional state*) — no near common parent, so two `narrowMatch` rows at 0.75.

**A parent that covers *most* senses is not a subsumer.** This is the seductive failure: a
plausible parent covers two of three senses, retargeting to it feels like the tidy answer, and
the third sense disappears without a trace. Verify the candidate subsumes **every** sense before
retargeting. `ptsd_adult.irritable` ("extremely irritable or angry to the point of yelling,
fights, or destroying things") was proposed for HP:0031467 *Dysregulated negative emotional
state* on exactly those grounds — it *is* the parent of both *Irritability* and *Anger*. But the
behavioural escalation is HP:0000718 *Aggressive behavior*, which sits under *Disinhibition*, a
different branch, so HP:0031467 cannot reach it. Three senses, three `narrowMatch` rows;
retargeting would have silently dropped the clause that makes it a PTSD item at all.

**A parent can be structurally right and definitionally wrong.** The `is_a` check is necessary
but not sufficient — also read the parent's *definition* and confirm it describes the grouping
rather than just one of its children. `dsm5_adult.someone_hear_thoughts` ("someone could hear
your thoughts, or… you could hear another person's thoughts") was mapped `broadMatch` to
HP:5200419 *Disorder of thought control*, and the hierarchy genuinely backs that up: HP:0025777
*Thought broadcasting* **is_a** HP:5200419. But HP:5200419's definition is "a false belief that
another person… **controls** one's thoughts", i.e. delusion of control — which describes neither
clause of the item, and does not even describe two of its own four children (*Thought echo* is a
perception, *Thought blocking* is a cessation, neither is a belief about control). The class
behaves as a grouping term for thought alienation while its text defines one specific delusion.
Map the precise children instead.

The lesson generalises past this term: when a parent looks like a convenient umbrella, check
whether its definition was written for the group or inherited from one member. `search_hpo.py
<CURIE>` prints definition and parents together so the two can be compared in one look — and it
flags labels containing *disorder / syndrome / episode / disease*, which is what this one trips.

**When a sense has no term at all, retarget — splitting would drop it.** The mirror of the rule
above: there, a parent failed because it could not reach every sense; here, *splitting* fails
because the ontology has no term for one of the senses, so a per-sense split silently loses it.
`dsm5_adult.memory_issues` ("Problems with memory (learning new information) **or with location
(finding your way home)**") is one yes/no answer covering memory and spatial orientation. HPO has
no topographical-disorientation term — the nearest are *Left-right disorientation*,
*Impaired visuospatial constructive cognition* and *Confusion*, none of which is wayfinding — so
mapping the memory half to HP:0002354 asserts a specific deficit the answer does not establish.
Retargeted to HP:0100543 *Cognitive impairment*, which subsumes both and whose own definition
names "learning new things".

So the full decision for a conflated column: **every sense has a term and they share no near
parent → split; a sense has no term → retarget to a parent that covers all of them; no parent
reaches every sense → split and state the gap in the `comment`.** Searching for the missing term
and finding nothing is a result worth recording — write down what you searched, so the next
curator does not repeat it and can recognise the gap as an upstream request.

**First check whether the senses can co-occur — a disjunctive column cannot be split at all.**
Everything above assumes the senses are *conjunctive*: worthlessness and guilt can both be true
of one respondent, so each `narrowMatch` row is a legitimate partial claim. When the senses are
**mutually exclusive** — "X **or its opposite**" — a one-sided row is not a partial claim but a
coin flip, because a "yes" cannot be attributed to a pole. PHQ-9 `move_speak_slow` ("moving or
speaking so slowly that other people could have noticed? **Or the opposite** — being so fidgety
or restless…") is disjunctive. The restless pole even has a clean term (HP:0000711 *Restlessness*,
exact synonym "Fidgetiness"), which makes a split look available — but mapping it would assert
psychomotor agitation for respondents who endorsed the item because they had slowed down.
Leave a disjunctive column unmapped unless a term genuinely subsumes both poles.

So the very first question about a conflated column is not "split or retarget" but **"can these
senses be true at the same time?"** — no is a hard stop.

**A conflation is easy to miss when the `subject_label` is abbreviated.** That row read
"Feeling bad about yourself - or that you are a failure", which looks single-sense; the data
dictionary's actual text ends "…*or have let yourself or your family down*", which is the guilt
half. Copy the full item text from the data dict before judging a column — and note that a
`subject_label` is only a human aid, so the validator can never catch this for you.

## Worked examples (from real B2AI curation)

| Source (description) | Object | Predicate | Why |
| --- | --- | --- | --- |
| `shortness_breath` | HP:0002094 Dyspnea | exact | "Shortness of breath" is an exact synonym |
| `di_air_in` "trouble getting air in" | HP:0002094 Dyspnea | broad | item is a specific expression of the umbrella phenotype |
| `pulmonary_hypertension` | HP:0002092 Pulmonary arterial hypertension | narrow | HPO term is the arterial subtype |
| `no_appetite` "poor appetite **or** overeating" | HP:0100738 Abnormal eating behavior | broad | retargeted to a term that subsumes both poles |
| `brain_tumor` "Brain tumor" **and** `brain_cancer` "Brain cancer" | HP:0030692 Brain neoplasm | **exact** *and* **broad** — same term, two predicates | HPO defines it as "a benign **or** malignant neoplasm": that is what *tumor* means (exact), and wider than *cancer*, which is malignant only (broad) |
| `headache_migraine` "Headache or migraine" | HP:0002315 Headache **+** HP:0002076 Migraine | broad **+** narrow | the rare conflation with *both* a true subsumer and a named sub-sense: Migraine **is_a** Headache, so Headache alone is already honest — the second row just records the sense the column names |
| `speech_difficulty` "Speech difficulty" | HP:0002167 Abnormal speech pattern | related | **trap 1:** the label reads like an umbrella, but the definition is "an abnormality in the **sound (volume) or cadence (rate)** of speech" — it does not reach articulation or word-finding |
| `panic_disorder` (a disorder) | HP:0025269 Panic attack | broad — **contested**, see *A disease column cannot hierarchically match a phenotype* | disorder → its core phenotype; HPO term is the manifestation the disorder subsumes (cf. `anxiety_disorder`→Anxiety) |
| `acid_reflux` "heart burn, GERD" | HP:0002020 Gastroesophageal reflux | exact | GERD is an exact synonym — a named disease *with* an exact HPO term stays exact |
| `stroke` "stroke **and/or** aphasia" | HP:0001297 Stroke **+** HP:0002381 Aphasia | narrow ×2 | split; each sense is narrower than the column |
| `dizziness` "lightheadedness or balance disturbance" | HP:0002321 Vertigo | related | **trap 1:** HPO *Vertigo* = spinning, which the source excludes — synonym match is misleading, so downgrade + comment |
| `phq9.feeling_depressed` "Feeling down, depressed, or hopeless" | HP:5200273 Pathological sadness | exact — **contested**, see *One term, three predicates* | **trap 2:** *not* HP:0000716 *Depression*, whose exact synonym is "Depressive episode" — a syndrome, not one item. HP:5200273's exact synonyms ("Down in the dumps", "Feeling hopeless all the time") match the item verbatim |
| `custom_affect_scale.sad_or_down` "Sad or down" | HP:5200273 Pathological sadness | related | same term, weaker predicate: one momentary 0–10 rating does not establish the excess in intensity/duration that makes sadness *pathological* |
| `phq9.trouble_concentrate` "Trouble concentrating… reading the newspaper or watching television" | HP:0031987 Diminished ability to concentrate | exact | exact synonyms "Concentration problems"/"Poor concentration", and the HPO definition names re-reading text without understanding — the item's own example |
| `adhd_adult.difficulty_attention` "difficulty keeping your attention when doing boring/repetitive work" | HP:0000736 Short attention span | broad | **sibling terms, don't merge:** HP:0000736 is *distractibility and impulsivity*, HP:0031987 is *failure to sustain focus*. The ASRS construct is the former, so this row stays put while the PHQ-9 one moves |
| `panas.hostile` "Hostile" | HP:0031473 Anger | related | "Hostile"/"Hostility" are **exact synonyms of Anger**, not of Irritability — found only by re-checking every row that used HP:0000737, not from the review |
| `custom_affect_scale.irritated` "Irritated or angry **(towards something or someone)**" | HP:0000737 + HP:0031473 | related ×2 | near-synonyms in English, distinct in HPO: *Irritability* is an undirected lowered threshold, *Anger* is hostility **directed** at a provocation — which the parenthetical states outright |

## Worked examples from the 2026-08-24 clinical review

A clinician (Sek Won Kong) reviewed the shipped questionnaire mappings. Every call below is an
**expert override of an agent proposal** — the highest-value kind of example this file can carry.

| Source (description) | Was | Now | The lesson |
| --- | --- | --- | --- |
| `gad7_anxiety.afraid_of_things` "Feeling afraid, as if something awful might happen" | broad → HP:0033845 *Sense of impending doom* | **narrow** | "Subj is broader than Obj." HPO's "life-threatening or tragic is about to occur" is the *stronger* phenomenon — and the row's own comment said so while the predicate said the opposite |
| `ptsd_adult.losing_interest` "Losing interest in activities you used to enjoy **before a stressful experience**" | exact → HP:0012154 *Anhedonia* | **broad** | the item is event-scoped; Anhedonia covers event-related and unrelated alike, so the HPO term is broader |
| `dsm5_adult.feeling_down` "Feeling down, depressed, or hopeless" | exact → HP:5200273 *Pathological sadness* | **related**, ungated | a normal-range mood descriptor is not the *pathological* term, however well the synonyms match |
| `dsm5_adult.feeling_panic` "Feeling panic or being frightened" | broad → HP:0025269 *Panic attack* | **related**, ungated | a named clinical entity is not the feeling that names it. Contrast `panic_disorder` (a reported **diagnosis**) → Panic attack, which stays `broad` pending the MONDO layer — the column kind decides |
| `dsm5_adult.feeling_detached` "…from yourself, your body, surroundings, or memories" | exact → HP:5200217 *Depersonalization* | **narrow ×2**: + HP:5200218 *Derealization*, ungated | a conflation the agent read as one sense: "yourself/your body" is depersonalization, "surroundings" is derealization |
| `dsm5_adult.someone_hear_thoughts` inbound sense ("you could hear another person's thoughts") | related → HP:0025776 *Thought insertion* | **row removed** | "I don't think there is an HPO term for this" |
| `dsm5_adult.self_harm` "**Thoughts of** actually hurting yourself" | related → HP:0100716 *Self-injurious behavior* | **row removed** | same: HPO has no self-harm-*ideation* term |
| `pediatric_hqa.peds_phqa_feeling_depressed` "Feeling down, depressed, **irritable**, or hopeless" | broad → HP:5200273 | **narrow ×2**: + HP:0000737 *Irritability* | the PHQ-**A** adds a sense the adult PHQ-9 item does not have. Never assume a paediatric variant is the adult item |
| `pediatric_hqa.peds_phqa_thoughts_death` "better off dead, **or of hurting yourself**" | exact → HP:0031589 *Suicidal ideation* | **related** | conflates a passive death wish with self-harm ideation; the term matches neither exactly |
| `pediatric_vhi10.peds_dry_raspy_hoarse` "dry, raspy, and/or hoarse" | broad → HP:0001609 *Hoarse voice* | **narrow** | three senses in one item; *Hoarse voice* is one of them, so it is narrower than the column |

Three rules generalise out of that list.

**`relatedMatch` is for *associated* concepts, not for "closest available term".** Two rows were
deleted outright rather than downgraded. The tell is a `comment` that argues the term means
something else — "*Thought insertion* emphasises alien thoughts imposed on the person rather than
another's thoughts perceived", "HPO term denotes the behavior" — which is a description of a gap,
not of a mapping. **A gap is a result: record what you searched and leave the sense unmapped.** A
`relatedMatch` placed there survives review as a real assertion and misleads a downstream consumer
who never reads the comment.

**Thought vs act — the ideation twin of "state vs behaviour".** `dsm5_adult.self_harm` asks about
*thoughts of* hurting yourself; HP:0100716 *Self-injurious behavior* is the act. HPO carries
ideation terms only where it has minted them (HP:0031589 *Suicidal ideation*), so the absence of a
self-harm-ideation term is a genuine gap, not an invitation to use the behaviour term. Check the
item's verb before the item's topic.

**A near-synonym is not an `exactMatch` when one side carries a severity or diagnostic claim.**
"Feeling down, depressed, or hopeless" reads like *Pathological sadness*, whose exact synonyms are
"Down in the dumps" and "Feeling hopeless all the time" — Trap 2 below uses that synonym list to
*promote* the row to `exactMatch`. The reviewer pushed back: the HPO definition requires sadness
"excessive in intensity, duration, or resistance to self-regulation", which a single screener item
at "several days" does not establish. **Exact synonyms justify the topic, not the severity.** When
the ontology definition adds a threshold the item does not test, downgrade. This rule has *not*
yet been applied to `phq9.feeling_depressed`, which carries the identical item text and is still
`exactMatch` and gated — see *One term, three predicates* under Trap 2.

### "Cancer" is malignant; "neoplasm" and "tumor" are not

A `<site> cancer` column is **not** an `exactMatch` for HPO's `Neoplasm of the <site>`, whose
definitions read "a benign **or** malignant neoplasm" — the HPO term is strictly wider, so the
column `broadMatch`es it. Eight confounders columns were corrected this way on the 2026-08-24
clinical review. The reviewer left `confounders.brain_tumor` alone on the *same* HPO term, which
is right: *tumor* is benign-or-malignant too, so that one really is exact.

**Check that the umbrella exists before flipping the predicate.** The rule needs an HPO *neoplasm*
term to point at, and one column had none: `thyroid_cancer` targeted HP:0002890 *Thyroid
carcinoma*, which is malignant-specific and a **child** of HP:0100031 *Neoplasm of the thyroid
gland*. Flipping the predicate alone would have asserted that a malignancy-specific term is
broader than "thyroid cancer", which is backwards. Applying a reviewer's rule sometimes means
retargeting the object, not just editing one column — and HPO does not always carry a generic
"<site> cancer" term (searched: only *Medullary* and *Non-medullary thyroid carcinoma* exist).

### A disease column cannot hierarchically match a phenotype

`confounders.epilepsy` → HP:0001250 *Seizure* was `broadMatch` under the "disorder → its core
phenotype" convention in the table above. The clinical review downgraded it to `relatedMatch`:
epilepsy is a **disease** and Seizure is a **phenotype**, so neither subsumes the other — a
seizure can occur without epilepsy, and the diagnosis is not a kind of seizure.

That is the same observation as the reviewer's "Mapping MONDO?" on ~25 other confounders columns,
and it is now settled: **a disease-kind column belongs in a disease ontology.** Nineteen of them
have MONDO rows in `mappings/b2ai-voice-conditions.sssom.tsv`, and their HPO rows were resolved
two ways, which is the distinction to carry forward:

- **The HPO term is the *same concept* as the column** (`asthma`, `bipolar_disorder`, `diabetes`,
  `schizophrenia`, …). Both rows stay `exactMatch`. Mapping one subject into two ontologies is
  ordinary SSSOM, not a contradiction — `HP:0002099` *Asthma* and `MONDO:0004979` *asthma* are the
  same thing said twice.
- **The HPO term is a *manifestation* of the disease** (`anxiety_disorder`→Anxiety,
  `panic_disorder`→Panic attack, `parkinsons_disease`→Parkinsonism, `obsessive_compulsive_disorder`
  →Compulsive behaviors, `eating_disorder`→Abnormal eating behavior, `epilepsy`→Seizure). Demoted
  to `relatedMatch`; the MONDO row is now the column's real mapping.

Those six were exactly the rows the retired convention covered, which is what made the line
drawable: *is the HPO term the disease, or a sign of it?*

**A "no term" answer is a result, not a failure — and this is where it pays.** Seven diagnosis-kind
columns have **no** MONDO row because MONDO has no term for them: `chronic_cough`,
`glottic_insufficiency`, `laryngospasm_irritable_larynx`, `lesions_vocal_cord`,
`muscle_tension_dysphonia`, `swallowing_disorder`, `vocal_cord_dysfunction`. That is the
voice/laryngology core of this dataset, and those columns stay HPO-only — the near-misses are
traps, not candidates (`MONDO:0007876` is *laryngeal abductor paralysis*, a hereditary disease, not
functional vocal cord dysfunction; the `dysphagia` hits are all syndromes that merely *feature*
dysphagia). **Search before assuming a disease ontology is the better home**: for a functional
voice finding, HPO is not the fallback, it is the right answer.

### Map the wording the participant answered, not the source's concept name — and say which

A source can attach a concept name to an item that is narrower, or just different, from the
question actually asked. AI-READI's OMOP export labels `mhoccur_crt` *Bilateral cataracts* for the
item "Cataracts (in one or both eyes)", `mhoccur_ear` *Ear anomalies with hearing impairment* for
"Hearing impairment", and `mhoccur_ra` *Arthritis* for "Arthritis (joint pain)". The clinical
review's rule (2026-11-03): **"if this information is extracted from EHR, the mapping is exact"** —
i.e. the predicate depends on *what the row is evidence of*. An EHR-coded row is evidence of the
coded concept, so the concept name is the subject. A self-report row is evidence of the answer to
the question, so the **question wording** is the subject and the concept name is a relabelling to
ignore. All three items are REDCap self-report ("Has a doctor … ever told you that you have/had …"),
so `cataract`, `hearing loss disorder` and `arthropathy` stand. Two habits follow:

- **Establish provenance before choosing a predicate**, and record it in the file header, not
  just in your head — the same variable names would flip to `exactMatch` on an EHR-sourced release.
- When `subject_label` is the source's concept name (kept for traceability), **quote the asked
  wording in the comment** wherever the two differ, so a reviewer can see which one the predicate
  was chosen against.

## Trap 1 — lexical match ≠ semantic match

If the ontology term matched only because a **word** matched (label/synonym), re-read the
source **description**. If the clinical sense differs (dizziness≠vertigo), the lexical hit is
wrong — downgrade to `relatedMatch` (or pick a better term / split), and never leave it as
`exactMatch`.

### State vs behaviour — a feeling word that implies an act

Affect words often name an action ("hostile", "aggressive", "explosive"). An instrument that
rates a **feeling** maps to the emotional-state term, never to the behaviour term, however
strongly the word connotes the act. HPO keeps these in separate branches — states under
HP:0100851 *Abnormal emotional state*, behaviours under HP:0000734 *Disinhibition* — so the
choice is structural, not stylistic. PANAS `hostile` maps to HP:0031473 *Anger* (a state, with
"Hostile"/"Hostility" as exact synonyms), **not** HP:0000718 *Aggressive behavior* (an act aimed
at harming), because the data dictionary defines the item as "feeling hostile (angry or
irritable)". The reverse also holds: `ptsd_adult.irritable` earns its HP:0000718 row precisely
because the item *does* name acts — "yelling, fights, or destroying things".

Reading the data-dictionary `description` is what settles it. The column name alone ("hostile")
cannot tell you whether the instrument is asking about a feeling or an act.

## Trap 2 — the granularity trap: a symptom-sounding label that denotes a *syndrome*

Trap 1 is the right level, wrong sense. This one is the right *topic*, wrong **level**: the term
names a syndrome/episode/disorder — a whole cluster of features — while your column is one item
*of* that cluster. Mapping the item to it overstates what a single answer can assert.

**Judge a term by its definition and synonyms, never by its label.** HPO `Depression`
(HP:0000716) looks like a perfect hit for "Feeling down, depressed, or hopeless". But its exact
synonym is **"Depressive episode"**, and its definition enumerates a full cluster — pessimism,
pervasive shame, low self-worth, *"thoughts of suicide and engaging in suicidal behavior"*. That
is a PHQ-9 **total score**, not PHQ-9 item 2.

```
uv run python .claude/skills/curation-assist/scripts/search_hpo.py HP:0000716
```

Inspect mode prints the full definition, every synonym class, the editor comment and the is_a
parents, and warns when *episode / disorder / syndrome / disease* shows up in the label or exact
synonyms. Treat that warning as "go read the definition", not as a verdict.

**The same check promotes as well as demotes.** The synonym list is often what justifies an
`exactMatch` the primary label would never suggest: HP:5200273 *Pathological sadness* reads
nothing like the PHQ-9 item, but its **exact** synonyms include "Down in the dumps" and "Feeling
hopeless all the time" — so that row is `exactMatch` 0.95, not `broadMatch` 0.8. Rule 1 above
says exact synonyms count; this is the reminder to actually *look at the list*.

### One term, three predicates — an open question on HP:5200273

**That promotion is contested.** The 2026-08-24 clinical review applied the opposite reasoning to
`dsm5_adult.feeling_down`, whose item text is *word for word* the PHQ-9 row's — "Feeling down,
depressed, or hopeless" — and downgraded it to `relatedMatch`, ungated, because HP:5200273's
definition requires sadness "excessive in intensity, duration, or resistance to self-regulation"
and one screener item does not establish that. Four rows now point at this one term:

| Row | Item text | Answer scale | Predicate |
| --- | --- | --- | --- |
| `phq9.feeling_depressed` | "Feeling down, depressed, or hopeless" | 0–3 frequency, 2 weeks | `exactMatch` 0.95, gated `>=1` |
| `dsm5_adult.feeling_down` | **identical** | 0–4 frequency, 2 weeks | `relatedMatch` 0.6, ungated |
| `custom_affect_scale.sad_or_down` | "Sad or down" | 0–10 momentary | `relatedMatch` 0.6, ungated |
| `pediatric_hqa.peds_phqa_feeling_depressed` | + "irritable" | PHQ-A 0–3 | `narrowMatch` ×2 |

Rows 3 and 4 are principled — a momentary state rating, and a conflated item. **Rows 1 and 2 are
not distinguishable on any ground the file states**: the reviewer flagged one and not the other,
and whether PHQ-9 should follow was sent back to him rather than guessed, because the `>=1` gate
rides on the answer (PHQ-9 depressed mood leaves the output entirely if it does).

Two things follow for anyone curating here before that returns:

- **Don't reconcile them by find-and-replace.** Some rows on this term *should* differ — see the
  next subsection. Reconciling by hand-wave would flatten a real distinction to fix an accidental
  one.
- **Don't read row 1 as settled precedent.** Cite *Exact synonyms justify the topic, not the
  severity* (above) as the live rule, and this row as the case it has not yet been applied to.

The general shape is worth recognising: a reviewer works down a sheet and flags one instance of a
pattern. **Before shipping their call, grep the file for the same item text** — a flag on one of
two identical rows is nearly always fatigue, not a judgment that they differ.

### The column kind decides the granularity — never blanket-replace a term

The identical source words map to **different** terms depending on what kind of column it is:

| Column | Kind | Object | Why |
| --- | --- | --- | --- |
| `phq9.feeling_depressed` "Feeling down, depressed, or hopeless" | screener **item** | HP:5200273 Pathological sadness | one symptom, rated 0–3 |
| `confounders.depression_major_depressive_disorder` "Depression or major depressive disorder" | reported **diagnosis** | HP:0000716 Depression | the column *is* the syndrome, so the syndrome-level term is correct |

So when a term turns out to be wrong for one row, **classify each other row using it before
changing it** — a find-and-replace across the mapping files would have broken the second row
here. (Real example: PR #10 review, 5 symptom rows moved off HP:0000716, 2 diagnosis rows
deliberately kept.)

The same discipline applies when the two candidates are **siblings** rather than different
levels. HP:0000736 *Short attention span* (distractibility + impulsivity) and HP:0031987
*Diminished ability to concentrate* (failure to sustain focus) share a parent and read alike,
but they are different constructs: the PHQ-9/PHQ-A "trouble concentrating" items belong to the
second, while the ASRS inattention item belongs to the first. Three rows used HP:0000736; two
moved and one stayed. **When you keep a row that a reviewer might expect to change, say so in
its `comment`** — otherwise it reads as an oversight rather than a decision.

### Check whether the term is contested

Before leaning on a term for many rows, search the ontology's issue tracker. HP:0000716 is
[proposed for renaming to *Depressive Episode*](https://github.com/obophenotype/human-phenotype-ontology/issues/11460)
— filed precisely because "Mania and Depression lean towards a disease rather than a phenotype".
A pending rename is two warnings at once: the term is semantically contested, and when the
rename lands `validate-mappings` will fail every row whose `object_label` still says
`Depression`. That failure is the guardrail working — but better to not build on sand.

### Cross-check against an independent curation, when one exists

For **standard instruments** (PHQ-9, GAD-7, MADRS, …) someone may have already curated
item→HPO mappings. Diffing against one is a far stronger check than "does the code resolve",
because it surfaces *omissions* and *judgment differences*, which no validator can catch.
[`rorschach-rs`](https://github.com/SmartMonkey-git/rorschach-rs) (`src/presets/*.rs`) carries
Robinson-group mappings for PHQ-9, GAD-7, ASRM, YMRS, MADRS and HAM-A. Treat a disagreement as
a question to resolve, not an error — but resolve it explicitly.
