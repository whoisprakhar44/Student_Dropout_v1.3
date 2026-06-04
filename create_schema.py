#!/usr/bin/env python3
"""
Create and seed the local SQLite sample database from schema/curated_datamodels.
"""

from __future__ import annotations

try:
    import sys
    import pysqlite3
    sys.modules["sqlite3"] = pysqlite3
except ImportError:
    pass

import os
import random
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "database" / "schema.db"
TABLES_DIR = ROOT_DIR / "schema" / "curated_datamodels" / "tables"

ACADEMIC_YEARS = ["2021-22", "2022-23", "2023-24"]
FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Arjun", "Ananya", "Diya", "Priya", "Neha",
    "Rohan", "Karan", "Rahul", "Aryan", "Nisha", "Pooja", "Zara", "Isha",
    "Vikram", "Suresh", "Amit", "Rajesh", "Divya", "Shreya", "Anjali", "Sneha",
]
LAST_NAMES = [
    "Sharma", "Kumar", "Singh", "Patel", "Gupta", "Verma", "Reddy", "Nair",
    "Menon", "Desai", "Iyer", "Rao", "Bhatt", "Joshi", "Kapoor", "Malik",
]
DISTRICTS = ["Guntur", "Krishna", "Nellore", "Visakhapatnam", "Chittoor", "Kurnool"]


def _now_text() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")


def _sqlite_type(source_type: str) -> str:
    t = (source_type or "").upper()
    if any(token in t for token in ("BIGINT", "INT", "BOOLEAN")):
        return "INTEGER"
    if any(token in t for token in ("DECIMAL", "DOUBLE", "FLOAT", "REAL")):
        return "REAL"
    return "TEXT"


