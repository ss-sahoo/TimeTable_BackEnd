## Project Overview

ExamFlow is a Django-based backend for a multi-tenant exam management platform. It supports online/offline exams, AI-powered question extraction from PDFs, OMR sheet generation, proctoring, and timetable management.

## Commands

### Django Backend (primary service, runs on port 8000)
```bash
# Run the development server
python manage.py runserver

# Apply migrations
python manage.py migrate

# Create and apply new migrations after model changes
python manage.py makemigrations
python manage.py migrate

# Django shell
python manage.py shell

# Run tests for a specific app
python manage.py test accounts
python manage.py test exams
python manage.py test questions
```

### Celery Worker (required for question extraction)
```bash
celery -A exam_flow_backend worker --loglevel=info
```

### Extraction Microservice (FastAPI, runs on port 8020)
```bash
cd extraction_service
pip install -r requirements.txt
uvicorn main:app --reload --port 8020
```

### Management Commands
```bash
python manage.py populate_templates    # Seed 150+ question templates
python manage.py setup_rag             # Initialize embeddings/RAG
python manage.py add_demo_questions    # Demo data
python manage.py fix_question_numbers  # Data repair utility
python manage.py create_test_data      # Create test exam data
python manage.py clear_legacy_snapshots # Cleanup old proctoring data
```

### Environment
Config is loaded from `.env` via `python-decouple` in [config.py](config.py). Key variables:
- `SECRET_KEY`, `DEBUG`, `DATABASE_URL` (PostgreSQL)
- `GEMINI_API_KEY` — Google Gemini for AI extraction (model: `gemini-2.0-flash`)
- `MATHPIX_APP_ID` / `MATHPIX_APP_KEY` — OCR for PDF math content
- `CELERY_BROKER_URL` — Redis (default: `redis://localhost:6379/0`)
- `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` — Optional Azure OpenAI fallback
- `MAILGUN_API_KEY` / `MAILGUN_DOMAIN` — Bulk email via Mailgun
- `REDIS_PASSWORD` — If set, used to build the Celery broker URL automatically

## Architecture

### Django Apps
- **`accounts`** — Custom `User` (`AUTH_USER_MODEL = 'accounts.User'`, extends `AbstractUser`) and `Institute` models. All users belong to an institute and have a role: `super_admin`, `institute_admin`, `exam_admin`, `teacher`, `student`, `admin`, `staff`, `manager`. Also handles device session management, center/batch management, timetable auth.
- **`exams`** — Core `Exam` model, student submissions (`ExamAttempt`), evaluation (auto + AI for subjective), geolocation, proctoring (webcam/gaze), and analytics.
- **`questions`** — `Question` and `QuestionBank` models. Questions belong to **Exams**, not Patterns. The app also contains the full AI extraction pipeline (see below).
- **`patterns`** — `ExamPattern` and `PatternSection` templates. Patterns define exam structure (sections, subjects, question types, marks, numbering) but do not hold questions directly.
- **`omr`** — OMR sheet PDF generation and bubble-sheet evaluation.
- **`timetable`** — Timetable and scheduling (reuses `accounts` models).

### Key Relationships
```
Institute → ExamPattern (template) → PatternSection (per subject/type)
Institute → Exam → ExamPattern (required)
Exam → Question (direct FK — questions belong to exams)
ExamAttempt → Exam (student submissions)
```

### API URL Structure
All API routes are JWT-authenticated (CSRF disabled for `/api/` paths). Base URL prefixes:
```
/api/auth/              → accounts.urls (register, login, profile, institutes, people, devices)
/api/auth/token/refresh/ → JWT token refresh
/api/patterns/          → patterns.urls
/api/exams/             → exams.urls (CRUD, start/submit, attempts, results)
/api/questions/         → questions.urls (CRUD, bulk-extract, extraction-jobs, templates)
/api/evaluation/        → exams.evaluation_urls
/api/student-analytics/ → exams.student_analytics_urls
/api/timetable/         → timetable.urls
/api/omr/               → omr.urls
/api/ai-evaluation/     → exams.ai_evaluation_urls
```

