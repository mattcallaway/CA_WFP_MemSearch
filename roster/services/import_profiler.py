"""Explicit production importer phase instrumentation.

Wraps CaptureQueriesContext around named production import phases
to produce per-phase query counts, row counts, and timing.

Usage:
    profiler = ImportProfiler()
    with profiler.phase('parsing', input_rows=1000):
        # ... parsing logic ...
    with profiler.phase('raw_insertion', input_rows=1000, expected_bound=10):
        # ... insertion logic ...

    summary = profiler.summary()
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import time


@dataclass
class PhaseResult:
    """Result of profiling a single import phase."""
    name: str
    query_count: int = 0
    input_rows: int = 0
    output_rows: int = 0
    chunk_count: int = 0
    duration_seconds: float = 0.0
    expected_query_bound: int | None = None
    actual_query_count: int = 0
    queries: list = field(default_factory=list)
    exceeded_bound: bool = False


class ImportProfiler:
    """Wraps CaptureQueriesContext around explicit importer phases."""

    PHASES = [
        'setup',
        'parsing',
        'existing_row_preload',
        'raw_insertion',
        'duplicate_amendment_classification',
        'contribution_insertion',
        'source_record_link_insertion',
        'clustering',
        'entity_resolution',
        'membership_evaluation',
        'audit_creation',
        'finalization',
    ]

    def __init__(self):
        self.phases: list[PhaseResult] = []
        self._start_time: float | None = None

    @contextmanager
    def phase(self, name: str, *, input_rows: int = 0, expected_bound: int | None = None):
        """Context manager capturing queries for a named phase.

        Args:
            name: Phase name (should be one of PHASES).
            input_rows: Number of input rows entering this phase.
            expected_bound: Expected maximum query count for this phase.

        Yields:
            PhaseResult that will be populated on exit.
        """
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        result = PhaseResult(
            name=name,
            input_rows=input_rows,
            expected_query_bound=expected_bound,
        )

        start = time.perf_counter()
        with CaptureQueriesContext(connection) as ctx:
            yield result

        result.duration_seconds = time.perf_counter() - start
        result.query_count = len(ctx)
        result.actual_query_count = len(ctx)
        result.queries = [
            {
                'sql': q['sql'][:200],  # Truncate for summary
                'time': q.get('time', ''),
            }
            for q in ctx.captured_queries
        ]
        result.exceeded_bound = (
            expected_bound is not None and result.actual_query_count > expected_bound
        )
        self.phases.append(result)

    def summary(self) -> dict:
        """Returns phase-level profile with totals."""
        total_queries = sum(p.query_count for p in self.phases)
        total_duration = sum(p.duration_seconds for p in self.phases)
        any_exceeded = any(p.exceeded_bound for p in self.phases)

        return {
            'total_queries': total_queries,
            'total_duration_seconds': round(total_duration, 4),
            'phase_count': len(self.phases),
            'any_bound_exceeded': any_exceeded,
            'phases': [
                {
                    'name': p.name,
                    'query_count': p.query_count,
                    'input_rows': p.input_rows,
                    'output_rows': p.output_rows,
                    'chunk_count': p.chunk_count,
                    'duration_seconds': round(p.duration_seconds, 4),
                    'expected_bound': p.expected_query_bound,
                    'exceeded_bound': p.exceeded_bound,
                }
                for p in self.phases
            ],
        }

    def classify_phase_costs(self, results_by_scale: dict) -> dict:
        """Classify per-phase cost type from multi-scale results.

        Args:
            results_by_scale: {scale: summary_dict} from runs at multiple scales.

        Returns:
            Phase cost classification with formula coefficients.
        """
        scales = sorted(results_by_scale.keys())
        if len(scales) < 2:
            return {'error': 'Need at least 2 scales for classification'}

        classifications = {}
        phase_names = set()
        for scale_data in results_by_scale.values():
            for phase in scale_data.get('phases', []):
                phase_names.add(phase['name'])

        for phase_name in sorted(phase_names):
            counts = {}
            for scale in scales:
                phases = results_by_scale[scale].get('phases', [])
                for p in phases:
                    if p['name'] == phase_name:
                        counts[scale] = p['query_count']
                        break

            if len(counts) < 2:
                classifications[phase_name] = {'type': 'UNKNOWN', 'reason': 'insufficient data'}
                continue

            values = [counts[s] for s in scales if s in counts]
            scale_keys = [s for s in scales if s in counts]

            # Check if fixed (identical across scales)
            if len(set(values)) == 1:
                classifications[phase_name] = {
                    'type': 'FIXED',
                    'fixed_cost': values[0],
                }
                continue

            # Check per-chunk (proportional to ceil(N/chunk_size))
            # Check per-row (proportional to N — failure unless justified)
            ratio = values[-1] / values[0] if values[0] > 0 else float('inf')
            scale_ratio = scale_keys[-1] / scale_keys[0]

            if 0.8 * scale_ratio <= ratio <= 1.2 * scale_ratio:
                classifications[phase_name] = {
                    'type': 'PER_ROW',
                    'warning': 'Query count grows per-row — potential N+1',
                    'ratio': round(ratio, 2),
                    'scale_ratio': round(scale_ratio, 2),
                }
            else:
                classifications[phase_name] = {
                    'type': 'PER_CHUNK',
                    'ratio': round(ratio, 2),
                    'scale_ratio': round(scale_ratio, 2),
                }

        return classifications
