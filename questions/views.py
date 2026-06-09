from rest_framework import generics, permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.conf import settings
import pandas as pd
import io
import csv
import random
import hashlib
import json
import re
from .models import Question, QuestionBank, ExamQuestion, QuestionImage, QuestionComment, QuestionTemplate
from .serializers import (
    QuestionSerializer, QuestionCreateSerializer, QuestionBankSerializer,
    ExamQuestionSerializer, QuestionTemplateSerializer, QuestionSearchSerializer,
    BulkQuestionImportSerializer, QuestionImageSerializer
)
# RAG utils - optional import
try:
    from .rag_utils import configure_gemini
except ImportError:
    configure_gemini = None
# Google AI imports - optional
try:
    from google.api_core import exceptions as google_exceptions
    import google.generativeai as genai
    GOOGLE_AI_AVAILABLE = True
except ImportError:
    google_exceptions = None
    genai = None
    GOOGLE_AI_AVAILABLE = False


class QuestionListView(generics.ListCreateAPIView):
    """List and create questions"""
    permission_classes = [permissions.IsAuthenticated]
    # Whitelisted sort keys. Default ordering (queryset .order_by('-created_at'))
    # is preserved when `?ordering=` is omitted; this is purely additive.
    ordering_fields = ['created_at', 'updated_at', 'subject', 'topic', 'difficulty', 'question_number']

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return QuestionCreateSerializer
        return QuestionSerializer

    def get_queryset(self):
        user = self.request.user
        queryset = Question.objects.filter(institute=user.institute, is_active=True)
        
        # Apply filters
        search = self.request.query_params.get('search')
        subject = self.request.query_params.get('subject')
        topic = self.request.query_params.get('topic')
        difficulty = self.request.query_params.get('difficulty')
        question_type = self.request.query_params.get('question_type')
        question_bank = self.request.query_params.get('question_bank')
        is_verified = self.request.query_params.get('is_verified')
        
        if search:
            queryset = queryset.filter(
                Q(question_text__icontains=search) |
                Q(subject__icontains=search) |
                Q(topic__icontains=search)
            )
        
        if subject:
            queryset = queryset.filter(subject__icontains=subject)
        
        if topic:
            queryset = queryset.filter(topic__icontains=topic)
        
        if difficulty:
            queryset = queryset.filter(difficulty=difficulty)

        exam_param = self.request.query_params.get('exam')
        if exam_param:
            try:
                queryset = queryset.filter(exam_id=int(exam_param))
            except (TypeError, ValueError):
                queryset = queryset.none()
        
        if question_type:
            queryset = queryset.filter(question_type=question_type)
        
        if question_bank:
            queryset = queryset.filter(question_bank_id=question_bank)
        
        if is_verified is not None:
            queryset = queryset.filter(is_verified=is_verified.lower() == 'true')
        
        # Filter by pattern section
        pattern_section = self.request.query_params.get('pattern_section')
        if pattern_section:
            try:
                queryset = queryset.filter(pattern_section_id=int(pattern_section))
            except (TypeError, ValueError):
                queryset = queryset.none()
        
        # Filter by absolute question number within pattern
        question_number = self.request.query_params.get('question_number')
        if question_number:
            try:
                queryset = queryset.filter(question_number=int(question_number))
            except (TypeError, ValueError):
                queryset = queryset.none()
        
        return queryset.order_by('-created_at')

    def perform_create(self, serializer):
        # The logic to check for existing questions is now handled inside QuestionCreateSerializer.create()
        serializer.save(
            institute=self.request.user.institute,
            created_by=self.request.user
        )


class QuestionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Get, update, and delete questions"""
    serializer_class = QuestionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.can_manage_exams():
            return Question.objects.filter(institute=user.institute)
        return Question.objects.filter(institute=user.institute, is_active=True)


class QuestionBankListView(generics.ListCreateAPIView):
    """List and create question banks"""
    serializer_class = QuestionBankSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = QuestionBank.objects.filter(institute=user.institute)
        
        # Show public banks from other institutes
        if user.can_manage_exams():
            public_banks = QuestionBank.objects.filter(is_public=True).exclude(institute=user.institute)
            queryset = queryset.union(public_banks)
        
        return queryset.order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(
            institute=self.request.user.institute,
            created_by=self.request.user
        )


class QuestionBankDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Get, update, and delete question banks"""
    serializer_class = QuestionBankSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.can_manage_exams():
            return QuestionBank.objects.filter(institute=user.institute)
        return QuestionBank.objects.filter(
            Q(institute=user.institute) | Q(is_public=True)
        )


