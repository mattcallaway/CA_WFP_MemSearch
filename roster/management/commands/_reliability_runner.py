"""Subprocess-based reliability database lifecycle manager.

Provides isolated file-backed SQLite databases for benchmark and
evidence commands. Never switches the Django ORM database inside
an already-initialized management-command process.

Usage:
    runner = ReliabilityRunner()
    with runner.isolated_session() as session:
        result = session.run_command('benchmark_import_pipeline', '--scales', '100', '1000')
        print(result.stdout)
        print(result.stderr)
        print(result.exit_code)
"""
import os
import sys
import subprocess
import tempfile
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class CommandResult:
    command: str
    args: tuple
    exit_code: int
    stdout: str
    stderr: str
    settings_module: str
    resolved_db_path: str
    duration_seconds: float
    timestamp: str


@dataclass
class SessionReport:
    tmpdir: str
    db_path: str
    migrations_result: CommandResult | None = None
    command_results: list = field(default_factory=list)
    cleanup_success: bool = False
    cleanup_details: dict = field(default_factory=dict)


class ReliabilitySession:
    """An active reliability database session."""
    
    SETTINGS_MODULE = 'wfp_memsearch.test_settings_reliability'
    
    def __init__(self, tmpdir: str, db_path: str, manage_py_path: str):
        self.tmpdir = tmpdir
        self.db_path = db_path
        self.manage_py_path = manage_py_path
        self.report = SessionReport(tmpdir=tmpdir, db_path=db_path)
        self._migrated = False
    
    def _run_subprocess(self, *args) -> CommandResult:
        """Run a Django management command in a subprocess with reliability settings."""
        env = os.environ.copy()
        env['WFP_RELIABILITY_DB_PATH'] = self.db_path
        env['DJANGO_SETTINGS_MODULE'] = self.SETTINGS_MODULE
        
        cmd = [sys.executable, self.manage_py_path] + list(args)
        
        start = datetime.now(timezone.utc)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(self.manage_py_path).parent),
            timeout=600,  # 10 minute timeout
        )
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        
        return CommandResult(
            command=args[0] if args else '',
            args=tuple(args[1:]),
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            settings_module=self.SETTINGS_MODULE,
            resolved_db_path=self.db_path,
            duration_seconds=duration,
            timestamp=start.isoformat(),
        )
    
    def migrate(self) -> CommandResult:
        """Apply all migrations to the isolated database."""
        result = self._run_subprocess('migrate', '--run-syncdb', '--verbosity=0')
        self.report.migrations_result = result
        if result.exit_code != 0:
            raise RuntimeError(
                f'Migration failed (exit {result.exit_code}):\n{result.stderr}'
            )
        self._migrated = True
        return result
    
    def run_command(self, command: str, *args) -> CommandResult:
        """Run a management command in the isolated database."""
        if not self._migrated:
            self.migrate()
        
        result = self._run_subprocess(command, *args)
        self.report.command_results.append(result)
        
        # Print subprocess confirmation info
        print(f'[ReliabilityRunner] Settings: {self.SETTINGS_MODULE}')
        print(f'[ReliabilityRunner] DB Path: {self.db_path}')
        print(f'[ReliabilityRunner] Backend: django.db.backends.sqlite3')
        print(f'[ReliabilityRunner] Exit Code: {result.exit_code}')
        
        return result
    
    def verify_not_active_db(self, active_db_path: str) -> bool:
        """Confirm the isolated DB is not the active development database."""
        isolated = Path(self.db_path).resolve()
        active = Path(active_db_path).resolve()
        assert isolated != active, (
            f'Reliability database {isolated} must not be the active database {active}'
        )
        return True


class ReliabilityRunner:
    """Creates and manages isolated file-backed SQLite databases."""
    
    def __init__(self, manage_py_path: str | None = None):
        if manage_py_path is None:
            # Auto-detect manage.py relative to this file
            self.manage_py_path = str(
                Path(__file__).resolve().parents[3] / 'manage.py'
            )
        else:
            self.manage_py_path = manage_py_path
    
    class _IsolatedSession:
        def __init__(self, runner: 'ReliabilityRunner'):
            self.runner = runner
            self.session = None
            self._tmpdir_obj = None
        
        def __enter__(self) -> ReliabilitySession:
            self._tmpdir_obj = tempfile.TemporaryDirectory(prefix='wfp_reliability_')
            tmpdir = self._tmpdir_obj.name
            db_path = os.path.join(tmpdir, 'reliability.sqlite3')
            self.session = ReliabilitySession(
                tmpdir=tmpdir,
                db_path=db_path,
                manage_py_path=self.runner.manage_py_path,
            )
            print(f'[ReliabilityRunner] Created isolated DB: {db_path}')
            print(f'[ReliabilityRunner] Temp directory: {tmpdir}')
            return self.session
        
        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.session is None:
                return
            
            db_path = Path(self.session.db_path)
            cleanup_details = {}
            
            # Delete database files
            for suffix in ['', '-wal', '-shm']:
                p = db_path.with_name(db_path.name + suffix) if suffix else db_path
                if p.exists():
                    try:
                        p.unlink()
                        cleanup_details[f'deleted_{suffix or "db"}'] = True
                    except Exception as e:
                        cleanup_details[f'deleted_{suffix or "db"}'] = str(e)
            
            # Remove temp directory
            try:
                self._tmpdir_obj.cleanup()
                cleanup_details['tmpdir_removed'] = True
                cleanup_details['tmpdir_exists_after'] = Path(self.session.tmpdir).exists()
            except Exception as e:
                cleanup_details['tmpdir_removed'] = str(e)
            
            self.session.report.cleanup_details = cleanup_details
            self.session.report.cleanup_success = (
                cleanup_details.get('tmpdir_removed') is True
                and not cleanup_details.get('tmpdir_exists_after', True)
            )
            
            print(f'[ReliabilityRunner] Cleanup: {"SUCCESS" if self.session.report.cleanup_success else "FAILED"}')
    
    def isolated_session(self) -> _IsolatedSession:
        """Context manager providing an isolated reliability database session."""
        return self._IsolatedSession(self)
