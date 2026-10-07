"""F-12 sign-in throttling and F-13 password policy - tested for safety AND for not being a nuisance."""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(__file__))
import _harness as H

app = H.boot_app()


def reset_attempts():
    from database import get_db
    with app.app_context():
        db = get_db(); db.execute('DELETE FROM login_attempts'); db.commit()


class SignIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.members, cls.loans = H.seed(app)

    def setUp(self):
        reset_attempts()

    def _web(self, user, pw, ip='198.51.100.7'):
        return app.test_client().post('/login', data={'username': user, 'password': pw},
                                      environ_overrides={'REMOTE_ADDR': ip})

    def test_one_persons_typos_do_not_lock_out_the_shared_network(self):
        for i in range(6):
            self._web(f'nobody{i}', 'x')
        self.assertEqual(self._web('admin', H.ADMIN_PW).status_code, 302)      # signed in

    def test_forgetful_member_gets_generous_tries_then_a_short_friendly_pause(self):
        for _ in range(7):
            r = self._web('treasurer', 'wrong')
            self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn('left before a short pause', text)           # warned, in plain words
        self.assertIn('Forgot password', text)
        self._web('treasurer', 'wrong')                            # 8th
        r = self._web('treasurer', H.TREAS_PW)                     # even the right password waits
        self.assertEqual(r.status_code, 200)
        self.assertIn('minute', r.get_data(as_text=True))
        self.assertIn('Forgot password', r.get_data(as_text=True))
        # someone else on the same network is unaffected
        self.assertEqual(self._web('secretary', H.SECR_PW).status_code, 302)

    def test_pause_is_short_not_a_lockout(self):
        from utils import ACCOUNT_PAUSE, sign_in_pause_seconds
        for _ in range(8):
            self._web('treasurer', 'wrong')
        with app.app_context():
            self.assertLessEqual(sign_in_pause_seconds('198.51.100.7', 'treasurer'), ACCOUNT_PAUSE)
        self.assertLessEqual(ACCOUNT_PAUSE, 300)

    def test_success_clears_that_account_only(self):
        from utils import sign_in_tries_left
        for _ in range(5):
            self._web('treasurer', 'wrong')
        self._web('treasurer', H.TREAS_PW)
        with app.app_context():
            self.assertEqual(sign_in_tries_left('treasurer'), 8)

    def test_password_spraying_from_one_machine_is_stopped(self):
        for i in range(40):
            self._web(f'someone{i}', 'x', ip='203.0.113.50')
        self.assertEqual(self._web('admin', H.ADMIN_PW, ip='203.0.113.50').status_code, 200)   # paused
        self.assertEqual(self._web('admin', H.ADMIN_PW, ip='203.0.113.51').status_code, 302)   # other machine fine

    def test_attacker_cannot_reset_network_counter_by_logging_into_own_account(self):
        for i in range(39):
            self._web(f'someone{i}', 'x', ip='203.0.113.60')
        self._web('secretary', H.SECR_PW, ip='203.0.113.60')       # a successful sign-in
        self._web('someone99', 'x', ip='203.0.113.60')             # 40th failure
        self.assertEqual(self._web('admin', H.ADMIN_PW, ip='203.0.113.60').status_code, 200)

    def test_forgot_password_page_works_while_paused(self):
        for _ in range(8):
            self._web('treasurer', 'wrong')
        r = app.test_client().get('/forgot-password', environ_overrides={'REMOTE_ADDR': '198.51.100.7'})
        self.assertEqual(r.status_code, 200)
        r = app.test_client().post('/forgot-password', data={'identifier': 'treasurer'},
                                   environ_overrides={'REMOTE_ADDR': '198.51.100.7'})
        self.assertIn(r.status_code, (200, 302))

    def test_mobile_uses_the_same_friendly_rule(self):
        c = app.test_client()
        codes = [c.post('/api/mobile/login', json={'username': 'treasurer', 'password': 'wrong'}).get_json().get('code')
                 for _ in range(8)]
        self.assertEqual(set(codes), {'invalid_credentials'})
        r = c.post('/api/mobile/login', json={'username': 'treasurer', 'password': H.TREAS_PW})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.get_json()['code'], 'try_again_later')
        self.assertIn('Forgot password', r.get_json()['error'])

    def test_members_stay_signed_in_longer_than_staff(self):
        c = app.test_client()
        m = c.post('/api/mobile/login', json={'username': 'alice@sectest.invalid', 'password': H.MEMBER_PW}).get_json()
        s = c.post('/api/mobile/login', json={'username': 'secretary', 'password': H.SECR_PW}).get_json()
        self.assertEqual(m['expires_in_seconds'], 30 * 24 * 3600)
        self.assertEqual(s['expires_in_seconds'], 24 * 3600)

    def test_members_get_a_longer_web_idle_timeout(self):
        import app as app_module
        self.assertEqual(app_module.idle_timeout_for_role('member'), 30 * 60)
        self.assertEqual(app_module.idle_timeout_for_role('treasurer'), 15 * 60)


class Passwords(unittest.TestCase):
    def _v(self, pw, **kw):
        from database import get_db
        from security import validate_password_strength
        with app.app_context():
            return validate_password_strength(pw, get_db(), **kw)

    def test_the_worst_passwords_are_refused_with_a_helpful_message(self):
        for pw in ('Password1', 'Welcome1', 'Qwerty123', 'Coop12345', '12345678', '11111111', 'abcd1234', 'Welcome2024!'):
            ok, errors = self._v(pw)
            self.assertFalse(ok, pw)
            self.assertIn('three or four ordinary', ' '.join(errors))

    def test_easy_to_remember_phrases_are_accepted_without_symbols_or_capitals(self):
        for pw in ('blue river market sunday', 'my grandson plays football', 'tuesday market is busy', 'maple tree 2019'):
            ok, errors = self._v(pw)
            self.assertTrue(ok, (pw, errors))

    def test_a_normal_eight_character_password_is_fine_for_a_member(self):
        self.assertTrue(self._v('maple1957')[0])

    def test_officers_need_ten_characters(self):
        self.assertFalse(self._v('maple195', role='treasurer')[0])
        self.assertTrue(self._v('maple1957x', role='treasurer')[0])

    def test_own_name_or_email_is_refused(self):
        self.assertFalse(self._v('adeolu2020', identifiers=('adeolu@example.com',))[0])

    def test_policy_text_is_plain_and_helpful(self):
        from database import get_db
        from security import password_policy_description
        with app.app_context():
            t = password_policy_description(get_db())
        self.assertIn('short phrase', t)


if __name__ == '__main__':
    unittest.main()
