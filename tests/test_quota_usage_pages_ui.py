"""Account/model quotas remain visible, private and distinct from token totals."""
from html.parser import HTMLParser

import pytest

from ccfleetd import bilingual, render, slots, usersite
from tests.test_html_document_preservation import Document

NOW = 1_800_000_000.0


def quota(checked=NOW):
    return {'checked_at': checked, 'week': {'used_pct': 51, 'resets_at': NOW + 86400},
            'models': {'fable': {'used_pct': 100, 'resets_at': NOW + 86400},
                       'opus': {'used_pct': 12, 'resets_at': NOW + 86400}}}


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_fable_cap_is_distinct_from_overall_allowance(language):
    source, checked = render._quota_panel(quota(), NOW)
    text = Document(bilingual.project_html(source, language)).text
    assert checked == NOW
    assert '51%' in text and '100%' in text and '12%' in text
    assert ('49% left' if language == 'en' else '剩余 49%') in text
    assert ('Fable limit reached' if language == 'en' else 'Fable额度已用完') in text
    assert ('Other models may still be available.' if language == 'en'
            else '其他模型可能仍可使用。') in text
    assert ('do not add the percentages' if language == 'en' else '请勿把百分比相加') in text
    assert 'quota-fable' in source and 'quota-opus' in source
    assert source.count('class="meter-track"') == 3


@pytest.mark.parametrize('case', ['old', 'report_stale', 'reset_passed'])
def test_old_full_model_quota_never_claims_a_current_block(case):
    data = quota(NOW - 1200 if case == 'old' else NOW)
    if case == 'reset_passed':
        data['models']['fable']['resets_at'] = NOW - 1
    source, _ = render._quota_panel(data, NOW, observation_fresh=case != 'report_stale')
    text = Document(bilingual.project_html(source, 'en')).text
    assert 'Fable limit reached' not in text
    assert 'Current remaining quota unknown' in text
    if case == 'reset_passed':
        assert 'Reset time passed' in text


def test_unreported_fable_is_unknown_and_unknown_model_labels_never_escape():
    data = quota()
    data['models'] = {'<script>PRIVATE_IDENTIFIER</script>': {'used_pct': 100}}
    source, _ = render._quota_panel(data, NOW)
    text = Document(bilingual.project_html(source, 'en')).text
    assert 'Fable limit: not reported' in text
    assert 'PRIVATE_IDENTIFIER' not in source
    assert source.count('class="meter-track"') == 1


class QuotaAncestors(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.stack, self.quota_ancestors = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'usage-bars' in values.get('class', '').split():
            self.quota_ancestors.append(tuple(self.stack))
        if tag not in {'input', 'meta', 'link', 'br', 'img', 'hr'}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]


def test_customer_slot_quota_is_visible_before_connect_and_has_one_refresh_region(cfg):
    slot = {'id': 'synthetic', 'name': 'slot-2066', 'unix_user': 'slot01',
            'state': slots.ACTIVE, 'claimed_at': NOW - 1000}
    report = {'unix_user': 'slot01', 'present': True,
              'credentials': {'logged_in': True}, 'quota': quota()}
    page = usersite._slot_card(slot, {}, {'ts': NOW, 'payload': {'slots': [report]}},
                               {}, 'csrf', cfg, NOW)
    parsed = QuotaAncestors(page)
    assert len(parsed.quota_ancestors) == 1
    assert 'details' not in parsed.quota_ancestors[0]
    assert page.count('id="slot-synthetic-quota"') == 1
    assert page.index('id="slot-synthetic-quota"') < page.index('connect-instructions')
    assert 'href="/account/usage"' in page


def test_account_transition_does_not_show_previous_model_limit(cfg):
    slot = {'id': 'synthetic', 'state': slots.ACTIVE, 'claimed_at': NOW - 1000}
    report = {'quota': quota(), 'credentials': {
        'account_fp': '1' * 16, 'bound_fp': '2' * 16, 'logged_in': True}}
    source = usersite._account_quota(slot, report, NOW, 'csrf', heard=NOW)
    assert '100%' not in source and '51%' not in source
    assert 'Current quota is unavailable' in source


def test_admin_deduplicates_verified_account_without_adding_percentages(cfg):
    row = {'id': 'node-a', 'slot_id': 'slot-a', 'slot_state': slots.ACTIVE,
           'name': 'slot-1111', 'held_name': True, 'machine': True, 'enabled': True,
           'owner': 'operator', 'credentials_email': 'private@example.test',
           'quota_account': '1' * 16, 'quota': quota()}
    older = {**row, 'id': 'node-b', 'slot_id': 'slot-b', 'name': 'slot-2222',
             'quota': quota(NOW - 20)}
    retired = {**row, 'id': 'node-c', 'enabled': False}
    page = render.render_usage_dashboard([older, row, retired], NOW, cfg, 'csrf')
    document = Document(page)
    assert page.count('class="card quota-account-card"') == 1
    assert 'slot-1111' in document.text and 'slot-2222' not in document.text
    assert 'private@example.test' in document.text
    assert 'name="usage_page" value="1"' in page
    assert not document.duplicate_ids


