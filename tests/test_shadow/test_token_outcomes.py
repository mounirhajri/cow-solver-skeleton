from src.shadow.token_outcomes import extract_token_outcomes


def test_extracts_all_order_tokens():
    auction = {"orders": [
        {"sellToken": "0xA", "buyToken": "0xB"},
        {"sellToken": "0xC", "buyToken": "0xA"},
    ]}
    outcomes = extract_token_outcomes(auction, None, None)
    addrs = {o["token_address"] for o in outcomes}
    assert addrs == {"0xa", "0xb", "0xc"}


def test_marks_winner_tokens():
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}
    winner = {"prices": {"0xA": "1", "0xB": "1"}, "trades": []}
    outcomes = extract_token_outcomes(auction, winner, None)
    by_addr = {o["token_address"]: o for o in outcomes}
    assert by_addr["0xa"]["appeared_in_winner"]
    assert by_addr["0xb"]["appeared_in_winner"]


def test_marks_ours_separately():
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}
    winner = {"prices": {"0xA": "1", "0xB": "1"}}
    ours = {"prices": {"0xA": "1"}}  # only A appeared in ours
    outcomes = extract_token_outcomes(auction, winner, ours)
    by_addr = {o["token_address"]: o for o in outcomes}
    assert by_addr["0xa"]["appeared_in_winner"]
    assert by_addr["0xa"]["appeared_in_ours"]
    assert by_addr["0xb"]["appeared_in_winner"]
    assert not by_addr["0xb"]["appeared_in_ours"]


def test_empty_solutions_means_no_winner_or_ours():
    auction = {"orders": [{"sellToken": "0xA", "buyToken": "0xB"}]}
    outcomes = extract_token_outcomes(auction, None, None)
    for o in outcomes:
        assert not o["appeared_in_winner"]
        assert not o["appeared_in_ours"]


def test_handles_snake_case_keys():
    """Some payloads use sell_token/buy_token instead of sellToken/buyToken."""
    auction = {"orders": [{"sell_token": "0xA", "buy_token": "0xB"}]}
    outcomes = extract_token_outcomes(auction, None, None)
    addrs = {o["token_address"] for o in outcomes}
    assert addrs == {"0xa", "0xb"}
