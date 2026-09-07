import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from office_pr_checks import inspect_pr


class PrChecksTests(unittest.TestCase):
    def inspect(self, checks, logs='AssertionError: expected: active'):
        pr = {'state': 'OPEN', 'headRefOid': 'abc', 'statusCheckRollup': checks}
        with patch('office_pr_checks.subprocess.run', side_effect=[
            SimpleNamespace(returncode=0, stdout=json.dumps(pr)),
            SimpleNamespace(returncode=0, stdout=logs),
        ]) as run:
            result = inspect_pr('https://github.com/example/app/pull/1', 'gh', {})
            self.assertTrue(all(call.args[0][1:3] in (['pr', 'view'], ['run', 'view']) for call in run.call_args_list))
            return result

    def test_check_states(self):
        for checks, expected in [([], 'waiting'),
                                 ([{'status': 'IN_PROGRESS'}], 'pending'),
                                 ([{'conclusion': 'SUCCESS'}], 'passing'),
                                 ([{'state': 'ERROR'}], 'failed'),
                                 ([{'conclusion': 'CANCELLED'}], 'failed')]:
            self.assertEqual(self.inspect(checks)['status'], expected)

    def test_failure_evidence_and_suggestion(self):
        result = self.inspect([{'name': 'Tests', 'conclusion': 'FAILURE',
                               'detailsUrl': 'https://github.com/example/app/actions/runs/12/job/4'}])
        self.assertIn('AssertionError', result['evidence'])
        self.assertIn('assertions', result['suggestions'][0])

    def test_missing_logs_does_not_invent_diagnosis(self):
        result = self.inspect([{'name': 'External CI', 'state': 'FAILURE'}])
        self.assertEqual(result['evidence'], '')
        self.assertIn('not a verified', result['suggestionNote'])

    def test_url_validation(self):
        for url in ['--help', 'https://example.org/a/b/pull/1', 'https://github.com/a/b/pull/1;echo']:
            with self.assertRaises(ValueError):
                inspect_pr(url, 'gh', {})

    def test_auth_failure_surfaces(self):
        with patch('office_pr_checks.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='Log in first')):
            with self.assertRaisesRegex(RuntimeError, 'Log in first'):
                inspect_pr('https://github.com/example/app/pull/1', 'gh', {})
