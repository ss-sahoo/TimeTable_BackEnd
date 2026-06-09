"""
Celery tasks for question extraction
"""
import logging
from uuid import UUID
from celery import shared_task
from django.core.cache import cache

from questions.services.extraction_pipeline import ExtractionPipeline
from questions.services.extraction_pipeline_v2 import ExtractionPipelineV2
from questions.models import ExtractionJob

logger = logging.getLogger('extraction')


# ──────────────────────────────────────────────
# Agentic Pipeline Task (Mathpix + Gemini + Micro-Batching)
# ──────────────────────────────────────────────

@shared_task(
    bind=True,
    name='questions.extract_questions_v3',
    max_retries=3,
    default_retry_delay=60,
)
def extract_questions_v3_task(self, job_id: str, subjects: list = None):
    """
    High-fidelity extraction using AgentExtractionService.
    Replaces the old V3 pipeline with the new Agentic logic.
    """
    from django.conf import settings
    from questions.models import ExtractionJob, ExtractedQuestion
    from questions.services.agent_extraction_service import AgentExtractionService
    
    job_uuid = UUID(job_id)
    job = ExtractionJob.objects.get(id=job_uuid)
    job.status = 'processing'
    job.progress_percent = 10
    job.save()
    
    try:
        service = AgentExtractionService(
            gemini_key=getattr(settings, 'GEMINI_API_KEY', ''),
            mathpix_id=getattr(settings, 'MATHPIX_APP_ID', ''),
            mathpix_key=getattr(settings, 'MATHPIX_APP_KEY', '')
        )
        
        # Run the full agentic pipeline
        # Passing subjects_to_process=None triggers auto-detection
        
        # Try to use pre-analysis or perform on-the-fly separation
        separated_content = None
        # 1. Use passed subjects if available
        if subjects:
             logger.info(f"Task received specific subjects to extract: {subjects}")
             # Preserving the passed subjects
        
        # 2. Use existing PreAnalysisJob if available
        elif job.pre_analysis_job and job.pre_analysis_job.subject_separated_content:
            logger.info(f"Using existing pre-analysis for job {job.id}")
            # Ensure format is Dict[subject, content_str] or compatible
            raw_content = job.pre_analysis_job.subject_separated_content
            separated_content = {}
            
            # Normalize content format
            for subj, data in raw_content.items():
                if isinstance(data, dict):
                    separated_content[subj] = data.get('content', '')
                else:
                    separated_content[subj] = str(data)
                    
            subjects = list(separated_content.keys())
            
        # 3. If no pre-analysis and no subjects passed, perform on-the-fly separation
        else:
            logger.info("No pre-analysis found. Performing on-the-fly separation...")
            try:
                # We need text for analyzer. Use Mathpix if available, else local parser.
                markdown_text = ""
                if getattr(settings, 'MATHPIX_APP_ID', ''):
                    try:
                        from questions.services.agent_extraction_service import MathpixOCR
                        mathpix = MathpixOCR(getattr(settings, 'MATHPIX_APP_ID', ''), getattr(settings, 'MATHPIX_APP_KEY', ''))
                        markdown_text = mathpix.process_pdf(job.file_path)
                    except Exception as e:
                        logger.warning(f"Mathpix failed during on-the-fly separation: {e}")
                
                if not markdown_text:
                    logger.info("Using local parser for on-the-fly separation text extraction.")
                    from questions.services.file_parser import FileParserService
                    parser = FileParserService()
                    markdown_text = parser.parse_file(job.file_path, 'application/pdf')
                
                # Get pattern subjects to guide separation
                pattern_subjects = []
                if job.pattern:
                    pattern_subjects = list(job.pattern.sections.values_list('subject', flat=True).distinct())
                
                # Run analyzer
                from questions.services.document_pre_analyzer import DocumentPreAnalyzer
                analyzer = DocumentPreAnalyzer(api_key=getattr(settings, 'GEMINI_API_KEY', None))
                result = analyzer.analyze_document(markdown_text, pattern_subjects)
                
                if result.is_valid and result.subject_separated_content:
                    # Normalize content format
                    separated_content = {}
                    for subj, data in result.subject_separated_content.items():
                        if isinstance(data, dict):
                            separated_content[subj] = data.get('content', '')
                        else:
                            separated_content[subj] = str(data)
                            
                    subjects = list(separated_content.keys())
                    logger.info(f"On-the-fly separation successful: {subjects}")
                else:
                    logger.warning("On-the-fly separation returned no content. Falling back to simple detection.")
                    # Fallback allows AgentExtractionService to do its own simple detection
                    subjects = service.detect_subjects(markdown_text)
                    
            except Exception as e:
                logger.error(f"On-the-fly separation failed: {e}")
                # Fallback handled below
        
        # 4. Final Fallback if still no subjects
        if not subjects:
             # This means both pre-analysis and on-the-fly failed or weren't run?
             # For on-the-fly failure, we might not have markdown_text in scope if it failed early.
             # We let AgentExtractionService handle detection internally if we pass empty list.
             # But we need subjects for the loop below.
             
             # Re-instantiate mathpix to be safe or use service's detection
             from questions.services.agent_extraction_service import MathpixOCR
             mathpix = MathpixOCR(getattr(settings, 'MATHPIX_APP_ID', ''), getattr(settings, 'MATHPIX_APP_KEY', ''))
             markdown_text = mathpix.process_pdf(job.file_path)
             subjects = service.detect_subjects(markdown_text)
        
        total_subjects = len(subjects) if subjects else 1

        # Get exam_mode from pattern for post-processing (e.g. image conversion)
        exam_mode = getattr(job.pattern, 'exam_mode', None) if job.pattern else None

        raw_questions = []
        for idx, subj in enumerate(subjects):
            # Update progress per subject
            job.progress_percent = int((idx / total_subjects) * 90) + 10
            job.save()

            # Pass separated_content and exam_mode
            subject_qs = service.run_full_pipeline(
                job.file_path,
                subjects_to_process=[subj],
                separated_content=separated_content,
                exam_mode=exam_mode
            )
            raw_questions.extend(subject_qs)
        
        # Save results...
        for q_data in raw_questions:
            ExtractedQuestion.objects.create(
                job=job,
                question_text=q_data.get('question_text') or q_data.get('question') or '',
                question_type=q_data.get('question_type') or 'single_mcq',
                options=q_data.get('options') or [],
                correct_answer=q_data.get('correct_answer') or q_data.get('answer') or '',
                solution=q_data.get('solution') or q_data.get('explanation') or '',
                suggested_subject=q_data.get('subject') or '',
                structure=q_data.get('subparts') or q_data.get('structure') or {},
                images_data=q_data.get('images_data') or {},
                confidence_score=q_data.get('confidence_score', 0.95)
            )
            
        job.status = 'completed'
        job.progress_percent = 100
        job.questions_extracted = len(raw_questions)
        job.save()
        
        return {'success': True, 'count': len(raw_questions)}
        
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise e


