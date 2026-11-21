"""M3-A: deterministic DAG validation (pure unit tests: no database, no model, no network)."""

import itertools
import random

import pytest

from agentplatform.dag import MAX_DEPENDENCIES_PER_TASK, MAX_TASKS, DagResult, TaskNode, validate_dag


def nodes(spec: dict) -> list:
    return [TaskNode(key, tuple(deps)) for key, deps in spec.items()]


def codes(result: DagResult) -> list:
    return [e.code for e in result.errors]


def test_a_chain_orders_and_levels_deterministically():
    result = validate_dag(nodes({"a": [], "b": ["a"], "c": ["b"]}))
    assert result.valid and result.order == ("a", "b", "c") and result.levels == (("a",), ("b",), ("c",))
    assert result.level_of == {"a": 0, "b": 1, "c": 2}


def test_independent_tasks_share_a_level_in_input_order():
    result = validate_dag(nodes({"research": [], "pricing": [], "audience": [], "plan": ["research", "pricing", "audience"]}))
    assert result.levels == (("research", "pricing", "audience"), ("plan",))  # input order inside a level
    assert result.order == ("research", "pricing", "audience", "plan")


def test_levels_are_longest_path_layers_not_first_ready():
    # d depends on a (level 0) and c (level 2): it must wait for the deepest dependency
    result = validate_dag(nodes({"a": [], "b": ["a"], "c": ["b"], "d": ["a", "c"], "e": ["a"]}))
    assert result.level_of == {"a": 0, "b": 1, "e": 1, "c": 2, "d": 3}
    assert result.levels == (("a",), ("b", "e"), ("c",), ("d",))
    assert all(d in result.level_of and result.level_of[d] < result.level_of[k] for k, deps in
               {"b": ["a"], "c": ["b"], "d": ["a", "c"], "e": ["a"]}.items() for d in deps)  # every dependency is on an earlier level


def test_a_diamond():
    result = validate_dag(nodes({"top": [], "left": ["top"], "right": ["top"], "join": ["left", "right"]}))
    assert result.levels == (("top",), ("left", "right"), ("join",))


def test_the_result_does_not_depend_on_how_dependencies_are_listed():
    base = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"], "e": ["d", "a"]}
    expected = validate_dag(nodes(base))
    for _ in range(25):
        shuffled = {k: random.sample(v, len(v)) for k, v in base.items()}
        assert validate_dag(nodes(shuffled)) == expected  # equivalent plans, identical results


def test_input_order_is_the_only_tie_break_and_it_is_stable():
    a = validate_dag(nodes({"x": [], "y": [], "z": []}))
    b = validate_dag(nodes({"z": [], "y": [], "x": []}))
    assert a.order == ("x", "y", "z") and b.order == ("z", "y", "x")  # position decides, nothing else (no hashing, no sets)
    assert validate_dag(nodes({"x": [], "y": [], "z": []})) == a  # repeated runs are identical


def test_every_permutation_of_a_valid_plan_is_valid_and_respects_dependencies():
    spec = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
    for order in itertools.permutations(spec):
        result = validate_dag(nodes({k: spec[k] for k in order}))
        assert result.valid
        position = {k: i for i, k in enumerate(result.order)}
        assert all(position[d] < position[k] for k, deps in spec.items() for d in deps)


@pytest.mark.parametrize("spec,expected_cycle", [
    ({"a": ["b"], "b": ["a"]}, ["a", "b", "a"]),
    ({"a": ["b"], "b": ["c"], "c": ["a"]}, ["a", "b", "c", "a"]),
    ({"start": [], "a": ["start", "c"], "b": ["a"], "c": ["b"]}, ["a", "c", "b", "a"]),
])
def test_cycles_are_reported_with_a_deterministic_path(spec, expected_cycle):
    result = validate_dag(nodes(spec))
    assert not result.valid and codes(result) == ["cycle"] and result.order == () and result.levels == ()
    assert result.errors[0].message.endswith(" -> ".join(expected_cycle) + ".")
    assert validate_dag(nodes(spec)) == result  # the same answer every time


def test_self_missing_duplicate_and_invalid_references_are_all_reported_together():
    result = validate_dag([TaskNode("a", ("a",)), TaskNode("b", ("ghost",)), TaskNode("c", ("a", "a")), TaskNode("a"), TaskNode("Bad Id"),
                           TaskNode("d", (), state="running")])
    assert sorted(set(codes(result))) == ["duplicate_dependency", "duplicate_task_id", "invalid_state", "invalid_task_id", "missing_dependency", "self_dependency"]
    assert {e.task for e in result.errors if e.code == "missing_dependency"} == {"b"}
    assert not result.valid and result.order == ()
    assert "cycle" not in codes(result)  # a graph with broken references is not also analysed for cycles


def test_each_rule_alone():
    assert codes(validate_dag([])) == ["empty_plan"]
    assert codes(validate_dag(nodes({"a": ["a"]}))) == ["self_dependency"]
    assert codes(validate_dag(nodes({"a": [], "b": ["zzz"]}))) == ["missing_dependency"]
    assert codes(validate_dag([TaskNode("a"), TaskNode("a")])) == ["duplicate_task_id"]
    assert codes(validate_dag(nodes({"a": [], "b": ["a", "a"]}))) == ["duplicate_dependency"]
    for state in ("running", "succeeded", "ready", "", "PLANNED"):
        assert codes(validate_dag([TaskNode("a", state=state)])) == ["invalid_state"], state
    for bad in ("", "A", "1a", "a b", "a" * 33, "a/b", "a.b", "é"):
        assert codes(validate_dag([TaskNode(bad)])) == ["invalid_task_id"], bad
    assert validate_dag([TaskNode("a" * 32), TaskNode("b_1-x", ("a" * 32,))]).valid


def test_size_limits():
    assert validate_dag(nodes({f"t{i}": [] for i in range(MAX_TASKS)})).valid
    too_many = validate_dag(nodes({f"t{i}": [] for i in range(MAX_TASKS + 1)}))
    assert "too_many_tasks" in codes(too_many)
    spec = {f"d{i}": [] for i in range(MAX_DEPENDENCIES_PER_TASK + 1)}
    spec["hub"] = list(spec)
    assert codes(validate_dag(nodes(spec))) == ["too_many_dependencies"]
    ok = {f"d{i}": [] for i in range(MAX_DEPENDENCIES_PER_TASK)}
    ok["hub"] = list(ok)
    assert validate_dag(nodes(ok)).valid


def test_error_messages_are_fixed_text_and_errors_are_sorted():
    result = validate_dag([TaskNode("z", ("a",)), TaskNode("m", ("nope",)), TaskNode("a", ("a",))])
    keys = [(e.code, e.task or "", e.message) for e in result.errors]
    assert keys == sorted(keys)  # deterministic order
    assert [e.code for e in result.errors] == sorted(e.code for e in result.errors)
    for e in result.errors:
        assert e.message and e.message[0].isupper() and e.message.endswith(".")


def test_a_large_valid_graph_stays_fast_and_correct():
    spec = {"t0": []}
    for i in range(1, MAX_TASKS):
        spec[f"t{i}"] = [f"t{i - 1}"] + ([f"t{i - 2}"] if i > 1 else [])
    result = validate_dag(nodes(spec))
    assert result.valid and result.order == tuple(f"t{i}" for i in range(MAX_TASKS)) and len(result.levels) == MAX_TASKS
