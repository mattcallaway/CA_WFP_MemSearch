"""
Chapter engine benchmark management command.
Creates realistic synthetic data in an isolated test database and benchmarks
the chapter evaluation engine across two scales.

Usage:
    python manage.py benchmark_chapter_engine --scale 1000 10000 --output path
"""
import json
import math
import os
import random
import time
import tracemalloc
from collections import defaultdict
from datetime import date

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext, setup_databases, teardown_databases
from django.utils import timezone

from roster.models import (
    ContributorEntity, ContributionCluster, Location, LocationGeographyResolution,
    County, GeographicPlace, PostalArea, Chapter, ChapterRuleSet, ChapterRule,
    ChapterEvaluationRun, GeographyDataset, GeographyResolutionRun,
    ChapterAssignment, ChapterRuleMatch
)
from roster.services.chapter_engine import run_chapter_evaluation


class Command(BaseCommand):
    help = "Benchmark the Chapter Engine evaluation in an isolated test environment."

    def add_arguments(self, parser):
        parser.add_argument("--scale", nargs="+", type=int, default=[1000, 10000])
        parser.add_argument("--output", type=str, default=".")

    def handle(self, *args, **options):
        scales = options["scale"]
        out_dir = options["output"]
        os.makedirs(out_dir, exist_ok=True)

        is_reliability_env = bool(os.environ.get("WFP_RELIABILITY_DB_PATH"))
        active_db = str(connection.settings_dict.get("NAME", ""))
        self.stdout.write(f"Active database: {active_db}")

        if not is_reliability_env:
            self.stdout.write("Setting up isolated test database...")
            old_config = setup_databases(0, False, aliases=["default"])
        else:
            old_config = None

        try:
            all_results = []
            for scale in scales:
                results_data = self._run_benchmark(scale)
                all_results.append(results_data)

                fname = f"chapter_benchmark_{scale}.json"
                path = os.path.join(out_dir, fname)
                with open(path, "w") as f:
                    json.dump(results_data, f, indent=2)
                self.stdout.write(f"  Written: {path}")

                # Clean up between scales
                call_command("flush", "--noinput", verbosity=0)

            # Write summary artifact chapter_benchmark.json
            summary = {
                "benchmark_method": "management_command:benchmark_chapter_engine",
                "entry_function": "roster.services.chapter_engine.run_chapter_evaluation",
                "database_backend": "sqlite3:isolated-file-backed",
                "query_capture_method": "CaptureQueriesContext",
                "chapters_tested": len(all_results[0]["results"]) if all_results else 0,
                "scales": scales,
                "results": all_results,
                "overall_pass": all(r["overall_pass"] for r in all_results),
            }
            summary_path = os.path.join(out_dir, "chapter_benchmark.json")
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2)
            self.stdout.write(f"  Summary: {summary_path}")
        finally:
            if old_config:
                teardown_databases(old_config, 0)
                self.stdout.write("Test database destroyed.")

    def _run_benchmark(self, scale):
        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"CHAPTER BENCHMARK: {scale} entities")
        self.stdout.write(f"{'='*60}")

        random.seed(42)

        # Create geography dataset
        dataset = GeographyDataset.objects.create(
            name="Benchmark Dataset",
            dataset_type="COUNTY",
            status="ACTIVE",
        )

        # Create Geographies
        county_a = County.objects.create(state_code="CA", normalized_name="county a", display_name="County A")
        county_b = County.objects.create(state_code="CA", normalized_name="county b", display_name="County B")
        county_z = County.objects.create(state_code="CA", normalized_name="county z", display_name="County Z")

        place_c = GeographicPlace.objects.create(state_code="CA", canonical_name="Place C", normalized_name="place c", general_category="CITY", is_active=True)
        place_z = GeographicPlace.objects.create(state_code="CA", canonical_name="Place Z", normalized_name="place z", general_category="CITY", is_active=True)

        postal_d = PostalArea.objects.create(postal_code="9000D", postal_area_type="STANDARD", is_active=True)
        postal_z = PostalArea.objects.create(postal_code="9000Z", postal_area_type="STANDARD", is_active=True)

        # Build geography resolution run
        res_run = GeographyResolutionRun.objects.create(
            trigger_type="DATASET_ACTIVATION",
            dataset=dataset,
            status="COMPLETED",
            actor="benchmark_runner",
        )

        self.stdout.write(f"Creating {scale} entities with locations...")
        
        entities_to_create = []
        clusters_to_create = []
        locs_to_create = []
        resolutions_to_create = []
        
        composition = defaultdict(int)

        def stage_entity(i, p_idx):
            is_org = (p_idx == 2)
            is_unverified = (p_idx == 1)
            
            ent_type = "ORGANIZATION" if is_org else "INDIVIDUAL"
            is_ver = not is_unverified
            
            entity = ContributorEntity(
                display_name=f"BENCHMARK_ENTITY_{i}",
                entity_type=ent_type,
                verification_status="VERIFIED" if is_ver else "UNVERIFIED",
                verification_method="ADMIN_REVIEW" if is_ver else "NONE",
                verified_by="bench_admin" if is_ver else None,
                verified_at=timezone.now() if is_ver else None,
                is_verified=is_ver,
            )
            entities_to_create.append(entity)
            
            if is_org:
                composition['organization'] += 1
            elif is_unverified:
                composition['unverified'] += 1
            else:
                composition['verified'] += 1
                
            return entity

        for i in range(scale):
            stage_entity(i, i % 8)

        batch_size = 500
        ContributorEntity.objects.bulk_create(entities_to_create, batch_size=batch_size)
        
        entities = list(ContributorEntity.objects.all().order_by('-id')[:scale])
        entities.reverse()
        
        for i, entity in enumerate(entities):
            cluster = ContributionCluster(
                contributor_entity=entity,
                confidence_level="HIGH",
                zip_code="90000",
            )
            clusters_to_create.append(cluster)
        
        ContributionCluster.objects.bulk_create(clusters_to_create, batch_size=batch_size)
        clusters = list(ContributionCluster.objects.all().order_by('-id')[:scale])
        clusters.reverse()
        
        for i, (entity, cluster) in enumerate(zip(entities, clusters)):
            p_idx = i % 8
            
            loc_defs = []
            if p_idx == 0 or p_idx == 1:
                loc_defs.append((county_a, place_c, postal_z))
            elif p_idx == 2:
                loc_defs.append((county_a, place_z, postal_z))
            elif p_idx == 3:
                loc_defs.append((county_a, place_z, postal_z))
                loc_defs.append((county_b, place_z, postal_z))
                composition['ambiguous_location'] += 1
            elif p_idx == 4:
                loc_defs.append((county_z, place_z, postal_d))
            elif p_idx == 5:
                loc_defs.append((county_b, place_z, postal_z))
            elif p_idx == 6:
                loc_defs.append((county_z, place_z, postal_z))
            elif p_idx == 7:
                loc_defs.append((county_a, place_c, postal_d))
                
            for idx, (c, p, z) in enumerate(loc_defs):
                loc = Location(
                    contributor_profile=cluster,
                    street_address=f"{i} Main St",
                    city=p.canonical_name,
                    state="CA",
                    zip=z.postal_code,
                    status="CURRENT",
                )
                loc.temp_idx = idx
                loc.temp_c = c
                loc.temp_p = p
                loc.temp_z = z
                locs_to_create.append(loc)
                
        Location.objects.bulk_create(locs_to_create, batch_size=batch_size)
        locs_len = len(locs_to_create)
        locs = list(Location.objects.all().order_by('-id')[:locs_len])
        locs.reverse()
        
        for loc, loc_def in zip(locs, locs_to_create):
            res = LocationGeographyResolution(
                location=loc,
                resolution_run=res_run,
                observed_city=loc_def.temp_p.canonical_name,
                observed_state="CA",
                observed_zip=loc_def.temp_z.postal_code,
                matched_canonical_county=loc_def.temp_c,
                matched_canonical_place=loc_def.temp_p,
                matched_postal_area=loc_def.temp_z,
                match_method="EXACT_PLACE_ZIP_MATCH",
                confidence="HIGH",
                status="CURRENT",
            )
            resolutions_to_create.append(res)
            
        LocationGeographyResolution.objects.bulk_create(resolutions_to_create, batch_size=batch_size)

        self.stdout.write("Setting up chapters and rulesets...")
        
        chapter1 = Chapter.objects.create(name="County Chapter", slug="county-chap", created_by="bench")
        ruleset1 = ChapterRuleSet.objects.create(chapter=chapter1, version=1, status="ACTIVE", created_by="bench")
        ChapterRule.objects.create(rule_set=ruleset1, effect="INCLUDE", target_type="COUNTY", county=county_a, display_order=10)
        ChapterRule.objects.create(rule_set=ruleset1, effect="EXCLUDE", target_type="COUNTY", county=county_b, display_order=20)

        chapter2 = Chapter.objects.create(name="Place Chapter", slug="place-chap", created_by="bench")
        ruleset2 = ChapterRuleSet.objects.create(chapter=chapter2, version=1, status="ACTIVE", created_by="bench")
        ChapterRule.objects.create(rule_set=ruleset2, effect="INCLUDE", target_type="PLACE", place=place_c, display_order=10)

        chapter3 = Chapter.objects.create(name="Postal Chapter", slug="postal-chap", created_by="bench")
        ruleset3 = ChapterRuleSet.objects.create(chapter=chapter3, version=1, status="ACTIVE", created_by="bench")
        ChapterRule.objects.create(rule_set=ruleset3, effect="INCLUDE", target_type="POSTAL_AREA", postal_area=postal_d, display_order=10)

        chapter_ruleset_pairs = [
            (chapter1, ruleset1),
            (chapter2, ruleset2),
            (chapter3, ruleset3),
        ]
        
        eval_runs = []
        for chapter, ruleset in chapter_ruleset_pairs:
            eval_runs.append(ChapterEvaluationRun.objects.create(
                chapter=chapter,
                rule_set=ruleset,
                run_mode="APPLY",
                trigger_type="MANUAL_FULL_EVALUATION",
                geography_dataset_snapshot={"dataset_id": dataset.id},
                resolver_version="1.0",
                evaluation_engine_version="1.0",
                membership_snapshot_date=date.today(),
                scope="FULL_ROSTER",
                actor="benchmark_runner",
                status="PENDING",
            ))

        self.stdout.write("Running evaluations...")
        tracemalloc.start()
        start_time = time.time()

        with CaptureQueriesContext(connection) as ctx:
            for eval_run in eval_runs:
                run_chapter_evaluation(eval_run.id)

        duration = time.time() - start_time
        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        actual_queries = len(ctx)
        
        expected_overlaps = sum(1 for i in range(scale) if i % 8 in (0, 1, 7))
        
        entity_assignment_counts = defaultdict(int)
        for assignment in ChapterAssignment.objects.filter(
            evaluation_run__in=eval_runs,
            assignment_status__in=['INCLUDED', 'PROVISIONALLY_INCLUDED']
        ):
            entity_assignment_counts[assignment.contributor_entity_id] += 1
            
        actual_overlaps = sum(1 for v in entity_assignment_counts.values() if v > 1)
        
        expected_ambiguities = sum(1 for i in range(scale) if i % 8 == 3)
        actual_ambiguities = sum(r.ambiguous_count for r in eval_runs)
        
        expected_assignments = (
            sum(1 for i in range(scale) if i % 8 == 0) * 2 +
            sum(1 for i in range(scale) if i % 8 == 1) * 2 +
            sum(1 for i in range(scale) if i % 8 == 4) * 1 +
            sum(1 for i in range(scale) if i % 8 == 7) * 3
        )
        actual_assignments = sum(v for v in entity_assignment_counts.values())
        
        expected_decisive_matches = (
            sum(1 for i in range(scale) if i % 8 == 0) * 2 +
            sum(1 for i in range(scale) if i % 8 == 1) * 2 +
            sum(1 for i in range(scale) if i % 8 == 4) * 1 +
            sum(1 for i in range(scale) if i % 8 == 5) * 1 +
            sum(1 for i in range(scale) if i % 8 == 7) * 3
        )
        actual_decisive_matches = sum(
            1 for _ in ChapterRuleMatch.objects.filter(evaluation_result__evaluation_run__in=eval_runs)
        )

        chunk_size = 500
        # Per entity chunk per chapter: 5 reads (entity slice, locations,
        # resolutions, overrides, assessments) + 4 bulk writes (selections,
        # results, rule_matches, assignments) = 9 queries
        q_per_chunk = 9
        # Fixed per-chapter: run.get + status update + ruleset fetch +
        # entity count + chapter FK + completion save + audit = ~5
        q_per_chapter = 5
        fixed_cost = 20  # savepoints, promotion, overlap computation
        chapter_count = len(eval_runs)
        n_chunks = math.ceil(scale / chunk_size)
        
        # Each chapter independently iterates through all entity chunks
        formula_ceiling = fixed_cost + n_chunks * q_per_chunk * chapter_count + chapter_count * q_per_chapter
        overall_pass = actual_queries <= formula_ceiling

        self.stdout.write(f"  Queries: {actual_queries} (ceiling: {formula_ceiling})")
        self.stdout.write(f"  Duration: {duration:.3f}s")
        self.stdout.write(f"  Result: {'PASS' if overall_pass else 'FAIL'}")

        results_data = {
            "scale": scale,
            "overall_pass": overall_pass,
            "formula_ceiling": formula_ceiling,
            "actual_queries": actual_queries,
            "chapters_tested": chapter_count,
            "duration_seconds": round(duration, 3),
            "peak_memory_bytes": peak_mem,
            "peak_memory_mb": round(peak_mem / (1024 * 1024), 2),
            "entity_composition": dict(composition),
            "overlaps": {
                "expected": expected_overlaps,
                "actual": actual_overlaps
            },
            "ambiguities": {
                "expected": expected_ambiguities,
                "actual": actual_ambiguities
            },
            "assignments": {
                "expected": expected_assignments,
                "actual": actual_assignments
            },
            "decisive_matches": {
                "expected": expected_decisive_matches,
                "actual": actual_decisive_matches
            },
            "results": []
        }
        
        for eval_run in eval_runs:
            eval_run.refresh_from_db()
            results_data["results"].append({
                "chapter_id": eval_run.chapter_id,
                "chapter_name": eval_run.chapter.name,
                "matched_count": eval_run.included_count,
                "ambiguous_count": eval_run.ambiguous_count,
                "unresolved_count": eval_run.unresolved_count,
            })

        return results_data