### Middleware Stack (order matters)
1. `CorsMiddleware` — CORS; allows all origins in DEBUG mode
2. `SecurityMiddleware`
3. `SessionMiddleware`
4. `CommonMiddleware`
5. `DisableCSRFForAPI` — custom, skips CSRF for `/api/` paths
6. `CsrfViewMiddleware`
7. `AuthenticationMiddleware`
8. `DeviceSessionValidationMiddleware` — validates `x-device-fingerprint` header per request
9. `TenantMiddleware` — sets institute context from JWT/header for multi-tenancy DB routing
10. `PDFResponseMiddleware` — adds embed-friendly headers for `/media/*.pdf`

### Multi-Tenancy
- `TenantMiddleware` ([accounts/tenant_middleware.py](accounts/tenant_middleware.py)) extracts institute from JWT and sets request context
- `InstituteRouter` ([accounts/router.py](accounts/router.py)) in `DATABASE_ROUTERS` routes queries to the correct tenant database
- Shared models (Users, Institutes) live in the `default` database

### AI Question Extraction — Two Paths

**Path 1: Django + Celery** (primary, used in production)
- Upload handled by `questions/extraction_views.py` → creates `ExtractionJob`
- Celery task `extract_questions_v3_task` in [questions/tasks.py](questions/tasks.py) orchestrates:
  1. Optional pre-analysis via `DocumentPreAnalyzer` (subject separation using Gemini)
  2. `AgentExtractionService` in [questions/services/agent_extraction_service.py](questions/services/agent_extraction_service.py) — Mathpix OCR + Gemini per-subject
  3. Saves results as `ExtractedQuestion` records linked to the job
- Older tasks (`extract_questions_task`, `extract_questions_v2_task`) use `ExtractionPipeline` / `ExtractionPipelineV2`
- Celery task time limit: 30 minutes

**Path 2: Standalone FastAPI Microservice** ([extraction_service/](extraction_service/))
- Separate process on port 8020, called via HTTP from Django
- Uses **LangGraph** state machine: `process_document → split_sections → extract_questions → validate_extraction`
- Mathpix for OCR → regex chunker (20 questions/chunk) → Gemini 2.0 Flash for structured extraction
- Jobs are in-memory only (not persisted across restarts)

### Authentication
- JWT via `djangorestframework-simplejwt`. Custom JWT utils in [accounts/jwt_utils.py](accounts/jwt_utils.py)
- Access token: 60 min, refresh token: 7 days, rotation enabled
- Custom claims: `email`, `role`, `first_name`, `last_name`, `institute_id`, `institute_name`
- Device session middleware validates `x-device-fingerprint` header on every request

### Evaluation System
- **Auto-eval** for MCQ — string matching with shuffle-aware answer mapping
- **Manual eval** — teacher grading via evaluation endpoints
- **AI eval** — Gemini-based subjective answer evaluation with rubrics (`EvaluationRubric` model)
- Batch evaluation with progress tracking (`EvaluationBatch`, `EvaluationProgress`)

### Storage
- Local media files by default (`MEDIA_ROOT`)
- Optional DigitalOcean Spaces (S3-compatible) when `ALWAYS_UPLOAD_FILES_TO_AWS = True` in settings

### REST Framework Defaults
- Pagination: `PageNumberPagination`, 20 items per page
- Filters: `DjangoFilterBackend`, `SearchFilter`, `OrderingFilter`
- Auth: JWT + SessionAuthentication
- Default permission: `IsAuthenticated`

## Important Conventions

- All model PKs use UUID except `Institute` (BigAutoField — noted in models as intentional, migration required to change)
- `TimeStampedModel` abstract base provides `id` (UUID), `created_at`, `updated_at` — use it for new models
- Roles are stored **lowercase** (e.g., `student`, `super_admin`). Legacy uppercase values (`ADMIN`, `SUPER_ADMIN`) may exist in old data — role checks in code handle both cases
- `PatternSection` uses **subject-wise question numbering**: two sections from different subjects can have overlapping `start_question`/`end_question` numbers
- Questions use `structure` JSONField for nested/sub-part questions (complex subjective types)
- The `questions/services/` directory has many extraction service variants; the active production one is `agent_extraction_service.py`
- File uploads max 50 MB; allowed types: DOCX, DOC, TXT, PDF, JPG, PNG
- Frontend URL assumed at `http://localhost:5173` (used for email links and CSRF trusted origins)
