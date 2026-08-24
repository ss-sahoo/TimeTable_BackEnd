"""
Celery tasks for timetable generation.
"""
from celery import shared_task
from celery.utils.log import get_task_logger
from django.conf import settings
import json
import copy
import time

# Setup logger for Celery tasks
logger = get_task_logger(__name__)


@shared_task(bind=True, name='timetable.tasks.run_genetic_algorithm_task')
def run_genetic_algorithm_task(
    self,
    timetable_id: str,
    batches_data: dict,
    teachers_data: dict,
    available_slots: dict,
    fixed_slots: dict,
    generations: int = 100,
    population_size: int = 50,
    mutation_rate: float = 0.03,
    elite_size: int = 10,
    tournament_size: int = 5,
    max_retries_per_individual: int = 100,
    clear_existing: bool = True,
    # Fitness function parameters
    hard_constraint_penalty: int = 30000,
    min_weekly_classes_penalty: int = 40,
    min_weekly_classes_penalty_exponent: int = 2,
    max_weekly_classes_penalty: int = 200,
    max_weekly_classes_penalty_exponent: int = 4,
    consu_sub_rep_penalty: int = 40,
    consu_sub_rep_penalty_fector: int = 3,
    sub_variation_per_day_reward_fector: float = 1.2,
    sub_spread_over_week_reward_fector: float = 2.0,
    tenant_db: str = 'default'
):
    """
    Celery task to run genetic algorithm for timetable optimization.
    This runs asynchronously since it can take a long time.
    """
    from accounts.utils import set_current_db, clear_current_db
    from django.conf import settings
    
    if tenant_db and tenant_db != 'default':
        if tenant_db not in settings.DATABASES:
            from accounts.models import Institute
            from accounts.database_utils import register_institute_database
            try:
                institute = Institute.objects.using('default').get(db_name=tenant_db)
                register_institute_database(institute)
            except Institute.DoesNotExist:
                logger.error(f"Tenant database {tenant_db} not found in master database.")
        set_current_db(tenant_db)

    from .genetic_algorithm import (
        generate_random_timetable,
        fitness_score,
        crossover,
        mutate,
        check_constraints
    )
    from .optimization import TeacherDTO
    from .models import Timetable, TimetableEntry, DaySlot
    from accounts.models import User as AccountUser, Batch
    from django.db import transaction
    from collections import defaultdict
    import random
    
    # Track overall start time
    task_start_time = time.time()
    
    logger.info(f"=== GENETIC ALGORITHM STARTED ===")
    logger.info(f"Timetable ID: {timetable_id}")
    logger.info(f"Generations: {generations}, Population: {population_size}")
    
    # Update task state
    self.update_state(state='PROGRESS', meta={
        'current': 0,
        'total': generations,
        'status': 'Initializing...',
        'phase': 'initialization',
        'elapsed_seconds': 0,
        'estimated_remaining_seconds': None,
        'percent_complete': 0
    })
    
    try:
        # Reconstruct teacher and batch objects from serialized data
        teachers_dict = {}
        for code, data in teachers_data.items():
            teacher = TeacherDTO(
                code=data['code'],
                name=data['name'],
                employ_id=data.get('employ_id', ''),
                subject=data.get('subject', ''),
                available_slots=data['avilable_slots']
            )
            teachers_dict[code] = teacher
        
        batches_dict_algo = {}
        for key, data in batches_data.items():
            sub_teachers = {}
            for sub_key, sub_data in data['sub_teachers'].items():
                teacher_code = sub_data['teacher_code']
                sub_teachers[sub_key] = {
                    'subject': sub_data['subject'],
                    'teacher': teachers_dict[teacher_code],
                    'min_class_per_week': sub_data['min_class_per_week'],
                    'max_class_per_week': sub_data['max_class_per_week'],
                    'max_class_per_day': sub_data['max_class_per_day']
                }
            
            # Create batch object using simple class
            batch = type('Batch', (), {
                'batch_code': data['batch_code'],
                'sub_teachers': sub_teachers
            })()
            batches_dict_algo[key] = batch
        
        logger.info(f"Data loaded: {len(teachers_dict)} teachers, {len(batches_dict_algo)} batches")
        
        # Generate initial population
        population_start_time = time.time()
        logger.info(f"Generating initial population of {population_size} timetables...")
        self.update_state(state='PROGRESS', meta={
            'current': 0,
            'total': generations,
            'status': f'Generating initial population ({population_size} individuals)...',
            'phase': 'population_generation',
            'elapsed_seconds': round(time.time() - task_start_time, 1),
            'estimated_remaining_seconds': None,
            'percent_complete': 0
        })
        
        population = []
        for i in range(population_size):
            try:
                timetable = generate_random_timetable(
                    batches=batches_dict_algo,
                    teachers=teachers_dict,
                    avilable_slots=available_slots,
                    fixed_slots=fixed_slots,
                    MAX_RETRIES=max_retries_per_individual
                )
                fitness = fitness_score(
                    timetable, batches_dict_algo, teachers_dict, available_slots, fixed_slots,
                    hard_constraint_penalty=hard_constraint_penalty,
                    min_weekly_classes_penalty=min_weekly_classes_penalty,
                    min_weekly_classes_penalty_exponent=min_weekly_classes_penalty_exponent,
                    max_weekly_classes_penalty=max_weekly_classes_penalty,
                    max_weekly_classes_penalty_exponent=max_weekly_classes_penalty_exponent,
                    consu_sub_rep_penalty=consu_sub_rep_penalty,
                    consu_sub_rep_penalty_fector=consu_sub_rep_penalty_fector,
                    sub_variation_per_day_reward_fector=sub_variation_per_day_reward_fector,
                    sub_spread_over_week_reward_fector=sub_spread_over_week_reward_fector
                )
                population.append((timetable, fitness))
                
                if (i + 1) % 10 == 0:
                    elapsed = time.time() - task_start_time
                    self.update_state(state='PROGRESS', meta={
                        'current': 0,
                        'total': generations,
                        'status': f'Generated {i + 1}/{population_size} initial timetables...',
                        'phase': 'population_generation',
                        'population_progress': i + 1,
                        'population_total': population_size,
                        'elapsed_seconds': round(elapsed, 1),
                        'estimated_remaining_seconds': None,
                        'percent_complete': 0
                    })
            except Exception as e:
                if len(population) >= elite_size:
                    break
                continue
        
        if not population:
            logger.error("Failed to generate any valid timetables for initial population")
            return {
                'success': False,
                'error': 'Failed to generate any valid timetables for initial population'
            }
        
        population_time = time.time() - population_start_time
        logger.info(f"Initial population created: {len(population)} timetables in {population_time:.1f}s")
        logger.info(f"Starting evolution for {generations} generations...")
        
        # Track generation times for estimation
        generation_times = []
        evolution_start_time = time.time()
        
        # Run genetic algorithm
        for gen in range(generations):
            gen_start_time = time.time()
            
            # Sort by fitness (descending - higher is better)
            population.sort(key=lambda x: x[1], reverse=True)
            
            # Keep elite individuals
            new_population = population[:elite_size]
            
            # Generate new individuals
            while len(new_population) < population_size:
                # Tournament selection
                parent1 = random.choice(population[:tournament_size])[0]
                parent2 = random.choice(population[:tournament_size])[0]
                
                # Crossover
                child = crossover(parent1, parent2, fixed_slots)
                
                # Mutation
                mutate(child, batches_dict_algo, teachers_dict, mutation_rate)
                
                # Calculate fitness
                score = fitness_score(
                    child, batches_dict_algo, teachers_dict, available_slots, fixed_slots,
                    hard_constraint_penalty=hard_constraint_penalty,
                    min_weekly_classes_penalty=min_weekly_classes_penalty,
                    min_weekly_classes_penalty_exponent=min_weekly_classes_penalty_exponent,
                    max_weekly_classes_penalty=max_weekly_classes_penalty,
                    max_weekly_classes_penalty_exponent=max_weekly_classes_penalty_exponent,
                    consu_sub_rep_penalty=consu_sub_rep_penalty,
                    consu_sub_rep_penalty_fector=consu_sub_rep_penalty_fector,
                    sub_variation_per_day_reward_fector=sub_variation_per_day_reward_fector,
                    sub_spread_over_week_reward_fector=sub_spread_over_week_reward_fector
                )
                new_population.append((child, score))
            
            population = new_population
            
            # Track generation time
            gen_time = time.time() - gen_start_time
            generation_times.append(gen_time)
            
            # Calculate time estimates
            elapsed_total = time.time() - task_start_time
            elapsed_evolution = time.time() - evolution_start_time
            
            # Use average of last 10 generations for better estimation
            recent_times = generation_times[-10:] if len(generation_times) >= 10 else generation_times
            avg_gen_time = sum(recent_times) / len(recent_times)
            
            remaining_generations = generations - (gen + 1)
            estimated_remaining = remaining_generations * avg_gen_time
            
            # Calculate percent complete (population generation is ~10%, evolution is ~90%)
            percent_complete = round(10 + (90 * (gen + 1) / generations), 1)
            
            # Update progress and log every 10 generations
            best_fitness = population[0][1]
            if (gen + 1) % 10 == 0:
                logger.info(f"Generation {gen + 1}/{generations}: Best Fitness = {best_fitness:.2f}, "
                           f"Elapsed: {elapsed_total:.1f}s, ETA: {estimated_remaining:.1f}s")
            
            self.update_state(state='PROGRESS', meta={
                'current': gen + 1,
                'total': generations,
                'status': f'Generation {gen + 1}/{generations}',
                'phase': 'evolution',
                'best_fitness': best_fitness,
                'elapsed_seconds': round(elapsed_total, 1),
                'estimated_remaining_seconds': round(estimated_remaining, 1),
                'percent_complete': percent_complete,
                'avg_generation_time': round(avg_gen_time, 3),
                'current_generation_time': round(gen_time, 3)
            })
        
        # Get best timetable
        population.sort(key=lambda x: x[1], reverse=True)
        best_timetable, best_fitness = population[0]
        
        evolution_time = time.time() - evolution_start_time
        logger.info(f"=== EVOLUTION COMPLETE ===")
        logger.info(f"Best Fitness: {best_fitness:.2f}")
        logger.info(f"Evolution time: {evolution_time:.1f}s")
        
        # Check final constraints
        final_violations = check_constraints(
            best_timetable, batches_dict_algo, teachers_dict, available_slots, fixed_slots
        )
        logger.info(f"Final Violations: {final_violations}")
        
        # Save to database
        save_start_time = time.time()
        logger.info("Saving timetable to database...")
        self.update_state(state='PROGRESS', meta={
            'current': generations,
            'total': generations,
            'status': 'Saving timetable to database...',
            'phase': 'saving',
            'best_fitness': best_fitness,
            'elapsed_seconds': round(time.time() - task_start_time, 1),
            'estimated_remaining_seconds': 5,
            'percent_complete': 95
        })
        
        entries_created = save_timetable_to_db(
            timetable_id, best_timetable, teachers_dict, batches_dict_algo, clear_existing
        )
        
        save_time = time.time() - save_start_time
        total_time = time.time() - task_start_time
        
        logger.info(f"=== GENETIC ALGORITHM COMPLETED ===")
        logger.info(f"Entries created: {entries_created}")
        logger.info(f"Total time: {total_time:.1f}s (Population: {population_time:.1f}s, "
                   f"Evolution: {evolution_time:.1f}s, Save: {save_time:.1f}s)")
        
        return {
            'success': True,
            'entries_created': entries_created,
            'best_fitness': best_fitness,
            'generations': generations,
            'population_size': population_size,
            'final_violations': final_violations,
            'algorithm_used': 'genetic_algorithm',
            'timing': {
                'total_seconds': round(total_time, 1),
                'population_generation_seconds': round(population_time, 1),
                'evolution_seconds': round(evolution_time, 1),
                'save_seconds': round(save_time, 1),
                'avg_generation_time': round(sum(generation_times) / len(generation_times), 3) if generation_times else 0
            }
        }
        
    except Exception as e:
        import traceback
        total_time = time.time() - task_start_time
        logger.error(f"GENETIC ALGORITHM FAILED after {total_time:.1f}s: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc(),
            'elapsed_seconds': round(total_time, 1)
        }
    finally:
        from accounts.utils import clear_current_db
        if tenant_db and tenant_db != 'default':
            clear_current_db()


