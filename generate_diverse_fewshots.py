import json
import uuid

output_file = "school_dropout_fewshots_diverse.jsonl"
fewshots = []

def add_shot(intent, topic, q, sql, tables, columns):
    fewshots.append({
        "id": f"diverse_{uuid.uuid4().hex[:8]}",
        "use_case": "school_dropout",
        "intent": intent,
        "topic": topic,
        "difficulty": "hard",
        "dialect": "hive_sql",
        "question": q,
        "sql": sql.strip(),
        "tables": tables,
        "risk_signal": topic,
        "grain": "student",
        "output_columns": columns,
        "quality_notes": "Diverse few-shot for full schema coverage"
    })

# 1. Infrastructure deficiency
add_shot(
    "student_risk_list",
    "infrastructure_deficiency",
    "List students in Class 10 studying in schools where functional units are less than 50% of the total units, along with the physical progress percentage.",
    """
SELECT
  st.citizen_student_id_pk,
  st.student_name,
  st.current_grade,
  sc.school_name,
  inf.total_units,
  inf.functional_units,
  inf.physical_progress_percentage
FROM curated_datamodels.school_infrastructure_progress_fact inf
JOIN curated_datamodels.citizen_school sc
  ON sc.citizen_school_id_pk = inf.citizen_school_id_pk
JOIN curated_datamodels.citizen_student st
  ON st.citizen_school_id_fk = sc.citizen_school_id_pk
WHERE st.current_grade = '10'
  AND inf.total_units > 0
  AND (inf.functional_units / inf.total_units) < 0.5
ORDER BY inf.physical_progress_percentage ASC
LIMIT 20
    """,
    ["curated_datamodels.school_infrastructure_progress_fact", "curated_datamodels.citizen_school", "curated_datamodels.citizen_student"],
    ["citizen_student_id_pk", "student_name", "current_grade", "school_name", "total_units", "functional_units", "physical_progress_percentage"]
)

# 2. Teacher attendance impact on students
add_shot(
    "student_risk_list",
    "teacher_absence_risk",
    "Show me students affected by high teacher absence. List students where their assigned teachers were absent more than 10 times.",
    """
SELECT
  st.citizen_student_id_pk,
  st.student_name,
  st.current_grade,
  t.teacher_name,
  COUNT(ta.school_teacher_attendance_fact_id_pk) as teacher_absent_days
FROM curated_datamodels.school_teacher_attendance_fact ta
JOIN curated_datamodels.citizen_school_teacher t
  ON t.citizen_school_teacher_id_pk = ta.citizen_school_teacher_id_fk
JOIN curated_datamodels.citizen_student st
  ON st.citizen_student_id_pk = ta.citizen_student_id_fk
WHERE ta.present_flag = 'N'
GROUP BY
  st.citizen_student_id_pk, st.student_name, st.current_grade, t.teacher_name
HAVING COUNT(ta.school_teacher_attendance_fact_id_pk) > 10
ORDER BY teacher_absent_days DESC
LIMIT 20
    """,
    ["curated_datamodels.school_teacher_attendance_fact", "curated_datamodels.citizen_school_teacher", "curated_datamodels.citizen_student"],
    ["citizen_student_id_pk", "student_name", "current_grade", "teacher_name", "teacher_absent_days"]
)

# 3. Nutrition (Mid Day Meal)
add_shot(
    "student_risk_list",
    "nutrition_gap",
    "List students who received mid day meals less than 5 times this academic year but have a percentage score below 40%.",
    """
SELECT
  st.citizen_student_id_pk,
  st.student_name,
  st.current_grade,
  ap.percentage_score,
  COUNT(mdm.mid_day_meal_serving_fact_id_pk) AS meals_served
FROM curated_datamodels.citizen_student st
JOIN curated_datamodels.school_academic_performance_fact ap
  ON st.citizen_student_id_pk = ap.citizen_student_id_fk
LEFT JOIN curated_datamodels.mid_day_meal_serving_fact mdm
  ON st.citizen_student_id_pk = mdm.citizen_student_id_fk
WHERE ap.percentage_score < 40
GROUP BY
  st.citizen_student_id_pk, st.student_name, st.current_grade, ap.percentage_score
HAVING COUNT(mdm.mid_day_meal_serving_fact_id_pk) < 5
ORDER BY meals_served ASC, ap.percentage_score ASC
LIMIT 20
    """,
    ["curated_datamodels.citizen_student", "curated_datamodels.school_academic_performance_fact", "curated_datamodels.mid_day_meal_serving_fact"],
    ["citizen_student_id_pk", "student_name", "current_grade", "percentage_score", "meals_served"]
)

