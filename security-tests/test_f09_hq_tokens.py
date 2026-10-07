"""F-09 regression: each tenant accepts only its own HQ token; a tenant's token cannot control another tenant."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy', 'vps'))
import _harness as H

app = H.boot_app()
import hq_tokens, generate

MASTER = 'm' * 64


class HqTokens(unittest.TestCase):
    def setUp(self):
        for k in ('HQ_TENANT_TOKEN',):
            os.environ.pop(k, None)
        os.environ['HQ_SYNC_TOKEN'] = MASTER

    def _post(self, token):
        return app.test_client().post('/api/hq/set-status', json={'suspended': False}, headers={'X-HQ-Token': token})

    def test_tokens_are_per_tenant_and_not_the_master(self):
        a = hq_tokens.derive_tenant_token(MASTER, 'ooucoop')
        b = hq_tokens.derive_tenant_token(MASTER, 'smtcoop')
        self.assertNotEqual(a, b)
        self.assertNotIn(MASTER, (a, b))
        self.assertEqual(a, hq_tokens.derive_tenant_token(MASTER, 'ooucoop'))

    def test_tenant_with_own_token_rejects_master_and_other_tenants(self):
        os.environ['HQ_TENANT_TOKEN'] = hq_tokens.derive_tenant_token(MASTER, 'ooucoop')
        os.environ['HQ_SYNC_TOKEN'] = ''                       # tenants do not hold the master
        self.assertEqual(self._post(hq_tokens.derive_tenant_token(MASTER, 'ooucoop')).status_code, 200)
        self.assertEqual(self._post(hq_tokens.derive_tenant_token(MASTER, 'smtcoop')).status_code, 403)
        self.assertEqual(self._post(MASTER).status_code, 403)
        self.assertEqual(self._post('').status_code, 403)

    def test_legacy_tenant_still_works_during_rollout(self):
        self.assertEqual(self._post(MASTER).status_code, 200)
        self.assertEqual(self._post('wrong').status_code, 403)

    def test_hq_derives_the_right_token_from_client_codes(self):
        want = hq_tokens.derive_tenant_token(MASTER, 'ooucoop')
        for code in ('ooucoop', 'ooucoop.cooperativems.com', 'https://ooucoop.cooperativems.com/'):
            self.assertEqual(hq_tokens.sender_token_for(code), want, code)
        self.assertEqual(hq_tokens.sender_token_for(''), '')
        self.assertEqual(hq_tokens.sender_token_for('bad name!'), '')

    def test_generated_compose_gives_tenants_only_their_own_token(self):
        import tempfile, shutil
        d = tempfile.mkdtemp()
        try:
            open(os.path.join(d, '.env'), 'w').write(f'HQ_SYNC_TOKEN={MASTER}\n')
            generate.HERE = d
            out = generate.render_compose([
                {'name': 'hq', 'domain': 'hq.x', 'own_db_url': True},
                {'name': 'ooucoop', 'domain': 'o.x', 'own_db_url': True},
                {'name': 'smtcoop', 'domain': 's.x', 'own_db_url': True}])
        finally:
            shutil.rmtree(d)
        self.assertNotIn(MASTER, out)                           # master never written for tenants
        self.assertIn(hq_tokens.derive_tenant_token(MASTER, 'ooucoop'), out)
        self.assertIn(hq_tokens.derive_tenant_token(MASTER, 'smtcoop'), out)
        hq_block = out.split('app-ooucoop:')[0]
        self.assertIn('HQ_SYNC_TOKEN: ${HQ_SYNC_TOKEN:-}', hq_block)   # only the HQ app gets the master
        self.assertEqual(out.count('HQ_SYNC_TOKEN: '), 1)


if __name__ == '__main__':
    unittest.main()
