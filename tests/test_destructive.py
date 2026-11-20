"""
Destructive-intent detection must be NARROW. The regression test that matters is
the first one: "why did our success rate drop last week" is this product's single
most important question, and a naive \bdrop\b pattern refuses it.
"""
import pytest

from backend.core.resolver import destructive_intent

MUST_ALLOW = [
    "why did our overall success rate drop last week",
    "why did success rate drop on netbanking",
    "what caused the drop in approval rate",
    "show me the biggest drop by issuer bank",
    "did GMV drop last month",
    "which merchants saw a sharp drop in volume",
    "how many transactions were removed from settlement batches",
    "update on payment performance",            # 'update' as a noun, not a command
    "success rate excluding dropped sessions",
]
MUST_REFUSE = [
    "delete all failed transactions",
    "Delete the transactions table",
    "please delete everything",
    "drop table fact_transactions",
    "drop the table dim_bank",
    "truncate table fact_settlements",
    "alter table fact_transactions add column x",
    "insert into dim_bank values (1)",
    "update fact_transactions set status = 'SUCCESS'",
    "wipe the database",
    "purge all records",
]


@pytest.mark.parametrize("q", MUST_ALLOW)
def test_analytics_questions_are_not_refused(q):
    assert destructive_intent(q) is None, f"{q!r} was wrongly flagged as destructive"


@pytest.mark.parametrize("q", MUST_REFUSE)
def test_mutations_are_refused(q):
    assert destructive_intent(q) is not None, f"{q!r} should have been refused"
