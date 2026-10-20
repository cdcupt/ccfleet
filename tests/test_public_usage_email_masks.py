"""Only bounded email fragments can cross the public ranking boundary."""

import json
from copy import deepcopy

import pytest

from ccfleetd import usage, usersite
from ccfleetd.config import Config
from tests.test_html_document_preservation import Document
from tests.test_public_usage import NOW, fleet_of, reading
from tests.test_public_usage_ranking import ranking, shown


def measure(fleet):
    return usage.public_summary(fleet, Config(heartbeat_max_age_s=300,
                                             cookie_secret="test-public-member-key"), NOW)


@pytest.mark.parametrize("email,mask", [
    ("cdcupt@example.test", "cd•••pt"),
    ("abcd@example.test", "a•••d"),
    ("abcde@example.test", "a•••e"),
    ("ABCDEF@example.test", "AB•••EF"),
    ("ab.cd+ef@example.test", "ab•••ef"),
    ("a_b-cd@example.test", "a_•••cd"),
])
def test_only_local_part_edges_are_returned(email, mask):
    assert usage._masked_email(email) == mask
    local, domain = email.split("@")
    assert local not in mask and domain not in mask and "@" not in mask


@pytest.mark.parametrize("email", [
    None, True, 17, {}, [], "a@example.test", "ab@example.test", "abc@example.test",
    "用户名字@example.test", "abcdef@例子.test", "ab\u202ecd@example.test",
    "ab\u200bcd@example.test", "abcdef@example.test\n", "ab\x00cd@example.test",
    " abcd@example.test", "abcd @example.test", '<abcd>@example.test',
    '"abcd"@example.test', "abcd@@example.test", "abcd@example", "abcd@.test",
    "abcd@example..test", "abcd@-example.test", "abcd@example-.test",
    ".abcd@example.test", "abcd.@example.test", "ab..cd@example.test",
    "abcd@example.123", "a" * 65 + "@example.test",
    "abcd@" + "a" * 64 + ".test", "abcd@" + ".".join(["a" * 60] * 5) + ".test",
])
def test_unsupported_or_ambiguous_addresses_use_existing_fallback(email):
    assert usage._masked_email(email) is None


def identified_fleet(email, handle="nickname"):
    fleet = fleet_of([reading()], holders=["PRIVATE_ACCOUNT_IDENTIFIER"])
    fleet.list_accounts = lambda: [{"id": "PRIVATE_ACCOUNT_IDENTIFIER", "handle": handle,
                                   "email": email}]
    return fleet


def test_public_dto_and_html_prefer_email_fragment_without_raw_identity():
    email = "distinctiveprivateperson@example-private.test"
    fleet = identified_fleet(email)
    report = measure(fleet)
    entry = report["entries"][0]
    assert entry["masked_email"] == "di•••on"
    assert entry["masked_handle"] == "n***e"
    serialized = json.dumps(report)
    for private in (email, *email.split("@"), "PRIVATE_ACCOUNT_IDENTIFIER"):
        assert private not in serialized
    for language in ("en", "zh"):
        page = shown(report, language)
        assert "di•••on" in Document(page).text
        assert "n***e" not in page
        for private in (email, *email.split("@"), "PRIVATE_ACCOUNT_IDENTIFIER"):
            assert private not in page
        assert 'href="mailto:' not in page


def test_email_changes_do_not_change_member_code_counters_rank_or_reference():
    before = measure(identified_fleet("distinctiveprivateperson@example-private.test"))
    after = measure(identified_fleet("replacementprivateperson@different-private.test"))
    assert before["entries"][0]["masked_email"] != after["entries"][0]["masked_email"]
    before["entries"][0].pop("masked_email")
    after["entries"][0].pop("masked_email")
    assert before == after


@pytest.mark.parametrize("email", [None, "abc@example.test", "not an address",
                                    "用户名字@example.test"])
@pytest.mark.parametrize("handle", ["nickname", None])
def test_missing_or_unsupported_email_keeps_nickname_or_member_and_measurements(email, handle):
    report = measure(identified_fleet(email, handle))
    entry = report["entries"][0]
    assert entry["masked_email"] is None
    assert entry["rank"] == 1 and entry["tokens"]["total_tokens"] > 0
    assert (entry["masked_handle"] or entry["member_code"]) in Document(shown(report)).text


def test_failed_account_lookup_keeps_only_purpose_specific_member_fallback():
    fleet = identified_fleet("distinctiveprivateperson@example-private.test")

    def unavailable():
        raise RuntimeError("PRIVATE_ACCOUNT_LOOKUP_ERROR")

    fleet.list_accounts = unavailable
    report = measure(fleet)
    entry = report["entries"][0]
    assert entry["masked_email"] is None and entry["masked_handle"] is None
    assert entry["member_code"] in Document(shown(report)).text
    assert "PRIVATE_" not in json.dumps(report) + shown(report)


@pytest.mark.parametrize("mask", [
    "PRIVATE_EMAIL@example.test", "<script>PRIVATE_EMAIL</script>",
    "ab•••cd@example.test", "ab•••cd\n", "ab••cd", "ab••••cd",
    "abc•••def", "a•••cd", "ab•••c", "a" * 10000, "用户•••名字", None, {}, True,
])
def test_renderer_revalidates_mask_and_never_uses_injected_raw_email(mask):
    report = ranking()
    report["entries"] = [report["entries"][0]]
    report["entries"][0].update(masked_email=mask, email="PRIVATE_EMAIL@example.test")
    before = deepcopy(report)
    page = shown(report)
    assert "a***e" in Document(page).text
    assert "PRIVATE_" not in page and 'mailto:' not in page
    assert report == before


@pytest.mark.parametrize("language,phrases", [
    ("en", ("masked email local-part fragment", "without its domain", "not anonymity")),
    ("zh", ("邮箱用户名的局部遮盖片段", "不展示邮箱域名", "不等于匿名")),
])
def test_public_method_and_privacy_disclose_fragments_and_reidentification(language, phrases):
    from ccfleetd import bilingual

    method = Document(shown(ranking(), language)).text
    privacy = Document(bilingual.project_html(usersite.privacy_page(Config()), language,
                                               "/privacy")).text
    for phrase in phrases:
        assert phrase in method
    assert "cd•••pt" in privacy
    assert ("domain" if language == "en" else "域名") in privacy
