import argparse
import ctypes as C
import json
import os
from pathlib import Path
import tempfile
from test_plugin import Libraries, bind, read_events, VOID

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin', type=Path, required=True)
    parser.add_argument('--framework', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp)/'traces'
        os.environ['MAA_TRACE_DIR'] = str(output)
        libs = Libraries(args.plugin, args.framework)
        emit = bind(libs.plugin, 'MaaTraceEmit', None, VOID, C.c_char_p, C.c_char_p)
        stop = bind(libs.plugin, 'MaaTraceRecordStopRequest', None, VOID, C.c_char_p)
        tasker = libs.fw.MaaTaskerCreate()
        try:
            libs.emit('Tasker', tasker, 'Tasker.Task.Starting', {'task_id': 42, 'entry':'A'})
            emit(tasker, b'my_script.popup_seen', b'{"kind":"network"}')
            stop(tasker, b'user')
            libs.emit('Tasker', tasker, 'Tasker.Task.Failed', {'task_id':42,'entry':'A'})
            emit(None, b'my_script.summary', b'{"tasks":1}')
            assert libs.plugin.MaaTraceShutdown() == 1
            events = read_events(output)
            custom = next(e for e in events if e['type']=='custom.event')
            stopping = next(e for e in events if e['type']=='stop.requested')
            assert custom['run_id'] == stopping['run_id'] == events[0]['run_id']
            assert custom['data']['native_details']['kind'] == 'network'
            assert stopping['data']['native_details']['reason'] == 'user'
            assert events[-1]['run_id'] is None
            assert not any(e['data'].get('outcome') == 'cancelled' for e in events)
        finally:
            libs.fw.MaaTaskerDestroy(tasker)
    print('Host APIs passed')
if __name__ == '__main__':
    main()
