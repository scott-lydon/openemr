-- ============================================================================
-- Clinical Co-Pilot synthetic demo patients
-- ============================================================================
-- Three patients with carefully-crafted clinical pictures that exercise the
-- Clinical Co-Pilot's diagnostic considerations and chart-error scan demos.
--
-- These patients live as real OpenEMR records (rather than as JSON fixtures
-- inside the sidecar) so the same data flows through OpenEMR's FHIR R4 API
-- on the way to the Co-Pilot. There is one source of truth: the database.
--
-- Patients (FHIR Patient/{uuid} mapping):
--   Patient/87413000-0000-4000-8000-000000000000  Barbara Boston (gout case)
--   Patient/87414000-0000-4000-8000-000000000000  Suzie Sanchez (osteoporosis)
--   Patient/87415000-0000-4000-8000-000000000000  Demo Patient (penicillin allergy)
--
-- Idempotent: every INSERT uses IGNORE keyed on the row's UUID, so re-running
-- this script will not duplicate rows. To regenerate from scratch, first
-- DELETE rows where pid IN (87413, 87414, 87415) across the touched tables.
--
-- Apply with:
--   docker compose exec -T mysql mariadb -u root -p"<root-pw>" openemr \
--       < sql/clinical_copilot_demo_patients.sql
--
-- Verify:
--   SELECT pid, fname, lname, HEX(uuid) FROM patient_data WHERE pid IN (87413,87414,87415);
--   SELECT pid, type, title FROM lists WHERE pid IN (87413,87414,87415) ORDER BY pid, type;
--   SELECT patient_id, drug, dosage, active FROM prescriptions WHERE patient_id IN (87413,87414,87415);
--
-- @package   OpenEMR
-- @author    Scott Lydon <relays.inanity.0n@icloud.com>
-- @copyright Copyright (c) 2026 Scott Lydon
-- @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
-- ============================================================================

START TRANSACTION;

-- ----------------------------------------------------------------------------
-- 1. patient_data: demographics
-- ----------------------------------------------------------------------------
-- The UUIDs below are deliberately memorable (87413000-..., 87414000-..., 87415000-...)
-- so a developer reading FHIR logs can map back to the case at a glance.
-- They are valid RFC 4122 v4 form (third group starts with 4, fourth with 8-b).

INSERT IGNORE INTO `patient_data`
  (`pid`, `uuid`, `title`, `language`, `fname`, `lname`, `DOB`, `sex`,
   `pubpid`, `date`, `street`, `city`, `state`, `postal_code`, `phone_home`)
VALUES
  (87413,
   UNHEX(REPLACE('87413000-0000-4000-8000-000000000000', '-', '')),
   'Mrs.', 'english', 'Barbara', 'Boston', '1955-04-12', 'Female',
   '87413', '2026-04-15 09:00:00',
   '12 Demo Street', 'San Diego', 'CA', '92101', '(555) 555-7413'),
  (87414,
   UNHEX(REPLACE('87414000-0000-4000-8000-000000000000', '-', '')),
   'Mrs.', 'english', 'Suzie', 'Sanchez', '1955-04-12', 'Female',
   '87414', '2026-02-22 08:00:00',
   '34 Demo Street', 'San Diego', 'CA', '92101', '(555) 555-7414'),
  (87415,
   UNHEX(REPLACE('87415000-0000-4000-8000-000000000000', '-', '')),
   'Mr.', 'english', 'Demo', 'Patient', '1972-09-30', 'Male',
   '87415', '2026-04-25 10:00:00',
   '56 Demo Street', 'San Diego', 'CA', '92101', '(555) 555-7415');

-- ----------------------------------------------------------------------------
-- 2. form_encounter: each patient gets one recent encounter so vitals/labs
--    have something to attach to. The encounter UUIDs are also stable.
-- ----------------------------------------------------------------------------

INSERT IGNORE INTO `form_encounter`
  (`uuid`, `date`, `reason`, `facility`, `facility_id`, `pid`, `encounter`,
   `onset_date`, `pc_catid`, `provider_id`, `class_code`)