class ExamQuestionListView(generics.ListCreateAPIView):
    """List and create exam questions"""
    permission_classes = [permissions.IsAuthenticated]

    def get_serializer_class(self):
        # Use different serializers for different response types
        if self.request.method == 'POST':
            return ExamQuestionSerializer
        # For GET, we'll handle serialization manually in list()
        return ExamQuestionSerializer

    def list(self, request, *args, **kwargs):
        """
        Override list to return questions from either ExamQuestion model
        or directly from Question model (fallback).
        Uses Question table as source of truth when ExamQuestion is incomplete.
        """
        exam_id = self.kwargs.get('exam_id')

        # Count questions in both tables to determine the best source
        exam_questions = ExamQuestion.objects.filter(exam_id=exam_id).order_by('section_name', 'question_number')
        all_questions = Question.objects.filter(exam_id=exam_id, is_active=True)

        exam_question_count = exam_questions.count()
        question_count = all_questions.count()

        # Use ExamQuestion only if it has ALL questions; otherwise fall back
        # to the Question table (source of truth) to avoid missing questions
        # due to partial population or unique_together conflicts with
        # subject-wise numbering.
        if exam_question_count > 0 and exam_question_count >= question_count:
            # Use ExamQuestion data (fully populated)
            serializer = ExamQuestionSerializer(exam_questions, many=True)
            result = serializer.data
        else:
            # Use Question model directly (source of truth)
            questions = all_questions.order_by('pattern_section_id', 'question_number', 'question_number_in_pattern')

            if questions.exists():
                # Return questions in a format similar to ExamQuestion serializer
                result = []
                for q in questions:
                    # Get pattern section info for section_name
                    section_name = q.pattern_section_name or q.subject or 'General'

                    result.append({
                        'id': q.id,
                        'exam': int(exam_id),
                        'question': QuestionSerializer(q).data,
                        'question_id': q.id,
                        'question_number': q.question_number or q.question_number_in_pattern or 0,
                        'section_name': section_name,
                        'marks': q.marks,
                        'negative_marks': float(q.negative_marks) if q.negative_marks else 0.25,
                        'order': q.question_number_in_pattern or q.question_number or 0
                    })
            else:
                return Response([])

        # Apply shuffling if enabled and user is a student
        user = request.user
        from exams.models import Exam
        try:
            exam = Exam.objects.get(id=exam_id)
        except Exam.DoesNotExist:
            return Response(result) # Should not happen

        # Detect if it's a student (students should see shuffled, admins/teachers might not)
        is_student = user.role in ['student', 'STUDENT']
        
        # Check if any shuffle is enabled
        shuffle_enabled = (
            exam.shuffle_questions or 
            exam.shuffle_within_sections or 
            exam.shuffle_sections or 
            exam.shuffle_subjects or 
            exam.shuffle_options
        )

        if is_student and shuffle_enabled:
            # Generate stable seed for the student/exam combination
            seed_source = f"exam_{exam.id}"
            if exam.shuffle_seed_per_student:
                seed_source += f"_user_{user.id}"
            
            # Create a deterministic seed (integer)
            seed_hex = hashlib.sha256(seed_source.encode()).hexdigest()
            # Use fixed part for reproducibility
            seed = int(seed_hex[:8], 16)
            
            # Use a local Random instance for question/section order
            rng = random.Random(seed)
            
            # 1. Global Shuffling (Absolute mix of all questions)
            if exam.shuffle_questions and not (exam.shuffle_within_sections or exam.shuffle_sections or exam.shuffle_subjects):
                rng.shuffle(result)
            
            # 2. Hierarchical Shuffling (Respecting structure)
            elif exam.shuffle_within_sections or exam.shuffle_sections or exam.shuffle_subjects:
                # Group by section/subject to respect hierarchical shuffling
                # We'll use (subject, section_name) as the group key
                groups = {}
                group_keys = [] 
                
                for q_data in result:
                    q_obj = q_data.get('question', {})
                    subject = q_obj.get('subject', 'General')
                    section = q_data.get('section_name', 'General')
                    key = (subject, section)
                    
                    if key not in groups:
                        groups[key] = []
                        group_keys.append(key)
                    groups[key].append(q_data)
                
                # Shuffle WITHIN groups (within section/subject blocks)
                if exam.shuffle_within_sections or exam.shuffle_questions:
                    for key in groups:
                        rng.shuffle(groups[key])
                
                # Shuffle GROUPS themselves
                if exam.shuffle_sections:
                    rng.shuffle(group_keys)
                elif exam.shuffle_subjects:
                    # Group keys by subject and shuffle subject blocks
                    subjects = {}
                    subject_order = []
                    for key in group_keys:
                        subj = key[0]
                        if subj not in subjects:
                            subjects[subj] = []
                            subject_order.append(subj)
                        subjects[subj].append(key)
                    
                    rng.shuffle(subject_order)
                    new_group_keys = []
                    for subj in subject_order:
                        new_group_keys.extend(subjects[subj])
                    group_keys = new_group_keys
                
                # Reconstruct result list
                shuffled_result = []
                for key in group_keys:
                    shuffled_result.extend(groups[key])
                result = shuffled_result

            # 2. Shuffle options for each question independently
            if exam.shuffle_options:
                for q_data in result:
                    q_obj_data = q_data.get('question')
                    if q_obj_data and q_obj_data.get('question_type') in ['single_mcq', 'multiple_mcq']:
                        options = q_obj_data.get('options', [])
                        if options and len(options) > 1:
                            # Use a question-specific seed to ensure stable but different options per question
                            q_seed_source = f"{seed_source}_q_{q_data['id']}"
                            q_seed = int(hashlib.sha256(q_seed_source.encode()).hexdigest()[:8], 16)
                            q_rng = random.Random(q_seed)
                            q_rng.shuffle(options)
                            q_obj_data['options'] = options

        # Security: Strip answer-revealing fields from student-facing response.
        # Students should NOT be able to see correct_answer / solution during the exam.
        # These are only returned via the results endpoint after submission.
        if is_student:
            for q_data in result:
                q_obj_data = q_data.get('question')
                if isinstance(q_obj_data, dict):
                    q_obj_data.pop('correct_answer', None)
                    q_obj_data.pop('solution', None)

        return Response(result)

    def get_queryset(self):
        exam_id = self.kwargs.get('exam_id')
        return ExamQuestion.objects.filter(exam_id=exam_id).order_by('question_number')

    def perform_create(self, serializer):
        exam_id = self.kwargs.get('exam_id')
        user = self.request.user
        
        # Check permissions
        try:
            from exams.models import Exam
            exam = Exam.objects.get(id=exam_id)
            if not user.can_manage_exams() or exam.institute != user.institute:
                raise permissions.PermissionDenied("You don't have permission to add questions to this exam")
        except Exam.DoesNotExist:
            raise permissions.PermissionDenied("Exam not found")
        
        serializer.save(exam_id=exam_id)


