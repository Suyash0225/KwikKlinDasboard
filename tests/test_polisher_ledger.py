from decimal import Decimal

from app.routers.polisher import BatchIn, PaymentIn, ReturnIn, VendorIn


def test_default_polisher_rate_is_twenty_five_rupees():
    vendor = VendorIn(name="Saree Polisher", phone="9876543210")
    assert vendor.rate_per_saree == Decimal("25.00")


def test_ten_sarees_at_default_rate_cost_two_hundred_fifty():
    vendor = VendorIn(name="Saree Polisher", phone="9876543210")
    batch = BatchIn(vendor_id="00000000-0000-0000-0000-000000000001", quantity_sent=10, sent_on="2026-10-20")
    assert Decimal(batch.quantity_sent) * vendor.rate_per_saree == Decimal("250.00")


def test_returned_quantity_cannot_be_negative():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReturnIn(quantity_returned=-1)


def test_payment_must_be_positive():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PaymentIn(vendor_id="00000000-0000-0000-0000-000000000001", amount=0, paid_on="2026-10-20")


def test_vendor_rate_must_be_positive():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        VendorIn(name="Saree Polisher", phone="9876543210", rate_per_saree=0)