def test_customer_usage_page_reads_only_held_accounts(store, cfg):
    account = store.add_account('a1', 'sub-a1', 'owner@example.test',
                                slot_quota=1, now=NOW - 2000)
    other = store.add_account('a2', 'sub-a2', 'other@example.test',
                              slot_quota=1, now=NOW - 2000)
    for node, holder in [('own-node', account), ('other-node', other)]:
        store.add_node(node, holder['id'], now=NOW - 1000)
        store.hold_owner_node(node, holder['id'], now=NOW - 999)
        store.insert_heartbeat(node, NOW, {'node_id': node, 'quota': quota(),
            'credentials': {'logged_in': True, 'email': holder['email']}})
    page = usersite.usage_page(store, cfg, account, 'session', NOW)
    text = Document(bilingual.project_html(page, 'en')).text
    assert 'own-node' in text and 'owner@example.test' in text
    assert 'other-node' not in text and 'other@example.test' not in text
    assert 'Usage & limits' in text and 'Fable limit reached' in text
    assert usersite.QUOTA_POLL_TAG in page
    assert not Document(page).duplicate_ids


def test_private_owner_quota_uses_nested_native_report_and_allows_refresh(store, cfg):
    account = store.add_account('operator', 'sub-operator', 'owner@example.test',
                                role='admin', now=NOW - 2000)
    store.register_private_owner_node('private-mac', 'operator', 'nativeuser',
        monthly_cost_usd_cents=10000, now=NOW - 1000)
    creds = {'logged_in': True, 'present': True, 'bound_fp': 'a' * 16, 'account_fp': 'a' * 16}
    store.insert_heartbeat('private-mac', NOW, {'node_id': 'private-mac',
        'mode': 'private_owner', 'slots': [{'unix_user': 'nativeuser', 'present': True,
                                          'credentials': creds, 'quota': quota()}]})
    rows = render.build_rows(store.list_nodes(), store.latest_heartbeats(), [], NOW,
                              store.list_slots())
    assert rows[0]['machine'] is False and rows[0]['private_owner'] is True
    assert rows[0]['quota']['models']['fable']['used_pct'] == 100
    assert rows[0]['slot_id'] == 'private-mac'
    admin = render.render_usage_dashboard(rows, NOW, cfg, 'csrf')
    customer = usersite.usage_page(store, cfg, account, 'session', NOW)
    assert 'action="/actions/slot/private-mac/quota"' in admin
    assert 'action="/account/slots/private-mac/quota"' in customer
    assert 'Fable limit reached' in Document(bilingual.project_html(customer, 'en')).text


@pytest.mark.parametrize('change', [
    {'account_fp': 'b' * 16}, {'logged_in': False},
    {'renewal': {'state': 'blocked', 'reason': 'account_transition', 'checked_at': NOW}},
])
def test_admin_and_customer_hide_quota_when_current_account_is_unconfirmed(store, cfg, change):
    account = store.add_account('operator', 'sub-operator', 'owner@example.test',
                                role='admin', now=NOW - 2000)
    store.register_private_owner_node('private-mac', 'operator', 'nativeuser',
        monthly_cost_usd_cents=10000, now=NOW - 1000)
    creds = {'logged_in': True, 'present': True, 'bound_fp': 'a' * 16,
             'account_fp': 'a' * 16, 'email': 'previous@example.test', **change}
    store.insert_heartbeat('private-mac', NOW, {'node_id': 'private-mac',
        'mode': 'private_owner', 'slots': [{'unix_user': 'nativeuser', 'present': True,
                                          'credentials': creds, 'quota': quota()}]})
    rows = render.build_rows(store.list_nodes(), store.latest_heartbeats(), [], NOW,
                              store.list_slots())
    assert rows[0]['quota'] == {} and rows[0]['quota_account'] is None
    admin = render.render_usage_dashboard(rows, NOW, cfg, 'csrf')
    customer = usersite.usage_page(store, cfg, account, 'session', NOW)
    for source in (admin, customer):
        text = Document(bilingual.project_html(source, 'en')).text
        assert '100%' not in text and '51%' not in text
        assert 'Fable limit reached' not in text
        assert 'Current quota is unavailable' in text
    assert 'previous@example.test' not in admin


def test_admin_heartbeat_freshness_is_independent_of_quota_timestamp(store, cfg):
    store.add_node('synthetic', 'operator', now=NOW - 2000)
    # A node clock may report a fresh-looking read inside an old heartbeat.
    store.insert_heartbeat('synthetic', NOW - 300, {'node_id': 'synthetic',
        'credentials': {'logged_in': True}, 'quota': quota()})
    rows = render.build_rows(store.list_nodes(), store.latest_heartbeats(), [], NOW,
                              max_age=120)
    assert rows[0]['quota_observation_fresh'] is False
    text = Document(bilingual.project_html(
        render.render_usage_dashboard(rows, NOW, cfg), 'en')).text
    assert 'Historical reading' in text and 'Current remaining quota unknown' in text
    assert 'Fable limit reached' not in text and '49% left' not in text
