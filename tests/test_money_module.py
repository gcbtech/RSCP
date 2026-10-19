"""app/services/money.py: parsing, formatting and rounding of integer cents,
plus a guard against float money creeping back into app code."""
import json
import os
import re
from decimal import Decimal

import pytest

from app.services import money


# --- parsing ------------------------------------------------------------------

@pytest.mark.parametrize('raw, cents', [
    ('19.99', 1999), ('$1,234.5', 123450), ('5', 500), (' 0.1 ', 10), ('.5', 50), ('5.', 500),
    ('19.995', 2000), ('19.994', 1999), ('0', 0), ('$0.01', 1), ('1,000,000', 100000000),
    (19.99, 1999), (0.1 + 0.2, 30), (10, 1000), (Decimal('2.675'), 268), (2.675, 268),
])
def test_parse(raw, cents):
    assert money.parse(raw) == cents


@pytest.mark.parametrize('raw', ['abc', '', '   ', None, '1,23', '1.2.3', 'nan', 'inf', '-inf', '--5', '1e5',
                                 '$', '.', '12a', True, float('nan'), float('inf')])
def test_parse_rejects_junk(raw):
    assert money.parse(raw) is None


def test_parse_negative_only_when_allowed():
    assert money.parse('-5') is None
    assert money.parse('-5', allow_negative=True) == -500
    assert money.parse('$-5.005', allow_negative=True) == -501   # half away from zero
    assert money.parse('-$1,234.56', allow_negative=True) == -123456


def test_parse_field_blank_is_default():
    assert money.parse_field('  ', default=7) == 7
    assert money.parse_field(None) is None
    assert money.parse_field('2.50') == 250
    assert money.parse_field('x', default=7) is None   # junk is not "blank"


def test_from_dollars_uses_str_not_binary_expansion():
    # int(x * 100) would give 1999.9999... -> 1999 and 0.29 * 100 -> 28
    assert money.from_dollars(19.999) == 2000
    assert money.from_dollars(0.29) == 29
    assert money.from_dollars(1.005) == 101    # '1.005' half-up (round(1.005, 2) gives 1.0)
    assert money.from_dollars(None) is None


# --- formatting -----------------------------------------------------------------

@pytest.mark.parametrize('cents, plain, signed', [
    (1999, '19.99', '$19.99'), (-550, '-5.50', '-$5.50'), (0, '0.00', '$0.00'), (5, '0.05', '$0.05'),
    (100000, '1000.00', '$1000.00'), (-1, '-0.01', '-$0.01'),
])
def test_dollars_and_fmt(cents, plain, signed):
    assert money.dollars(cents) == plain
    assert money.fmt(cents) == signed
    assert plain == '%.2f' % (cents / 100)     # the same text the dollar templates printed


def test_formatting_none_and_fractional_cents():
    assert money.dollars(None) == '' and money.fmt(None) == '' and money.dollars(None, '-') == '-'
    assert money.dollars(1234.5) == '12.35' and money.dollars(1234.4999) == '12.34'
    assert money.dollars_grouped(123456789) == '1,234,567.89'
    assert money.dollars_grouped(-123450) == '-1,234.50'
    assert money.whole_dollars_grouped(123450) == '1,235'


def test_every_cent_round_trips():
    for c in range(-20000, 20001):
        assert money.parse(money.dollars(c), allow_negative=True) == c


def test_json_edge_is_dollars_with_the_old_number_format():
    assert money.to_dollars(1999) == 19.99 and json.dumps(money.to_dollars(1999)) == '19.99'
    assert json.dumps(money.to_dollars(1000)) == '10.0'      # as a REAL 10.0 always serialised
    assert money.to_dollars(None) is None
    for c in range(0, 100001, 7):                            # shortest repr is the 2-decimal amount
        assert repr(money.to_dollars(c)) == repr(float(money.dollars(c)))
    assert money.dollar_fields({'sell_price_cents': 1999, 'name': 'x', 'buy_price_cents': None}) == \
        {'sell_price': 19.99, 'name': 'x', 'buy_price': None}
    assert money.to_decimal(1999) == Decimal('19.99')


# --- rounding ---------------------------------------------------------------------

def test_percentages_round_half_up():
    assert money.percent_of(1995, 10) == 200         # 199.5
    assert money.percent_of(600, 7.25) == 44         # 43.5
    assert money.percent_of(-1995, 10) == -200       # half away from zero
    assert money.percent_of(1000, 0) == 0 and money.percent_of(0, 10) == 0
    assert money.percent_of(1000, '12.5') == 125


def test_tax_rounds_half_up_on_exact_cents():
    assert money.apply_rate(2600, 0.0725) == 189     # 188.5
    assert money.apply_rate(970, 0.05) == 49         # 48.5 (float dollars gave 0.48, see money docstring)
    assert money.apply_rate(1000, 0.08375) == 84     # 83.75: a 3-decimal percent rate stays exact


def test_div_round_and_prorate():
    assert money.div_round(5, 2) == 3 and money.div_round(-5, 2) == -3
    assert money.div_round(7, 3) == 2 and money.div_round(8, 3) == 3
    assert money.div_round(5, -2) == -3
    with pytest.raises(ZeroDivisionError):
        money.div_round(1, 0)
    assert money.prorate(500, 2000, 10000) == 100
    assert money.prorate(500, 1, 0) == 0


# --- no float money in app code ---------------------------------------------------

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')

# (pattern, what it means). Money is integer cents; these are how float
# dollars used to be handled.
SUSPICIOUS = [
    (re.compile(r"round\([^()\n]*(price|total|amount|subtotal|cents|discount|tender|change|refund|paid|money)"
                r"[^()\n]*,\s*2\)"), 'round(<money>, 2)'),
    (re.compile(r"float\([^()\n]*(price|total|amount|subtotal|_cents|tender|change_given|refund|paid)[^()\n]*\)"),
     'float(<money>)'),
    (re.compile(r"(?<!%)[:%]\.2f"), "'.2f' formatting of a float"),
    (re.compile(r"int\([^()\n]*\*\s*100\s*\)"), 'int(x * 100)'),
]

# Files whose matches are deliberate:
#   migration.py  the frozen steps 2-14 run against the dollar (REAL) schema
#                 as it was then; v15 converts with Decimal
#   money.py      the module's own docstrings
ALLOWED_FILES = {'services/migration.py', 'services/money.py'}
# Individual lines that are not money (or format a non-money float).
ALLOWED_LINES = (
    "return '' if cents is None else round(cents / 100, 2)",   # Top Sellers CSV: fractional cents -> dollars
    "period_hours = round(totals['regular'] + totals['overtime'], 2)",   # timeclock hours
)


def test_no_float_money_in_app_code():
    hits = []
    for root, _dirs, files in os.walk(APP_DIR):
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, APP_DIR).replace(os.sep, '/')
            if rel in ALLOWED_FILES:
                continue
            with open(path, encoding='utf-8') as f:
                for n, line in enumerate(f, 1):
                    code = line.split('#', 1)[0]
                    if any(a in line for a in ALLOWED_LINES):
                        continue
                    for pattern, meaning in SUSPICIOUS:
                        if pattern.search(code):
                            hits.append(f'app/{rel}:{n}: {meaning}: {line.strip()}')
    assert not hits, 'float money in app code (use app/services/money.py):\n' + '\n'.join(hits)
