"""Deterministic DAG validation for plans (M3-A). Pure functions: no database, no model, no clock.

A plan is a set of tasks with explicit ``depends_on`` edges. ``validate_dag`` either returns a ``DagResult`` with a stable
topological order and the independent-task grouping, or a list of ``DagError``s describing EVERYTHING that is wrong
(never just the first problem), so a user or a revision can be corrected in one pass.

DETERMINISM. Equivalent plans give identical results: ties are always broken by the task's position in the input and then
by its id, and error lists are sorted. Levels are the longest-path layers: level 0 holds the tasks with no dependencies and
a task sits one level below its deepest dependency, so all tasks within one level are independent of each other and could
run together later. Nothing is executed here.
"""

import re
from dataclasses import dataclass, field

MAX_TASKS = 12
MAX_DEPENDENCIES_PER_TASK = 8
TASK_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VALID_STATES = frozenset({"planned"})  # M3-A tasks are only ever planned; execution states belong to the M3-B run model


@dataclass(frozen=True)
class TaskNode:
    key: str
    depends_on: tuple = ()
    state: str = "planned"


@dataclass(frozen=True)
class DagError:
    code: str  # stable machine-readable code
    task: str | None  # the task the problem is about (None for plan-level problems)
    message: str  # fixed, user-readable text; never echoes untrusted content beyond the (validated) task key


@dataclass(frozen=True)
class DagResult:
    order: tuple = ()  # task keys, a stable topological order
    levels: tuple = ()  # tuple of tuples of task keys; tasks in one level are independent of each other
    level_of: dict = field(default_factory=dict)
    errors: tuple = ()

    @property
    def valid(self) -> bool:
        return not self.errors


def _find_cycle(keys: list, edges: dict) -> list:
    """One cycle as a path of keys (deterministic: the smallest-position start, DFS in input order), or []."""
    color = {key: 0 for key in keys}  # 0 unvisited, 1 on the stack, 2 done
    stack: list = []

    def visit(node):
        color[node] = 1
        stack.append(node)
        for nxt in edges[node]:
            if color[nxt] == 1:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == 0:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = 2
        return []

    for key in keys:
        if color[key] == 0:
            found = visit(key)
            if found:
                return found
    return []


def validate_dag(tasks: list) -> DagResult:
    errors: list = []
    if not tasks:
        return DagResult(errors=(DagError("empty_plan", None, "A plan needs at least one task."),))
    if len(tasks) > MAX_TASKS:
        errors.append(DagError("too_many_tasks", None, f"A plan can have at most {MAX_TASKS} tasks."))

    seen: dict = {}
    for task in tasks:
        if not isinstance(task.key, str) or not TASK_KEY.fullmatch(task.key):
            errors.append(DagError("invalid_task_id", None, "A task id must be 1-32 lowercase letters, digits, hyphens or underscores, starting with a letter."))
            continue
        if task.key in seen:
            errors.append(DagError("duplicate_task_id", task.key, f"Task id '{task.key}' is used more than once."))
            continue
        seen[task.key] = task
        if task.state not in VALID_STATES:
            errors.append(DagError("invalid_state", task.key, f"Task '{task.key}' has an unsupported state; plans only contain planned tasks."))

    edges: dict = {key: [] for key in seen}
    for task in tasks:
        if task.key not in seen or seen[task.key] is not task:
            continue
        if len(task.depends_on) > MAX_DEPENDENCIES_PER_TASK:
            errors.append(DagError("too_many_dependencies", task.key, f"Task '{task.key}' depends on more than {MAX_DEPENDENCIES_PER_TASK} tasks."))
        listed: set = set()
        for dep in task.depends_on:
            if dep in listed:
                errors.append(DagError("duplicate_dependency", task.key, f"Task '{task.key}' lists the dependency '{dep}' more than once."))
                continue
            listed.add(dep)
            if dep == task.key:
                errors.append(DagError("self_dependency", task.key, f"Task '{task.key}' cannot depend on itself."))
            elif dep not in seen:
                errors.append(DagError("missing_dependency", task.key, f"Task '{task.key}' depends on '{dep}', which is not a task in the plan."))
            else:
                edges[task.key].append(dep)

    if not any(e.code in ("self_dependency", "missing_dependency", "duplicate_task_id", "invalid_task_id") for e in errors):
        cycle = _find_cycle(list(seen), edges)
        if cycle:
            errors.append(DagError("cycle", cycle[0], "The tasks depend on each other in a circle: " + " -> ".join(cycle) + "."))

    if errors:
        return DagResult(errors=tuple(sorted(errors, key=lambda e: (e.code, e.task or "", e.message))))

    position = {key: index for index, key in enumerate(seen)}
    level_of: dict = {}
    remaining = set(seen)
    while remaining:
        ready = sorted((k for k in remaining if all(d in level_of for d in edges[k])), key=lambda k: position[k])
        for key in ready:
            level_of[key] = 1 + max((level_of[d] for d in edges[key]), default=-1)
        remaining -= set(ready)
    levels: dict = {}
    for key in sorted(level_of, key=lambda k: (level_of[k], position[k])):
        levels.setdefault(level_of[key], []).append(key)
    order = tuple(k for lvl in sorted(levels) for k in levels[lvl])
    return DagResult(order=order, levels=tuple(tuple(levels[lvl]) for lvl in sorted(levels)), level_of=dict(level_of))
