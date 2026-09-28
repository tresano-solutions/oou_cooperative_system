"""Isolated local design preview. Never connects to a tenant database."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))
import test_hardening_features as fixture

app = fixture.app_module.app
app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5087, use_reloader=False)
