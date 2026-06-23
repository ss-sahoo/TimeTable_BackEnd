"""
Utility to create default exam patterns for any institute.
Call `create_default_patterns_for_institute(institute, creator_user)` from
any Institute creation flow to ensure every new institute starts with patterns.
"""

from django.db import transaction


DEFAULT_PATTERNS = [
    # ─── Pattern 1: JEE-style Full Test ──────────────────────────────────
    {
        "name": "JEE-Style Full Test (90 Questions)",
        "description": (
            "A standard JEE Mains–style pattern with 90 questions across "
            "Physics, Chemistry, and Mathematics (+4 / -1 marking)."
        ),
        "total_questions": 90,
        "total_duration": 180,
        "total_marks": 360,
        "pattern_type": "fixed",
        "exam_mode": "online",
        "sections": [
            {
                "name": "Physics",
                "subject": "Physics",
                "question_type": "single_mcq",
                "start_question": 1,
                "end_question": 30,
                "marks_per_question": 4,
                "negative_marking": 1.0,
                "min_questions_to_attempt": 30,
                "order": 1,
            },
            {
                "name": "Chemistry",
                "subject": "Chemistry",
                "question_type": "single_mcq",
                "start_question": 31,
                "end_question": 60,
                "marks_per_question": 4,
                "negative_marking": 1.0,
                "min_questions_to_attempt": 30,
                "order": 2,
            },
            {
                "name": "Mathematics",
                "subject": "Mathematics",
                "question_type": "single_mcq",
                "start_question": 61,
                "end_question": 90,
                "marks_per_question": 4,
                "negative_marking": 1.0,
                "min_questions_to_attempt": 30,
                "order": 3,
            },
        ],
    },
    # ─── Pattern 2: Quick Practice Test ──────────────────────────────────
    {
        "name": "Quick Practice Test (30 Questions)",
        "description": (
            "A light 30-question practice test ideal for weekly assessments "
            "or topic-specific practice (+4 / -1 marking, 60 minutes)."
        ),
        "total_questions": 30,
        "total_duration": 60,
        "total_marks": 120,
        "pattern_type": "fixed",
        "exam_mode": "online",
        "sections": [
            {
                "name": "Physics",
                "subject": "Physics",
                "question_type": "single_mcq",
                "start_question": 1,
                "end_question": 10,
                "marks_per_question": 4,
                "negative_marking": 1.0,
                "min_questions_to_attempt": 10,
                "order": 1,
            },
            {
                "name": "Chemistry",
                "subject": "Chemistry",
                "question_type": "single_mcq",
                "start_question": 11,
                "end_question": 20,
                "marks_per_question": 4,
                "negative_marking": 1.0,
                "min_questions_to_attempt": 10,
                "order": 2,
            },
            {
                "name": "Mathematics",
                "subject": "Mathematics",
                "question_type": "single_mcq",
                "start_question": 21,
                "end_question": 30,
                "marks_per_question": 4,
                "negative_marking": 1.0,
                "min_questions_to_attempt": 10,
                "order": 3,
            },
        ],
    },
]


def create_default_patterns_for_institute(institute, creator_user):
    """
    Create the two default patterns for a given institute.
    Safe to call multiple times — skips already-existing patterns.

    Returns:
        list[ExamPattern]: Patterns that were newly created.
    """
    from patterns.models import ExamPattern, PatternSection, Subject

    created = []

    for pd in DEFAULT_PATTERNS:
        if ExamPattern.objects.filter(institute=institute, name=pd["name"]).exists():
            print(f"  [skip] '{pd['name']}' already exists for '{institute.name}'")
            continue

        # Ensure subjects exist
        subjects_needed = {s["subject"] for s in pd["sections"]}
        for sub_name in subjects_needed:
            Subject.objects.get_or_create(name=sub_name, institute=institute)

        try:
            with transaction.atomic():
                pattern = ExamPattern.objects.create(
                    name=pd["name"],
                    description=pd["description"],
                    institute=institute,
                    total_questions=pd["total_questions"],
                    total_duration=pd["total_duration"],
                    total_marks=pd["total_marks"],
                    pattern_type=pd["pattern_type"],
                    exam_mode=pd["exam_mode"],
                    created_by=creator_user,
                )
                for sec in pd["sections"]:
                    PatternSection.objects.create(pattern=pattern, **sec)

            print(f"  [ok] Created '{pd['name']}' (ID={pattern.id}) for '{institute.name}'")
            created.append(pattern)

        except Exception as exc:
            print(f"  [error] Could not create '{pd['name']}' for '{institute.name}': {exc}")

    return created
