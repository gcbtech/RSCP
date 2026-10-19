"""
Money: integer cents everywhere inside RSCP.

Convention (schema v15 onward)
------------------------------
* Database: every money column is an INTEGER number of cents and its name
  ends in ``_cents`` (inventory_items.sell_price_cents, pos_orders.total_cents,
  pos_payments.amount_cents, ...). Settings likewise (POS_MAX_DISCOUNT_CENTS,
  CASH_DISCOUNT_CENTS). Nothing stores dollars any more, so nothing can read
  cents as dollars by accident: a plain ``total`` no longer exists.
* Python: money is an ``int`` of cents, in names ending in ``_cents``. All
  arithmetic is integer arithmetic; a result that needs rounding (a
  percentage, a tax, a pro-rata share) goes through percent_of / apply_rate /
  div_round below, which round half-up (half away from zero) exactly as the
  old Decimal ROUND_HALF_UP code did. Floats never hold money.
* Percentages are not money and are not converted: tax rates stay a fraction
  (0.0725), percent discounts/coupons/cash discounts stay a percent number
  (10 = 10%). They go into Decimal through str(), so 7.25 is exactly 7.25.
  (Basis points would be lossy: real tax rates such as 8.375% are 837.5 bp.)
* Edges -- everything a person or another program sees -- are in dollars:
    - form inputs are dollar strings, parsed with parse() / parse_field();
    - templates format cents with the ``dollars`` ("19.99") and ``money``
      ("$19.99") Jinja filters (registered in create_app);
    - JSON is dollars: RSCP's own page JS (static/js/pages/*.js) and the
      public storefront API keep the field names and numbers they always had
      (19.99, 10.0). to_dollars() is the one place a cents value becomes a
      JSON number. JSON *input* (e.g. a price typed into Edit Sale) is parsed
      with parse(), like a form field.
  Pro-rata analytics in the Top Sellers report (a line's share of an order
  discount, margins, averages) are ratios rather than amounts anyone paid;
  they are computed as fractional cents and rounded only for display.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
import re

_CENT = Decimal('0.01')
_ONE = Decimal('1')
_HUNDRED = Decimal('100')

# "$1,234.50", "-5", " 19.99 ", "(not "1.2.3", "abc", "1e5")
_DOLLARS_RE = re.compile(r'^[+-]?\$?[+-]?(\d{1,3}(,\d{3})+|\d*)(\.\d*)?$')


def _decimal(value):
    """Decimal for a dollar amount given as text or a number, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, float)):
        try:
            d = Decimal(str(value))   # str(): 19.99 -> '19.99', never the binary expansion
        except (InvalidOperation, ValueError):
            return None
    else:
        text = str(value).strip().replace(' ', '')
        if (not text or not _DOLLARS_RE.match(text) or not re.search(r'\d', text)
                or text.count('-') + text.count('+') > 1):
            return None
        negative = text.count('-') == 1
        text = text.replace('$', '').replace(',', '').replace('+', '').replace('-', '')
        try:
            d = Decimal(text)
        except InvalidOperation:
            return None
        if negative:
            d = -d
    if not d.is_finite():
        return None
    return d


def from_dollars(value):
    """Cents (int) for a dollar amount (text, int, float or Decimal), rounded
    half-up to the cent; None for blank/invalid input.

    Used for stored legacy dollar values (migration, carts saved before v15)
    and as the core of parse(). Floats go through str(), so 19.995 -> 2000
    and 0.1 + 0.2 -> 30, never int(x * 100) truncation.
    """
    d = _decimal(value)
    if d is None:
        return None
    return int(d.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)


def parse(raw, allow_negative=False):
    """Parse money a person typed ("19.99", "$1,234.5", "5") into cents.

    Returns None when it is not a number, or negative while allow_negative
    is False. More than two decimals round half-up ("19.995" -> 2000).
    """
    cents = from_dollars(raw)
    if cents is None or (cents < 0 and not allow_negative):
        return None
    return cents


