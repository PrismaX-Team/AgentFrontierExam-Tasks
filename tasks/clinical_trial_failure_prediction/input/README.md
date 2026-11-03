# Clinical trial failure prediction: data

Each row is one interventional trial protocol from TrialBench, with fields from the
ClinicalTrials.gov registry record. Identity columns and the post-outcome columns
`actual_recruitment` and `adverse_events` are removed.

## Files

| Path | Rows | Content |
|---|---|---|
| `enrollment/train.csv.gz` | 13,771 | labelled training trials for the enrollment failure mode |
| `safety/train.csv.gz` | 9,055 | labelled training trials for the safety failure mode |
| `efficacy/train.csv.gz` | 9,715 | labelled training trials for the efficacy failure mode |
| `test.csv.gz` | 4,120 | the test trials of all three modes, pooled, no labels |

All files are gzip-compressed UTF-8 CSV; `pandas.read_csv` reads them directly.

## Labels and splits

- `label` (train files only): **1 = success, 0 = failure in this mode** (poor enrollment,
  safety/adverse effects, or lack of efficacy), following the four TrialBench failure-reason
  categories: successful trials, poor enrollment, safety and lack of efficacy.
- Each mode pairs its failure trials with the same successful trials, so a successful
  trial appears in all three train files with the same `trial_id`.
- Train and test follow the 8:2 TrialBench split used by ClinicalReTrial.
- The test file pools the test trials of the three modes. Which mode a test trial
  belongs to is not given; each results file must cover every test trial, and a trial
  is scored only in the mode or modes it belongs to.

## Columns

`trial_id` is an opaque identifier, consistent across files. Values are copied from
the source as text; an empty cell means missing. Registry identifiers (`NCT` + 8 digits,
in any case or spacing) inside free text are replaced by `NCT[removed]`. List-valued
columns hold Python-literal lists as text, for example `['Drug', 'Drug']`.

| Column | Meaning |
|---|---|
| `trial_id` | opaque trial identifier |
| `label` | train files only: 1 = success, 0 = failure for that mode |
| `Behavioral intervention Number` | number of behavioral interventions |
| `Biological intervention Number` | number of biological interventions |
| `Combination Product intervention Number` | number of combination-product interventions |
| `Dietary Supplement intervention Number` | number of dietary-supplement interventions |
| `Drug intervention Number` | number of drug interventions |
| `Experimental Arm Number` | number of experimental arms |
| `MaskingType-Care Provider` | 1 if care providers are masked |
| `MaskingType-Investigator` | 1 if investigators are masked |
| `MaskingType-Outcomes Assessor` | 1 if outcome assessors are masked |
| `MaskingType-Participant` | 1 if participants are masked |
| `Other Arm Number` | number of arms of type Other |
| `Placebo Comparator Arm Number` | number of placebo-comparator arms |
| `Radiation intervention Number` | number of radiation interventions |
| `brief_summary/textblock` | brief summary (free text) |
| `brief_title` | brief title (free text) |
| `condition` | conditions studied |
| `condition_browse/mesh_term` | MeSH terms for the conditions |
| `detailed_description/textblock` | detailed description (free text) |
| `eligibility/criteria/textblock` | full eligibility criteria (free text) |
| `eligibility/gender_description` | gender eligibility description |
| `eligibility/healthy_volunteers` | whether healthy volunteers are accepted |
| `eligibility/maximum_age` | maximum age as text, for example "65 Years" |
| `eligibility/minimum_age` | minimum age as text, for example "18 Years" |
| `icdcode` | ICD-10 codes of the conditions: a list of lists, one per condition |
| `intervention/intervention_name` | intervention names |
| `intervention/intervention_type` | intervention types |
| `intervention_browse/mesh_term` | MeSH terms for the interventions |
| `ipd_info_type-Analytic Code` | individual participant data sharing: analytic code |
| `ipd_info_type-Statistical Analysis Plan (SAP)` | individual participant data sharing: statistical analysis plan |
| `ipd_info_type-Study Protocol` | individual participant data sharing: study protocol |
| `keyword` | registry keywords |
| `number_of_arms` | total number of arms |
| `oversight_info/has_dmc` | whether a data monitoring committee exists |
| `oversight_info/is_fda_regulated_device` | whether an FDA-regulated device is studied |
| `oversight_info/is_fda_regulated_drug` | whether an FDA-regulated drug is studied |
| `phase` | trial phase |
| `smiless` | SMILES strings of the intervention drugs |
| `study_design_info/allocation` | allocation (randomised or not) |
| `study_design_info/intervention_model` | intervention model |
| `study_design_info/intervention_model_description` | intervention model description (free text) |
| `study_design_info/masking` | masking description |
| `study_design_info/masking_num` | number of masked roles |
| `study_design_info/primary_purpose` | primary purpose |
| `study_type` | study type (always Interventional) |
| `eligibility/incl_text/age_only` | inclusion criteria that ClinicalReTrial's heuristic age extractor flagged (mostly age limits, also some other numeric thresholds) |
| `eligibility/incl_text/without_age` | the remaining inclusion criteria |
| `eligibility/excl_text/age_only` | exclusion criteria that ClinicalReTrial's heuristic age extractor flagged (mostly age limits, also some other numeric thresholds) |
| `eligibility/excl_text/without_age` | the remaining exclusion criteria |
| `locations` | countries of the trial sites |
| `target_recruitment` | enrollment figure where the registry still lists it as anticipated (about 6% of trials); empty otherwise |
| `start_date` | trial start date, YYYY-MM-DD (a few placeholder years such as 1900 or 2030-2050) |
| `dosage` | dosage description (free text) |
| `primary_outcome` | primary outcome measures (free text) |