VALUES
  -- Barbara (gout) — recent visit for right toe pain
  (UNHEX(REPLACE('87413000-e000-4000-8000-000000000001', '-', '')),
   '2026-04-15 09:12:00', 'Acute right toe pain x3 days', 'Clinic', 3,
   87413, 87413001, '2026-04-12 00:00:00', 5, 1, 'AMB'),
  -- Suzie (osteoporosis) — routine annual exam
  (UNHEX(REPLACE('87414000-e000-4000-8000-000000000001', '-', '')),
   '2026-02-22 08:15:00', 'Routine annual exam', 'Clinic', 3,
   87414, 87414001, '2026-02-22 00:00:00', 5, 1, 'AMB'),
  -- Demo (penicillin) — sinusitis follow-up
  (UNHEX(REPLACE('87415000-e000-4000-8000-000000000001', '-', '')),
   '2026-04-25 10:30:00', 'Sinusitis x5 days, congestion + facial pressure', 'Clinic', 3,
   87415, 87415001, '2026-04-20 00:00:00', 5, 1, 'AMB');

-- forms registry rows (OpenEMR requires every encounter to have a row in
-- forms with form_name='New Patient Encounter' and formdir='newpatient').
INSERT IGNORE INTO `forms`
  (`date`, `encounter`, `form_name`, `form_id`, `pid`, `formdir`, `provider_id`)
VALUES
  ('2026-04-15 09:12:00', 87413001, 'New Patient Encounter', 87413001, 87413, 'newpatient', 1),
  ('2026-02-22 08:15:00', 87414001, 'New Patient Encounter', 87414001, 87414, 'newpatient', 1),
  ('2026-04-25 10:30:00', 87415001, 'New Patient Encounter', 87415001, 87415, 'newpatient', 1);

-- ----------------------------------------------------------------------------
-- 3. lists (problems): active medical_problem entries that drive the AI's
--    diagnostic reasoning. The diagnosis column carries ICD-10 in OpenEMR's
--    `system:code` format. Activity=1 means active, outcome=0 means open.
-- ----------------------------------------------------------------------------

-- Barbara (gout case): T2DM + Gout + Hypertension
INSERT IGNORE INTO `lists`
  (`uuid`, `pid`, `type`, `title`, `diagnosis`, `begdate`, `date`,
   `activity`, `outcome`, `verification`, `user`)
VALUES
  (UNHEX(REPLACE('87413c01-0000-4000-8000-000000000001', '-', '')),
   87413, 'medical_problem', 'Type 2 diabetes mellitus', 'ICD10:E11.9',
   '2014-03-12', '2014-03-12 12:00:00', 1, 0, 'confirmed', 'admin'),
  (UNHEX(REPLACE('87413c02-0000-4000-8000-000000000001', '-', '')),
   87413, 'medical_problem', 'Gout, unspecified', 'ICD10:M10.9',
   '2019-06-04', '2019-06-04 12:00:00', 1, 0, 'confirmed', 'admin'),
  (UNHEX(REPLACE('87413c03-0000-4000-8000-000000000001', '-', '')),
   87413, 'medical_problem', 'Essential (primary) hypertension', 'ICD10:I10',
   '2010-01-15', '2010-01-15 12:00:00', 1, 0, 'confirmed', 'admin');

-- Suzie (osteoporosis case): Osteoporosis + Osteopenia + Hypothyroidism
INSERT IGNORE INTO `lists`
  (`uuid`, `pid`, `type`, `title`, `diagnosis`, `begdate`, `date`,
   `activity`, `outcome`, `verification`, `user`)
VALUES
  (UNHEX(REPLACE('87414c01-0000-4000-8000-000000000001', '-', '')),
   87414, 'medical_problem', 'Age-related osteoporosis without current pathological fracture', 'ICD10:M81.0',
   '2013-08-12', '2013-08-12 12:00:00', 1, 0, 'confirmed', 'admin'),
  (UNHEX(REPLACE('87414c02-0000-4000-8000-000000000001', '-', '')),
   87414, 'medical_problem', 'Other osteopenia', 'ICD10:M85.80',
   '2017-05-04', '2017-05-04 12:00:00', 1, 0, 'confirmed', 'admin'),
  (UNHEX(REPLACE('87414c03-0000-4000-8000-000000000001', '-', '')),
   87414, 'medical_problem', 'Hypothyroidism, unspecified', 'ICD10:E03.9',
   '2015-01-12', '2015-01-12 12:00:00', 1, 0, 'confirmed', 'admin');