@shared_task(
    bind=True,
    name='questions.extract_questions',
    max_retries=3,
    default_retry_delay=60,  # 1 minute
    time_limit=1800,  # 30 minutes
    soft_time_limit=1700,  # 28 minutes (soft limit before hard limit)
)
def extract_questions_task(self, job_id: str, use_v2: bool = True):
    """
    Celery task to extract questions from uploaded file
    
    Args:
        job_id: UUID string of the extraction job
        use_v2: Whether to use V2 enhanced pipeline (default: True)
        
    Returns:
        Dictionary with extraction results
    """
    job_uuid = UUID(job_id)
    
    try:
        logger.info(f"Starting extraction task for job {job_id} (V2: {use_v2})")
        
        # Create pipeline and process file
        if use_v2:
            pipeline = ExtractionPipelineV2()
            result = pipeline.process_file(job_uuid)
        else:
            pipeline = ExtractionPipeline()
            pipeline.process_file(job_uuid)
            result = None
        
        # Get final job status
        job = ExtractionJob.objects.get(id=job_uuid)
        
        task_result = {
            'success': True,
            'job_id': str(job.id),
            'status': job.status,
            'questions_extracted': job.questions_extracted,
            'questions_failed': job.questions_failed,
            'expected_count': job.total_questions_found,
            'completeness': (job.questions_extracted / job.total_questions_found * 100) if job.total_questions_found > 0 else 0,
        }
        
        if result:
            task_result.update({
                'type_distribution': result.get('type_distribution', {}),
                'has_latex': result.get('has_latex', False),
                'processing_time': result.get('processing_time', 0),
            })
        
        logger.info(f"Extraction task completed for job {job_id}: {task_result}")
        return task_result
        
    except ExtractionJob.DoesNotExist:
        error_msg = f"Extraction job {job_id} not found"
        logger.error(error_msg)
        return {
            'success': False,
            'error': error_msg
        }
    
    except Exception as exc:
        logger.error(f"Extraction task failed for job {job_id}: {str(exc)}", exc_info=True)
        
        # Retry with exponential backoff
        try:
            job = ExtractionJob.objects.get(id=job_uuid)
            
            # Check if we should retry
            if job.retry_count < 3:
                # Calculate exponential backoff: 2^retry_count minutes
                countdown = (2 ** job.retry_count) * 60
                
                logger.info(
                    f"Retrying extraction job {job_id} in {countdown} seconds "
                    f"(attempt {job.retry_count + 1}/3)"
                )
                
                # Update retry count
                job.retry_count += 1
                job.save(update_fields=['retry_count'])
                
                # Retry the task
                raise self.retry(exc=exc, countdown=countdown)
            else:
                # Max retries reached
                logger.error(f"Max retries reached for job {job_id}")
                job.mark_failed(f"Max retries reached: {str(exc)}")
                
        except ExtractionJob.DoesNotExist:
            pass
        
        return {
            'success': False,
            'error': str(exc)
        }


