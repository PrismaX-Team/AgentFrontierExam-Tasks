import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import os
spec = importlib.util.spec_from_file_location("governance", Path(__file__).with_name("governance.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

class GovernanceTests(unittest.TestCase):
    def proposal(self):
        schema = json.loads(Path(__file__).with_name("proposal-schema.json").read_text())
        body = "\n\n".join(f"### {f['label']}\n\n" + ("https://example.org/profile" if f['id']=='professionalUrl' else 'Research evidence') for f in schema['fields'])
        body += '\n\n### Metric type / 指标类型\n\nraw_only · Keep original'
        body += '\n\n### Permissions / 授权确认\n\n' + '\n'.join('- [X] '+c for c in schema['consents'])
        return dict(number=1, title='A scientific proposal', body=body, lastEditedAt=None, category={'slug':'proposals'}, closed=False, author={'login':'author'}, comments=[])
    def test_form_and_conditional_validation(self):
        row = self.proposal()
        g.validate_proposal(row)
        row['body'] = row['body'].replace('### Additional explanation\n\nResearch evidence', '### Additional explanation\n\n_No response_')
        with self.assertRaises(ValueError): g.validate_proposal(row)
    def test_references_allow_blank_lines_between_links(self):
        row = self.proposal()
        links = '\n\n'.join(f'https://example.org/{i}' for i in range(3))
        base = row['body']
        row['body'] = base + f'\n\n### References / 参考材料\n\n{links}\n'
        g.validate_proposal(row)
        row['body'] = base + '\n\n### References / 参考材料\n\nhttps://example.org/0\n\nsee the paper above\n'
        with self.assertRaises(ValueError): g.validate_proposal(row)
    def test_windows_newlines_and_http_links(self):
        row = self.proposal()
        row['body'] = row['body'].replace('https://example.org/profile', 'http://example.org/profile').replace('\n', '\r\n')
        row['body'] += '\r\n\r\n### References\r\n\r\nhttp://example.org/paper\r\n'
        g.validate_proposal(row)
        row['body'] = self.proposal()['body'] + '\n\n### References\n\nftp://example.org/paper\n'
        with self.assertRaises(ValueError): g.validate_proposal(row)
    def test_force_approve_skips_form_checks(self):
        row = self.proposal()
        row.update(id='D1', url=g.ROOT+'/discussions/1', labels={'nodes':[]}, body='not a form', closed=True)
        env = {'GITHUB_REF':'refs/heads/main', 'GITHUB_RUN_ATTEMPT':'1', 'GITHUB_ACTOR':'maintainer', 'GITHUB_TRIGGERING_ACTOR':'maintainer', 'DISCUSSION_NUMBER':'1', 'DECISION':'approve', 'REASON':'Checked the proposal and found no issues; approved.', 'FORCE':'true', 'GITHUB_RUN_ID':'99'}
        with patch.dict(os.environ, env), patch.object(g, 'can_maintain', return_value=True), patch.object(g, 'discussion', return_value=row), patch.object(g, 'rest_pages', return_value=[]), patch.object(g, 'gql') as gql, patch.object(g, 'api', return_value={'node_id':'L1'}):
            with self.assertRaisesRegex(ValueError, 'must be open'): g.decide()
            gql.assert_not_called()
            row['closed'] = False
            g.decide()
            body = next(call.kwargs['body'] for call in gql.call_args_list if 'body' in call.kwargs)
            self.assertIn('"force": true', body)
            self.assertIn('跳过了表单校验', body)
            self.assertIn('Next steps', body)
        env['FORCE'] = 'false'
        with patch.dict(os.environ, env), patch.object(g, 'can_maintain', return_value=True), patch.object(g, 'discussion', return_value=row), patch.object(g, 'gql') as gql:
            with self.assertRaisesRegex(ValueError, 'Required field missing'): g.decide()
            gql.assert_not_called()
    def test_missing_consent(self):
        row = self.proposal(); row['body'] = row['body'].replace('- [X]', '- [ ]')
        with self.assertRaises(ValueError): g.validate_proposal(row)

    def test_rebrand_preserves_legacy_proposals(self):
        for brand in ('Science Innovation Exam', 'LiveDiscoveryBench'):
            for separator in ('  \n', ' / '):
                with self.subTest(brand=brand, separator=separator):
                    row = self.proposal()
                    row['body'] = row['body'].replace('Science Innovation Exam', brand).replace('  \n', separator)
                    before = g.digest(row)
                    g.validate_proposal(row)
                    self.assertEqual(before, g.digest(row))
                    row['body'] = row['body'].replace('- [X]', '- [ ]', 1)
                    with self.assertRaises(ValueError): g.validate_proposal(row)

    def test_rebrand_does_not_accept_other_consent_changes(self):
        row = self.proposal()
        row['body'] = row['body'].replace('Science Innovation Exam', 'Another Benchmark')
        with self.assertRaises(ValueError): g.validate_proposal(row)

    def test_current_and_earlier_form_layouts(self):
        row = self.proposal()
        row['body'] = row['body'].replace('Metric type / 指标类型', 'Metric type').replace('Permissions / 授权确认', 'Permissions')
        g.validate_proposal(row)
        schema = json.loads(Path(__file__).with_name('proposal-schema.json').read_text())
        for field in schema['fields']:
            row['body'] = row['body'].replace('### '+field['label']+'\n', '### '+field['label']+' / 旧版中文标题\n')
        row['body'] = row['body'].replace('  \n', ' / ')
        g.validate_proposal(row)
    def test_consent_text_outside_confirmation_is_not_consent(self):
        row = self.proposal()
        row['body'] = row['body'].replace('### Permissions / 授权确认', '### Example text')
        with self.assertRaises(ValueError): g.validate_proposal(row)
    def test_edit_then_revert_changes_digest(self):
        row = self.proposal(); before = g.digest(row); row['lastEditedAt']='2026-09-08T00:00:00Z'
        self.assertNotEqual(before, g.digest(row))
    def test_cross_repo_proposal_rejected(self):
        for body in ['Proposal: https://github.com/other/repo/discussions/1', 'Proposal: '+g.ROOT+'/issues/1', 'Proposal: '+g.ROOT+'/discussions/1\nProposal: '+g.ROOT+'/discussions/2']:
            with self.assertRaises(ValueError): g.proposal_number(body)
        self.assertEqual(g.proposal_number('Proposal: '+g.ROOT+'/discussions/1'), 1)
    def test_unapproved_cannot_pass(self):
        with self.assertRaises(ValueError): g.approved_proposal({'user':{'login':'author'}}, self.proposal())
    def test_author_and_self_approval(self):
        row=self.proposal(); record={'actor':'maintainer','decision':'approve','digest':g.digest(row)}
        with patch.object(g, 'decision_record', return_value=record):
            g.approved_proposal({'user':{'login':'author'}},row)
            with self.assertRaises(ValueError): g.approved_proposal({'user':{'login':'someone-else'}}, row)
            record['actor']='author'
            with self.assertRaises(ValueError): g.approved_proposal({'user':{'login':'author'}}, row)
    def test_edited_approval_invalid(self):
        row=self.proposal(); record={'actor':'maintainer','decision':'approve','digest':g.digest(row)}; row['body']+='\nChanged science'
        with patch.object(g,'decision_record',return_value=record):
            self.assertEqual(g.proposal_status(row)[0], 'needs_reapproval')
            with self.assertRaises(ValueError): g.approved_proposal({'user':{'login':'author'}}, row)
    def comment(self, body, author='maintainer', id=1):
        return dict(body=body,user={'login':author}, updated_at=f'2026-09-08T00:00:0{id}Z', id=id)
    def test_seats(self):
        pr={'user':{'login':'author'}}; permission=lambda login: login=='maintainer'
        for body in ['/reviewers domain=@author technical=@two', '/reviewers domain=@one technical=@one']:
            with self.assertRaises(ValueError): g.seats(pr,[self.comment(body)],permission)
        with self.assertRaises(ValueError): g.seats(pr,[self.comment('/reviewers domain=@one technical=@two','outsider')],permission)
        self.assertEqual(g.seats(pr,[self.comment('/reviewers domain=@one technical=@two')],permission),dict(domain='one',technical='two'))
        with self.assertRaises(ValueError): g.seats(pr,[self.comment('/reviewers domain=@one technical=@two'),self.comment('/reviewers revoke',id=2)],permission)
    def test_current_reviews_and_dismissal(self):
        rows=[dict(id=1,user={'login':'one'},state='APPROVED',commit_id='old')]
        self.assertFalse(g.approved_review('one',rows,'new'))
        rows.append(dict(id=2,user={'login':'one'},state='APPROVED',commit_id='new'))
        self.assertTrue(g.approved_review('one',rows,'new'))
        self.assertFalse(g.approved_review('two',rows,'new'))
        rows.append(dict(id=3,user={'login':'one'},state='COMMENTED',commit_id='new'))
        self.assertTrue(g.approved_review('one',rows,'new'))
        rows[1]['state']='DISMISSED'
        self.assertFalse(g.approved_review('one',rows,'new'))
    def test_forged_bot_record_and_api_failure(self):
        row=self.proposal(); data=dict(run='1',actor='maintainer',discussion=1,decision='approve',digest=g.digest(row))
        row['comments']=[dict(author={'login':'github-actions'}, lastEditedAt=None, createdAt='2026-09-08T01:00:00Z', body=g.MARKER+json.dumps(data)+' -->')]
        g._run_cache.clear()
        with patch.object(g,'api',return_value={'path':'.github/workflows/forged.yml'}): self.assertIsNone(g.decision_record(row))
        g._run_cache.clear()
        with patch.object(g,'api',side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError): g.decision_record(row)
    def test_proposal_title_prefers_form_field(self):
        self.assertEqual(g.proposal_title(dict(title='[Proposal]', body='### Name\n\nA\n\n### Task title\n\nPredicting stability\n\n### Scientific domain\n\nX\n')), 'Predicting stability')
        self.assertEqual(g.proposal_title(dict(title='[Proposal] Typed title', body='### Task title\n\n_No response_\n')), 'Typed title')
        self.assertEqual(g.proposal_title(dict(title='[Proposal]', body='')), '[Proposal]')
    def test_acceptance_excluded(self):
        self.assertTrue(g.acceptance(dict(title='[ACCEPTANCE] Test',labels=[])))
        self.assertTrue(g.acceptance(dict(title='Test',labels={'nodes':[{'name':'acceptance'}]})))

    def test_rerun_cannot_publish_a_decision(self):
        for actor in ['maintainer', 'author']:
            with patch.dict(os.environ, {'GITHUB_REF':'refs/heads/main', 'GITHUB_RUN_ATTEMPT':'2', 'GITHUB_ACTOR':'maintainer', 'GITHUB_TRIGGERING_ACTOR':actor}), patch.object(g, 'api') as api:
                with self.assertRaisesRegex(ValueError, 'rerunning old decisions'):
                    g.decide()
                api.assert_not_called()

    def test_decision_checks_actual_initiator(self):
        env = {'GITHUB_REF':'refs/heads/main', 'GITHUB_RUN_ATTEMPT':'1', 'GITHUB_ACTOR':'maintainer', 'GITHUB_TRIGGERING_ACTOR':'author'}
        with patch.dict(os.environ, env), patch.object(g, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'actual initiating maintainer'):
                g.decide()
            api.assert_not_called()
        env['GITHUB_ACTOR'] = 'author'
        env['DISCUSSION_NUMBER'] = '1'
        with patch.dict(os.environ, env), patch.object(g, 'can_maintain', return_value=True), patch.object(g, 'discussion', return_value=self.proposal()), patch.object(g, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'self-approval'):
                g.decide()
            api.assert_not_called()

    def test_records_require_successful_first_attempt_by_actual_actor(self):
        row = self.proposal()
        data = dict(run='1', actor='maintainer', discussion=1, decision='approve', digest=g.digest(row))
        row['comments'] = [dict(author={'login':'github-actions[bot]'}, lastEditedAt=None, createdAt='2026-09-08T01:00:01Z', body=g.MARKER+json.dumps(data)+' -->')]
        run = dict(path='.github/workflows/proposal-review.yml', event='workflow_dispatch', head_branch='main', head_repository={'full_name':g.REPO}, conclusion='success', actor={'login':'maintainer'}, triggering_actor={'login':'maintainer'}, run_attempt=1, created_at='2026-09-08T01:00:00Z', updated_at='2026-09-08T01:00:02Z')
        for attempt, actor, expected in [(1, 'maintainer', data), (2, 'maintainer', None), (2, 'author', None), (1, 'author', None)]:
            g._run_cache.clear()
            with patch.object(g, 'api', return_value={**run, 'run_attempt':attempt, 'triggering_actor':{'login':actor}}):
                self.assertEqual(g.decision_record(row), expected)
        g._run_cache.clear()
    def test_unlabeled_pr_is_pending_not_failure(self):
        with self.assertRaises(g.Pending): g.confirmed_kind({'labels': [], 'number': 1})
        with patch.object(g, 'rest_pages', return_value=[]):
            with self.assertRaises(ValueError) as caught: g.confirmed_kind({'labels': [{'name':'type:new-task'},{'name':'type:task-fix'}], 'number': 1})
        self.assertNotIsInstance(caught.exception, g.Pending)
    def test_identity_resolves_avatar_for_login_only_records(self):
        with patch.object(g, 'api', return_value={'avatar_url': 'https://avatars.example/1'}) as mocked:
            self.assertEqual(g.identity({'login': 'someone'})['image'], 'https://avatars.example/1')
            self.assertEqual(g.identity({'login': 'someone'})['image'], 'https://avatars.example/1')
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(g.identity({'login': 'other', 'avatarUrl': 'https://avatars.example/2'})['image'], 'https://avatars.example/2')
        self.assertIsNone(g.identity(None)['image'])
        g._avatars.clear()

if __name__ == '__main__': unittest.main()