class QuestionTemplateListView(generics.ListAPIView):
    """List question templates"""
    queryset = QuestionTemplate.objects.filter(is_public=True)
    serializer_class = QuestionTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def bulk_import_questions(request):
    """Bulk import questions from JSON data"""
    serializer = BulkQuestionImportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    questions_data = serializer.validated_data['questions_data']
    question_bank_id = serializer.validated_data.get('question_bank_id')
    subject = serializer.validated_data.get('subject')
    topic = serializer.validated_data.get('topic')
    
    created_questions = []
    errors = []
    
    with transaction.atomic():
        for i, question_data in enumerate(questions_data):
            try:
                # Set default values
                question_data['institute'] = user.institute
                question_data['created_by'] = user
                
                if question_bank_id:
                    question_data['question_bank_id'] = question_bank_id
                
                if subject:
                    question_data['subject'] = subject
                
                if topic:
                    question_data['topic'] = topic
                
                # Use QuestionCreateSerializer to ensure normalization and validation
                serializer = QuestionCreateSerializer(data=question_data, context={'request': request})
                serializer.is_valid(raise_exception=True)
                question = serializer.save()
                created_questions.append(QuestionSerializer(question).data)
                
            except Exception as e:
                errors.append(f"Question {i+1}: {str(e)}")
    
    return Response({
        'success': True,
        'created_count': len(created_questions),
        'error_count': len(errors),
        'created_questions': created_questions,
        'errors': errors
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def bulk_import_csv(request):
    """Bulk import questions from CSV file"""
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    if 'file' not in request.FILES:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    file = request.FILES['file']
    question_bank_id = request.data.get('question_bank_id')
    subject = request.data.get('subject', '')
    topic = request.data.get('topic', '')
    
    if not file.name.endswith('.csv'):
        return Response({'error': 'File must be a CSV'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        # Read CSV file
        csv_data = file.read().decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(csv_data))
        
        created_questions = []
        errors = []
        
        with transaction.atomic():
            for i, row in enumerate(csv_reader):
                try:
                    # Map CSV columns to question fields
                    question_data = {
                        'question_text': row.get('question_text', ''),
                        'question_type': row.get('question_type', 'single_mcq'),
                        'difficulty': row.get('difficulty', 'medium'),
                        'correct_answer': row.get('correct_answer', ''),
                        'solution': row.get('solution', ''),
                        'explanation': row.get('explanation', ''),
                        'marks': int(row.get('marks', 1)),
                        'negative_marks': float(row.get('negative_marks', 0.25)),
                        'subject': row.get('subject', subject),
                        'topic': row.get('topic', topic),
                        'subtopic': row.get('subtopic', ''),
                        'institute': user.institute,
                        'created_by': user,
                    }
                    
                    # Handle options for MCQ questions
                    if question_data['question_type'] in ['single_mcq', 'multiple_mcq']:
                        options = []
                        for j in range(1, 6):  # Support up to 5 options
                            option = row.get(f'option_{j}', '').strip()
                            if option:
                                options.append(option)
                        question_data['options'] = options
                    
                    # Handle tags
                    tags_str = row.get('tags', '')
                    if tags_str:
                        question_data['tags'] = [tag.strip() for tag in tags_str.split(',')]
                    
                    if question_bank_id:
                        question_data['question_bank_id'] = question_bank_id
                    
                    # Use QuestionCreateSerializer to ensure normalization and validation
                    serializer = QuestionCreateSerializer(data=question_data, context={'request': request})
                    serializer.is_valid(raise_exception=True)
                    question = serializer.save()
                    created_questions.append(QuestionSerializer(question).data)
                    
                except Exception as e:
                    errors.append(f"Row {i+2}: {str(e)}")
        
        return Response({
            'success': True,
            'created_count': len(created_questions),
            'error_count': len(errors),
            'created_questions': created_questions,
            'errors': errors
        })
        
    except Exception as e:
        return Response({'error': f'Failed to process CSV: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def bulk_import_excel(request):
    """Bulk import questions from Excel file"""
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    if 'file' not in request.FILES:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    file = request.FILES['file']
    question_bank_id = request.data.get('question_bank_id')
    subject = request.data.get('subject', '')
    topic = request.data.get('topic', '')
    
    if not (file.name.endswith('.xlsx') or file.name.endswith('.xls')):
        return Response({'error': 'File must be an Excel file (.xlsx or .xls)'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        # Read Excel file
        df = pd.read_excel(file)
        
        created_questions = []
        errors = []
        
        with transaction.atomic():
            for i, row in df.iterrows():
                try:
                    # Map Excel columns to question fields
                    question_data = {
                        'question_text': str(row.get('question_text', '')),
                        'question_type': str(row.get('question_type', 'single_mcq')),
                        'difficulty': str(row.get('difficulty', 'medium')),
                        'correct_answer': str(row.get('correct_answer', '')),
                        'solution': str(row.get('solution', '')),
                        'explanation': str(row.get('explanation', '')),
                        'marks': int(row.get('marks', 1)),
                        'negative_marks': float(row.get('negative_marks', 0.25)),
                        'subject': str(row.get('subject', subject)),
                        'topic': str(row.get('topic', topic)),
                        'subtopic': str(row.get('subtopic', '')),
                        'institute': user.institute,
                        'created_by': user,
                    }
                    
                    # Handle options for MCQ questions
                    if question_data['question_type'] in ['single_mcq', 'multiple_mcq']:
                        options = []
                        for j in range(1, 6):  # Support up to 5 options
                            option = str(row.get(f'option_{j}', '')).strip()
                            if option and option != 'nan':
                                options.append(option)
                        question_data['options'] = options
                    
                    # Handle tags
                    tags_str = str(row.get('tags', ''))
                    if tags_str and tags_str != 'nan':
                        question_data['tags'] = [tag.strip() for tag in tags_str.split(',')]
                    
                    if question_bank_id:
                        question_data['question_bank_id'] = question_bank_id
                    
                    # Use QuestionCreateSerializer to ensure normalization and validation
                    serializer = QuestionCreateSerializer(data=question_data, context={'request': request})
                    serializer.is_valid(raise_exception=True)
                    question = serializer.save()
                    created_questions.append(QuestionSerializer(question).data)
                    
                except Exception as e:
                    errors.append(f"Row {i+2}: {str(e)}")
        
        return Response({
            'success': True,
            'created_count': len(created_questions),
            'error_count': len(errors),
            'created_questions': created_questions,
            'errors': errors
        })
        
    except Exception as e:
        return Response({'error': f'Failed to process Excel: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def download_import_template(request):
    """Download CSV template for question import"""
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    # Create CSV template
    template_data = [
        {
            'question_text': 'What is the capital of France?',
            'question_type': 'single_mcq',
            'difficulty': 'easy',
            'option_1': 'London',
            'option_2': 'Berlin',
            'option_3': 'Paris',
            'option_4': 'Madrid',
            'option_5': '',
            'correct_answer': 'Paris',
            'solution': 'Paris is the capital of France',
            'explanation': 'France is a country in Europe with Paris as its capital city',
            'marks': '1',
            'negative_marks': '0.25',
            'subject': 'Geography',
            'topic': 'European Capitals',
            'subtopic': 'Western Europe',
            'tags': 'capital, europe, france'
        },
        {
            'question_text': 'Solve: 2x + 5 = 15',
            'question_type': 'numerical',
            'difficulty': 'medium',
            'option_1': '',
            'option_2': '',
            'option_3': '',
            'option_4': '',
            'option_5': '',
            'correct_answer': '5',
            'solution': '2x = 15 - 5 = 10, x = 5',
            'explanation': 'Subtract 5 from both sides, then divide by 2',
            'marks': '2',
            'negative_marks': '0.5',
            'subject': 'Mathematics',
            'topic': 'Algebra',
            'subtopic': 'Linear Equations',
            'tags': 'algebra, equation, linear'
        }
    ]
    
    response = JsonResponse(template_data, safe=False)
    response['Content-Disposition'] = 'attachment; filename="question_import_template.csv"'
    response['Content-Type'] = 'text/csv'
    
    return response


def _build_ai_prompt(question_type: str, subject: str, topic: str, difficulty: str, instructions: str, marks: int, pattern_section: str, question_number: int):
    question_type_display = question_type.replace('_', ' ').title()
    subject_line = f"Subject: {subject}" if subject else "Subject: General Knowledge"
    topic_line = f"Topic: {topic}" if topic else "Topic: Mixed Concepts"
    section_line = f"Pattern Section: {pattern_section}" if pattern_section else ""
    question_number_line = f"Question Number in Paper: {question_number}" if question_number else ""
    marks_line = f"This question carries {marks} mark(s)." if marks else "This question carries 1 mark."
    instructions_line = instructions.strip() if instructions else "Focus on concept clarity and avoid unnecessary complexity."

    guidance = ""
    normalized_type = question_type.lower()
    if normalized_type in ['single_mcq', 'mcq', 'single correct mcq']:
        guidance = "Provide exactly four high-quality options with only one correct answer."
    elif normalized_type in ['multiple_mcq', 'multiple correct mcq']:
        guidance = "Provide five options with at least two correct answers. Return the correct answers as an array containing the exact option text."
    elif normalized_type in ['true_false', 'true/false']:
        guidance = "Use options ['True', 'False'] and ensure the correct answer is either 'True' or 'False'."
    elif normalized_type in ['numerical', 'numerical question']:
        guidance = "Ensure the correct answer is a numerical value. Provide a clear, step-by-step solution."
    elif normalized_type in ['subjective', 'descriptive']:
        guidance = "Frame the question to elicit a descriptive answer. Provide an ideal answer in the explanation."

    return f"""
You are an expert assessment designer. Generate one {difficulty} level {question_type_display} question.
{subject_line}
{topic_line}
{section_line}
{question_number_line}
{marks_line}

Additional author instructions:
{instructions_line}

Output must be a single JSON object with the following keys:
  "question_text": string (use minimal HTML, keep it clean)
  "options": array of answer option strings (empty array for numerical or subjective questions; for true/false use ["True","False"])
  "correct_answer": for single choice / numerical / true_false return a string; for multiple correct return an array of exact option texts
  "solution": detailed worked solution or model answer (string)
  "explanation": conceptual explanation or key takeaways (string)
  "difficulty": one of ["easy","medium","hard"]
  "topic": short topic descriptor string
  "tags": array of short tags (optional)

Guidelines:
{guidance}
- Do NOT include any text outside of the JSON object. No markdown fences, no commentary.
- Options must be unique, well-structured, and aligned with the correct answer.
- Ensure factual accuracy and avoid ambiguity.
"""


def _build_nested_ai_prompt(question_configuration: dict, nested_type: str, subject: str, topic: str, difficulty: str, instructions: str, marks: int):
    """Build AI prompt for nested/multipart questions based on the question configuration."""
    subject_line = f"Subject: {subject}" if subject else "Subject: General Knowledge"
    topic_line = f"Topic: {topic}" if topic else "Topic: Mixed Concepts"
    marks_line = f"This question carries {marks} mark(s) total." if marks else ""
    instructions_line = instructions.strip() if instructions else "Create questions that test understanding at the appropriate difficulty level."
    
    # Build the structure description from the configuration
    parts_description = []
    structure_example = []
    
    options = question_configuration.get('options', []) or question_configuration.get('sub_questions', [])
    
    for part in options:
        part_type = part.get('type', 'part')
        label = part.get('label', '')
        description = part.get('description', '')
        part_marks = part.get('marks', 1)
        
        if part_type == 'choice_group':
            # Handle OR choice groups
            choice_options = part.get('options', [])
            choice_descriptions = []
            for choice in choice_options:
                choice_label = choice.get('label', choice.get('description', ''))
                choice_marks = choice.get('marks', 1)
                sub_parts = choice.get('sub_parts', [])
                
                if sub_parts:
                    sub_desc = ", ".join([f"({sp.get('label', '')})" for sp in sub_parts])
                    choice_descriptions.append(f"  - Option {choice_label} ({choice_marks} marks) with sub-parts: {sub_desc}")
                    # Add structure for choices with sub_parts
                    structure_example.append({
                        "label": choice_label,
                        "text": f"Question text for choice {choice_label}",
                        "sub_parts": [{"label": sp.get('label', ''), "text": f"Sub-part {sp.get('label', '')} question"} for sp in sub_parts]
                    })
                else:
                    choice_descriptions.append(f"  - Option {choice_label} ({choice_marks} marks)")
                    structure_example.append({
                        "label": choice_label,
                        "text": f"Question text for choice {choice_label}"
                    })
            
            parts_description.append(f"CHOICE GROUP (student picks ONE):\n" + "\n".join(choice_descriptions))
        else:
            # Regular part
            sub_parts = part.get('sub_parts', []) or part.get('parts', [])
            if sub_parts:
                sub_desc = ", ".join([f"({sp.get('label', '')})" for sp in sub_parts])
                parts_description.append(f"Part {label} ({part_marks} marks) with sub-parts: {sub_desc}")
                structure_example.append({
                    "label": label,
                    "text": f"Introduction or context for part {label}",
                    "sub_parts": [{"label": sp.get('label', ''), "text": f"Sub-part {sp.get('label', '')} question"} for sp in sub_parts]
                })
            else:
                parts_description.append(f"Part {label} ({part_marks} marks)")
                structure_example.append({
                    "label": label,
                    "text": f"Question text for part {label}"
                })
    
    structure_desc = "\n".join([f"  - {p}" for p in parts_description])
    
    # Determine type description
    if nested_type == 'internal_choice':
        type_desc = "an OR/CHOICE question where the student must answer ONE of the given options"
    elif nested_type == 'mixed':
        type_desc = "a MIXED question with both required parts AND choice options"
    else:
        type_desc = "a MULTI-PART question with sequential parts (a), (b), (c), etc."
    
    import json
    example_json = json.dumps({
        "question_text": "Answer the following:",
        "structure": {
            "nested_parts": structure_example[:3]  # Show first 3 parts as example
        },
        "difficulty": difficulty,
        "topic": topic or "Relevant Topic"
    }, indent=2)
    
    return f"""
You are an expert assessment designer. Generate {type_desc}.
{subject_line}
{topic_line}
{marks_line}
Difficulty: {difficulty}

The question MUST have this EXACT structure:
{structure_desc}

Additional author instructions:
{instructions_line}

IMPORTANT: Generate content for EACH part according to the structure above.
- For regular parts: Provide a specific question or task
- For choice groups: Provide DIFFERENT but equally valid questions for each option
- For sub-parts: Each sub-part should be a distinct, specific question

Output must be a single JSON object with this format:
{example_json}

Guidelines:
- Generate unique, pedagogically sound content for EVERY part and sub-part
- Each part should test different aspects of the topic
- Ensure factual accuracy
- Do NOT include any text outside of the JSON object
- Do NOT include markdown fences or commentary
"""


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def generate_ai_question(request):
    """Generate a draft question using Google Gemini"""
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

    payload = request.data
    question_type = payload.get('question_type') or 'single_mcq'
    subject = payload.get('subject', '')
    topic = payload.get('topic', '')
    difficulty = (payload.get('difficulty') or 'medium').lower()
    instructions = payload.get('instructions', '')
    marks = payload.get('marks') or 1
    pattern_section_name = payload.get('pattern_section_name', '')
    question_number = payload.get('question_number') or 0
    
    # New: Handle nested questions
    is_nested = payload.get('is_nested', False)
    nested_type = payload.get('nested_type', '')
    question_configuration = payload.get('question_configuration')

    if difficulty not in ['easy', 'medium', 'hard']:
        difficulty = 'medium'

    print(f"🧠 AI generate request: user_id={user.id}, type={question_type}, subject={subject or 'N/A'}")
    if not configure_gemini():
        print(" AI generate aborted: Gemini API is not configured")
        return Response(
            {'error': 'Gemini API is not configured on the server.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Build appropriate prompt based on question type
    if is_nested and question_configuration:
        prompt = _build_nested_ai_prompt(
            question_configuration=question_configuration,
            nested_type=nested_type,
            subject=subject,
            topic=topic,
            difficulty=difficulty,
            instructions=instructions,
            marks=marks,
        )
    else:
        prompt = _build_ai_prompt(
            question_type=question_type,
            subject=subject,
            topic=topic,
            difficulty=difficulty,
            instructions=instructions,
            marks=marks,
            pattern_section=pattern_section_name,
            question_number=question_number
        )

    try:
        if not GOOGLE_AI_AVAILABLE or genai is None:
            return Response(
                {"error": "Google AI (Gemini) is not available. Please install google-generativeai package."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        model = genai.GenerativeModel(settings.GEMINI_MODEL)
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.7,
                "top_p": 0.9,
                "max_output_tokens": 4096,
            }
        )

        text = ""
        try:
            raw_text = getattr(response, 'text', None)
        except Exception:
            raw_text = None
        if raw_text:
            text = str(raw_text).strip()
        finish_reasons = []
        candidates = getattr(response, 'candidates', None) or []
        if candidates:
            finish_reasons = [str(getattr(candidate, 'finish_reason', None)) for candidate in candidates]
        if not text and candidates:
            for candidate in response.candidates:
                content = getattr(candidate, 'content', None)
                if content and getattr(content, 'parts', None):
                    for part in content.parts:
                        part_text = getattr(part, 'text', None)
                        if part_text:
                            text += part_text
                if text:
                    break
            text = text.strip()
        if not text:
            raise ValueError(f"AI returned no content (finish reasons: {finish_reasons or 'unknown'}). Consider simplifying the prompt.")

        # Robust JSON extraction
        ai_data = None
        
        # Method 1: Direct parsing
        try:
            ai_data = json.loads(text)
        except json.JSONDecodeError:
            pass
            
        # Method 2: Regex find outermost braces
        if not ai_data:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    ai_data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
                    
        # Method 3: Clean markdown blocks and retry direct parsing
        if not ai_data:
            cleaned_text = re.sub(r'```json\s*|\s*```', '', text).strip()
            try:
                ai_data = json.loads(cleaned_text)
            except json.JSONDecodeError:
                pass
                
        # Method 4: If text is an array, take the first item
        if isinstance(ai_data, list) and ai_data:
            ai_data = ai_data[0]
            
        if not isinstance(ai_data, dict):
            # Final fallback: look for ANY JSON-like structure
            if not ai_data:
                # Try to see if it's just a string that didn't parse but looks like an object
                raise ValueError(f"AI response did not contain a valid JSON object. Raw response was: {text[:200]}...")
            else:
                raise ValueError("AI response was parsed but is not a dictionary object.")

        question_text = ai_data.get('question_text', '').strip()
        options = ai_data.get('options', [])
        correct_answer = ai_data.get('correct_answer', '')
        solution = ai_data.get('solution', '').strip()
        explanation = ai_data.get('explanation', '').strip()
        ai_difficulty = (ai_data.get('difficulty') or difficulty).lower()
        ai_topic = ai_data.get('topic', topic).strip()
        tags = ai_data.get('tags', [])

        if not isinstance(options, list):
            options = []
        options = [str(opt).strip() for opt in options if str(opt).strip()]

        def map_answer_value(value):
            if isinstance(value, (int, float)):
                index = int(value) - 1
                if 0 <= index < len(options):
                    return options[index]
                return str(value)
            if isinstance(value, str):
                candidate = value.strip()
                if len(candidate) == 1 and candidate.upper().isalpha():
                    idx = ord(candidate.upper()) - 65
                    if 0 <= idx < len(options):
                        return options[idx]
                return candidate
            return str(value)

        normalized_type = question_type.lower()
        if normalized_type in ['multiple_mcq', 'multiple correct mcq']:
            if isinstance(correct_answer, list):
                normalized_answers = [map_answer_value(ans) for ans in correct_answer if str(ans).strip()]
            elif isinstance(correct_answer, str):
                parts = [part for part in re.split(r'[|,]', correct_answer) if part.strip()]
                normalized_answers = [map_answer_value(part) for part in parts]
            else:
                normalized_answers = [map_answer_value(correct_answer)]
            correct_answer_value = '|'.join(dict.fromkeys(filter(None, normalized_answers)))
        else:
            if isinstance(correct_answer, list):
                correct_answer_value = map_answer_value(correct_answer[0]) if correct_answer else ''
            else:
                correct_answer_value = map_answer_value(correct_answer)

        if normalized_type in ['true_false', 'true/false']:
            options = ['True', 'False']
            if correct_answer_value.lower() in ['true', 'false']:
                correct_answer_value = correct_answer_value.capitalize()

        question_payload = {
            'question_text': question_text,
            'options': options,
            'correct_answer': correct_answer_value,
            'solution': solution,
            'explanation': explanation or solution,
            'difficulty': ai_difficulty if ai_difficulty in ['easy', 'medium', 'hard'] else difficulty,
            'topic': ai_topic,
            'tags': tags,
        }
        
        # Include structure for nested questions
        ai_structure = ai_data.get('structure')
        if ai_structure and ai_structure.get('nested_parts'):
            question_payload['structure'] = ai_structure

        return Response({
            'question': question_payload,
            'message': 'AI generated a draft question. Review and adjust before saving.'
        })

    except json.JSONDecodeError:
        return Response(
            {'error': 'AI response could not be parsed. Please try again.'},
            status=status.HTTP_502_BAD_GATEWAY
        )
    except Exception as exc:
        if not GOOGLE_AI_AVAILABLE or google_exceptions is None:
            return Response(
                {"error": "Google AI (Gemini) is not available. Please install google-generativeai package."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        
        # Handle Google API exceptions
        if isinstance(exc, google_exceptions.ResourceExhausted):
            retry_seconds = None
            details = str(exc)
            retry_delay = getattr(exc, 'retry_delay', None)
            if retry_delay is not None and getattr(retry_delay, 'seconds', None) is not None:
                retry_seconds = retry_delay.seconds
            message = "Gemini API free-tier quota reached. Please wait a minute and try again."
            if retry_seconds:
                message = f"Gemini API free-tier quota reached. Please wait about {retry_seconds} seconds and try again."
            return Response(
                {'error': message, 'details': details},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        elif isinstance(exc, google_exceptions.GoogleAPIError):
            return Response(
                {'error': f'Gemini API error: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY
            )
        else:
            # Return generic error for other exceptions
            return Response(
                {'error': f'Question generation failed: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY
            )


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def use_question_template(request, template_id):
    """Use a question template to create a new question"""
    try:
        template = QuestionTemplate.objects.get(id=template_id)
    except QuestionTemplate.DoesNotExist:
        return Response({'error': 'Template not found'}, status=status.HTTP_404_NOT_FOUND)
    
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    # Get template variables from request
    template_variables = request.data.get('variables', {})
    
    # Generate question from template
    try:
        question_data = generate_question_from_template(template, template_variables)
        
        # Set user and institute
        question_data['institute'] = user.institute
        question_data['created_by'] = user
        
        # Create the question
        question = Question.objects.create(**question_data)
        
        # Increment template usage
        template.increment_usage()
        
        return Response({
            'success': True,
            'question': QuestionSerializer(question).data,
            'message': f'Question created successfully using template "{template.name}"'
        })
        
    except Exception as e:
        return Response({
            'error': f'Failed to generate question from template: {str(e)}'
        }, status=status.HTTP_400_BAD_REQUEST)


def generate_question_from_template(template, variables):
    """Generate question data from template and variables"""
    template_data = template.template_data
    question_data = {
        'question_text': template.example_question,
        'question_type': template.question_type,
        'difficulty': template.difficulty,
        'subject': template.subject or variables.get('subject', ''),
        'topic': template.topic or variables.get('topic', ''),
        'tags': template.tags,
        'marks': variables.get('marks', 1),
        'negative_marks': variables.get('negative_marks', 0.25),
    }
    
    # Generate question text from template format
    if 'question_format' in template_data:
        question_text = template_data['question_format']
        for var, value in variables.items():
            question_text = question_text.replace(f'{{{var}}}', str(value))
        question_data['question_text'] = question_text
    
    # Generate options for MCQ questions
    if template.question_type in ['single_mcq', 'multiple_mcq'] and 'options_format' in template_data:
        options = []
        for option_template in template_data['options_format']:
            option_text = option_template
            for var, value in variables.items():
                option_text = option_text.replace(f'{{{var}}}', str(value))
            if option_text.strip():
                options.append(option_text)
        question_data['options'] = options
    
    # Generate correct answer
    if 'solution_format' in template_data:
        correct_answer = template_data['solution_format']
        for var, value in variables.items():
            correct_answer = correct_answer.replace(f'{{{var}}}', str(value))
        question_data['correct_answer'] = correct_answer
    else:
        question_data['correct_answer'] = variables.get('correct_answer', '')
    
    # Generate solution
    if 'explanation_template' in template_data:
        solution = template_data['explanation_template']
        for var, value in variables.items():
            solution = solution.replace(f'{{{var}}}', str(value))
        question_data['solution'] = solution
    else:
        question_data['solution'] = variables.get('solution', '')
    
    # Generate explanation
    question_data['explanation'] = variables.get('explanation', question_data.get('solution', ''))
    
    return question_data


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_template_categories(request):
    """Get all available template categories"""
    categories = [
        {'value': choice[0], 'label': choice[1]} 
        for choice in QuestionTemplate.TEMPLATE_CATEGORIES
    ]
    return Response(categories)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def search_templates(request):
    """Search question templates with filters"""
    user = request.user
    
    # Base queryset
    queryset = QuestionTemplate.objects.filter(is_public=True)
    
    # Apply filters
    category = request.GET.get('category')
    if category:
        queryset = queryset.filter(category=category)
    
    question_type = request.GET.get('question_type')
    if question_type:
        queryset = queryset.filter(question_type=question_type)
    
    difficulty = request.GET.get('difficulty')
    if difficulty:
        queryset = queryset.filter(difficulty=difficulty)
    
    subject = request.GET.get('subject')
    if subject:
        queryset = queryset.filter(subject__icontains=subject)
    
    search = request.GET.get('search')
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) | 
            Q(description__icontains=search) |
            Q(tags__icontains=search)
        )
    
    # Order by featured first, then usage count
    queryset = queryset.order_by('-is_featured', '-usage_count', 'name')
    
    # Serialize results
    templates = []
    for template in queryset:
        templates.append({
            'id': template.id,
            'name': template.name,
            'description': template.description,
            'category': template.category,
            'question_type': template.question_type,
            'difficulty': template.difficulty,
            'subject': template.subject,
            'topic': template.topic,
            'example_question': template.example_question,
            'tags': template.tags,
            'usage_count': template.usage_count,
            'is_featured': template.is_featured,
            'created_at': template.created_at,
        })
    
    return Response({
        'templates': templates,
        'total': len(templates)
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def verify_question(request, question_id):
    """Verify a question"""
    try:
        question = Question.objects.get(id=question_id, institute=request.user.institute)
    except Question.DoesNotExist:
        return Response({'error': 'Question not found'}, status=status.HTTP_404_NOT_FOUND)
    
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    question.is_verified = True
    question.verified_by = user
    question.save()
    
    return Response({'message': 'Question verified successfully'})


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def add_question_image(request, question_id):
    """Add an image/diagram to a question"""
    try:
        question = Question.objects.get(id=question_id, institute=request.user.institute)
    except Question.DoesNotExist:
        return Response({'error': 'Question not found'}, status=status.HTTP_404_NOT_FOUND)
    
    if 'image' not in request.FILES:
        return Response({'error': 'No image provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    image_file = request.FILES['image']
    caption = request.data.get('caption', '')
    order = request.data.get('order', 1)
    
    question_image = QuestionImage.objects.create(
        question=question,
        image=image_file,
        caption=caption,
        order=order
    )
    
    return Response(QuestionImageSerializer(question_image).data, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([permissions.IsAuthenticated])
def delete_question_image(request, image_id):
    """Delete an image/diagram from a question"""
    try:
        image = QuestionImage.objects.get(id=image_id, question__institute=request.user.institute)
    except QuestionImage.DoesNotExist:
        return Response({'error': 'Image not found'}, status=status.HTTP_404_NOT_FOUND)
    
    image.delete()
    return Response({'message': 'Image deleted successfully'}, status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def add_question_comment(request, question_id):
    """Add a comment to a question"""
    try:
        question = Question.objects.get(id=question_id, institute=request.user.institute)
    except Question.DoesNotExist:
        return Response({'error': 'Question not found'}, status=status.HTTP_404_NOT_FOUND)
    
    comment_text = request.data.get('comment')
    rating = request.data.get('rating')
    is_review = request.data.get('is_review', False)
    
    if not comment_text:
        return Response({'error': 'Comment is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    comment = QuestionComment.objects.create(
        question=question,
        user=request.user,
        comment=comment_text,
        rating=rating,
        is_review=is_review
    )
    
    return Response({
        'comment': {
            'id': comment.id,
            'comment': comment.comment,
            'rating': comment.rating,
            'is_review': comment.is_review,
            'user_name': comment.user.get_full_name(),
            'created_at': comment.created_at
        }
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def debug_pattern_questions(request):
    """Debug endpoint to see what questions exist for a pattern"""
    user = request.user
    pattern_id = request.query_params.get('pattern_id')
    exam_id = request.query_params.get('exam_id')
    
    if not pattern_id:
        return Response({'error': 'pattern_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        from patterns.models import ExamPattern, PatternSection
        pattern = ExamPattern.objects.prefetch_related('sections').get(id=pattern_id)
    except ExamPattern.DoesNotExist:
        return Response({'error': 'Pattern not found'}, status=status.HTTP_404_NOT_FOUND)
    
    sections = pattern.sections.all().order_by('order', 'start_question')
    section_ids = [s.id for s in sections]
    
    # Get ALL questions for this pattern (no exam filter)
    all_questions = Question.objects.filter(
        institute=user.institute,
        pattern_section_id__in=section_ids,
        is_active=True
    ).values('id', 'question_number', 'question_number_in_pattern', 'pattern_section_id', 'exam_id', 'subject')
    
    # Get questions filtered by exam
    exam_questions = []
    if exam_id:
        exam_questions = Question.objects.filter(
            institute=user.institute,
            pattern_section_id__in=section_ids,
            exam_id=exam_id,
            is_active=True
        ).values('id', 'question_number', 'question_number_in_pattern', 'pattern_section_id', 'exam_id', 'subject')
    
    return Response({
        'pattern_id': pattern_id,
        'exam_id': exam_id,
        'sections': [{'id': s.id, 'name': s.name, 'subject': s.subject, 'start': s.start_question, 'end': s.end_question} for s in sections],
        'all_questions_count': all_questions.count(),
        'all_questions': list(all_questions)[:50],  # First 50
        'exam_questions_count': len(list(exam_questions)) if exam_id else 0,
        'exam_questions': list(exam_questions)[:50] if exam_id else [],
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_pattern_questions_bulk(request):
    """
    Optimized endpoint to fetch ALL questions for a pattern/exam in a single call.
    Returns questions grouped by section with proper numbering.
    
    Query params:
    - pattern_id (required): Pattern ID
    - exam_id (optional): Exam ID to filter questions
    
    Returns:
    - sections: List of sections with their questions
    - questions_by_section: Dict mapping section_id to list of questions
    - existing_numbers_by_section: Dict mapping section_id to set of existing question numbers
    - total_questions: Total count of questions
    """
    user = request.user
    
    pattern_id = request.query_params.get('pattern_id')
    exam_id = request.query_params.get('exam_id')
    
    if not pattern_id:
        return Response({'error': 'pattern_id is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        from patterns.models import ExamPattern, PatternSection
        pattern = ExamPattern.objects.prefetch_related('sections').get(id=pattern_id)
    except ExamPattern.DoesNotExist:
        return Response({'error': 'Pattern not found'}, status=status.HTTP_404_NOT_FOUND)
    
    # Build base queryset for questions
    questions_qs = Question.objects.filter(
        institute=user.institute,
        is_active=True
    ).select_related('created_by', 'verified_by')
    
    # Filter by exam if provided
    if exam_id:
        questions_qs = questions_qs.filter(exam_id=exam_id)
    
    # Get all sections for this pattern
    sections = pattern.sections.all().order_by('order', 'start_question')
    section_ids = [s.id for s in sections]
    
    # Filter questions by pattern sections
    questions_qs = questions_qs.filter(pattern_section_id__in=section_ids)
    
    # Order by section and question number
    questions_qs = questions_qs.order_by('pattern_section_id', 'question_number')
    
    # Serialize all questions at once
    all_questions = list(questions_qs)
    
    # Group questions by section
    questions_by_section = {}
    existing_numbers_by_section = {}
    
    # First, compute subject_start for each section (grouped by subject)
    subject_offsets = {}  # subject -> current offset
    section_subject_starts = {}  # section_id -> subject_start
    
    for section in sections:
        subject = section.subject
        if subject not in subject_offsets:
            subject_offsets[subject] = 0
        
        section_length = section.end_question - section.start_question + 1
        section_subject_starts[section.id] = subject_offsets[subject] + 1  # 1-indexed
        subject_offsets[subject] += section_length
    
    for section in sections:
        section_questions = [q for q in all_questions if q.pattern_section_id == section.id]
        questions_by_section[section.id] = QuestionSerializer(section_questions, many=True).data
        
        # Build set of existing question numbers for this section
        # These should be subject-local numbers (1, 2, 3... within the subject)
        existing_nums = set()
        subject_start = section_subject_starts.get(section.id, 1)
        
        for idx, q in enumerate(section_questions):
            # Use question_number_in_pattern if available
            if q.question_number_in_pattern is not None:
                existing_nums.add(q.question_number_in_pattern)
            elif q.question_number is not None:
                # Convert database question_number to subject-local number
                # question_number is absolute (31, 32, 33...)
                # We need to convert to subject-local (1, 2, 3...)
                offset = q.question_number - section.start_question
                subject_local = subject_start + offset
                existing_nums.add(subject_local)
            else:
                # Fallback: use index-based numbering
                subject_local = subject_start + idx
                existing_nums.add(subject_local)
        
        existing_numbers_by_section[section.id] = list(existing_nums)
    
    # Build section stats with complete information
    section_stats = []
    for section in sections:
        total_needed = section.end_question - section.start_question + 1
        total_added = len(questions_by_section.get(section.id, []))
        
        section_stats.append({
            'section_id': section.id,
            'id': section.id,  # Alias for frontend compatibility
            'section_name': section.name,
            'name': section.name,  # Alias for frontend compatibility
            'subject': section.subject,
            'question_type': section.question_type,
            'start_question': section.start_question,
            'end_question': section.end_question,
            'marks_per_question': section.marks_per_question,
            'negative_marking': float(section.negative_marking),
            'min_questions_to_attempt': section.min_questions_to_attempt,
            'is_compulsory': section.is_compulsory,
            'order': section.order,
            'total_needed': total_needed,
            'total_added': total_added,
            'remaining': total_needed - total_added,
            'progress_percentage': (total_added / total_needed * 100) if total_needed > 0 else 0,
        })
    
    return Response({
        'pattern_id': pattern.id,
        'pattern_name': pattern.name,
        'sections': section_stats,
        'questions_by_section': questions_by_section,
        'existing_numbers_by_section': existing_numbers_by_section,
        'total_questions': len(all_questions),
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def get_section_questions(request, section_id):
    """
    Optimized endpoint to fetch all questions for a specific section.
    
    Query params:
    - exam_id (optional): Exam ID to filter questions
    
    Returns:
    - questions: List of questions in this section
    - existing_numbers: List of existing question numbers (subject-local)
    - section: Section details
    """
    user = request.user
    exam_id = request.query_params.get('exam_id')
    
    try:
        from patterns.models import PatternSection
        section = PatternSection.objects.select_related('pattern').get(id=section_id)
    except PatternSection.DoesNotExist:
        return Response({'error': 'Section not found'}, status=status.HTTP_404_NOT_FOUND)
    
    # Build queryset
    questions_qs = Question.objects.filter(
        institute=user.institute,
        is_active=True,
        pattern_section_id=section_id
    ).select_related('created_by', 'verified_by').order_by('question_number')
    
    if exam_id:
        questions_qs = questions_qs.filter(exam_id=exam_id)
    
    questions = list(questions_qs)
    
    # Build existing numbers set
    existing_numbers = []
    questions_map = {}  # Map question_number_in_pattern -> question data
    
    for q in questions:
        q_data = QuestionSerializer(q).data
        
        # Determine the subject-local question number
        if q.question_number_in_pattern is not None:
            local_num = q.question_number_in_pattern
        elif q.question_number is not None:
            # Compute subject-local from database question_number
            local_num = q.question_number
        else:
            continue
        
        existing_numbers.append(local_num)
        questions_map[local_num] = q_data
    
    return Response({
        'section': {
            'id': section.id,
            'name': section.name,
            'subject': section.subject,
            'question_type': section.question_type,
            'start_question': section.start_question,
            'end_question': section.end_question,
            'marks_per_question': section.marks_per_question,
            'negative_marking': float(section.negative_marking),
            'total_needed': section.end_question - section.start_question + 1,
        },
        'questions': QuestionSerializer(questions, many=True).data,
        'existing_numbers': existing_numbers,
        'questions_map': questions_map,
        'total_count': len(questions),
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def fix_question_numbers(request):
    """
    Fix question_number_in_pattern for questions in a specific exam/pattern.
    This ensures imported questions show up correctly in the question navigator.
    
    POST body:
    - exam_id (optional): Fix questions for specific exam
    - pattern_id (optional): Fix questions for specific pattern
    
    At least one of exam_id or pattern_id must be provided.
    """
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    exam_id = request.data.get('exam_id')
    pattern_id = request.data.get('pattern_id')
    
    if not exam_id and not pattern_id:
        return Response(
            {'error': 'Either exam_id or pattern_id is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    from patterns.models import PatternSection
    
    # Build base queryset
    questions_qs = Question.objects.filter(
        institute=user.institute,
        pattern_section_id__isnull=False,
        is_active=True
    )
    
    if exam_id:
        questions_qs = questions_qs.filter(exam_id=exam_id)
    
    if pattern_id:
        section_ids = list(PatternSection.objects.filter(
            pattern_id=pattern_id
        ).values_list('id', flat=True))
        questions_qs = questions_qs.filter(pattern_section_id__in=section_ids)
    
    # Group by section and fix numbers
    section_ids = questions_qs.values_list('pattern_section_id', flat=True).distinct()
    
    total_fixed = 0
    sections_processed = []
    
    for section_id in section_ids:
        try:
            section = PatternSection.objects.get(id=section_id)
        except PatternSection.DoesNotExist:
            continue
        
        # Get questions for this section, ordered by question_number or id
        section_questions = list(questions_qs.filter(
            pattern_section_id=section_id
        ).order_by('question_number', 'id'))
        
        questions_to_update = []
        
        for idx, question in enumerate(section_questions):
            expected_number = idx + 1
            
            if question.question_number_in_pattern != expected_number:
                question.question_number_in_pattern = expected_number
                questions_to_update.append(question)
        
        if questions_to_update:
            with transaction.atomic():
                Question.objects.bulk_update(
                    questions_to_update,
                    ['question_number_in_pattern'],
                    batch_size=100
                )
            total_fixed += len(questions_to_update)
        
        sections_processed.append({
            'section_id': section_id,
            'section_name': section.name,
            'questions_fixed': len(questions_to_update),
            'total_questions': len(section_questions)
        })
    
    return Response({
        'success': True,
        'total_fixed': total_fixed,
        'sections_processed': sections_processed,
        'message': f'Fixed {total_fixed} question numbers across {len(sections_processed)} sections'
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def solve_question_with_ai(request):
    """Use AI to solve a specific question and provide the answer and solution."""
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

    payload = request.data
    question_text = payload.get('question_text', '')
    options = payload.get('options', [])
    question_type = payload.get('question_type', 'mcq')
    subject = payload.get('subject', '')

    if not question_text:
        return Response({'error': 'Question text is required'}, status=status.HTTP_400_BAD_REQUEST)

    print(f"🧠 AI solve request: user_id={user.id}, type={question_type}, subject={subject or 'N/A'}")
    if not configure_gemini():
        print(" AI solve aborted: Gemini API is not configured")
        return Response(
            {'error': 'Gemini API is not configured on the server.'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )

    # Build solve prompt
    prompt = f"""
Solve the following {subject} question accurately and provide a detailed step-by-step solution.

Question:
{question_text}

Question Type: {question_type}
"""
    if options and len(options) > 0:
        prompt += "\nOptions:\n"
        for i, opt in enumerate(options):
            prompt += f"{chr(65+i)}) {opt}\n"

    prompt += """
Return the result EXCLUSIVELY in the following JSON format:
{
  "correct_answer": "Provide the exact text of the correct option (for MCQs) or the specific numerical/short answer value.",
  "solution": "Provide a detailed step-by-step worked solution using LaTeX for mathematical formulas or scientific notations.",
  "explanation": "Provide a brief conceptual explanation."
}

Important:
- Use LaTeX for all mathematical formulas, scientific notations, and chemical equations (e.g., $E=mc^2$).
- If multiple options are correct for a 'multiple_mcq', return an array of strings in 'correct_answer'.
- Do NOT include any text outside of the JSON object.
"""

    def _fallback_solve_payload(message: str, raw_solution: str = ""):
        return {
            "correct_answer": "",
            "solution": raw_solution or "AI solver is temporarily unavailable. Please try again with a shorter question or fewer options.",
            "explanation": raw_solution or "AI solver could not generate a structured result.",
            "_meta": {
                "fallback": True,
                "message": message,
            }
        }

    try:
        if not GOOGLE_AI_AVAILABLE or genai is None:
            return Response(_fallback_solve_payload("Google AI (Gemini) package is not available."), status=status.HTTP_200_OK)

        configured_model = settings.GEMINI_MODEL
        candidate_models = [configured_model]

        response = None
        last_error = None
        selected_model = None
        for model_name in candidate_models:
            try:
                print(f"🧠 AI solve using model={model_name}")
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0.2,
                        "top_p": 0.9,
                        "max_output_tokens": 2048,
                    }
                )
                selected_model = model_name
                break
            except Exception as model_exc:
                last_error = model_exc
                print(f"⚠️ AI solve model failed ({model_name}): {model_exc}")
                continue

        if response is None:
            raise last_error or ValueError("Gemini did not return a response")

        text = ""
        try:
            raw_text = getattr(response, 'text', None)
        except Exception:
            raw_text = None
        if raw_text:
            text = str(raw_text).strip()

        finish_reasons = []
        candidates = getattr(response, 'candidates', None) or []
        if candidates:
            finish_reasons = [str(getattr(candidate, 'finish_reason', None)) for candidate in candidates]

        if not text and candidates:
            for candidate in candidates:
                content = getattr(candidate, 'content', None)
                if content and getattr(content, 'parts', None):
                    for part in content.parts:
                        part_text = getattr(part, 'text', None)
                        if part_text:
                            text += part_text
                if text:
                    break
            text = text.strip()

        if not text:
            raise ValueError(f"AI returned no content (finish reasons: {finish_reasons or 'unknown'})")

        ai_data = None
        try:
            ai_data = json.loads(text)
        except json.JSONDecodeError:
            pass

        if not ai_data:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                json_str = match.group(0)
                try:
                    ai_data = json.loads(json_str)
                except json.JSONDecodeError:
                    json_str_fixed = re.sub(r'\\(?![\\"/bfnrtu])', r'\\\\', json_str)
                    try:
                        ai_data = json.loads(json_str_fixed)
                    except json.JSONDecodeError:
                        ai_data = None

        if not isinstance(ai_data, dict):
            # Last-resort fallback for non-JSON responses: still return useful content.
            extracted_answer = ""
            answer_patterns = [
                r'(?im)^\s*correct[_\s-]*answer\s*[:\-]\s*(.+?)\s*$',
                r'(?im)^\s*answer\s*[:\-]\s*(.+?)\s*$',
                r'(?im)^\s*final\s*answer\s*[:\-]\s*(.+?)\s*$',
            ]
            for pattern in answer_patterns:
                answer_match = re.search(pattern, text)
                if answer_match:
                    extracted_answer = answer_match.group(1).strip()
                    break

            ai_data = {
                "correct_answer": extracted_answer,
                "solution": text,
                "explanation": text,
            }

        ai_data['_meta'] = {
            'model': selected_model,
            'finish_reasons': finish_reasons,
        }
        return Response(ai_data)

    except json.JSONDecodeError as exc:
        return Response(
            _fallback_solve_payload(f"AI returned invalid JSON: {exc}"),
            status=status.HTTP_200_OK
        )
    except Exception as exc:
        if not GOOGLE_AI_AVAILABLE or google_exceptions is None:
            return Response(
                _fallback_solve_payload("Google AI (Gemini) is not available. Please install google-generativeai package."),
                status=status.HTTP_200_OK
            )
        if isinstance(exc, google_exceptions.ResourceExhausted):
            details = str(exc)
            return Response(
                _fallback_solve_payload(f"Gemini API quota reached. Details: {details}"),
                status=status.HTTP_200_OK
            )
        if isinstance(exc, google_exceptions.GoogleAPIError):
            return Response(
                _fallback_solve_payload(f"Gemini API error: {exc}"),
                status=status.HTTP_200_OK
            )
        return Response(
            _fallback_solve_payload(f"AI solver error: {exc}"),
            status=status.HTTP_200_OK
        )


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def question_statistics(request):
    """Get question statistics for the institute"""
    user = request.user
    if not user.can_manage_exams():
        return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    
    institute = user.institute
    
    stats = {
        'total_questions': Question.objects.filter(institute=institute).count(),
        'verified_questions': Question.objects.filter(institute=institute, is_verified=True).count(),
        'questions_by_type': {},
        'questions_by_difficulty': {},
        'questions_by_subject': {},
        'recent_questions': []
    }
    
    # Questions by type
    for question_type, _ in Question.QUESTION_TYPE_CHOICES:
        count = Question.objects.filter(institute=institute, question_type=question_type).count()
        stats['questions_by_type'][question_type] = count
    
    # Questions by difficulty
    for difficulty, _ in Question.DIFFICULTY_CHOICES:
        count = Question.objects.filter(institute=institute, difficulty=difficulty).count()
        stats['questions_by_difficulty'][difficulty] = count
    
    # Questions by subject
    subjects = Question.objects.filter(institute=institute).values_list('subject', flat=True).distinct()
    for subject in subjects:
        count = Question.objects.filter(institute=institute, subject=subject).count()
        stats['questions_by_subject'][subject] = count
    
    # Recent questions
    recent_questions = Question.objects.filter(institute=institute).order_by('-created_at')[:10]
    stats['recent_questions'] = QuestionSerializer(recent_questions, many=True).data
    
    return Response(stats)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def validate_exam_questions(request, exam_id):
    """
    Validate that all questions in an exam have answers/solutions.
    Returns a list of questions with missing answers.
    """
    try:
        from exams.models import Exam
        exam = Exam.objects.get(id=exam_id)
        if not request.user.can_manage_exams() or exam.institute != request.user.institute:
            return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)
    except Exam.DoesNotExist:
        return Response({'error': 'Exam not found'}, status=status.HTTP_404_NOT_FOUND)

    # Get questions for this exam
    questions = Question.objects.filter(exam_id=exam_id, is_active=True).order_by('question_number')
    
    missing_answers = []
    
    for q in questions:
        has_answer = False
        if q.question_type == 'subjective':
            # For subjective, we need either a solution or a correct_answer (some text)
            if (q.solution and q.solution.strip()) or (q.correct_answer and q.correct_answer.strip()):
                has_answer = True
        elif q.question_type in ['multiple_mcq', 'multiple correct mcq']:
             # Multiple MCQ might have array in correct_answer or non-empty string
             if q.correct_answer and len(q.correct_answer) > 0:
                 has_answer = True
        else:
            # Single MCQ, Numerical, True/False
            if q.correct_answer and str(q.correct_answer).strip():
                has_answer = True
        
        if not has_answer:
            missing_answers.append({
                'id': q.id,
                'question_number': q.question_number,
                'question_text': q.question_text,
                'subject': q.subject,
                'type': q.question_type
            })
            
    return Response({
        'valid': len(missing_answers) == 0,
        'missing_answers': missing_answers,
        'total_questions': questions.count()
    })
