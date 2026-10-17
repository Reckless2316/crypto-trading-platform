"""Exact loss authority; Decimal fractions are reports, never limit inputs."""
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from .contracts import ContractViolation, decimal_text


def _positive(value: Decimal) -> Fraction:
    decimal_text(value)
    if value <= 0:
        raise ContractViolation('invalid loss baseline')
    return Fraction(value)


def loss_fractions(nav: Decimal, high_water: Decimal, day_open: Decimal) -> tuple[Decimal, Decimal]:
    n,h,d=map(_positive,(nav,high_water,day_open))
    with localcontext(Context(prec=50)):
        return tuple(Decimal(x.numerator)/Decimal(x.denominator) for x in
            (max(Fraction(0),1-n/h),max(Fraction(0),1-n/d)))


def loss_triggers(nav: Fraction, high_water: Fraction, day_open: Fraction) -> tuple[bool,bool]:
    if any(type(x) is not Fraction or x<=0 for x in (nav,high_water,day_open)):
        raise ContractViolation('invalid exact loss baseline')
    # Fraction comparison uses integer cross products, independent of Decimal context.
    return 100*nav<=85*high_water,100*nav<=98*day_open