@shared_task(
    bind=True,
    name='questions.extract_questions_v2',
    max_retries=3,
    default_retry_delay=60,
    time_limit=2400,  # 40 minutes for large files
    soft_time_limit=2300,
)
def extract_questions_v2_task(self, job_id: str):
    """
    V2 extraction task with enhanced features
    
    Features:
    - Complete extraction (100% of questions)
    - Accurate type classification
    - LaTeX preservation
    - Large file support (500+ questions)
    """
    return extract_questions_task(self, job_id, use_v2=True)


@shared_task(name='questions.cleanup_old_extraction_jobs')
def cleanup_old_extraction_jobs():
    """
    Periodic task to clean up old extraction jobs and files
    
    This should be run daily via Celery Beat
    """
    from datetime import timedelta
    from django.utils import timezone
    import os
    
    try:
        # Delete extraction jobs older than 30 days
        cutoff_date = timezone.now() - timedelta(days=30)
        
        old_jobs = ExtractionJob.objects.filter(
            created_at__lt=cutoff_date,
            status__in=['completed', 'failed']
        )
        
        deleted_count = 0
        for job in old_jobs:
            # Delete associated file if it exists
            if job.file_path and os.path.exists(job.file_path):
                try:
                    os.remove(job.file_path)
                    logger.info(f"Deleted file: {job.file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete file {job.file_path}: {e}")
            
            # Delete job and related extracted questions (cascade)
            job.delete()
            deleted_count += 1
        
        logger.info(f"Cleaned up {deleted_count} old extraction jobs")
        
        return {
            'success': True,
            'deleted_count': deleted_count
        }
        
    except Exception as e:
        logger.error(f"Cleanup task failed: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }


@shared_task(
    bind=True,
    name='questions.extract_answers',
    max_retries=3,
    default_retry_delay=60,
    time_limit=900,
    soft_time_limit=840,
)
def extract_answers_task(self, job_id: str):
    """Run OCR + Gemini on an answer-key file and stage the results for review.

    Matches each extracted (question_number, answer) pair to a Question in the job's
    exam by question_number. Unmatched rows are still saved so the reviewer can see them.
    """
    from questions.models import AnswerExtractionJob, ExtractedAnswer, Question
    from questions.services.answer_key_extractor import extract_answer_key

    job_uuid = UUID(job_id)
    job = AnswerExtractionJob.objects.get(id=job_uuid)
    job.status = 'processing'
    job.progress_percent = 10
    job.save(update_fields=['status', 'progress_percent'])

    try:
        entries = extract_answer_key(job.file_path, job.file_type)
        job.progress_percent = 70
        job.save(update_fields=['progress_percent'])

        questions_by_number = {
            q.question_number: q
            for q in Question.objects.filter(exam=job.exam, is_active=True)
            if q.question_number is not None
        }

        seen_numbers = set()
        for entry in entries:
            q_no = entry['question_number']
            if q_no in seen_numbers:
                # Answer keys occasionally repeat a row (e.g. continuation pages); keep the first.
                continue
            seen_numbers.add(q_no)

            question = questions_by_number.get(q_no)
            ExtractedAnswer.objects.update_or_create(
                job=job,
                question_number=q_no,
                defaults={
                    'extracted_answer': entry['answer'],
                    'matched_question': question,
                    'current_answer': question.correct_answer if question else '',
                    'match_status': 'matched' if question else 'unmatched',
                },
            )

        job.mark_completed()
        return {'success': True, 'count': len(seen_numbers)}

    except Exception as exc:
        logger.error(f"Answer extraction failed for job {job_id}: {exc}", exc_info=True)
        job.mark_failed(str(exc))
        raise


@shared_task(name='questions.update_extraction_metrics')
def update_extraction_metrics():
    """
    Periodic task to update extraction metrics and statistics
    
    This should be run hourly via Celery Beat
    """
    try:
        from django.db.models import Count, Avg, Sum
        from datetime import timedelta
        from django.utils import timezone
        
        # Calculate metrics for last 24 hours
        since = timezone.now() - timedelta(hours=24)
        
        metrics = ExtractionJob.objects.filter(
            created_at__gte=since
        ).aggregate(
            total_jobs=Count('id'),
            completed_jobs=Count('id', filter=models.Q(status='completed')),
            failed_jobs=Count('id', filter=models.Q(status='failed')),
            avg_processing_time=Avg('processing_time_seconds'),
            total_questions_extracted=Sum('questions_extracted'),
            total_tokens_used=Sum('tokens_used'),
        )
        
        # Store in cache for dashboard
        cache.set('extraction_metrics_24h', metrics, timeout=3600)  # 1 hour
        
        logger.info(f"Updated extraction metrics: {metrics}")
        
        return {
            'success': True,
            'metrics': metrics
        }
        
    except Exception as e:
        logger.error(f"Metrics update task failed: {e}", exc_info=True)
        return {
            'success': False,
            'error': str(e)
        }
