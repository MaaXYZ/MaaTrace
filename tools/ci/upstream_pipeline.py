#!/usr/bin/env python3
"""Resolve/download the official MaaFramework test artifact, then run it unchanged.

Only successful pushes to MaaXYZ/MaaFramework main's test.yml are accepted.
Authentication is delegated to gh; no credentials are passed to PipelineTesting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

REPOSITORY = 'MaaXYZ/MaaFramework'
WORKFLOW = '.github/workflows/test.yml'
PLATFORMS = ('linux-x86_64', 'win-x86_64', 'macos-aarch64')


def api(endpoint):
    result = subprocess.run(['gh', 'api', f'repos/{REPOSITORY}/{endpoint}'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Cannot read upstream Actions metadata. A token with access to the public upstream Actions artifacts may be required.\n'
                           + result.stderr.decode('utf-8', errors='replace').strip())
    # GitHub JSON is UTF-8. Passing bytes to json.loads avoids Windows ACP
    # decoding of non-ASCII commit messages inside subprocess reader threads.
    return json.loads(result.stdout)


def validate_run(run):
    if (run.get('repository', {}).get('full_name') != REPOSITORY
            or run.get('head_repository', {}).get('full_name') != REPOSITORY
            or run.get('path') != WORKFLOW or run.get('event') != 'push'
            or run.get('head_branch') != 'main' or run.get('conclusion') != 'success'
            or run.get('status') != 'completed'):
        raise ValueError('Upstream run must be a successful completed main push from MaaFramework test.yml')
    if not re.fullmatch(r'[0-9a-f]{40}', run.get('head_sha', '')):
        raise ValueError('Upstream run is missing a full commit SHA')
    return run


def artifacts(run_id):
    result = []
    for page in range(1, 11):
        values = api(f'actions/runs/{run_id}/artifacts?per_page=100&page={page}').get('artifacts', [])
        result.extend(values)
        if len(values) < 100:
            return result
    raise ValueError('Unexpectedly large upstream artifact listing')


def available_artifacts(values):
    selected = {}
    for platform in PLATFORMS:
        name = f'MAA-{platform}-full'
        matches = [value for value in values if value.get('name') == name and not value.get('expired')]
        if len(matches) == 1:
            selected[platform] = matches[0]
    return selected


def resolve(run_id=None):
    if run_id:
        run = validate_run(api(f'actions/runs/{run_id}'))
        selected = available_artifacts(artifacts(run['id']))
        if len(selected) != len(PLATFORMS):
            raise ValueError('Requested run does not have all three unexpired full artifacts')
        return run, selected
    for page in range(1, 6):
        result = api('actions/workflows/test.yml/runs?branch=main&event=push&status=success'
                     f'&per_page=20&page={page}')
        runs = result.get('workflow_runs', [])
        for candidate in runs:
            run = validate_run(candidate)
            selected = available_artifacts(artifacts(run['id']))
            if len(selected) == len(PLATFORMS):
                return run, selected
        if len(runs) < 20:
            break
    raise ValueError('No successful main test run with all required non-expired artifacts was found')


def provenance(run, selected):
    return {
        'repository': REPOSITORY, 'workflow': WORKFLOW, 'run_id': run['id'],
        'run_attempt': run.get('run_attempt'), 'commit': run['head_sha'], 'url': run['html_url'],
        'artifacts': {platform: {'id': value['id'], 'name': value['name'], 'digest': value.get('digest'),
                                 'size_in_bytes': value['size_in_bytes']}
                      for platform, value in selected.items()},
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def extract_archive(archive, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Artifact destination must be empty')
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if sum(entry.file_size for entry in entries) > 12 * 1024**3:
            raise ValueError('Upstream artifact exceeds the extraction budget')
        seen = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or ':' in entry.filename or stat.S_ISLNK(mode)
                    or entry.filename in seen):
                raise ValueError(f'Unsafe or duplicate artifact path: {entry.filename}')
            seen.add(entry.filename)
        package.extractall(output)


def download(run_id, platform, output, metadata):
    run, selected = resolve(run_id)
    artifact = selected[platform]
    metadata.parent.mkdir(parents=True, exist_ok=True)
    report = provenance(run, selected)
    report['platform'] = platform
    # Store upstream identity even when archive download/extraction fails.
    metadata.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='maatrace-upstream-') as temporary:
        archive = Path(temporary) / 'artifact.zip'
        with archive.open('wb') as stream:
            result = subprocess.run(['gh', 'api', f'repos/{REPOSITORY}/actions/artifacts/{artifact["id"]}/zip'],
                                    stdout=stream, stderr=subprocess.PIPE, text=False)
        if result.returncode:
            raise RuntimeError('Cannot download upstream CI artifact. Check Actions-read access to MaaXYZ/MaaFramework.\n'
                               + result.stderr.decode('utf-8', errors='replace'))
        digest = sha256_file(archive)
        expected = artifact.get('digest')
        if expected and expected != f'sha256:{digest}':
            raise ValueError('Downloaded upstream artifact SHA256 does not match GitHub metadata')
        report['download_sha256'] = digest
        report['github_digest_verified'] = bool(expected)
        extract_archive(archive, output)
    executable = output / 'bin' / ('PipelineTesting.exe' if platform.startswith('win-') else 'PipelineTesting')
    if not executable.is_file() or not (output / 'test').is_dir() or not (output / 'include/MaaFramework/MaaAPI.h').is_file():
        raise ValueError('Full artifact is missing PipelineTesting, its test data, or SDK headers')
    if os.name != 'nt':
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    metadata.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'Downloaded {artifact["name"]} from {run["html_url"]} ({run["head_sha"]})')


def execute(sdk, plugin_dir, trace_dir, report_dir, timeout):
    sdk, plugin_dir, trace_dir, report_dir = (p.resolve() for p in (sdk, plugin_dir, trace_dir, report_dir))
    suffix = '.dll' if os.name == 'nt' else '.dylib' if sys.platform == 'darwin' else '.so'
    plugins = list(plugin_dir.glob('*MaaTrace' + suffix))
    if len(plugins) != 1:
        raise ValueError('Expected exactly one installed MaaTrace plugin')
    target_dir = sdk / 'bin/plugins'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / plugins[0].name
    shutil.copy2(plugins[0], target)
    trace_dir.mkdir(parents=True, exist_ok=True)
    if any(trace_dir.iterdir()):
        raise ValueError('Trace directory must be empty so stale records cannot satisfy the check')
    report_dir.mkdir(parents=True, exist_ok=True)
    executable = sdk / 'bin' / ('PipelineTesting.exe' if os.name == 'nt' else 'PipelineTesting')
    if os.name != 'nt': executable.chmod(executable.stat().st_mode | 0o111)
    environment = dict(os.environ)
    # Do not expose downloader credentials to the upstream test process.
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'MAA_UPSTREAM_ACTIONS_TOKEN'):
        environment.pop(key, None)
    environment.update({'MAA_TRACE_ENABLED': '1', 'MAA_TRACE_DIR': str(trace_dir), 'MAA_TRACE_IMAGES': 'none'})
    loader_key = 'PATH' if os.name == 'nt' else 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
    environment[loader_key] = os.pathsep.join([str(sdk/'bin'), str(sdk/'bin/msvc-debug'), environment.get(loader_key, '')])
    report = {'command': [str(executable), str(sdk/'test')], 'cwd': str(sdk),
              'plugin_sha256': sha256_file(target), 'timeout_seconds': timeout}
    start = time.monotonic()
    with (report_dir/'pipeline.stdout.log').open('wb') as stdout, (report_dir/'pipeline.stderr.log').open('wb') as stderr:
        try:
            result = subprocess.run(report['command'], cwd=sdk, env=environment, stdout=stdout, stderr=stderr, timeout=timeout)
            report['exit_code'] = result.returncode
        except subprocess.TimeoutExpired:
            report['exit_code'] = None
            report['timed_out'] = True
    report['duration_seconds'] = round(time.monotonic() - start, 3)
    # Windows has no explicit plugin shutdown in the unchanged upstream runner;
    # catch recorder errors even when manifest final counters cannot be written.
    recorder_errors = []
    for filename in ('pipeline.stdout.log', 'pipeline.stderr.log'):
        with (report_dir/filename).open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if '[MaaTrace] ' in line:
                    recorder_errors.append(line.rstrip()[:2000])
    report['recorder_errors'] = recorder_errors[:100]
    (report_dir/'execution.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    if report['exit_code'] != 0 or recorder_errors:
        raise RuntimeError('Upstream PipelineTesting failed or the recorder reported an error; see uploaded logs')


def run_number(value):
    if value == '': return None
    if not value.isdigit() or int(value) <= 0: raise argparse.ArgumentTypeError('run ID must be a positive integer')
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    select = commands.add_parser('resolve')
    select.add_argument('--run-id', type=run_number)
    select.add_argument('--output', type=Path, required=True)
    fetch = commands.add_parser('download')
    fetch.add_argument('--run-id', type=run_number, required=True)
    fetch.add_argument('--platform', choices=PLATFORMS, required=True)
    fetch.add_argument('--output', type=Path, required=True)
    fetch.add_argument('--metadata', type=Path, required=True)
    run = commands.add_parser('run')
    run.add_argument('--sdk', type=Path, required=True)
    run.add_argument('--plugin-dir', type=Path, required=True)
    run.add_argument('--trace-dir', type=Path, required=True)
    run.add_argument('--report-dir', type=Path, required=True)
    run.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    if args.command == 'resolve':
        info, selected = resolve(args.run_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(provenance(info, selected), indent=2)+'\n', encoding='utf-8')
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
                stream.write(f'run_id={info["id"]}\ncommit={info["head_sha"]}\n')
        print(f'Using {info["html_url"]} at {info["head_sha"]}')
    elif args.command == 'download':
        download(args.run_id, args.platform, args.output, args.metadata)
    else:
        execute(args.sdk, args.plugin_dir, args.trace_dir, args.report_dir, args.timeout)

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as error:
        print(f'upstream-pipeline: {error}', file=sys.stderr)
        raise SystemExit(1)
