from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
from metadataservice.client.commands import find_run_starts, runstart_given_uid
from metadataservice.client.commands import *
import time
from collections import deque
import uuid
import numpy as np


conf.connection_config['host'] = 'localhost'
conf.connection_config['port'] = 7770
custom = {}


def setup_syn():

    data_keys = {k:  {'source': k,
                      'dtype': 'number',
                      'shape': None} for k in 'ABCEDEFHIJKL'
                 }
    scan_id = 1
    cfg = {'beamline_id': 'testing',
            'custom': {},
            'group': 'test',
            'owner': 'test',
            'project': 'test',
            'scan_id': 1,
            'time': 1441727144.985973,
            'uid': str(uuid.uuid4())}
    # Create a BeginRunEvent that serves as entry point for a run
    rs = insert_run_start(scan_id=scan_id, beamline_id='testing', time=time.time(),
                          custom=custom, uid=str(uuid.uuid4()), config=cfg, project='test',
                          owner='test', group='test')
    
    # Create an EventDescriptor that indicates the data
    # keys and serves as header for set of Event(s)

    e_desc = insert_event_descriptor(data_keys=data_keys, time=time.time(),
                                     run_start=rs, uid=str(uuid.uuid4()))
    return rs, e_desc, data_keys


def syn_data(data_keys, count):
    all_data = deque()
    for seq_num in range(count):
        data = {k: float(seq_num) for k in data_keys}
        timestamps = {k: time.time() for k in data_keys}

        _time = time.time()
        uid = str(uuid.uuid4())
        all_data.append({'data': data, 'timestamps': timestamps,
                         'seq_num': seq_num, 'time':_time,
                         'uid': uid})
    return all_data

func = np.cos
num = 65000
start = 0
stop = 10


k = find_run_starts(range_floor=0, range_ceil=1000, owner="xf23id1")
tmp = 0
for _ in k:
    print(_)
    print(tmp)
    tmp += 1

