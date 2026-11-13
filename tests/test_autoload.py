"""Stage a real SDK layout and verify drop-in discovery plus offline export."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, required=True)
    parser.add_argument('--framework', type=Path, required=True)
    parser.add_argument('--staged', type=Path)
    parser.add_argument('--original-sdk', type=Path)
    args = parser.parse_args()
    if args.staged:
        dll_dirs = []
        if os.name == 'nt':
            dll_dirs.append(os.add_dll_directory(str(args.original_sdk)))
        from test_plugin import Libraries, integration, read_events
        output = args.staged/'traces'
        os.environ['MAA_TRACE_DIR'] = str(output)
        os.environ['MAA_TRACE_IMAGES'] = 'none'
        libs = Libraries(args.plugin, args.framework)
        # The test helper normally explicitly loads. Here only the framework's
        # automatic plugins directory discovery is allowed to register callbacks.
        libs.fw.MaaGlobalLoadPlugin = lambda path: True
        integration(libs, output, args.plugin)
        events = read_events(output)
        assert any(e['data'].get('flow', {}).get('attempts') for e in events)
        configs = [e['data']['config'] for e in events if e['data'].get('config', {}).get('availability') == 'available']
        assert configs, 'effective configuration snapshots missing'
        session = next(output.glob('session-*'))
        for ref in configs:
            content = (session/ref['path']).read_bytes()
            assert len(content) == ref['size'] and hashlib.sha256(content).hexdigest() == ref['sha256']
        cli = Path(__file__).resolve().parents[1]/'tools/maatrace.py'
        subprocess.run([sys.executable, str(cli), 'validate', str(session)], check=True)
        archive = args.staged/'report.zip'
        subprocess.run([sys.executable, str(cli), 'export', str(session), '-o', str(archive)], check=True)
        with zipfile.ZipFile(archive) as z:
            assert 'events.jsonl' in z.namelist() and 'export.json' in z.namelist()
            assert any(name.startswith('configs/') for name in z.namelist())
        print('Drop-in autoload, configuration integrity and export passed')
        return
    with tempfile.TemporaryDirectory(prefix='maatrace-autoload-') as temp:
        stage = Path(temp)/'release with spaces'
        (stage/'plugins').mkdir(parents=True)
        framework = args.framework.resolve()
        plugin = args.plugin.resolve()
        shutil.copy2(framework, stage/framework.name)
        utilities = list(framework.parent.glob('*MaaUtils*'))
        assert utilities, 'SDK is missing MaaUtils'
        for path in utilities:
            if path.is_file(): shutil.copy2(path, stage/path.name)
        shutil.copy2(plugin, stage/'plugins'/plugin.name)
        environment = dict(os.environ)
        key = 'PATH' if os.name == 'nt' else 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
        environment[key] = os.pathsep.join([str(stage), str(framework.parent), environment.get(key, '')])
        subprocess.run([sys.executable, __file__, '--plugin', str(stage/'plugins'/plugin.name),
                        '--framework', str(stage/framework.name), '--staged', str(stage),
                        '--original-sdk', str(framework.parent)], env=environment, check=True, timeout=90)

if __name__ == '__main__':
    main()
