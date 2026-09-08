import json
import unittest
from pathlib import Path
from unittest import mock

import office_server as app


class ExistingPrPublishTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'repository': {'path': '/repo'}, 'pullRequest': 'https://github.com/team/app/pull/81',
                        'expectedHead': 'a' * 40, 'title': 'Fix CI', 'selection': {'accepted': ['patch']}}
        self.pr = {'state': 'OPEN', 'headRefName': 'task/fix', 'headRefOid': 'a' * 40,
                   'headRepository': {'name': 'app'}, 'headRepositoryOwner': {'login': 'team'}, 'baseRefName': 'master'}
        self.commands = []
        self.branch = 'task/fix'
        self.remote = 'git@github.com:team/app.git'
        self.committed = False
        self.fail_push = False
        self.patches = [mock.patch.object(app, 'existing_repo_path', return_value=Path('/repo')),
                        mock.patch.object(app, 'find_cli', return_value='gh'),
                        mock.patch.object(app, 'git_executable', return_value='git'),
                        mock.patch.object(app, 'stage_selected_changes'),
                        mock.patch.object(app, 'checked_command', side_effect=self.command)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def command(self, args, *unused, **kwargs):
        self.commands.append(args)
        if args[0] == 'gh': return json.dumps(self.pr)
        if args[1] == 'remote': return self.remote
        if args[1] == 'branch': return self.branch
        if args[1] == 'rev-parse': return ('b' if self.committed else 'a') * 40
        if args[1] == 'commit': self.committed = True
        if args[1] == 'push' and self.fail_push: raise RuntimeError('Network unavailable')
        return ''

    def test_updates_existing_branch_without_pr_creation(self):
        result = app.push_pr_fix(self.payload)
        self.assertEqual(result['pullRequest'], self.payload['pullRequest'])
        self.assertTrue(result['updatedExistingPr'])
        self.assertIn(['git', 'push', 'origin', 'b' * 40 + ':refs/heads/task/fix'], self.commands)
        self.assertFalse(any('create' in c or '--force' in c or 'switch' in c for c in self.commands))
        app.stage_selected_changes.assert_called_once_with(Path('/repo'), self.payload['selection'], 'git')

    def test_preflight_rejects_unsafe_targets_without_staging(self):
        for change in ['closed', 'branch', 'remote', 'head', 'confirmation']:
            with self.subTest(change=change):
                self.pr['state'] = 'CLOSED' if change == 'closed' else 'OPEN'
                self.pr['headRefOid'] = 'c' * 40 if change == 'head' else 'a' * 40
                self.branch = 'unrelated' if change == 'branch' else 'task/fix'
                self.remote = 'git@github.com:other/repo.git' if change == 'remote' else 'git@github.com:team/app.git'
                self.payload['expectedHead'] = 'd' * 40 if change == 'confirmation' else 'a' * 40
                with self.assertRaises(ValueError): app.push_pr_fix(self.payload)
                app.stage_selected_changes.assert_not_called()

    def test_push_failure_reports_preserved_commit(self):
        self.fail_push = True
        with self.assertRaisesRegex(RuntimeError, 'committed locally.*commit is preserved'):
            app.push_pr_fix(self.payload)

    def test_override_is_recorded_in_commit(self):
        app.push_pr_fix({**self.payload, 'reviewOverride': 'PostgreSQL not verified'})
        commit = next(c for c in self.commands if c[1] == 'commit')
        self.assertIn('PostgreSQL not verified', commit[-1])
