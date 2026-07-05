from datetime import date

from watcher import classify_context, date_variants, inspect_payload, text_has_date


def test_date_variants_include_english_and_italian():
    variants = date_variants(date(2026, 7, 18))
    assert "18 july 2026" in variants
    assert "18 luglio 2026" in variants
    assert "2026-07-18" in variants


def test_text_has_date():
    assert text_has_date("Saturday, July 18, 2026", date(2026, 7, 18))
    assert text_has_date("18 luglio 2026", date(2026, 7, 18))


def test_negative_wording_wins():
    positive, _ = classify_context("18 July 2026 - not available", min_tickets=2)
    assert not positive


def test_quantity_is_positive():
    positive, _ = classify_context("18 July 2026 - 3 tickets available", min_tickets=2)
    assert positive


def test_json_payload_available():
    payload = '{"date":"2026-07-18","available":true,"seats":4}'
    result = inspect_payload(payload, date(2026, 7, 18), min_tickets=2)
    assert result is not None
    assert result[0]


def test_json_payload_sold_out():
    payload = '{"date":"2026-07-18","status":"sold out","seats":0}'
    result = inspect_payload(payload, date(2026, 7, 18), min_tickets=2)
    assert result is None


def test_json_payload_available_false_is_not_positive():
    payload = '{"date":"2026-07-18","available":false,"seats":0}'
    result = inspect_payload(payload, date(2026, 7, 18), min_tickets=2)
    assert result is None


def test_unrelated_ticket_word_is_not_positive():
    positive, _ = classify_context("18 July 2026 - admission tickets information", min_tickets=2)
    assert not positive
