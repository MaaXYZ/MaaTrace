"""Offline checks for upstream artifact identity, extraction and execution failures."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('upstream_pipeline', ROOT/'tools/ci/upstream_pipeline.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def run_info(number=10):
    return {'id': number, 'repository': {'full_name': ci.REPOSITORY},
            'head_repository': {'full_name': ci.REPOSITORY}, 'head_sha': 'a'*40,
            'head_branch': 'main', 'event': 'push', 'path': ci.WORKFLOW,
            'conclusion': 'success', 'status': 'completed', 'html_url': 'https://example.test/run'}


def artifacts():
    return [{'id': i+1, 'name': f'MAA-{platform}-full', 'expired': False, 'size_in_bytes': 123}
            for i, platform in enumerate(ci.PLATFORMS)]


class UpstreamTests(unittest.TestCase):
    def test_only_successful_official_main_push_is_executed(self):
        self.assertEqual(ci.validate_run(run_info())['id'], 10)
        for key, value in [('head_branch', 'other'), ('event', 'pull_request'),
                           ('conclusion', 'failure'), ('path', '.github/workflows/build.yml'),
                           ('head_repository', {'full_name': 'someone/fork'})]:
            with self.subTest(key=key):
                bad = run_info(); bad[key] = value
                with self.assertRaises(ValueError): ci.validate_run(bad)

    def test_selection_skips_run_with_expired_required_artifact(self):
        old, new = run_info(10), run_info(11)
        stale = artifacts(); stale[0]['expired'] = True
        def api(path):
            if 'workflows/test.yml/runs?' in path: return {'workflow_runs': [new, old]}
            if path.startswith('compare/'): return {'status': 'ahead'}
            return {'artifacts': stale if '/runs/11/' in path else artifacts()}
        with mock.patch.object(ci, 'api', side_effect=api):
            run, selected = ci.resolve()
        self.assertEqual(run['id'], 10)
        self.assertEqual(set(selected), set(ci.PLATFORMS))

    def test_explicit_run_does_not_silently_fall_back(self):
        values = artifacts()[:-1]
        def api(path):
            if path.startswith('compare/'): return {'status': 'identical'}
            return {'artifacts': values} if '/artifacts?' in path else run_info(10)
        with mock.patch.object(ci, 'api', side_effect=api):
            with self.assertRaises(ValueError): ci.resolve(10)

    def test_only_runs_containing_the_context_callback_fix_are_selected(self):
        for status in ('ahead', 'identical', 'behind', 'diverged'):
            with self.subTest(status=status):
                def api(path):
                    if path.startswith('compare/'):
                        self.assertEqual(path, f'compare/{ci.MINIMUM_FRAMEWORK_REF}...{run_info()["head_sha"]}')
                        return {'status': status}
                    if 'workflows/test.yml/runs?' in path: return {'workflow_runs': [run_info()]}
                    return {'artifacts': artifacts()} if '/artifacts?' in path else run_info()
                with mock.patch.object(ci, 'api', side_effect=api):
                    for run_id in (None, 10):
                        if status in ('ahead', 'identical'):
                            self.assertEqual(ci.resolve(run_id)[0]['id'], 10)
                        else:
                            with self.assertRaises(ValueError): ci.resolve(run_id)

    def test_full_artifact_extract_and_stale_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); archive=root/'full.zip'; output=root/'sdk'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('bin/PipelineTesting', b'program')
                z.writestr('test/PipelineSmoking/data', b'fixture')
            ci.extract_archive(archive, output)
            self.assertEqual((output/'test/PipelineSmoking/data').read_bytes(), b'fixture')
            with self.assertRaises(ValueError): ci.extract_archive(archive, output)

    def test_archive_rejects_traversal_and_symlinks_before_extraction(self):
        for name in ('../outside', '/absolute', 'C:/drive', 'bin/link'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); archive=root/'bad.zip'
                info=zipfile.ZipInfo(name)
                if name=='bin/link': info.external_attr=(stat.S_IFLNK|0o777)<<16
                with zipfile.ZipFile(archive,'w') as z: z.writestr(info, b'outside')
                with self.assertRaises(ValueError): ci.extract_archive(archive,root/'sdk')
                self.assertFalse((root/'outside').exists())

    def test_native_exit_and_recorder_stderr_are_both_failures(self):
        for code, error in ((2, b''), (0, b'[MaaTrace] cannot write\n'), (0, b'')):
            with self.subTest(code=code,error=error), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); sdk=root/'sdk'; plugins=root/'plugins'
                (sdk/'bin').mkdir(parents=True); (sdk/'test').mkdir(); plugins.mkdir()
                suffix='.dll' if os.name=='nt' else '.dylib' if sys.platform=='darwin' else '.so'
                (plugins/('MaaTrace'+suffix)).write_bytes(b'plugin')
                (sdk/'bin'/('PipelineTesting.exe' if os.name=='nt' else 'PipelineTesting')).write_bytes(b'exe')
                def execute(command, **kwargs):
                    self.assertNotIn('GH_TOKEN',kwargs['env'])
                    self.assertEqual(kwargs['env']['MAA_TRACE_IMAGES'],'none')
                    kwargs['stderr'].write(error)
                    return types.SimpleNamespace(returncode=code)
                with mock.patch.dict(os.environ, {'GH_TOKEN':'test-not-a-real-token'}), mock.patch.object(ci.subprocess,'run',side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
                    if code or error:
                        with self.assertRaises(RuntimeError):ci.execute(sdk,plugins,root/'traces',root/'reports',5)
                    else:ci.execute(sdk,plugins,root/'traces',root/'reports',5)
                report=json.loads((root/'reports/execution.json').read_text())
                self.assertEqual(report['exit_code'],code)


if __name__ == '__main__':
    unittest.main()