-- Demo (penicillin case): Acute sinusitis
INSERT IGNORE INTO `lists`
  (`uuid`, `pid`, `type`, `title`, `diagnosis`, `begdate`, `date`,
   `activity`, `outcome`, `verification`, `user`)
VALUES
  (UNHEX(REPLACE('87415c01-0000-4000-8000-000000000001', '-', '')),
   87415, 'medical_problem', 'Acute sinusitis, unspecified', 'ICD10:J01.90',
   '2026-04-25', '2026-04-25 10:30:00', 1, 0, 'confirmed', 'admin');

-- ----------------------------------------------------------------------------
-- 4. lists (allergies): only Demo Patient has one. Penicillin moderate, hives.
-- ----------------------------------------------------------------------------

INSERT IGNORE INTO `lists`
  (`uuid`, `pid`, `type`, `title`, `diagnosis`, `begdate`, `date`,
   `activity`, `outcome`, `severity_al`, `reaction`, `verification`, `user`)
VALUES
  (UNHEX(REPLACE('87415a01-0000-4000-8000-000000000001', '-', '')),
   87415, 'allergy', 'Penicillin', 'RXNORM:7980',
   '2002-06-12', '2002-06-12 12:00:00', 1, 0, 'moderate', 'Hives', 'confirmed', 'admin');

-- ----------------------------------------------------------------------------
-- 5. prescriptions (medications). active=1 means currently prescribed.
--    The `drug` column holds the human label; `rxnorm_drugcode` carries the
--    code that FHIR MedicationRequest exposes downstream.
-- ----------------------------------------------------------------------------

INSERT IGNORE INTO `prescriptions`
  (`uuid`, `patient_id`, `provider_id`, `start_date`, `drug`, `rxnorm_drugcode`,
   `dosage`, `route`, `active`, `date_added`, `txDate`,
   `usage_category_title`, `request_intent_title`)
VALUES
  -- Barbara: Metformin + Lisinopril
  (UNHEX(REPLACE('87413r01-0000-4000-8000-000000000001', '-', '')),
   87413, 1, '2014-03-15', 'Metformin 500 mg', '860975',
   '500 mg PO twice daily', 'oral', 1, '2014-03-15 12:00:00', '2014-03-15',
   'Outpatient', 'Order'),
  (UNHEX(REPLACE('87413r02-0000-4000-8000-000000000001', '-', '')),
   87413, 1, '2010-02-01', 'Lisinopril 10 mg', '314076',
   '10 mg PO daily', 'oral', 1, '2010-02-01 12:00:00', '2010-02-01',
   'Outpatient', 'Order'),
  -- Suzie: Levothyroxine
  (UNHEX(REPLACE('87414r01-0000-4000-8000-000000000001', '-', '')),
   87414, 1, '2015-01-15', 'Levothyroxine 50 mcg', '892244',
   '50 mcg PO daily', 'oral', 1, '2015-01-15 12:00:00', '2015-01-15',
   'Outpatient', 'Order'),
  -- Demo: Amoxicillin (acute course)
  (UNHEX(REPLACE('87415r01-0000-4000-8000-000000000001', '-', '')),
   87415, 1, '2026-04-25', 'Amoxicillin 500 mg', '308182',
   '500 mg PO three times daily', 'oral', 1, '2026-04-25 10:30:00', '2026-04-25',
   'Outpatient', 'Order');

-- ----------------------------------------------------------------------------
-- 6. form_vitals: vitals tied to the encounter created above. Barbara has
--    a recent BP reading (138/82). Others have none, matching the fixtures.
-- ----------------------------------------------------------------------------