def parse_field(raw, default=None, allow_negative=False):
    """parse() for an optional field: blank/missing -> default."""
    if raw is None or str(raw).strip() == '':
        return default
    return parse(raw, allow_negative=allow_negative)


def to_dollars(cents):
    """Cents -> a JSON-ready dollar number (1999 -> 19.99, 1000 -> 10.0).

    Only for serialising at an edge (JSON). int / 100 is correctly rounded,
    so the float's shortest repr is exactly the two-decimal amount.
    """
    if cents is None:
        return None
    return int(cents) / 100


def dollar_fields(row):
    """A dict copy of `row` for JSON: every `<name>_cents` field becomes
    `<name>` in dollars (sell_price_cents: 1999 -> sell_price: 19.99)."""
    out = {}
    for key, value in dict(row).items():
        if key.endswith('_cents'):
            out[key[:-6]] = to_dollars(value)
        else:
            out[key] = value
    return out


def to_decimal(cents):
    """Cents -> Decimal dollars (1999 -> Decimal('19.99')), e.g. for Excel."""
    if cents is None:
        return None
    return (Decimal(int(cents)) / _HUNDRED).quantize(_CENT)


def _round_cents(value):
    """Round a fractional cents value (Fraction/Decimal/float) half-up to int."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        value = Decimal(str(value))
    elif isinstance(value, Fraction):
        return div_round(value.numerator, value.denominator)
    return int(Decimal(value).quantize(_ONE, rounding=ROUND_HALF_UP))


def dollars(cents, default=''):
    """Cents -> "19.99" (no currency sign; "-5.50" when negative).

    Same text as the old '%.2f' % dollars. Accepts fractional cents
    (analytics) and rounds them half-up. None -> default.
    """
    if cents is None or cents == '':
        return default
    c = _round_cents(cents)
    sign = '-' if c < 0 else ''
    c = abs(c)
    return f'{sign}{c // 100}.{c % 100:02d}'


def dollars_grouped(cents, default=''):
    """Cents -> "1,234.56" (thousands separators, like '{:,.2f}')."""
    if cents is None or cents == '':
        return default
    c = _round_cents(cents)
    sign = '-' if c < 0 else ''
    c = abs(c)
    return f'{sign}{c // 100:,}.{c % 100:02d}'


def whole_dollars_grouped(cents, default=''):
    """Cents -> whole dollars with separators ("1,235"), like '{:,.0f}'."""
    if cents is None or cents == '':
        return default
    c = _round_cents(cents)
    return f'{div_round(c, 100):,}'


def fmt(cents, default=''):
    """Cents -> "$19.99"; negatives "-$19.99"."""
    if cents is None or cents == '':
        return default
    c = _round_cents(cents)
    return ('-$' if c < 0 else '$') + dollars(abs(c))


def div_round(numerator, denominator):
    """numerator / denominator for integers, rounded half away from zero."""
    numerator, denominator = int(numerator), int(denominator)
    if denominator == 0:
        raise ZeroDivisionError('div_round by zero')
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    q, r = divmod(abs(numerator), denominator)
    if 2 * r >= denominator:
        q += 1
    return q if numerator >= 0 else -q


def percent_of(cents, percent):
    """`percent`% of `cents`, rounded half-up to the cent (10% of 1995 -> 200).

    `percent` is a percent number (7.25 means 7.25%), read exactly via str().
    """
    if not cents or not percent:
        return 0
    p = _decimal(percent)
    if p is None:
        return 0
    return int((Decimal(int(cents)) * p / _HUNDRED).quantize(_ONE, rounding=ROUND_HALF_UP))


def apply_rate(cents, rate):
    """`cents` times a fractional rate (tax: rate 0.0725), rounded half-up."""
    if not cents or not rate:
        return 0
    r = _decimal(rate)
    if r is None:
        return 0
    return int((Decimal(int(cents)) * r).quantize(_ONE, rounding=ROUND_HALF_UP))


def prorate(cents, part, whole):
    """cents * part / whole, rounded half-up (0 when whole is 0)."""
    if not whole:
        return 0
    return div_round(int(cents) * int(part), int(whole))