def save_timetable_to_db(timetable_id, generated_timetable, teachers_dict, batches_dict_algo, clear_existing):
    """Save generated timetable to database."""
    from .models import Timetable, TimetableEntry, DaySlot
    from accounts.models import User as AccountUser, Batch
    from django.db import transaction
    
    timetable = Timetable.objects.get(id=timetable_id)
    
    from .optimization import DAY_MAP_SHORT
    
    # Create mapping: slot_code -> DaySlot object
    slot_code_to_dayslot = {}
    for day_slot in timetable.day_slots.all():
        if day_slot.day_index:
            day_key = f"d{day_slot.day_index}"
        else:
            day_key = DAY_MAP_SHORT.get(day_slot.day, "")
        
        code = day_slot.slot_code or f"{day_key}{day_slot.slot_number}"
        slot_code_to_dayslot[code] = day_slot
    
    # Create mapping: teacher_code -> User object
    teacher_code_to_user = {}
    for teacher_code in teachers_dict.keys():
        user = AccountUser.objects.filter(
            teacher_code=teacher_code
        ).first() or AccountUser.objects.filter(
            username=teacher_code
        ).first()
        if user:
            teacher_code_to_user[teacher_code] = user
    
    # Create mapping: batch_code -> Batch object
    batch_code_to_batch = {}
    for batch_code in batches_dict_algo.keys():
        batch = Batch.objects.filter(code=batch_code).first()
        if batch:
            batch_code_to_batch[batch_code] = batch
    
    entries_created = 0
    
    with transaction.atomic():
        if clear_existing:
            TimetableEntry.objects.filter(day_slot__timetable=timetable).delete()
        
        for day_key, slots in generated_timetable.items():
            for slot_code, batch_assignments in slots.items():
                day_slot = slot_code_to_dayslot.get(slot_code)
                if not day_slot:
                    continue
                
                for batch_code, (subject, teacher_code) in batch_assignments.items():
                    batch = batch_code_to_batch.get(batch_code)
                    if not batch:
                        continue
                    
                    teacher = teacher_code_to_user.get(teacher_code)
                    
                    entry, created = TimetableEntry.objects.get_or_create(
                        day_slot=day_slot,
                        batch=batch,
                        defaults={
                            "subject": subject,
                            "teacher": teacher,
                        }
                    )
                    if not created:
                        entry.subject = subject
                        entry.teacher = teacher
                        entry.save()
                    
                    entries_created += 1
    
    return entries_created