INSERT IGNORE INTO `form_vitals`
  (`uuid`, `date`, `pid`, `bps`, `bpd`, `weight`, `height`,
   `temperature`, `pulse`, `respiration`, `BMI`, `oxygen_saturation`,
   `user`, `authorized`, `activity`)
VALUES
  -- Barbara: BP 138/82, weight 74kg, height 162cm (per JSON fixture)
  (UNHEX(REPLACE('87413v01-0000-4000-8000-000000000001', '-', '')),
   '2026-04-15 09:12:00', 87413, '138', '82', 163.142, 63.78,
   98.6, 76, 16, 28.20, 98.0,
   'admin', 1, 1);

-- Link the vitals form to its encounter via the forms registry.
INSERT IGNORE INTO `forms`
  (`date`, `encounter`, `form_name`, `form_id`, `pid`, `formdir`, `provider_id`)
SELECT
  v.`date`, 87413001, 'Vitals', v.`id`, v.`pid`, 'vitals', 1
FROM `form_vitals` v
WHERE v.`pid` = 87413
  AND v.`date` = '2026-04-15 09:12:00'
  AND NOT EXISTS (
    SELECT 1 FROM `forms` f
    WHERE f.`form_id` = v.`id` AND f.`formdir` = 'vitals'
  );

-- ----------------------------------------------------------------------------
-- 7. Encounter.reasonCode — explicit comma-separated presenting symptoms.
--    The sidecar reconciler reads this column via FHIR
--    Encounter.reasonCode[].text and splits on ',' / ';' to populate
--    the "Presenting" panel. The seed originally packed all of Barbara's
--    chief complaint into one phrase ("Acute right toe pain x3 days"),
--    which surfaced as a single bullet; expanding it here lets each
--    symptom show up individually and lets the LLM pair them against
--    candidate diagnoses one-by-one (matching the JSON fixture's
--    presenting.symptoms list verbatim).
--    UPDATE (not INSERT) so re-running the seed overwrites whatever
--    text the prior version wrote.
-- ----------------------------------------------------------------------------
UPDATE `form_encounter`
   SET `reason` = 'right toe pain, swollen toe, body aches; 3 days'
 WHERE `pid` = 87413 AND `encounter` = 87413001;
UPDATE `form_encounter`
   SET `reason` = 'congestion, facial pressure, post-nasal drip; 5 days'
 WHERE `pid` = 87415 AND `encounter` = 87415001;

-- ----------------------------------------------------------------------------
-- 8. Lab results — Barbara: HbA1c + C-reactive protein, Suzie: TSH.
--    OpenEMR's FHIR Observation?category=laboratory query joins four
--    tables: procedure_order (the order row), procedure_order_code
--    (the LOINC + name on the order line), procedure_report (the result
--    document), and procedure_result (the individual result values).
--    Each lab needs one row in each table, all chained by
--    procedure_order_id / procedure_report_id, so that
--    ObservationLabService.search() (src/Services/ObservationLabService.php)
--    can materialise them as FHIR Observations.
-- ----------------------------------------------------------------------------

-- 8a. procedure_order — one order row per lab.
INSERT IGNORE INTO `procedure_order`
  (`procedure_order_id`, `provider_id`, `patient_id`, `encounter_id`,
   `date_collected`, `date_ordered`, `order_priority`, `order_status`,
   `lab_id`, `specimen_type`, `procedure_order_type`, `activity`)
VALUES
  -- Barbara: HbA1c order
  (87413010, 1, 87413, 87413001,
   '2026-03-10 08:30:00', '2026-03-09', 'normal', 'complete',
   0, 'blood', 'laboratory_test', 1),
  -- Barbara: CRP order
  (87413011, 1, 87413, 87413001,
   '2026-04-27 08:30:00', '2026-04-26', 'normal', 'complete',
   0, 'blood', 'laboratory_test', 1),
  -- Suzie: TSH order
  (87414010, 1, 87414, 87414001,
   '2026-02-22 08:15:00', '2026-02-21', 'normal', 'complete',
   0, 'blood', 'laboratory_test', 1);