def _load_table_docs() -> list[dict[str, Any]]:
    docs = []
    for path in sorted(TABLES_DIR.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if doc and doc.get("table") and doc.get("columns"):
            docs.append(doc)
    return docs


def _create_tables(cursor: sqlite3.Cursor, table_docs: list[dict[str, Any]]) -> None:
    for doc in table_docs:
        table = doc["table"]
        columns = []
        primary_key = set(doc.get("primary_key") or [])
        for col in doc["columns"]:
            name = col["name"]
            col_type = _sqlite_type(col.get("type", "TEXT"))
            suffix = " PRIMARY KEY" if name in primary_key and len(primary_key) == 1 else ""
            columns.append(f'"{name}" {col_type}{suffix}')
        cursor.execute(f'DROP TABLE IF EXISTS "{table}"')
        cursor.execute(f'CREATE TABLE "{table}" ({", ".join(columns)})')


def _insert(cursor: sqlite3.Cursor, table_columns: dict[str, list[str]], table: str, row: dict[str, Any]) -> None:
    columns = [c for c in table_columns[table] if c in row]
    values = [row[c] for c in columns]
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{c}"' for c in columns)
    cursor.execute(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', values)


def _default_value(column: str, sqlite_type: str, row_id: int) -> Any:
    name = column.lower()
    today = date(2026, 5, 28)
    if sqlite_type == "INTEGER":
        if name.endswith("_pk") or name.endswith("_id_pk") or name.endswith("_id"):
            return row_id
        if "count" in name or "days" in name or "age" in name or "class" in name:
            return random.randint(1, 100)
        return row_id
    if sqlite_type == "REAL":
        if "percentage" in name or "rate" in name or "score" in name:
            return round(random.uniform(50, 98), 2)
        if "amount" in name:
            return round(random.uniform(500, 50000), 2)
        return round(random.uniform(1, 100), 2)
    if "date" in name:
        return today.isoformat()
    if "flag" in name or name.startswith("is_"):
        return random.choice(["Y", "N"])
    if "created_by" in name or "updated_by" in name:
        return "sample_loader"
    return f"{column}_{row_id}"


def _seed_generic_dimensions(
    cursor: sqlite3.Cursor,
    table_docs: list[dict[str, Any]],
    table_columns: dict[str, list[str]],
    seeded_tables: set[str],
) -> None:
    custom_seeded = {"citizen_school", "citizen_student", "citizen_school_teacher"}
    for doc in table_docs:
        table = doc["table"]
        if table in seeded_tables or table in custom_seeded or table.endswith("_fact"):
            continue
        col_types = {c["name"]: _sqlite_type(c.get("type", "")) for c in doc["columns"]}
        for row_id in range(1, 6):
            row = {
                col: _default_value(col, col_types[col], row_id)
                for col in table_columns[table]
            }
            _insert(cursor, table_columns, table, row)
        seeded_tables.add(table)


def _seed_reference_data(cursor: sqlite3.Cursor, table_columns: dict[str, list[str]]) -> set[str]:
    seeded = set()

    for row in [
        {"school_medium_pk": 1, "medium_code": "ENG", "medium_name": "English", "language_family": "Indo-European"},
        {"school_medium_pk": 2, "medium_code": "TEL", "medium_name": "Telugu", "language_family": "Dravidian"},
        {"school_medium_pk": 3, "medium_code": "URD", "medium_name": "Urdu", "language_family": "Indo-Aryan"},
    ]:
        _insert(cursor, table_columns, "school_medium", row)
    seeded.add("school_medium")

    for row in [
        {"school_category_id_pk": 1, "category_code": "PRI", "category_name": "Primary", "category_type": "Primary"},
        {"school_category_id_pk": 2, "category_code": "UPR", "category_name": "Upper Primary", "category_type": "Upper Primary"},
        {"school_category_id_pk": 3, "category_code": "SEC", "category_name": "Secondary", "category_type": "Secondary"},
    ]:
        _insert(cursor, table_columns, "school_category", row)
    seeded.add("school_category")

    for row_id, class_name in enumerate(["Class 1", "Class 2", "Class 3", "Class 4", "Class 5", "Class 6", "Class 7", "Class 8", "Class 9", "Class 10"], 1):
        _insert(cursor, table_columns, "student_class_dim", {
            "student_class_dim_id_pk": row_id,
            "class_code": f"C{row_id:02d}",
            "class_name": class_name,
            "education_stage": "Primary" if row_id <= 5 else "Secondary",
            "min_age": 5 + row_id,
            "max_age": 6 + row_id,
            "is_active": "Y",
            "effective_start_date": "2021-06-01",
            "effective_end_date": "2099-12-31",
            "created_date": "2026-05-28 00:00:00",
            "created_by": "sample_loader",
        })
    seeded.add("student_class_dim")

    for row_id, subject in enumerate(["Mathematics", "Science", "English", "Telugu", "Social Studies"], 1):
        _insert(cursor, table_columns, "school_subject_master", {
            "subject_pk": row_id,
            "subject_code": subject[:3].upper(),
            "subject_name": subject,
            "subject_short_name": subject[:3].upper(),
            "subject_category": "Core",
            "subject_type": "Scholastic",
            "language_order": row_id,
            "is_vocational_flag": "N",
            "is_skill_subject_flag": "N",
            "min_class": 1,
            "max_class": 10,
            "medium_id": random.randint(1, 3),
            "is_compulsory_flag": "Y",
            "board_code": "STATE",
            "assessment_pattern": "Marks",
            "marks_scheme": "100",
            "credit_weight": 1.0,
            "subject_status": "Active",
        })
    seeded.add("school_subject_master")

    for row_id, assessment in enumerate(["Quarterly", "Half Yearly", "Annual"], 1):
        _insert(cursor, table_columns, "assessment_dim", {
            "assessment_dim_id_pk": row_id,
            "assessment_code": assessment[:3].upper(),
            "assessment_type": assessment,
            "assessment_name": assessment,
            "board_code": "STATE",
            "grade_scheme": "Percentage",
            "is_summative": "Y",
            "is_active": "Y",
        })
    seeded.add("assessment_dim")

    for row_id, reason in enumerate(["Health", "Family Work", "Transport", "Seasonal Migration", "Unknown"], 1):
        _insert(cursor, table_columns, "absent_reason_dim", {
            "absent_reason_dim_id_pk": row_id,
            "reason_code": reason[:3].upper(),
            "reason_name": reason,
            "reason_category": "Student",
            "is_govt_approved": "Y",
            "is_active": "Y",
        })
    seeded.add("absent_reason_dim")

    return seeded


def _seed_schools_and_people(cursor: sqlite3.Cursor, table_columns: dict[str, list[str]]) -> None:
    for teacher_id in range(1, 101):
        name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        _insert(cursor, table_columns, "citizen_school_teacher", {
            "citizen_school_teacher_id_pk": teacher_id,
            "treasury_id": f"TR{teacher_id:05d}",
            "cfms_id": f"CFMS{teacher_id:05d}",
            "national_teacher_code": f"NTC{teacher_id:05d}",
            "teacher_citizen_master_fk": 500000 + teacher_id,
            "teacher_emp_id": f"EMP{teacher_id:05d}",
            "teacher_name": name,
            "gender": random.choice(["M", "F"]),
            "date_of_birth": (date(1980, 1, 1) + timedelta(days=random.randint(0, 7000))).isoformat(),
            "joining_date": (date(2010, 6, 1) + timedelta(days=random.randint(0, 4000))).isoformat(),
            "qualification": random.choice(["B.Ed", "M.Ed", "D.Ed"]),
            "employment_type": "Regular",
        })

    for school_id in range(1, 51):
        district = random.choice(DISTRICTS)
        _insert(cursor, table_columns, "citizen_school", {
            "citizen_school_id_pk": school_id,
            "school_medium_id_fk": random.randint(1, 3),
            "school_teacher_id_fk": random.randint(1, 100),
            "school_category_id_fk": random.randint(1, 3),
            "school_management_id_fk": random.randint(1, 4),
            "school_dept_code": "SED",
            "school_udise_code": f"UDISE{school_id:07d}",
            "school_name": f"{district} Model School {school_id}",
            "school_address": f"Main Road, {district}",
            "district_name": district,
            "district_lgd_code": str(500 + DISTRICTS.index(district)),
            "mandal_name": f"Mandal {random.randint(1, 12)}",
            "mandal_lgd_code": str(random.randint(1000, 9999)),
            "village_name": f"Village {random.randint(1, 200)}",
            "village_lgd_code": str(random.randint(10000, 99999)),
            "urban_rural_flag": random.choice(["Urban", "Rural"]),
            "latitude": round(random.uniform(13.0, 18.0), 6),
            "longitude": round(random.uniform(77.0, 84.0), 6),
            "school_pincode": str(random.randint(500000, 599999)),
            "functional_status": "Functional",
            "academic_year_opened": random.choice(ACADEMIC_YEARS),
            "min_class": 1,
            "max_class": random.randint(5, 10),
            "electricity_availability_flag": random.choice(["Y", "Y", "N"]),
            "drinking_water_availability_flag": random.choice(["Y", "Y", "N"]),
            "toilet_boys_flag": random.choice(["Y", "Y", "N"]),
            "toilet_girls_flag": random.choice(["Y", "Y", "N"]),
            "ict_enabled_flag": random.choice(["Y", "N"]),
            "playground_flag": random.choice(["Y", "Y", "N"]),
            "head_master_name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
            "is_current": "Y",
        })

    for student_id in range(1, 1001):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        school_id = random.randint(1, 50)
        grade = random.randint(1, 10)
        _insert(cursor, table_columns, "citizen_student", {
            "citizen_student_id_pk": student_id,
            "citizen_school_id_fk": school_id,
            "student_citizen_master_id_fk": 100000 + student_id,
            "student_subject_id_fk": random.randint(1, 5),
            "student_aadhaar_id": str(900000000000 + student_id),
            "student_name": f"{first} {last}",
            "gender": random.choice(["M", "F"]),
            "surname": last,
            "mother_tongue": random.choice(["Telugu", "Urdu", "English"]),
            "date_of_birth": (date(2008, 1, 1) + timedelta(days=random.randint(0, 3650))).isoformat(),
            "social_category": random.choice(["SC", "ST", "BC", "General", "Minority"]),
            "minority_status": random.choice(["Y", "N"]),
            "disability_flag": random.choice(["N", "N", "Y"]),
            "disability_percentage": 0,
            "admission_flag": "Y",
            "current_grade": str(grade),
            "medium_key": random.choice(["ENG", "TEL", "URD"]),
            "is_current": "Y",
            "mother_citizen_master_id_fk": 800000000000 + student_id,
            "father_citizen_master_id_fk": 700000000000 + student_id,
            "is_current_flag": "Y",
            "primary_mobile_no": str(9000000000 + student_id),
            "address": f"House {student_id}, Village Road",
            "pincode": str(random.randint(500000, 599999)),
            "effective_start_date": "2021-06-01",
            "effective_end_date": "2099-12-31",
        })


def _seed_facts(cursor: sqlite3.Cursor, table_columns: dict[str, list[str]]) -> None:
    attendance_id = 1
    performance_id = 1
    benefit_id = 1
    for student_id in range(1, 1001):
        school_id = cursor.execute(
            "SELECT citizen_school_id_fk FROM citizen_student WHERE citizen_student_id_pk = ?",
            (student_id,),
        ).fetchone()[0]
        class_id = int(cursor.execute(
            "SELECT current_grade FROM citizen_student WHERE citizen_student_id_pk = ?",
            (student_id,),
        ).fetchone()[0])
        for year in ACADEMIC_YEARS:
            roster_days = random.randint(200, 220)
            absent_days = random.randint(30, 80) if random.random() < 0.3 else random.randint(0, 25)
            present_days = roster_days - absent_days
            for _day in range(1, 4):
                is_absent = random.random() < (absent_days / roster_days)
                _insert(cursor, table_columns, "school_student_attendance_fact", {
                    "school_student_attendance_fact_id_pk": attendance_id,
                    "citizen_student_id_fk": student_id,
                    "school_medium_id_fk": random.randint(1, 3),
                    "student_class_dim_id_fk": class_id,
                    "student_school_id_fk": school_id,
                    "academic_year": year,
                    "attendance_date_id_fk": int(year[:4]) * 10000 + _day,
                    "present_flag": "N" if is_absent else "Y",
                    "absent_flag": "Y" if is_absent else "N",
                    "half_day_flag": "N",
                    "attendance_weight": 0.0 if is_absent else 1.0,
                    "attendance_status_code": "ABSENT" if is_absent else "PRESENT",
                    "attendance_reason_code": random.choice(["HEA", "FAM", "TRN", "UNK"]) if is_absent else None,
                    "marked_by_role": "Teacher",
                    "attendance_capture_method": "Mobile",
                    "mdm_eligible_flag": "Y",
                    "meals_consumed_flag": "N" if is_absent else "Y",
                    "absence_reason_dim_id_fk": random.randint(1, 5) if is_absent else None,
                    "created_date": _now_text(),
                    "created_by": "sample_loader",
                })
                attendance_id += 1

            marks = random.randint(35, 100)
            _insert(cursor, table_columns, "school_academic_performance_fact", {
                "school_academic_performance_fact_id_pk": performance_id,
                "citizen_student_id_fk": student_id,
                "citizen_school_id_fk": school_id,
                "student_subject_id_fk": random.randint(1, 5),
                "exam_date_key_id_fk": int(year[:4]) * 10000 + 301,
                "student_class_dim_id_fk": class_id,
                "assessment_dim_id_fk": random.randint(1, 3),
                "school_medium_id_fk": random.randint(1, 3),
                "academic_year": year,
                "student_exam_id_fk": 300000 + performance_id,
                "student_roll_number": f"R{student_id:05d}",
                "assessment_cycle": "Annual",
                "marks_obtained": marks,
                "maximum_marks": 100,
                "percentage_score": marks,
                "pass_flag": "Y" if marks >= 35 else "N",
                "fail_flag": "N" if marks >= 35 else "Y",
                "absent_flag": "N",
                "grade_point": round(marks / 10, 2),
                "exam_type": "Annual",
                "score": marks,
                "performance_band": "High" if marks >= 75 else "Medium" if marks >= 50 else "Low",
                "created_date": _now_text(),
                "created_by": "sample_loader",
            })
            performance_id += 1

        _insert(cursor, table_columns, "scheme_benefits_fact", {
            "scheme_benefits_fact_id_pk": benefit_id,
            "citizen_school_id_fk": school_id,
            "school_scheme_master_id_fk": random.randint(1, 5),
            "academic_year": "2023-24",
            "benefit_type_id_fk": random.randint(1, 5),
            "beneficiary_type_id_fk": random.randint(1, 5),
            "citizen_student_id_fk": student_id,
            "school_teacher_id_fk": None,
            "scheme_application_id": f"APP{benefit_id:06d}",
            "social_category": random.choice(["SC", "ST", "BC", "General", "Minority"]),
            "eligible_flag": "Y",
            "attendance_eligible_flag": random.choice(["Y", "N"]),
            "benefit_sanctioned_flag": random.choice(["Y", "Y", "N"]),
            "benefit_disbursed_flag": random.choice(["Y", "N"]),
            "benefit_amount_sanctioned": random.randint(500, 5000),
            "benefit_amount_disbursed": random.randint(0, 5000),
            "benefit_amount": random.randint(500, 5000),
            "beneficiary_count": 1,
            "application_date": "2023-08-01",
            "created_date": _now_text(),
            "created_by": "sample_loader",
        })
        benefit_id += 1

    for row_id in range(1, 151):
        school_id = random.randint(1, 50)
        _insert(cursor, table_columns, "mid_day_meal_serving_fact", {
            "mid_day_meal_serving_fact_id_pk": row_id,
            "citizen_school_id_fk": school_id,
            "date_key_id_fk": 20240500 + random.randint(1, 28),
            "meal_type_id_fk": random.randint(1, 5),
            "nutrition_item_dim_key_fk": random.randint(1, 5),
            "scheme_key_id_fk": random.randint(1, 5),
            "academic_year": "2023-24",
            "students_enrolled_count": random.randint(100, 900),
            "students_served_count": random.randint(80, 850),
            "meal_served_count": random.randint(80, 850),
            "created_date": _now_text(),
            "created_by": "sample_loader",
        })

        _insert(cursor, table_columns, "school_infrastructure_progress_fact", {
            "school_infrastructure_progress_fact_id_pk": row_id,
            "citizen_school_id_pk": school_id,
            "reporting_date_key_fk": 20240500 + random.randint(1, 28),
            "infrastructure_component_id_fk": random.randint(1, 5),
            "infrastructure_category_id_fk": random.randint(1, 5),
            "academic_year": "2023-24",
            "total_units": random.randint(5, 50),
            "functional_units": random.randint(3, 45),
            "non_functional_units": random.randint(0, 5),
            "planned_unit_count": random.randint(5, 50),
            "completed_unit_count": random.randint(3, 45),
            "physical_progress_percentage": round(random.uniform(40, 100), 2),
            "financial_progress_percentage": round(random.uniform(40, 100), 2),
            "estimated_cost_amount": random.randint(50000, 500000),
            "work_status": random.choice(["Completed", "In Progress", "Pending"]),
            "is_functional_flag": random.choice(["Y", "N"]),
            "geo_tagged_flag": "Y",
        })


def create_database(db_path: str | os.PathLike[str] = DB_PATH, *, replace: bool = True) -> None:
    random.seed(42)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if replace and db_path.exists():
        db_path.unlink()

    table_docs = _load_table_docs()
    table_columns = {
        doc["table"]: [col["name"] for col in doc["columns"]]
        for doc in table_docs
    }

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    _create_tables(cursor, table_docs)
    seeded = _seed_reference_data(cursor, table_columns)
    _seed_generic_dimensions(cursor, table_docs, table_columns, seeded)
    _seed_schools_and_people(cursor, table_columns)
    _seed_facts(cursor, table_columns)
    conn.commit()
    conn.close()
    print(f"Curated sample database created at {db_path}")


if __name__ == "__main__":
    create_database()
