"""Application logs contain allowlisted correlation fields, never payloads/URLs."""
import json
import logging
from .source_diagnostics import SourceFailure


def configure_telemetry():
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('replay').setLevel(logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)



def event(name, *, request_id=None, job_id=None, tenant_id=None, version=None, state=None, code=None,
          source_failure: SourceFailure | None = None):
    record = {'event': name, 'request_id': request_id, 'job_id': job_id, 'tenant_id': tenant_id,
              'version': version, 'state': state, 'code': code}
    if isinstance(source_failure, SourceFailure):
        record['source_failure'] = source_failure.model_dump(exclude_none=True)
    logging.getLogger('replay.operations').info(json.dumps({k: v for k, v in record.items() if v is not None}, separators=(',', ':')))