-- 8b. procedure_order_code — the LOINC code + procedure name on each order line.
INSERT IGNORE INTO `procedure_order_code`
  (`procedure_order_id`, `procedure_code`, `procedure_name`,
   `procedure_order_seq`, `procedure_type`, `procedure_source`,
   `do_not_send`)
VALUES
  (87413010, '4548-4', 'Hemoglobin A1c',         1, 'ord', '1', 0),
  (87413011, '1988-5', 'C-reactive protein',     1, 'ord', '1', 0),
  (87414010, '3016-3', 'Thyroid stimulating hormone', 1, 'ord', '1', 0);

-- 8c. procedure_report — one report per order.
INSERT IGNORE INTO `procedure_report`
  (`procedure_report_id`, `procedure_order_id`, `procedure_order_seq`,
   `date_collected`, `date_report`, `source`, `report_status`)
VALUES
  (87413010, 87413010, 1, '2026-03-10 08:30:00', '2026-03-10 12:00:00', 1, 'final'),
  (87413011, 87413011, 1, '2026-04-27 08:30:00', '2026-04-27 13:00:00', 1, 'final'),
  (87414010, 87414010, 1, '2026-02-22 08:15:00', '2026-02-22 11:00:00', 1, 'final');

-- 8d. procedure_result — the individual values FHIR exposes as Observations.
--     `abnormal` flags map to FHIR Observation.interpretation; 'high' for
--     both A1c and CRP because they exceed the upper reference bound.
INSERT IGNORE INTO `procedure_result`
  (`uuid`, `procedure_report_id`, `result_data_type`, `result_code`,
   `result_text`, `result`, `range`, `units`, `result_status`,
   `abnormal`, `comments`, `date`)
VALUES
  -- Barbara A1c 7.2% (ref 4.0-5.7)
  (UNHEX(REPLACE('87413l01-0000-4000-8000-000000000001', '-', '')),
   87413010, 'N', '4548-4', 'Hemoglobin A1c', '7.2', '4.0-5.7', '%',
   'final', 'high', '', '2026-03-10 12:00:00'),
  -- Barbara CRP 42 mg/L (ref 0-5)
  (UNHEX(REPLACE('87413l02-0000-4000-8000-000000000001', '-', '')),
   87413011, 'N', '1988-5', 'C-reactive protein', '42.0', '0-5', 'mg/L',
   'final', 'high', '', '2026-04-27 13:00:00'),
  -- Suzie TSH 2.4 mIU/L (ref 0.4-4.0)
  (UNHEX(REPLACE('87414l01-0000-4000-8000-000000000001', '-', '')),
   87414010, 'N', '3016-3', 'Thyroid stimulating hormone', '2.4', '0.4-4.0', 'mIU/L',
   'final', 'normal', '', '2026-02-22 11:00:00');

COMMIT;

-- ============================================================================
-- Summary verification block. Run after the script to confirm rows landed.
-- ============================================================================
SELECT '-- patients --' AS section;
SELECT pid, fname, lname, sex, DOB, HEX(uuid) AS uuid_hex
FROM patient_data WHERE pid IN (87413, 87414, 87415);

SELECT '-- problems --' AS section;
SELECT pid, type, title, diagnosis, begdate
FROM lists WHERE pid IN (87413, 87414, 87415) AND type = 'medical_problem'
ORDER BY pid, begdate;

SELECT '-- allergies --' AS section;
SELECT pid, type, title, diagnosis, reaction, severity_al
FROM lists WHERE pid IN (87413, 87414, 87415) AND type = 'allergy';

SELECT '-- meds --' AS section;
SELECT patient_id AS pid, drug, dosage, active, rxnorm_drugcode
FROM prescriptions WHERE patient_id IN (87413, 87414, 87415);

SELECT '-- encounters --' AS section;
SELECT pid, encounter, date, reason
FROM form_encounter WHERE pid IN (87413, 87414, 87415);

SELECT '-- vitals --' AS section;
SELECT pid, date, bps, bpd, weight, height, BMI
FROM form_vitals WHERE pid IN (87413, 87414, 87415);
