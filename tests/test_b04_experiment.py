"""B04: the published results file has the contract's fields and the headline numbers hold."""
import json
import pathlib

RES = pathlib.Path(__file__).resolve().parents[1] / "experiments" / "b04_valve" / "results.json"


def test_results_contract_fields():
    d = json.loads(RES.read_text())
    for k in ("experiment", "date", "method", "params", "results", "limitations"):
        assert k in d
    assert d["params"]["trials_per_cell"] >= 500
    assert len(d["results"]) == 8
    for cond, by in d["results"].items():
        for design in ("fire_and_forget", "ack_based", "state_machine"):
            assert by[design]["runs"] == d["params"]["trials_per_cell"]


def test_headline_numbers():
    r = json.loads(RES.read_text())["results"]
    for cond in r:
        assert r[cond]["state_machine"]["false_closed_claims_pct"] == 0.0
        assert r[cond]["state_machine"]["explanation_statements_matching_a_record_pct"] == 100.0
    for cond in ("valve_stuck", "valve_partial"):
        assert r[cond]["fire_and_forget"]["false_closed_claims_pct"] == 100.0
        assert r[cond]["ack_based"]["false_closed_claims_pct"] == 100.0
        assert r[cond]["state_machine"]["of_those_escalated_within_60s_pct"] == 100.0