# 4. Complex Subject Failures
add_shot(
    "student_risk_list",
    "academic_failure_subject",
    "Identify students who failed in Science and have more than 20 days of absence. Include their school name.",
    """
SELECT
  st.citizen_student_id_pk,
  st.student_name,
  sc.school_name,
  ap.percentage_score,
  COUNT(att.school_student_attendance_fact_id_pk) AS absent_days
FROM curated_datamodels.school_academic_performance_fact ap
JOIN curated_datamodels.school_subject_master sub
  ON sub.school_subject_master_id_pk = ap.subject_id_fk
JOIN curated_datamodels.citizen_student st
  ON st.citizen_student_id_pk = ap.citizen_student_id_fk
JOIN curated_datamodels.citizen_school sc
  ON sc.citizen_school_id_pk = st.citizen_school_id_fk
JOIN curated_datamodels.school_student_attendance_fact att
  ON st.citizen_student_id_pk = att.citizen_student_id_fk
WHERE sub.subject_name = 'Science'
  AND ap.fail_flag = 'Y'
  AND att.absent_flag = 'Y'
GROUP BY
  st.citizen_student_id_pk, st.student_name, sc.school_name, ap.percentage_score
HAVING COUNT(att.school_student_attendance_fact_id_pk) > 20
ORDER BY absent_days DESC
LIMIT 20
    """,
    ["curated_datamodels.school_academic_performance_fact", "curated_datamodels.school_subject_master", "curated_datamodels.citizen_student", "curated_datamodels.citizen_school", "curated_datamodels.school_student_attendance_fact"],
    ["citizen_student_id_pk", "student_name", "school_name", "percentage_score", "absent_days"]
)

# 5. Infrastructure Financial Delay
add_shot(
    "school_risk_list",
    "infrastructure_delay",
    "Which schools have infrastructure projects with less than 20% financial progress but physical progress is over 80%?",
    """
SELECT
  sc.school_udise_code,
  sc.school_name,
  inf.work_order_no,
  inf.physical_progress_percentage,
  inf.financial_progress_percentage,
  inf.estimated_cost_amount
FROM curated_datamodels.school_infrastructure_progress_fact inf
JOIN curated_datamodels.citizen_school sc
  ON sc.citizen_school_id_pk = inf.citizen_school_id_pk
WHERE inf.physical_progress_percentage > 80
  AND inf.financial_progress_percentage < 20
ORDER BY inf.estimated_cost_amount DESC
LIMIT 20
    """,
    ["curated_datamodels.school_infrastructure_progress_fact", "curated_datamodels.citizen_school"],
    ["school_udise_code", "school_name", "work_order_no", "physical_progress_percentage", "financial_progress_percentage", "estimated_cost_amount"]
)

# 6. Scheme benefits
add_shot(
    "student_risk_list",
    "welfare_scheme_gap",
    "Give me students who are eligible for schemes but benefit disbursed flag is N and they are in grade 9.",
    """
SELECT
  st.citizen_student_id_pk,
  st.student_name,
  st.current_grade,
  sb.delay_days,
  sb.payment_failure_reason_code
FROM curated_datamodels.scheme_benefits_fact sb
JOIN curated_datamodels.citizen_student st
  ON st.citizen_student_id_pk = sb.citizen_student_id_fk
WHERE sb.eligible_flag = 'Y'
  AND sb.benefit_disbursed_flag = 'N'
  AND st.current_grade = '9'
ORDER BY sb.delay_days DESC
LIMIT 20
    """,
    ["curated_datamodels.scheme_benefits_fact", "curated_datamodels.citizen_student"],
    ["citizen_student_id_pk", "student_name", "current_grade", "delay_days", "payment_failure_reason_code"]
)

# 7. Overall school teacher stats
add_shot(
    "school_stats",
    "teacher_demographics",
    "Show me the top 10 schools with the highest number of teachers who have only basic academic qualifications but no professional qualification.",
    """
SELECT
  sc.school_name,
  COUNT(t.citizen_school_teacher_id_pk) AS underqualified_teachers
FROM curated_datamodels.citizen_school_teacher t
JOIN curated_datamodels.school_teacher_attendance_fact ta
  ON t.citizen_school_teacher_id_pk = ta.citizen_school_teacher_id_fk
JOIN curated_datamodels.citizen_student st
  ON st.citizen_student_id_pk = ta.citizen_student_id_fk
JOIN curated_datamodels.citizen_school sc
  ON sc.citizen_school_id_pk = st.citizen_school_id_fk
WHERE t.professional_qualification IS NULL
   OR t.professional_qualification = 'None'
GROUP BY sc.school_name
ORDER BY underqualified_teachers DESC
LIMIT 10
    """,
    ["curated_datamodels.citizen_school_teacher", "curated_datamodels.school_teacher_attendance_fact", "curated_datamodels.citizen_student", "curated_datamodels.citizen_school"],
    ["school_name", "underqualified_teachers"]
)

with open(output_file, 'w') as f:
    for shot in fewshots:
        f.write(json.dumps(shot) + "\n")
print(f"Generated {len(fewshots)} diverse few-shots in {output_file}.")
