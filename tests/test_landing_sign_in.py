import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vps_generate', ROOT / 'deploy/vps/generate.py')
generate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate)


class LandingSignInTests(unittest.TestCase):
    def test_landing_routes_only_exact_tenant_lookup_to_backend(self):
        with patch.object(generate, 'landing_domain', return_value='cooperativems.com'), patch.object(generate, 'landing_api_client', return_value='hq'):
            result = generate.render_caddyfile([])
        self.assertIn('@tenant_lookup path /api/mobile/v1/tenants/resolve', result)
        self.assertIn('reverse_proxy @tenant_lookup app-hq:8000', result)
        self.assertIn('try_files {path} {path}.html {path}/index.html', result)

    def test_home_signin_is_not_an_email_link(self):
        html = (ROOT / 'deploy/vps/landing/index.html').read_text(encoding='utf-8')
        self.assertIn('href="/sign-in">Sign in</a>', html)
        self.assertNotIn('CoopMS%20sign-in%20help', html)

    def test_no_public_client_directory_or_tracking_on_signin(self):
        html = (ROOT / 'deploy/vps/landing/sign-in.html').read_text(encoding='utf-8')
        for forbidden in ('smtcoop', 'ooucoop', 'googletagmanager', 'type="password"'):
            self.assertNotIn(forbidden, html)
        self.assertIn('noindex, nofollow', html)


if __name__ == '__main__':
    unittest.main()
