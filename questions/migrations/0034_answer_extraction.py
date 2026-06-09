import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('exams', '0025_remove_examattempt_shuffle_mapping'),
        ('questions', '0033_fix_examquestion_unique_together_for_sections'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AnswerExtractionJob',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('file_name', models.CharField(max_length=255)),
                ('file_type', models.CharField(max_length=100)),
                ('file_size', models.IntegerField()),
                ('file_path', models.CharField(max_length=500)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('processing', 'Processing'),
                             ('completed', 'Completed'), ('failed', 'Failed')],
                    db_index=True, default='pending', max_length=20)),
                ('progress_percent', models.IntegerField(
                    default=0,
                    validators=[django.core.validators.MinValueValidator(0),
                                django.core.validators.MaxValueValidator(100)])),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(
                    db_constraint=False,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='answer_extraction_jobs',
                    to=settings.AUTH_USER_MODEL)),
                ('exam', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='answer_extraction_jobs',
                    to='exams.exam')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['exam', 'status'], name='questions_a_exam_id_status_idx')],
            },
        ),
        migrations.CreateModel(
            name='ExtractedAnswer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('question_number', models.IntegerField()),
                ('extracted_answer', models.TextField()),
                ('current_answer', models.TextField(
                    blank=True,
                    help_text='Snapshot of Question.correct_answer when the row was matched, for diff display.')),
                ('match_status', models.CharField(
                    choices=[('matched', 'Matched'), ('unmatched', 'Unmatched')],
                    db_index=True, max_length=20)),
                ('skip', models.BooleanField(default=False)),
                ('is_applied', models.BooleanField(db_index=True, default=False)),
                ('apply_error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='extracted_answers',
                    to='questions.answerextractionjob')),
                ('matched_question', models.ForeignKey(
                    blank=True, db_constraint=False, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='extracted_answer_rows',
                    to='questions.question')),
            ],
            options={
                'ordering': ['question_number'],
                'unique_together': {('job', 'question_number')},
            },
        ),
    ]
