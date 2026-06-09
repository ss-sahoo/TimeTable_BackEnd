import json
import re
from decimal import Decimal
from typing import Dict, List, Tuple, Optional
from django.utils import timezone
from django.db import transaction
from django.conf import settings
import google.generativeai as genai
import logging

logger = logging.getLogger(__name__)

# Configure Gemini
if hasattr(settings, 'GEMINI_API_KEY') and settings.GEMINI_API_KEY:
    genai.configure(api_key=settings.GEMINI_API_KEY)

from .models import ExamAttempt, QuestionEvaluation, EvaluationBatch, EvaluationSettings, EvaluationProgress
from questions.models import Question


class EvaluationService:
    """Service class for handling different types of question evaluation"""
    
    def __init__(self, exam_attempt: ExamAttempt):
        self.attempt = exam_attempt
        self.exam = exam_attempt.exam
        self.student = exam_attempt.student
        self.settings = self._get_evaluation_settings()
        self.question_configs = {} # Cache for dynamic marks/negative marks {question_id: {'marks': X, 'negative': Y}}
    
    def _get_evaluation_settings(self) -> EvaluationSettings:
        """Get or create evaluation settings for the exam"""
        settings, created = EvaluationSettings.objects.get_or_create(
            exam=self.exam,
            defaults={
                'enable_auto_evaluation': True,
                'enable_manual_evaluation': True,
                'enable_ai_evaluation': True,
            }
        )
        return settings
    
    def evaluate_attempt(self, answers: Dict[str, str]) -> Dict:
        """Main method to evaluate an exam attempt"""
        with transaction.atomic():
            # Create evaluation progress if it doesn't exist
            progress, _ = EvaluationProgress.objects.get_or_create(exam=self.exam)
            
            # Get all questions for this exam
            questions = self._get_exam_questions()
            progress.total_questions = len(questions)
            progress.save()
            
            # If answers is empty, try to get from ExamResult
            if not answers:
                try:
                    from .models import ExamResult
                    result = ExamResult.objects.get(attempt=self.attempt)
                    # Extract answers from ExamResult format
                    answers = {}
                    
                    # Create mapping from question ID to question number
                    id_to_number = {}
                    for i, question in enumerate(questions, 1):
                        id_to_number[question.id] = i
                    
                    for q_id, answer_data in result.answers.items():
                        if isinstance(answer_data, dict) and 'answer' in answer_data:
                            question_number = id_to_number.get(int(q_id))
                            if question_number:
                                answers[str(question_number)] = answer_data['answer']
                        else:
                            question_number = id_to_number.get(int(q_id))
                            if question_number:
                                answers[str(question_number)] = str(answer_data)
                except:
                    pass
            
            # Process each question
            evaluation_results = []
            auto_evaluated_count = 0
            ai_evaluated_count = 0
            manual_evaluation_required = []
            ai_evaluation_required = []
            
            for idx, question in enumerate(questions, 1):
                # Use 1-based index as the question number for this attempt
                # This avoids the bug where all questions without question_number_in_pattern all get number=1
                question_number = self._get_question_number(question, idx)
                
                # Get dynamic configs for this question
                config = self.question_configs.get(question.id, {
                    'marks': float(question.marks),
                    'negative': float(question.negative_marks) if getattr(question, 'negative_marking', None) is None else float(question.negative_marking)
                })

                # Try to get answer by question ID first (most reliable), then by question number
                answer_data = answers.get(str(question.id)) or answers.get(str(question_number))
                
                # Extract actual answer from the data
                if isinstance(answer_data, dict) and 'answer' in answer_data:
                    student_answer = str(answer_data['answer'])
                elif answer_data:
                    student_answer = str(answer_data)
                else:
                    student_answer = ''
                
                # Create question evaluation record
                q_eval, created = QuestionEvaluation.objects.get_or_create(
                    attempt=self.attempt,
                    question=question,
                    defaults={
                        'question_number': question_number,
                        'student_answer': student_answer,
                        'is_answered': bool(student_answer.strip()),
                        'max_marks': Decimal(str(config['marks'])),
                    }
                )
                
                if not created:
                    q_eval.student_answer = student_answer
                    q_eval.is_answered = bool(student_answer.strip())
                    q_eval.max_marks = Decimal(str(config['marks']))
                    q_eval.save()
                
                # Determine evaluation type and process
                eval_type = self._determine_evaluation_type(question)
                q_eval.evaluation_type = eval_type
                
                if eval_type == 'auto':
                    result = self._auto_evaluate_question(q_eval, question, student_answer, config)
                    auto_evaluated_count += 1
                elif eval_type == 'manual':
                    q_eval.evaluation_status = 'pending'
                    manual_evaluation_required.append(q_eval)
                elif eval_type == 'ai':
                    # Perform immediate AI evaluation if requested
                    result = self.evaluate_with_ai(q_eval)
                    if result.get('success'):
                        ai_evaluated_count += 1
                        evaluation_results.append(result)
                    else:
                        q_eval.evaluation_status = 'pending'
                        ai_evaluation_required.append(q_eval)
                        evaluation_results.append(None)
                
                q_eval.save()
                evaluation_results.append(result if eval_type == 'auto' else None)
            
            # Update progress
            progress.auto_evaluated = auto_evaluated_count
            progress.ai_evaluated = ai_evaluated_count
            progress.pending_evaluation = len(manual_evaluation_required) + len(ai_evaluation_required)
            progress.save()
            
            # Create evaluation batches if needed
            if manual_evaluation_required and self.settings.enable_manual_evaluation:
                self._create_evaluation_batch('manual', manual_evaluation_required)
            
            if ai_evaluation_required and self.settings.enable_ai_evaluation:
                self._create_evaluation_batch('ai', ai_evaluation_required)
            
            # Calculate final score
            final_score = self._calculate_final_score()
            
            return {
                'evaluation_results': evaluation_results,
                'auto_evaluated': auto_evaluated_count,
                'ai_evaluated': ai_evaluated_count,
                'manual_required': len(manual_evaluation_required),
                'ai_required': len(ai_evaluation_required),
                'final_score': final_score,
                'evaluation_progress': progress.completion_percentage
            }
    
    def _get_exam_questions(self) -> List[Question]:
        """Get all questions for the exam honoring pattern sections with safe fallbacks"""
        collected: List[Question] = []
        seen_ids = set()

        def add_questions(queryset, marks=None, negative=None):
            for question in queryset:
                if question.id not in seen_ids:
                    collected.append(question)
                    seen_ids.add(question.id)
                    # Store marks config
                    if marks is not None:
                        self.question_configs[question.id] = {
                            'marks': float(marks),
                            'negative': float(negative if negative is not None else 0)
                        }
                    else:
                        # Fallback to question or exam-question mapping
                        try:
                            from questions.models import ExamQuestion
                            eq = ExamQuestion.objects.filter(exam=self.exam, question=question).first()
                            if eq:
                                self.question_configs[question.id] = {
                                    'marks': float(eq.marks),
                                    'negative': float(eq.negative_marks)
                                }
                            else:
                                self.question_configs[question.id] = {
                                    'marks': float(question.marks),
                                    'negative': float(question.negative_marks)
                                }
                        except:
                            self.question_configs[question.id] = {
                                'marks': float(question.marks),
                                'negative': float(question.negative_marks)
                            }

        pattern = getattr(self.exam, 'pattern', None)
        if pattern and hasattr(pattern, 'sections'):
            for section in pattern.sections.all().order_by('start_question'):
                before_count = len(collected)
                marks = section.marks_per_question
                negative = section.negative_marking

                add_questions(
                    Question.objects.filter(
                        pattern_section_id=section.id
                    ).order_by('question_number_in_pattern', 'question_number', 'id'),
                    marks=marks, negative=negative
                )

                if len(collected) == before_count and section.name:
                    add_questions(
                        Question.objects.filter(
                            exam=self.exam,
                            pattern_section_name=section.name
                        ).order_by('question_number_in_pattern', 'question_number', 'id'),
                        marks=marks, negative=negative
                    )

                if len(collected) == before_count:
                    add_questions(
                        Question.objects.filter(
                            exam=self.exam,
                            subject__iexact=section.subject,
                            question_number__gte=section.start_question,
                            question_number__lte=section.end_question
                        ).order_by('question_number', 'id'),
                        marks=marks, negative=negative
                    )

        if not collected:
            add_questions(
                Question.objects.filter(exam=self.exam).order_by('question_number_in_pattern', 'question_number', 'id')
            )

        return collected
    
    def _get_question_number(self, question: Question, fallback_idx: int = 1) -> int:
        """Get the question number within the exam.
        Falls back to the 1-based index of the question in the exam list
        to avoid the bug where every question without a pattern number returns 1.
        """
        return question.question_number_in_pattern or question.question_number or fallback_idx
    
    def _determine_evaluation_type(self, question: Question) -> str:
        """Determine the evaluation type for a question"""
        question_type = question.question_type
        
        # Auto-evaluation for objective questions
        if question_type in ['single_mcq', 'multiple_mcq', 'true_false', 'numerical', 'fill_blank']:
            if self.settings.enable_auto_evaluation:
                return 'auto'
        
        # Manual evaluation for subjective questions
        # Manual/AI evaluation for subjective questions
        if question_type == 'subjective':
            # Disable automatic AI evaluation during student submission to prevent timeouts/hangs
            # if self.settings.enable_ai_evaluation:
            #     return 'ai'  # Try AI first
            if self.settings.enable_manual_evaluation:
                return 'manual'
        
        # Default to manual if nothing else is configured
        return 'manual'
    
    def _auto_evaluate_question(self, q_eval: QuestionEvaluation, question: Question, student_answer: str, config: Dict = None) -> Dict:
        """Auto-evaluate objective questions"""
        try:
            if not config:
                config = self.question_configs.get(question.id, {
                    'marks': float(question.marks),
                    'negative': float(question.negative_marks)
                })

            if question.question_type == 'single_mcq':
                result = self._evaluate_single_mcq(question, student_answer, marks=config['marks'], negative=config['negative'])
            elif question.question_type == 'multiple_mcq':
                result = self._evaluate_multiple_mcq(question, student_answer, marks=config['marks'], negative=config['negative'])
            elif question.question_type == 'true_false':
                result = self._evaluate_true_false(question, student_answer, marks=config['marks'], negative=config['negative'])
            elif question.question_type == 'numerical':
                result = self._evaluate_numerical(question, student_answer, marks=config['marks'], negative=config['negative'])
            elif question.question_type == 'fill_blank':
                result = self._evaluate_fill_blank(question, student_answer, marks=config['marks'], negative=config['negative'])
            else:
                result = {'is_correct': False, 'marks_obtained': 0, 'feedback': 'Unsupported question type for auto-evaluation'}
            
            # Update question evaluation
            q_eval.marks_obtained = Decimal(str(result['marks_obtained']))
            q_eval.is_correct = result['is_correct']
            q_eval.evaluation_status = 'auto_evaluated'
            q_eval.evaluated_at = timezone.now()
            q_eval.evaluation_notes = result.get('feedback', '')
            q_eval.save()
            
            return result
            
        except Exception as e:
            # Mark as failed and require manual evaluation
            q_eval.evaluation_status = 'pending'
            q_eval.evaluation_notes = f"Auto-evaluation failed: {str(e)}"
            q_eval.save()
            
            return {
                'is_correct': False,
                'marks_obtained': 0,
                'feedback': f"Auto-evaluation failed: {str(e)}",
                'error': True
            }
    
    def _evaluate_single_mcq(self, question: Question, student_answer: str, marks: float = None, negative: float = None) -> Dict:
        """Evaluate single correct MCQ with negative marking support"""
        if marks is None: marks = float(question.marks)
        if negative is None: negative = float(question.negative_marks) if question.negative_marks else 0.0

        correct_answer = question.correct_answer.strip().lower()

        student_answer_stripped = student_answer.strip().lower()
        
        is_correct = correct_answer == student_answer_stripped
        
        if is_correct:
            marks_obtained = float(marks)
        elif student_answer_stripped:  # Wrong answer — apply negative marks
            marks_obtained = -float(negative)
        else:  # Unanswered
            marks_obtained = 0
        
        return {
            'is_correct': is_correct,
            'marks_obtained': marks_obtained,
            'feedback': 'Correct!' if is_correct else (
                f'Wrong answer. Correct answer: {question.correct_answer}'
                if student_answer_stripped else 'Not attempted.'
            )
        }
    
    def _evaluate_multiple_mcq(self, question: Question, student_answer: str, marks: float = None, negative: float = None) -> Dict:
        """Evaluate multiple correct MCQ with negative marking support"""
        if marks is None: marks = float(question.marks)
        if negative is None: negative = float(question.negative_marks) if question.negative_marks else 0.0

        try:

            # Handle pipe-separated format: "option1|option2|option3"
            if isinstance(student_answer, str) and '|' in student_answer:
                student_answers = [ans.strip() for ans in student_answer.split('|') if ans.strip()]
            elif isinstance(student_answer, str):
                try:
                    # Try JSON format as fallback
                    student_answers = json.loads(student_answer)
                except:
                    student_answers = [student_answer] if student_answer.strip() else []
            else:
                student_answers = [student_answer]
            
            # Handle correct answer format
            if isinstance(question.correct_answer, str) and '|' in question.correct_answer:
                correct_answers = [ans.strip() for ans in question.correct_answer.split('|') if ans.strip()]
            elif isinstance(question.correct_answer, str):
                try:
                    correct_answers = json.loads(question.correct_answer)
                except:
                    correct_answers = [question.correct_answer]
            else:
                correct_answers = question.correct_answer
            
            # Convert to sets for comparison (case-insensitive)
            correct_set = set(str(ans).strip().lower() for ans in correct_answers)
            student_set = set(str(ans).strip().lower() for ans in student_answers)
            
            is_correct = correct_set == student_set
            
            if is_correct:
                marks_obtained = float(marks)
            elif student_set:  # Wrong answer — apply negative marks
                marks_obtained = -float(negative)
            else:  # Unanswered
                marks_obtained = 0

            
            return {
                'is_correct': is_correct,
                'marks_obtained': marks_obtained,
                'feedback': 'Correct!' if is_correct else (
                    f'Wrong answer. Correct answers: {", ".join(correct_answers)}'
                    if student_set else 'Not attempted.'
                )
            }
        except Exception as e:
            return {
                'is_correct': False,
                'marks_obtained': 0,
                'feedback': f'Error parsing answers: {str(e)}'
            }
    
    def _evaluate_true_false(self, question: Question, student_answer: str, marks: float = None, negative: float = None) -> Dict:
        """Evaluate True/False questions with negative marking support"""
        if marks is None: marks = float(question.marks)
        if negative is None: negative = float(question.negative_marks) if question.negative_marks else 0.0

        correct_answer = question.correct_answer.strip().lower()

        student_answer_stripped = student_answer.strip().lower()
        
        # Normalize answers
        correct_bool = correct_answer in ['true', 't', 'yes', 'y', '1']
        student_bool = student_answer_stripped in ['true', 't', 'yes', 'y', '1']
        
        is_correct = correct_bool == student_bool and bool(student_answer_stripped)
        
        if is_correct:
            marks_obtained = float(marks)
        elif student_answer_stripped:  # Wrong answer — apply negative marks
            marks_obtained = -float(negative)
        else:  # Unanswered
            marks_obtained = 0

        
        return {
            'is_correct': is_correct,
            'marks_obtained': marks_obtained,
            'feedback': 'Correct!' if is_correct else (
                f'Wrong answer. Correct answer: {question.correct_answer}'
                if student_answer_stripped else 'Not attempted.'
            )
        }
    
    def _evaluate_numerical(self, question: Question, student_answer: str, marks: float = None, negative: float = None) -> Dict:
        """Evaluate numerical questions with tolerance and negative marking support"""
        if marks is None: marks = float(question.marks)
        if negative is None: negative = float(question.negative_marks) if question.negative_marks else 0.0

        student_answer_stripped = student_answer.strip() if student_answer else ''

        if not student_answer_stripped:
            return {
                'is_correct': False,
                'marks_obtained': 0,
                'feedback': 'Not attempted.'
            }
        try:
            correct_value = float(question.correct_answer)
            student_value = float(student_answer_stripped)
            
            # Default tolerance of 1% or 0.01, whichever is larger
            tolerance = max(abs(correct_value) * 0.01, 0.01)
            
            is_correct = abs(correct_value - student_value) <= tolerance
            
            if is_correct:
                marks_obtained = float(marks)
            else:  # Wrong numerical answer — apply negative marks
                marks_obtained = -float(negative)

            
            return {
                'is_correct': is_correct,
                'marks_obtained': marks_obtained,
                'feedback': 'Correct!' if is_correct else f'Wrong answer. Correct answer: {correct_value} (±{tolerance})'
            }
        except (ValueError, TypeError):
            return {
                'is_correct': False,
                'marks_obtained': 0,
                'feedback': 'Invalid numerical answer'
            }
    
    def _evaluate_fill_blank(self, question: Question, student_answer: str, marks: float = None, negative: float = None) -> Dict:
        """Evaluate fill-in-the-blank questions with negative marking support"""
        if marks is None: marks = float(question.marks)
        if negative is None: negative = float(question.negative_marks) if question.negative_marks else 0.0

        correct_answer = question.correct_answer.strip().lower()

        student_answer_stripped = student_answer.strip().lower()
        
        # Simple string matching (can be enhanced with fuzzy matching)
        is_correct = correct_answer == student_answer_stripped
        
        if is_correct:
            marks_obtained = float(marks)
        elif student_answer_stripped:  # Wrong answer — apply negative marks
            marks_obtained = -float(negative)
        else:  # Unanswered
            marks_obtained = 0
        
        return {
            'is_correct': is_correct,
            'marks_obtained': marks_obtained,
            'feedback': 'Correct!' if is_correct else (
                f'Wrong answer. Correct answer: {question.correct_answer}'
                if student_answer_stripped else 'Not attempted.'
            )
        }
    
    def _create_evaluation_batch(self, batch_type: str, question_evaluations: List[QuestionEvaluation]) -> EvaluationBatch:
        """Create an evaluation batch for manual or AI evaluation"""
        batch = EvaluationBatch.objects.create(
            exam=self.exam,
            batch_type=batch_type,
            questions_count=len(question_evaluations),
            status='pending'
        )
        return batch
    
    def _calculate_final_score(self) -> float:
        """Calculate final score for the attempt"""
        total_marks = 0
        obtained_marks = 0
        
        for q_eval in QuestionEvaluation.objects.filter(attempt=self.attempt):
            total_marks += float(q_eval.max_marks)
            obtained_marks += float(q_eval.marks_obtained)
        
        return obtained_marks if total_marks > 0 else 0
    
    def evaluate_with_ai(self, question_evaluation: QuestionEvaluation) -> Dict:
        """Evaluate subjective questions using Google Gemini AI"""
        if not hasattr(settings, 'GEMINI_API_KEY') or not settings.GEMINI_API_KEY:
            logger.warning("Gemini API key not configured. Skipping AI evaluation.")
            return {
                'success': False,
                'error': "Gemini API key not configured",
                'fallback_to_manual': True
            }

        question = question_evaluation.question
        student_raw_answer = question_evaluation.student_answer
        
        if not student_raw_answer or not student_raw_answer.strip():
            return {
                'success': True,
                'marks_obtained': 0,
                'is_correct': False,
                'feedback': "Question not attempted."
            }

        # Handle structured answers (internal choices, parts)
        processed_answer = student_raw_answer
        selected_choice_context = ""
        
        try:
            # Check if it's a JSON string (our structured format)
            if student_raw_answer.startswith('{'):
                data = json.loads(student_raw_answer)
                if isinstance(data, dict) and 'selected_choice' in data:
                    selected_choice = data.get('selected_choice')
                    text_content = data.get('text', '')
                    parts_data = data.get('parts', {})
                    
                    selected_choice_context = f"The student chose to answer: {selected_choice}.\n"
                    
                    # If there are specific parts answered for this choice
                    if parts_data and selected_choice in parts_data:
                        parts = parts_data[selected_choice]
                        if isinstance(parts, dict):
                            parts_text = []
                            for part_idx, content in parts.items():
                                parts_text.append(f"Answer for Part {int(part_idx) + 1}: {content}")
                            processed_answer = "\n".join(parts_text)
                        else:
                            processed_answer = str(parts)
                    else:
                        processed_answer = text_content
        except Exception as e:
            logger.debug(f"Error parsing structured answer for AI evaluation: {e}")
            # Fall back to raw string if parsing fails
            processed_answer = student_raw_answer

        # Prepare evaluation criteria
        criteria = question.explanation or question.solution or "Evaluate based on general knowledge and accuracy."
        
        # Prepare prompt
        prompt = f"""
        You are an expert academic examiner. Please evaluate the student's response to the following question.
        
        QUESTION:
        {question.question_text}
        
        REFERENCE SOLUTION/CRITERIA:
        {criteria}
        
        STUDENT RESPONSE CONTEXT:
        {selected_choice_context}
        
        STUDENT ACTUAL ANSWER:
        {processed_answer}
        
        MAXIMUM MARKS POSSIBLE: {question.marks}
        
        EVALUATION REQUIREMENTS:
        1. Compare the student's answer against the reference solution/criteria.
        2. Assign objective marks based on quality, accuracy, and completeness.
        3. Provide brief, encouraging, yet critical feedback.
        4. Be fair but strict with technical accuracy.
        
        OUTPUT FORMAT (Return strictly JSON):
        {{
            "marks_obtained": <float between 0 and {question.marks}>,
            "is_correct": <boolean, true if marks >= 50% of max>,
            "feedback": "<short string>",
            "confidence": <float between 0 and 1>
        }}
        """

        try:
            model = genai.GenerativeModel(settings.GEMINI_MODEL)
            
            response = model.generate_content(prompt)
            response_text = response.text.strip()
            
            # Clean JSON from markdown blocks if present
            if response_text.startswith("```"):
                response_text = re.sub(r'^```json\s*|\s*```$', '', response_text, flags=re.MULTILINE)
            
            result = json.loads(response_text)
            
            # Extract and normalize values
            marks = float(result.get('marks_obtained', 0))
            is_correct = bool(result.get('is_correct', False))
            feedback = result.get('feedback', 'Evaluated by AI.')
            confidence = float(result.get('confidence', 0.9))
            
            # Clamp marks
            marks = max(0, min(marks, float(question.marks)))
            
            # Update question evaluation record
            question_evaluation.marks_obtained = Decimal(str(marks))
            question_evaluation.is_correct = is_correct
            question_evaluation.evaluation_status = 'ai_evaluated'
            question_evaluation.evaluated_at = timezone.now()
            question_evaluation.ai_confidence_score = Decimal(str(confidence))
            question_evaluation.ai_feedback = feedback
            question_evaluation.save()
            
            logger.info(f"AI Evaluation successful for attempt {self.attempt.id}, question {question.id}. Score: {marks}")
            
            return {
                'success': True,
                'is_correct': is_correct,
                'marks_obtained': marks,
                'feedback': feedback,
                'confidence': confidence
            }
            
        except Exception as e:
            logger.error(f"AI evaluation failed for question {question.id}: {str(e)}")
            # Fallback to manual evaluation if AI fails
            question_evaluation.evaluation_status = 'pending'
            question_evaluation.evaluation_notes = f"AI Evaluation failed: {str(e)}"
            question_evaluation.save()
            
            return {
                'success': False,
                'error': str(e),
                'fallback_to_manual': True
            }
    
    def get_evaluation_summary(self) -> Dict:
        """Get summary of evaluation progress"""
        progress = EvaluationProgress.objects.get(exam=self.exam)
        question_evaluations = QuestionEvaluation.objects.filter(attempt=self.attempt)
        
        return {
            'total_questions': progress.total_questions,
            'auto_evaluated': progress.auto_evaluated,
            'manually_evaluated': progress.manually_evaluated,
            'ai_evaluated': progress.ai_evaluated,
            'pending_evaluation': progress.pending_evaluation,
            'completion_percentage': progress.completion_percentage,
            'is_fully_evaluated': progress.is_fully_evaluated,
            'question_details': [
                {
                    'question_number': qe.question_number,
                    'question_type': qe.question.question_type,
                    'evaluation_status': qe.evaluation_status,
                    'marks_obtained': float(qe.marks_obtained),
                    'max_marks': float(qe.max_marks),
                    'is_correct': qe.is_correct
                }
                for qe in question_evaluations
            ]
        }
