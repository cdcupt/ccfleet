"""Customer instructions must match the active billing mode in each language."""

from __future__ import annotations

import pytest

from ccfleetd import bilingual, customer_docs, usersite
from ccfleetd.config import Config


@pytest.mark.parametrize(
    "render",
    [customer_docs.overview, customer_docs.guide, customer_docs.how_it_works, customer_docs.terms],
)
@pytest.mark.parametrize("language", ["en", "zh"])
def test_credit_mode_gives_customer_the_balance_purchase_flow_without_old_allowance(
    render, language
):
    kwargs = dict(wallet_enabled=True)
    if render is not customer_docs.how_it_works:
        kwargs["per_slot_pricing"] = True
    body = bilingual.project_html(render(Config(), **kwargs), language)
    if language == "en":
        assert "credit" in body and 'href="/account"' in body
        assert "Claim a slot" not in body
        assert "Claim this slot" not in body
        assert "within your allowance" not in body
        assert "pay the operator directly" not in body
    else:
        assert "余额" in body and "/zh/account" in body
        assert "领取此槽位" not in body
        assert "在自己的额度内" not in body
        assert 'class="copy-en"' not in body


@pytest.mark.parametrize("language", ["en", "zh"])
def test_credit_terms_explain_renewal_expiry_grandfathering_and_refund_boundaries(language):
    body = bilingual.project_html(customer_docs.terms(Config(), wallet_enabled=True), language)
    if language == "en":
        for phrase in (
            "starting at purchase",
            "new Fleet access pauses",
            "cannot be transferred",
            "Existing slots",
            "does not itself",
            "no automatic renewal",
        ):
            assert phrase in body
        assert "operator may take a slot back" not in body
    else:
        for phrase in (
            "购买时起",
            "新的 Fleet 访问暂停",
            "不能转让或提现",
            "已有的槽位",
            "不会自动续费或自动充值",
        ):
            assert phrase in body
        assert "逾期未付款时，运营者可能收回" not in body


def test_disabled_mode_keeps_current_manual_contract():
    body = bilingual.project_html(customer_docs.overview(Config(), wallet_enabled=False), "en")
    assert "Claim a slot" in body
    assert "USD service credit" not in body
    terms = bilingual.project_html(customer_docs.terms(Config()), "en")
    assert "Agree the quote and paid period directly with the operator" in terms
    assert "new Fleet access pauses" not in terms


@pytest.mark.parametrize("language", ["en", "zh"])
def test_enabled_credit_privacy_explains_billing_data_processor_and_retention(language):
    enabled = bilingual.project_html(
        usersite.privacy_page(Config(usd_slot_credit_enabled=True)), language
    )
    disabled = bilingual.project_html(usersite.privacy_page(Config()), language)
    assert 'href="https://stripe.com/privacy"' in enabled
    assert 'href="https://stripe.com/privacy"' not in disabled
    if language == "en":
        for words in ("append-only", "do not expire automatically", "account reference",
                      "does not store card numbers", "online top-up is available"):
            assert words in enabled
    else:
        for words in ("追加方式保存", "不会自动过期", "账号标识", "不保存银行卡号", "在线充值开通后"):
            assert words in enabled
