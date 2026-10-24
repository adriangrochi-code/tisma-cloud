"""Payments of the plans, on the master: the account page asks a payment provider for a checkout (the card or the
account of the user are entered at the provider, never here), and the provider tells the master each payment
(webhook). A payment is applied once: the plan of the user is paid one more month.

The provider is behind a small interface (Provider), so that the real one (to be chosen: Mercado Pago, dLocal, PayPal...)
is added in one place. "test" is a fake provider to try the whole flow; "manual" are the payments the administrator
records (transfer, cash).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from .models import Payment, Plan, User, utcnow

# A paid month (the subscription moves paid_until forward this much per month paid).
MONTH = timedelta(days=30)


class BillingError(Exception):
    pass


@dataclass
class Notice:
    """What a provider says about a payment, after the master verified that the notice is genuine."""

    payment_id: str          # our Payment.id, sent to the provider as the external reference
    provider_ref: str        # id of the payment at the provider
    status: str              # approved, rejected, pending
    amount_cents: int
    currency: str


class Provider:
    name = ""

    def checkout(self, payment: Payment, plan: Plan, user: User, return_url: str) -> str:
        """The address of the page of the provider where the user pays."""
        raise NotImplementedError

    def notices(self, headers: dict[str, str], query: dict[str, str], body: bytes) -> list[Notice]:
        """The payments of a notification of the provider; raises BillingError when it is not genuine."""
        raise NotImplementedError


class TestProvider(Provider):
    """A fake provider: its "checkout" is a page of the master with Approve / Reject buttons (TISMA_BILLING_PROVIDER=test).
    Never in production: it would let anyone approve their own payment."""

    name = "test"

    def __init__(self, public_url: str) -> None:
        self.public_url = public_url

    def checkout(self, payment: Payment, plan: Plan, user: User, return_url: str) -> str:
        return f"{self.public_url}/billing/test/{payment.id}"

    def notices(self, headers, query, body) -> list[Notice]:
        raise BillingError("The test provider has no webhook")


def provider_for(name: str, public_url: str) -> Provider | None:
    if name == "test":
        return TestProvider(public_url)
    return None


def start_payment(session: Session, user: User, plan: Plan, provider: str, months: int = 1,
                  extra_printers: int = 0) -> Payment:
    if months < 1 or months > 12:
        raise BillingError("Between 1 and 12 months")
    if extra_printers < 0 or extra_printers > 100:
        raise BillingError("Between 0 and 100 more printers")
    if extra_printers and plan.extra_printer_cents <= 0:
        raise BillingError("The plan has no more remote printers")
    monthly = plan.price_cents + extra_printers * plan.extra_printer_cents
    payment = Payment(user_id=user.id, plan_name=plan.name, months=months, extra_printers=extra_printers,
                      amount_cents=monthly * months, currency=plan.currency, provider=provider)
    session.add(payment)
    session.flush()
    return payment


def apply(session: Session, notice: Notice, provider: str) -> Payment | None:
    """Applies a notice of a provider. Idempotent: a payment already approved is not applied again. The amount and the
    currency must be the ones the master asked for."""
    payment = session.get(Payment, notice.payment_id)
    if payment is None or payment.provider != provider:
        raise BillingError("Unknown payment")
    if payment.status == "approved":
        return payment
    if notice.status == "approved":
        if notice.amount_cents != payment.amount_cents or notice.currency.upper() != payment.currency.upper():
            raise BillingError("The amount paid is not the price of the plan")
        payment.provider_ref = notice.provider_ref or None
        approve(session, payment)
    elif notice.status == "rejected":
        payment.status = "rejected"
    return payment


def approve(session: Session, payment: Payment) -> None:
    user = session.get(User, payment.user_id)
    now = utcnow()
    start = user.paid_until if user.paid_until and user.paid_until > now else now
    user.paid_until = start + MONTH * payment.months
    user.plan_name = payment.plan_name
    user.extra_printers = payment.extra_printers
    payment.status = "approved"
    payment.paid_at = now


def record_manual(session: Session, user: User, plan: Plan, months: int, amount_cents: int, currency: str,
                  note: str, extra_printers: int = 0) -> Payment:
    """A payment received outside the provider (transfer, cash), recorded by the administrator."""
    payment = start_payment(session, user, plan, "manual", months, extra_printers)
    payment.amount_cents = amount_cents
    payment.currency = currency.upper()[:3]
    payment.note = note[:200]
    approve(session, payment)
    return payment
