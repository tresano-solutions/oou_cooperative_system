"""Persistent upload outcomes stored alongside the existing audit trail."""
import json

from flask import request
from utils import audit


def record_upload(db, module, success, errors, skipped=0, warnings=None, rows=None):
    upload = request.files.get('file')
    filename = (upload.filename if upload else '') or 'Upload'
    payload = dict(filename=filename, success=success, skipped=skipped,
                   errors=list(errors), warnings=list(warnings or []), rows=list(rows or []))
    audit(db, 'UPLOAD_RESULT', module,
          f'{filename}: {success} imported, {skipped} skipped, {len(errors)} failed, '
          f'{len(payload["warnings"])} warnings', json.dumps(payload))


def decode_upload(row):
    result = dict(row)
    try:
        result['result'] = json.loads(row['data'] or '{}')
    except (ValueError, TypeError):
        result['result'] = {}
    return result
