import unittest
import app
from app import (
    TaskGraph,
    TaskExecutionError,
    TaskCancelledError,
    TaskControlError,
    TaskResumeError,
)


class SmokeTest(unittest.TestCase):
    def test_import(self):
        self.assertTrue(app)


class MissingDependencyTest(unittest.TestCase):
    def test_order_reports_task_and_missing_dependency(self):
        g = TaskGraph()
        g.add("a", lambda r: 1, depends=["ghost"])
        with self.assertRaises(KeyError) as ctx:
            g.order()
        message = ctx.exception.args[0]
        self.assertIn("a", message)
        self.assertIn("ghost", message)

    def test_first_report_ordered_by_task_then_dependency(self):
        g = TaskGraph()
        g.add("b", lambda r: 1, depends=["phantom", "ghost"])
        g.add("a", lambda r: 1, depends=["nope"])
        with self.assertRaises(KeyError) as ctx:
            g.order()
        message = ctx.exception.args[0]
        self.assertIn("a", message)
        self.assertIn("nope", message)
        self.assertNotIn("b", message)

        g2 = TaskGraph()
        g2.add("t", lambda r: None, depends=["z", "a", "m"])
        with self.assertRaises(KeyError) as ctx2:
            g2.order()
        self.assertIn(repr("t"), ctx2.exception.args[0])
        self.assertIn(repr("a"), ctx2.exception.args[0])

    def test_run_missing_dependency_executes_nothing_and_preserves_snapshot(self):
        g = TaskGraph()
        calls = []
        g.add("ok", lambda r: calls.append("ok") or 42)
        self.assertEqual(g.run(), {"ok": 42})
        g.add("later", lambda r: calls.append("later"), depends=["missing"])
        before = g.execution_state()
        with self.assertRaises(KeyError):
            g.run()
        self.assertEqual(calls, ["ok"])  # no task ran in the failed invocation
        self.assertEqual(g.execution_state(), before)
        self.assertEqual(g.execution_state()["ok"]["status"], "completed")

    def test_missing_dependency_is_not_reported_as_cycle(self):
        g = TaskGraph()
        g.add("a", lambda r: None, depends=["b"])
        g.add("b", lambda r: None, depends=["c"])
        with self.assertRaises(KeyError):
            g.order()

    def test_forward_registration_still_supported(self):
        g = TaskGraph()
        g.add("leaf", lambda r: r["root"] + 1, depends=["root"])
        g.add("root", lambda r: 41)
        self.assertEqual(g.order(), ["root", "leaf"])
        self.assertEqual(g.run(), {"root": 41, "leaf": 42})


class DuplicateAndCycleTest(unittest.TestCase):
    def test_duplicate_task(self):
        g = TaskGraph()
        g.add("x", lambda r: None)
        with self.assertRaises(ValueError) as ctx:
            g.add("x", lambda r: None)
        self.assertIn("duplicate task", str(ctx.exception))

    def test_cycle(self):
        g = TaskGraph()
        g.add("a", lambda r: None, depends=["b"])
        g.add("b", lambda r: None, depends=["a"])
        with self.assertRaises(ValueError) as ctx:
            g.order()
        self.assertIn("cycle detected", str(ctx.exception))


class InputBoundaryTest(unittest.TestCase):
    def test_only_direct_dependencies_visible_with_sorted_keys(self):
        captured = {}

        def root(r):
            captured["root"] = r
            return None  # None results are retained

        def mid(r):
            captured["mid"] = r
            self.assertEqual(list(r), ["root"])
            self.assertIsNone(r["root"])
            return [1, 2]

        def leaf(r):
            captured["leaf"] = r
            self.assertEqual(list(r), sorted(list(r)))
            self.assertEqual(set(r), {"mid", "zzz"})  # root not visible
            return "done"

        g = TaskGraph()
        g.add("root", root)
        g.add("zzz", lambda r: "z")
        g.add("mid", mid, depends=["root"])
        g.add("leaf", leaf, depends=["zzz", "mid"])
        order = g.order()
        results = g.run()
        self.assertEqual(list(results), order)
        self.assertEqual(captured["root"], {})
        self.assertIsNone(results["root"])
        self.assertEqual(results["mid"], [1, 2])
        self.assertEqual(results["leaf"], "done")

    def test_input_mutation_is_isolated(self):
        def naughty(r):
            r["injected"] = "evil"
            r.clear()
            return 1

        def consumer(r):
            self.assertEqual(r, {"a": 1})
            return 2

        g = TaskGraph()
        g.add("a", naughty)
        g.add("b", consumer, depends=["a"])
        self.assertEqual(g.run(), {"a": 1, "b": 2})
        self.assertEqual(g.run(), {"a": 1, "b": 2})

    def test_results_keys_follow_stable_topological_order(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2)
        g.add("c", lambda r: r["a"] + r["b"], depends=["a", "b"])
        self.assertEqual(list(g.run()), ["a", "b", "c"])


class RegistrationValidationTest(unittest.TestCase):
    def test_non_string_task_name(self):
        for bad in (None, 1, 1.5, b"x", ("x",), ["x"]):
            g = TaskGraph()
            with self.assertRaises(TypeError) as ctx:
                g.add(bad, lambda r: None)
            self.assertEqual(str(ctx.exception), "task name must be a string")
            self.assertEqual(g.tasks, {})
            self.assertEqual(g.execution_state(), {})

    def test_empty_task_name(self):
        g = TaskGraph()
        with self.assertRaises(ValueError) as ctx:
            g.add("", lambda r: None)
        self.assertEqual(str(ctx.exception), "task name must not be empty")
        self.assertEqual(g.tasks, {})

    def test_non_callable_task_function(self):
        for bad in (None, 1, "fn", object()):
            g = TaskGraph()
            with self.assertRaises(TypeError) as ctx:
                g.add("t", bad)
            self.assertEqual(str(ctx.exception), "task function must be callable")
            self.assertEqual(g.tasks, {})

    def test_depends_must_be_iterable_of_names(self):
        for bad in ("dep", b"dep", 1, None, object()):
            g = TaskGraph()
            with self.assertRaises(TypeError) as ctx:
                g.add("t", lambda r: None, depends=bad)
            self.assertEqual(
                str(ctx.exception), "depends must be an iterable of task names"
            )
            self.assertEqual(g.tasks, {})

    def test_non_string_dependency_name(self):
        for bad in ([1], (None,), [b"x"], ["ok", 2]):
            g = TaskGraph()
            with self.assertRaises(TypeError) as ctx:
                g.add("t", lambda r: None, depends=bad)
            self.assertEqual(
                str(ctx.exception), "dependency name must be a string"
            )
            self.assertEqual(g.tasks, {})

    def test_empty_dependency_name(self):
        g = TaskGraph()
        with self.assertRaises(ValueError) as ctx:
            g.add("t", lambda r: None, depends=["ok", ""])
        self.assertEqual(
            str(ctx.exception), "dependency name must not be empty"
        )
        self.assertEqual(g.tasks, {})

    def test_failed_validation_preserves_existing_graph_and_state(self):
        g = TaskGraph()
        g.add("ok", lambda r: 7)
        self.assertEqual(g.run(), {"ok": 7})
        before = g.execution_state()
        for call in (
            lambda: g.add(None, lambda r: None),
            lambda: g.add("", lambda r: None),
            lambda: g.add("bad", None),
            lambda: g.add("bad", lambda r: None, depends="x"),
            lambda: g.add("bad", lambda r: None, depends=[1]),
            lambda: g.add("bad", lambda r: None, depends=[""]),
        ):
            with self.assertRaises((TypeError, ValueError)):
                call()
        self.assertEqual(set(g.tasks), {"ok"})
        self.assertEqual(g.order(), ["ok"])
        self.assertEqual(g.execution_state(), before)
        self.assertEqual(g.run(), {"ok": 7})

    def test_generator_depends_and_duplicate_dep_collapse(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: r["a"] + 1, depends=(d for d in ["a", "a"]))
        self.assertEqual(g.order(), ["a", "b"])
        self.assertEqual(g.run(), {"a": 1, "b": 2})

    def test_duplicate_check_still_applies_after_validation(self):
        g = TaskGraph()
        g.add("x", lambda r: None)
        with self.assertRaises(ValueError) as ctx:
            g.add("x", lambda r: None, depends=["y"])
        self.assertIn("duplicate task", str(ctx.exception))
        self.assertNotIn("y", g.deps["x"])


class ExecutionFailureTest(unittest.TestCase):
    def test_failure_wrapped_with_cause_and_details(self):
        g = TaskGraph()
        calls_c = []
        g.add("a", lambda r: 1)

        def boom(r):
            raise RuntimeError("boom msg")

        g.add("b", boom, depends=["a"])
        g.add("c", lambda r: calls_c.append(1) or 3, depends=["b"])

        with self.assertRaises(TaskExecutionError) as ctx:
            g.run()
        err = ctx.exception
        self.assertEqual(err.task_name, "b")
        self.assertIsInstance(err.original, RuntimeError)
        self.assertEqual(str(err.original), "boom msg")
        self.assertIs(err.__cause__, err.original)
        message = str(err)
        self.assertIn("b", message)
        self.assertIn("RuntimeError", message)
        self.assertIn("boom msg", message)
        self.assertEqual(calls_c, [])  # pending nodes are not executed

        state = g.execution_state()
        self.assertEqual(
            state["a"], {"status": "completed", "result": 1, "error": None}
        )
        self.assertEqual(state["b"]["status"], "failed")
        self.assertIsNone(state["b"]["result"])
        self.assertEqual(
            state["b"]["error"],
            {"type": "RuntimeError", "message": "boom msg"},
        )
        self.assertEqual(
            state["c"], {"status": "pending", "result": None, "error": None}
        )

    def test_snapshot_is_independent_including_error_details(self):
        g = TaskGraph()

        def fail(r):
            raise ValueError("v")

        g.add("f", fail)
        with self.assertRaises(TaskExecutionError):
            g.run()
        snapshot = g.execution_state()
        snapshot["f"]["status"] = "completed"
        snapshot["f"]["error"]["message"] = "tampered"
        snapshot["new"] = {}

        again = g.execution_state()
        self.assertEqual(again["f"]["status"], "failed")
        self.assertEqual(again["f"]["error"]["message"], "v")
        self.assertNotIn("new", again)

    def test_each_run_starts_fresh_and_reexecutes(self):
        counter = {"n": 0}

        def bump(r):
            counter["n"] += 1
            return counter["n"]

        g = TaskGraph()
        g.add("a", bump)
        g.add("b", lambda r: r["a"] * 10, depends=["a"])
        self.assertEqual(g.run(), {"a": 1, "b": 10})
        self.assertEqual(g.run(), {"a": 2, "b": 20})
        state = g.execution_state()
        self.assertTrue(
            all(record["status"] == "completed" for record in state.values())
        )

    def test_arbitrary_result_object_preserved(self):
        marker = object()
        g = TaskGraph()
        g.add("o", lambda r: marker)
        self.assertIs(g.run()["o"], marker)


class RetryTest(unittest.TestCase):
    def test_default_and_zero_call_each_task_once(self):
        for kwargs in ({}, {"max_retries": 0}):
            calls = []

            def flaky(r):
                calls.append(1)
                raise RuntimeError("always")

            g = TaskGraph()
            g.add("f", flaky)
            with self.assertRaises(TaskExecutionError):
                g.run(**kwargs)
            self.assertEqual(len(calls), 1)

    def test_transient_failure_recovers_within_same_run(self):
        attempts = {"n": 0}

        def flaky(r):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient %d" % attempts["n"])
            return "ok"

        g = TaskGraph()
        g.add("flaky", flaky)
        g.add("down", lambda r: r["flaky"] + "!", depends=["flaky"])
        self.assertEqual(g.run(max_retries=2), {"flaky": "ok", "down": "ok!"})
        self.assertEqual(attempts["n"], 3)
        state = g.execution_state()
        self.assertEqual(
            state["flaky"], {"status": "completed", "result": "ok", "error": None}
        )
        self.assertEqual(state["down"]["status"], "completed")

    def test_retry_budget_is_per_task(self):
        fails = {"a": 1, "b": 1}

        def make(name):
            def task(r):
                if fails[name]:
                    fails[name] -= 1
                    raise RuntimeError(name)
                return name
            return task

        g = TaskGraph()
        g.add("a", make("a"))
        g.add("b", make("b"), depends=["a"])
        # Each task fails once; a shared budget of 1 would not suffice.
        self.assertEqual(g.run(max_retries=1), {"a": "a", "b": "b"})

    def test_exhaustion_reports_last_exception_and_skips_downstream(self):
        calls_down = []

        def flaky(r):
            raise ValueError("attempt fails")

        g = TaskGraph()
        g.add("up", lambda r: 1)
        g.add("flaky", flaky, depends=["up"])
        g.add("down", lambda r: calls_down.append(1), depends=["flaky"])

        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_retries=2)
        err = ctx.exception
        self.assertEqual(err.task_name, "flaky")
        self.assertIs(err.__cause__, err.original)
        self.assertIsInstance(err.original, ValueError)
        self.assertEqual(str(err.original), "attempt fails")
        self.assertEqual(calls_down, [])

        state = g.execution_state()
        self.assertEqual(state["up"]["status"], "completed")
        self.assertEqual(state["flaky"]["status"], "failed")
        self.assertEqual(
            state["flaky"]["error"],
            {"type": "ValueError", "message": "attempt fails"},
        )
        self.assertEqual(state["down"]["status"], "pending")

    def test_exhaustion_keeps_last_of_differing_exceptions(self):
        attempts = {"n": 0}

        def flaky(r):
            attempts["n"] += 1
            raise RuntimeError("failure %d" % attempts["n"])

        g = TaskGraph()
        g.add("flaky", flaky)
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_retries=1)
        self.assertEqual(str(ctx.exception.original), "failure 2")
        self.assertEqual(
            g.execution_state()["flaky"]["error"],
            {"type": "RuntimeError", "message": "failure 2"},
        )

    def test_each_attempt_gets_fresh_input_mapping(self):
        seen = []
        raw = []

        def dep(r):
            return 1

        def flaky(r):
            raw.append(r)
            seen.append(dict(r))
            r["polluted"] = True
            if len(seen) == 1:
                raise RuntimeError("once")
            return "ok"

        g = TaskGraph()
        g.add("dep", dep)
        g.add("flaky", flaky, depends=["dep"])
        self.assertEqual(g.run(max_retries=1)["flaky"], "ok")
        self.assertEqual(len(seen), 2)
        self.assertIsNot(raw[0], raw[1])  # each attempt gets a new mapping
        self.assertEqual(seen[0], {"dep": 1})
        self.assertEqual(seen[1], {"dep": 1})  # first attempt's mutation unseen

    def test_invalid_max_retries_rejected_before_any_execution(self):
        for bad in (-1, True, False, 1.5, "2", None):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(ValueError):
                g.run(max_retries=bad)
            self.assertEqual(calls, [1])  # nothing executed on the bad call
            self.assertEqual(g.execution_state(), before)

    def test_invalid_max_retries_with_empty_graph(self):
        g = TaskGraph()
        with self.assertRaises(ValueError):
            g.run(max_retries=-2)
        self.assertEqual(g.execution_state(), {})


class RetryLimitsTest(unittest.TestCase):
    def _flaky_factory(self, calls, name, failures):
        def task(r):
            calls.append(name)
            if calls.count(name) <= failures:
                raise RuntimeError("%s fail %d" % (name, calls.count(name)))
            return name
        return task

    def test_none_and_empty_mapping_keep_uniform_max_retries(self):
        for limits in (None, {}):
            calls = []
            g = TaskGraph()
            g.add("a", self._flaky_factory(calls, "a", 1))
            kwargs = {} if limits is None else {"retry_limits": limits}
            self.assertEqual(g.run(max_retries=1, **kwargs), {"a": "a"})
            self.assertEqual(calls, ["a", "a"])

    def test_per_task_budgets_differ_within_one_run(self):
        calls = []
        g = TaskGraph()
        # a needs 2 retries, b needs 1; a uniform budget cannot fit both.
        g.add("a", self._flaky_factory(calls, "a", 2))
        g.add("b", self._flaky_factory(calls, "b", 1), depends=["a"])
        results = g.run(max_retries=1, retry_limits={"a": 2})
        self.assertEqual(results, {"a": "a", "b": "b"})
        self.assertEqual(calls, ["a", "a", "a", "b", "b"])

    def test_entry_of_zero_disables_retries_for_that_task_only(self):
        calls = []
        g = TaskGraph()
        g.add("a", self._flaky_factory(calls, "a", 1))
        g.add("b", self._flaky_factory(calls, "b", 1))
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_retries=2, retry_limits={"a": 0})
        self.assertEqual(ctx.exception.task_name, "a")
        self.assertEqual(calls, ["a"])  # a never retried; b never reached

    def test_missing_entry_falls_back_to_max_retries(self):
        calls = []
        g = TaskGraph()
        g.add("a", self._flaky_factory(calls, "a", 2))
        g.add("b", self._flaky_factory(calls, "b", 1), depends=["a"])
        # Only b is configured; a keeps the max_retries budget of 2.
        results = g.run(max_retries=2, retry_limits={"b": 1})
        self.assertEqual(results, {"a": "a", "b": "b"})
        self.assertEqual(calls, ["a", "a", "a", "b", "b"])

    def test_exhaustion_reports_last_exception_of_that_task(self):
        attempts = {"n": 0}

        def flaky(r):
            attempts["n"] += 1
            raise ValueError("failure %d" % attempts["n"])

        g = TaskGraph()
        g.add("flaky", flaky)
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_retries=0, retry_limits={"flaky": 2})
        err = ctx.exception
        self.assertEqual(err.task_name, "flaky")
        self.assertIs(err.__cause__, err.original)
        self.assertEqual(str(err.original), "failure 3")
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(
            g.execution_state()["flaky"]["error"],
            {"type": "ValueError", "message": "failure 3"},
        )

    def test_retry_success_records_only_final_result(self):
        calls = []
        g = TaskGraph()
        g.add("a", self._flaky_factory(calls, "a", 1))
        self.assertEqual(g.run(retry_limits={"a": 1}), {"a": "a"})
        self.assertEqual(
            g.execution_state()["a"],
            {"status": "completed", "result": "a", "error": None},
        )

    def test_each_attempt_gets_fresh_input_mapping(self):
        raw = []
        seen = []

        def flaky(r):
            raw.append(r)
            seen.append(dict(r))
            r["polluted"] = True
            if len(seen) == 1:
                raise RuntimeError("once")
            return "ok"

        g = TaskGraph()
        g.add("dep", lambda r: 1)
        g.add("flaky", flaky, depends=["dep"])
        self.assertEqual(g.run(retry_limits={"flaky": 1})["flaky"], "ok")
        self.assertIsNot(raw[0], raw[1])
        self.assertEqual(seen, [{"dep": 1}, {"dep": 1}])

    def test_budgets_are_independent_of_other_tasks_consumption(self):
        calls = []
        g = TaskGraph()
        g.add("a", self._flaky_factory(calls, "a", 2))
        g.add("b", self._flaky_factory(calls, "b", 1))
        # a burns its whole budget of 2; b's single retry is unaffected.
        results = g.run(retry_limits={"a": 2, "b": 1})
        self.assertEqual(results, {"a": "a", "b": "b"})
        self.assertEqual(calls, ["a", "a", "a", "b", "b"])

    def test_entries_outside_targets_closure_are_allowed_but_inert(self):
        calls = []
        g = TaskGraph()
        g.add("a", self._flaky_factory(calls, "a", 1))
        g.add("b", lambda r: calls.append("b") or r["a"], depends=["a"])
        g.add("outside", lambda r: calls.append("outside") or 9)
        results = g.run(
            targets=["b"], retry_limits={"a": 1, "outside": 5}
        )
        self.assertEqual(results, {"a": "a", "b": "a"})
        self.assertEqual(calls, ["a", "a", "b"])  # outside never ran
        self.assertEqual(set(g.execution_state()), {"a", "b"})

    def test_retry_limits_is_keyword_only(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        with self.assertRaises(TypeError):
            g.run(0, False, None, 1, {"a": 1})

    def test_non_mapping_rejected_before_execution(self):
        for bad in (["a"], ("a",), "a", 1, 1.5, True, object()):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.run(retry_limits=bad)
            self.assertIn("retry_limits", str(ctx.exception))
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_non_string_key_rejected_before_execution(self):
        for bad in ({1: 0}, {None: 0}, {b"a": 0}, {("a",): 0}):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(TypeError):
                g.run(retry_limits=bad)
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_non_integer_value_rejected_before_execution(self):
        for bad in ({"ok": True}, {"ok": False}, {"ok": 1.5}, {"ok": "2"},
                    {"ok": None}):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(TypeError):
                g.run(retry_limits=bad)
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_negative_value_rejected_before_execution(self):
        for bad in ({"ok": -1}, {"ok": -100}):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(ValueError):
                g.run(retry_limits=bad)
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_unknown_task_key_rejected_before_execution(self):
        for bad in ({"ghost": 1}, {"ok": 1, "ghost": 0}):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(KeyError):
                g.run(retry_limits=bad)
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_invalid_retry_limits_with_empty_graph(self):
        g = TaskGraph()
        with self.assertRaises(TypeError):
            g.run(retry_limits=[1])
        with self.assertRaises(KeyError):
            g.run(retry_limits={"ghost": 0})
        self.assertEqual(g.execution_state(), {})

    def test_validation_precedes_graph_validation_and_snapshot(self):
        g = TaskGraph()
        g.add("ok", lambda r: 1)
        g.run()
        before = g.execution_state()
        g.add("late", lambda r: None, depends=["ghost"])
        # The retry_limits KeyError fires before the missing-dependency
        # KeyError from graph validation, and the snapshot survives.
        with self.assertRaises(KeyError) as ctx:
            g.run(retry_limits={"nope": 1})
        self.assertIn("nope", str(ctx.exception))
        self.assertEqual(g.execution_state(), before)

    def test_combines_with_continue_on_error(self):
        calls = []

        def boom(r):
            calls.append("boom")
            raise RuntimeError("boom")

        g = TaskGraph()
        g.add("boom", boom)
        g.add("free", lambda r: calls.append("free") or 1)
        g.add("down", lambda r: calls.append("down"), depends=["boom"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(
                continue_on_error=True,
                retry_limits={"boom": 2, "free": 0},
            )
        self.assertEqual(ctx.exception.task_name, "boom")
        self.assertEqual(calls, ["boom", "boom", "boom", "free"])
        state = g.execution_state()
        self.assertEqual(state["boom"]["status"], "failed")
        self.assertEqual(state["free"]["status"], "completed")
        self.assertEqual(state["down"]["status"], "pending")

    def test_combines_with_max_concurrency(self):
        calls = []
        g = TaskGraph()
        g.add("a", self._flaky_factory(calls, "a", 2))
        g.add("b", self._flaky_factory(calls, "b", 1))
        g.add("c", lambda r: calls.append("c") or r["a"],
              depends=["a", "b"])
        results = g.run(
            max_concurrency=2, retry_limits={"a": 2, "b": 1}
        )
        self.assertEqual(results, {"a": "a", "b": "b", "c": "a"})
        self.assertEqual(sorted(calls), ["a", "a", "a", "b", "b", "c"])
        self.assertEqual(list(results), ["a", "b", "c"])

    def test_concurrent_exhaustion_reports_earliest_failure(self):
        g = TaskGraph()
        g.add("a", lambda r: (_ for _ in ()).throw(ValueError("err a")))
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError("err b")))
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_concurrency=2, retry_limits={"a": 1, "b": 0})
        self.assertEqual(ctx.exception.task_name, "a")
        self.assertEqual(str(ctx.exception.original), "err a")
        state = g.execution_state()
        self.assertEqual(
            state["a"]["error"], {"type": "ValueError", "message": "err a"}
        )
        self.assertEqual(
            state["b"]["error"], {"type": "RuntimeError", "message": "err b"}
        )


class ContinueOnErrorTest(unittest.TestCase):
    def test_independent_tasks_run_and_dependents_stay_pending(self):
        calls = []

        def boom(r):
            calls.append("b")
            raise RuntimeError("boom b")

        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", boom, depends=["a"])
        g.add("c", lambda r: calls.append("c") or 3, depends=["b"])
        g.add("d", lambda r: calls.append("d") or 4, depends=["b"])
        g.add("e", lambda r: calls.append("e") or 5)
        g.add("f", lambda r: calls.append("f") or r["e"] + 1, depends=["e"])
        self.assertEqual(g.order(), ["a", "e", "b", "f", "c", "d"])

        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(continue_on_error=True)
        err = ctx.exception
        self.assertEqual(err.task_name, "b")
        self.assertIsInstance(err.original, RuntimeError)
        self.assertEqual(str(err.original), "boom b")
        self.assertIs(err.__cause__, err.original)
        # Still strictly stable topological order; blocked nodes never run.
        self.assertEqual(calls, ["a", "e", "b", "f"])

        state = g.execution_state()
        self.assertEqual(
            state["a"], {"status": "completed", "result": 1, "error": None}
        )
        self.assertEqual(state["b"]["status"], "failed")
        self.assertIsNone(state["b"]["result"])
        self.assertEqual(
            state["b"]["error"],
            {"type": "RuntimeError", "message": "boom b"},
        )
        self.assertEqual(
            state["c"], {"status": "pending", "result": None, "error": None}
        )
        self.assertEqual(
            state["d"], {"status": "pending", "result": None, "error": None}
        )
        self.assertEqual(
            state["e"], {"status": "completed", "result": 5, "error": None}
        )
        self.assertEqual(state["f"]["status"], "completed")
        self.assertEqual(state["f"]["result"], 6)

    def test_transitive_dependents_are_never_called(self):
        calls = []

        def fail(r):
            raise ValueError("up")

        g = TaskGraph()
        g.add("a", fail)
        g.add("b", lambda r: calls.append("b"), depends=["a"])
        g.add("c", lambda r: calls.append("c"), depends=["b"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(continue_on_error=True)
        self.assertEqual(ctx.exception.task_name, "a")
        self.assertEqual(calls, [])
        self.assertEqual(g.execution_state()["c"]["status"], "pending")

    def test_earliest_failure_in_topological_order_is_reported(self):
        def fail(letter):
            def task(r):
                raise ValueError("err " + letter)
            return task

        g = TaskGraph()
        g.add("a", fail("a"))
        g.add("b", fail("b"))
        g.add("c", lambda r: "ok")
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(continue_on_error=True)
        self.assertEqual(ctx.exception.task_name, "a")
        self.assertEqual(str(ctx.exception.original), "err a")
        self.assertIs(ctx.exception.__cause__, ctx.exception.original)

    def test_diamond_join_blocked_when_one_branch_fails(self):
        calls = []

        def fail(r):
            raise RuntimeError("bad branch")

        g = TaskGraph()
        g.add("bad", fail)
        g.add("good", lambda r: calls.append("good") or 2)
        g.add("join", lambda r: calls.append("join"), depends=["bad", "good"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        self.assertNotIn("join", calls)
        self.assertEqual(g.execution_state()["join"]["status"], "pending")

    def test_no_failure_returns_results_in_stable_order(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2)
        g.add("c", lambda r: r["a"] + r["b"], depends=["a", "b"])
        results = g.run(continue_on_error=True)
        self.assertEqual(results, {"a": 1, "b": 2, "c": 3})
        self.assertEqual(list(results), ["a", "b", "c"])
        self.assertTrue(
            all(
                record["status"] == "completed"
                for record in g.execution_state().values()
            )
        )

    def test_retry_budget_still_per_task_and_last_exception_reported(self):
        attempts = {"n": 0}

        def flaky(r):
            attempts["n"] += 1
            raise RuntimeError("fail %d" % attempts["n"])

        g = TaskGraph()
        g.add("flaky", flaky)
        g.add("ind", lambda r: "independent")
        g.add("down", lambda r: None, depends=["flaky"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_retries=2, continue_on_error=True)
        self.assertEqual(ctx.exception.task_name, "flaky")
        self.assertEqual(str(ctx.exception.original), "fail 3")
        self.assertEqual(attempts["n"], 3)
        state = g.execution_state()
        self.assertEqual(
            state["flaky"]["error"],
            {"type": "RuntimeError", "message": "fail 3"},
        )
        self.assertEqual(state["ind"]["status"], "completed")
        self.assertEqual(
            state["down"], {"status": "pending", "result": None, "error": None}
        )

    def test_retry_recovery_avoids_failure(self):
        attempts = {"n": 0}

        def flaky(r):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("once")
            return "ok"

        g = TaskGraph()
        g.add("flaky", flaky)
        g.add("down", lambda r: r["flaky"] + "!", depends=["flaky"])
        self.assertEqual(
            g.run(max_retries=1, continue_on_error=True),
            {"flaky": "ok", "down": "ok!"},
        )

    def test_each_attempt_still_gets_fresh_input_mapping(self):
        raw = []
        seen = []

        def flaky(r):
            raw.append(r)
            seen.append(dict(r))
            r["polluted"] = True
            raise RuntimeError("always")

        g = TaskGraph()
        g.add("dep", lambda r: 1)
        g.add("flaky", flaky, depends=["dep"])
        with self.assertRaises(TaskExecutionError):
            g.run(max_retries=1, continue_on_error=True)
        self.assertIsNot(raw[0], raw[1])
        self.assertEqual(seen, [{"dep": 1}, {"dep": 1}])

    def test_invalid_flag_type_rejected_before_execution(self):
        for bad in (0, 1, "true", None, 1.0, ["x"], ()):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.run(continue_on_error=bad)
            self.assertIn("continue_on_error", str(ctx.exception))
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_invalid_flag_type_with_empty_graph_keeps_state_empty(self):
        g = TaskGraph()
        with self.assertRaises(TypeError):
            g.run(continue_on_error=1)
        self.assertEqual(g.execution_state(), {})

    def test_invalid_max_retries_still_rejected_with_flag_on(self):
        g = TaskGraph()
        g.add("ok", lambda r: 7)
        g.run()
        before = g.execution_state()
        for bad in (-1, True, False, 1.5, "2", None):
            with self.assertRaises(ValueError):
                g.run(max_retries=bad, continue_on_error=True)
        self.assertEqual(g.execution_state(), before)

    def test_graph_validation_errors_unchanged_with_flag_on(self):
        ran = []
        g = TaskGraph()
        g.add("ok", lambda r: ran.append(1) or 1)
        g.run()
        before = g.execution_state()
        g.add("late", lambda r: ran.append(2), depends=["ghost"])
        with self.assertRaises(KeyError):
            g.run(continue_on_error=True)
        self.assertEqual(ran, [1])
        self.assertEqual(g.execution_state(), before)

        g2 = TaskGraph()
        g2.add("a", lambda r: None, depends=["b"])
        g2.add("b", lambda r: None, depends=["a"])
        with self.assertRaises(ValueError) as ctx:
            g2.run(continue_on_error=True)
        self.assertIn("cycle detected", str(ctx.exception))

    def test_snapshot_independence_for_failed_and_skipped_nodes(self):
        def fail(r):
            raise ValueError("v")

        g = TaskGraph()
        g.add("f", fail)
        g.add("d", lambda r: None, depends=["f"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        snapshot = g.execution_state()
        snapshot["f"]["error"]["message"] = "tampered"
        snapshot["d"]["status"] = "completed"
        again = g.execution_state()
        self.assertEqual(again["f"]["error"]["message"], "v")
        self.assertEqual(again["d"]["status"], "pending")

    def test_default_off_stops_at_first_failure(self):
        calls = []

        def fail(r):
            raise RuntimeError("stop")

        g = TaskGraph()
        g.add("a", lambda r: calls.append("a"))
        g.add("b", fail)
        g.add("c", lambda r: calls.append("c"))
        for kwargs in ({}, {"continue_on_error": False}):
            local = []
            g2 = TaskGraph()
            g2.add("a", lambda r: local.append("a"))
            g2.add("b", fail)
            g2.add("c", lambda r: local.append("c"))
            with self.assertRaises(TaskExecutionError) as ctx:
                g2.run(**kwargs)
            self.assertEqual(ctx.exception.task_name, "b")
            self.assertEqual(local, ["a"])
        # sanity: the first graph itself behaves identically
        with self.assertRaises(TaskExecutionError):
            g.run()
        self.assertEqual(calls, ["a"])


class TargetSelectionTest(unittest.TestCase):
    def _diamond(self):
        # order(): a, c, b, d, e (Kahn with the smallest ready name).
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        g.add("c", lambda r: calls.append("c") or 10)
        g.add("d", lambda r: calls.append("d") or r["b"] + r["c"], ["b", "c"])
        g.add("e", lambda r: calls.append("e") or r["d"] + 1, ["d"])
        return g, calls

    def test_closure_runs_in_stable_topological_projection(self):
        g, calls = self._diamond()
        self.assertEqual(g.order(), ["a", "c", "b", "d", "e"])
        results = g.run(targets=["d"])
        self.assertEqual(results, {"a": 1, "c": 10, "b": 2, "d": 12})
        self.assertEqual(list(results), ["a", "c", "b", "d"])
        self.assertEqual(calls, ["a", "c", "b", "d"])  # e never called
        state = g.execution_state()
        self.assertEqual(set(state), {"a", "b", "c", "d"})  # e absent
        self.assertTrue(
            all(record["status"] == "completed" for record in state.values())
        )

    def test_duplicate_targets_collapse_to_one_run(self):
        g, calls = self._diamond()
        results = g.run(targets=["b", "b", "b"])
        self.assertEqual(results, {"a": 1, "b": 2})
        self.assertEqual(calls, ["a", "b"])

    def test_multiple_targets_share_their_closure(self):
        g, calls = self._diamond()
        results = g.run(targets=["e", "b"])
        self.assertEqual(list(results), ["a", "c", "b", "d", "e"])
        self.assertEqual(calls, ["a", "c", "b", "d", "e"])

    def test_any_iterable_including_generators_is_accepted(self):
        g, calls = self._diamond()
        self.assertEqual(
            g.run(targets=(t for t in ["b"])), {"a": 1, "b": 2}
        )
        self.assertEqual(calls, ["a", "b"])

    def test_target_node_without_dependencies_runs_alone(self):
        g, calls = self._diamond()
        self.assertEqual(g.run(targets=["c"]), {"c": 10})
        self.assertEqual(calls, ["c"])
        self.assertEqual(set(g.execution_state()), {"c"})

    def test_omitted_or_none_targets_keeps_whole_graph_behavior(self):
        g, calls = self._diamond()
        self.assertEqual(
            g.run(), {"a": 1, "c": 10, "b": 2, "d": 12, "e": 13}
        )
        g, calls = self._diamond()
        self.assertEqual(
            g.run(targets=None), {"a": 1, "c": 10, "b": 2, "d": 12, "e": 13}
        )
        self.assertEqual(set(g.execution_state()), {"a", "b", "c", "d", "e"})

    def test_targets_is_keyword_only(self):
        g, _ = self._diamond()
        with self.assertRaises(TypeError):
            g.run(0, False, ["b"])

    def test_closure_inputs_match_whole_graph_inputs(self):
        seen = {}
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("x", lambda r: 99)
        g.add(
            "b",
            lambda r: (seen.update(b=r), r["a"] + 1)[1],
            ["a"],
        )
        g.add("c", lambda r: r["b"] + r["x"], ["b", "x"])
        g.run(targets=["c"])
        self.assertEqual(seen["b"], {"a": 1})  # no extra or missing keys

    def test_non_iterable_targets_raises_typeerror_without_running(self):
        for bad in ("b", b"b", 1, 1.5, 0, object()):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.run(targets=bad)
            self.assertIn("targets", str(ctx.exception))
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_non_string_target_name_raises_typeerror(self):
        for bad in (["b", 1], (None,), [b"x"], [[]]):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(TypeError):
                g.run(targets=bad)
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_empty_target_name_raises_valueerror(self):
        for bad in ([""], ["b", ""]):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(ValueError):
                g.run(targets=bad)
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_empty_target_collection_raises_valueerror(self):
        for bad in ([], set(), ()):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(ValueError):
                g.run(targets=bad)
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_unknown_target_raises_keyerror_without_running(self):
        for bad in (["ghost"], ["b", "ghost"]):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(KeyError):
                g.run(targets=bad)
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_missing_dependency_outside_selection_still_rejected(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or 2, ["a"])
        g.add("z", lambda r: None, ["missing"])
        with self.assertRaises(KeyError):
            g.run(targets=["b"])
        self.assertEqual(calls, [])
        self.assertEqual(g.execution_state(), {})

    def test_cycle_outside_selection_still_rejected(self):
        g = TaskGraph()
        g.add("a", lambda r: None, ["b"])
        g.add("b", lambda r: None, ["a"])
        g.add("ok", lambda r: 1)
        with self.assertRaises(ValueError) as ctx:
            g.run(targets=["ok"])
        self.assertIn("cycle detected", str(ctx.exception))
        self.assertEqual(g.execution_state(), {})

    def test_graph_errors_do_not_replace_a_previous_target_snapshot(self):
        g, _ = self._diamond()
        g.run(targets=["b"])
        before = g.execution_state()
        self.assertEqual(set(before), {"a", "b"})
        g.add("late", lambda r: None, ["ghost"])
        with self.assertRaises(KeyError):
            g.run(targets=["e"])
        self.assertEqual(g.execution_state(), before)

    def test_failure_wraps_and_snapshot_covers_only_closure(self):
        calls = []

        def boom(r):
            raise RuntimeError("boom")

        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", boom, ["a"])
        g.add("c", lambda r: calls.append("c") or 3, ["b"])
        g.add("free", lambda r: calls.append("free") or 9)
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(targets=["c"])
        err = ctx.exception
        self.assertEqual(err.task_name, "b")
        self.assertIs(err.original, err.__cause__)
        self.assertEqual(str(err.original), "boom")
        self.assertEqual(calls, ["a"])  # free is unselected; c blocked
        state = g.execution_state()
        self.assertEqual(set(state), {"a", "b", "c"})
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "failed")
        self.assertEqual(
            state["b"]["error"],
            {"type": "RuntimeError", "message": "boom"},
        )
        self.assertEqual(
            state["c"], {"status": "pending", "result": None, "error": None}
        )

    def test_continue_on_error_advances_only_unblocked_selected_tasks(self):
        def boom(r):
            raise RuntimeError("boom b")

        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", boom, ["a"])
        g.add("c", lambda r: calls.append("c") or 3, ["b"])
        g.add("d", lambda r: calls.append("d") or 4, ["b"])
        g.add("e", lambda r: calls.append("e") or 5)
        g.add("f", lambda r: calls.append("f") or r["e"] + 1, ["e"])
        g.add("outside", lambda r: calls.append("outside") or 99)
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(continue_on_error=True, targets=["c", "d", "f"])
        self.assertEqual(ctx.exception.task_name, "b")
        # The failing b is invoked but boom records nothing; blocked
        # downstream and unselected independent tasks never run.
        self.assertEqual(calls, ["a", "e", "f"])
        state = g.execution_state()
        self.assertEqual(set(state), {"a", "b", "c", "d", "e", "f"})
        self.assertEqual(state["c"]["status"], "pending")
        self.assertEqual(state["d"]["status"], "pending")
        self.assertEqual(state["f"]["result"], 6)
        self.assertNotIn("outside", state)

    def test_retries_still_apply_inside_closure(self):
        attempts = {"n": 0}

        def flaky(r):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("once")
            return "ok"

        g = TaskGraph()
        g.add("x", flaky)
        g.add("y", lambda r: r["x"] + "!", ["x"])
        self.assertEqual(
            g.run(max_retries=1, targets=["y"]),
            {"x": "ok", "y": "ok!"},
        )
        self.assertEqual(attempts["n"], 2)

    def test_exhausted_retries_skip_downstream_in_closure(self):
        attempts = []

        def always(r):
            attempts.append(1)
            raise ValueError("nope")

        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", always, ["a"])
        g.add("c", lambda r: 3, ["b"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_retries=2, targets=["c"])
        self.assertEqual(ctx.exception.task_name, "b")
        self.assertEqual(len(attempts), 3)
        state = g.execution_state()
        self.assertEqual(set(state), {"a", "b", "c"})
        self.assertEqual(state["c"]["status"], "pending")

    def test_target_snapshot_is_independent_of_previous_and_next_runs(self):
        g, _ = self._diamond()
        g.run(targets=["b"])
        snapshot = g.execution_state()
        snapshot["a"]["status"] = "tampered"
        snapshot["new"] = {}
        self.assertEqual(g.execution_state()["a"]["status"], "completed")
        self.assertNotIn("new", g.execution_state())
        # A later whole-graph run records the whole graph again.
        g.run()
        self.assertEqual(
            set(g.execution_state()), {"a", "b", "c", "d", "e"}
        )


class PlanTest(unittest.TestCase):
    def _diamond(self):
        # order(): a, c, b, d, e (Kahn with the smallest ready name).
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        g.add("c", lambda r: calls.append("c") or 10)
        g.add("d", lambda r: calls.append("d") or r["b"] + r["c"], ["b", "c"])
        g.add("e", lambda r: calls.append("e") or r["d"] + 1, ["d"])
        return g, calls

    def test_omitted_or_none_targets_covers_whole_graph(self):
        g, calls = self._diamond()
        self.assertEqual(g.plan(), ["a", "c", "b", "d", "e"])
        self.assertEqual(g.plan(None), ["a", "c", "b", "d", "e"])
        self.assertEqual(g.plan(targets=None), ["a", "c", "b", "d", "e"])
        self.assertEqual(calls, [])  # previewing never calls task functions

    def test_closure_is_stable_topological_projection(self):
        g, calls = self._diamond()
        self.assertEqual(g.plan(["d"]), ["a", "c", "b", "d"])
        self.assertEqual(g.plan(["e", "b"]), ["a", "c", "b", "d", "e"])
        self.assertEqual(calls, [])

    def test_duplicate_targets_collapse(self):
        g, _ = self._diamond()
        self.assertEqual(g.plan(["b", "b", "b"]), ["a", "b"])

    def test_target_without_dependencies_returns_itself(self):
        g, _ = self._diamond()
        self.assertEqual(g.plan(["c"]), ["c"])

    def test_any_iterable_including_generators_is_accepted(self):
        g, _ = self._diamond()
        self.assertEqual(g.plan(t for t in ["b"]), ["a", "b"])
        self.assertEqual(g.plan({"d"}), ["a", "c", "b", "d"])

    def test_each_call_returns_a_fresh_list(self):
        g, _ = self._diamond()
        first = g.plan(["d"])
        first.append("tampered")
        first[0] = "tampered"
        self.assertEqual(g.plan(["d"]), ["a", "c", "b", "d"])
        self.assertIsNot(g.plan(), g.plan())

    def test_plan_does_not_mutate_graph_or_state(self):
        g, _ = self._diamond()
        g.run(targets=["b"])
        before_state = g.execution_state()
        before_tasks = dict(g.tasks)
        before_deps = {k: set(v) for k, v in g.deps.items()}
        g.plan(["e"])
        g.plan()
        self.assertEqual(g.execution_state(), before_state)
        self.assertEqual(g.tasks, before_tasks)
        self.assertEqual(g.deps, before_deps)

    def test_plan_on_fresh_graph_leaves_state_empty(self):
        g, _ = self._diamond()
        g.plan()
        self.assertEqual(g.execution_state(), {})

    def test_non_iterable_targets_raises_typeerror(self):
        for bad in ("b", b"b", 1, 1.5, 0, object()):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.plan(bad)
            self.assertIn("targets", str(ctx.exception))
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_non_string_target_name_raises_typeerror(self):
        for bad in (["b", 1], (None,), [b"x"], [[]]):
            g, calls = self._diamond()
            with self.assertRaises(TypeError):
                g.plan(bad)
            self.assertEqual(calls, [])
            self.assertEqual(g.execution_state(), {})

    def test_empty_target_name_raises_valueerror(self):
        for bad in ([""], ["b", ""]):
            g, _ = self._diamond()
            with self.assertRaises(ValueError):
                g.plan(bad)

    def test_empty_target_collection_raises_valueerror(self):
        for bad in ([], set(), ()):
            g, _ = self._diamond()
            with self.assertRaises(ValueError):
                g.plan(bad)

    def test_unknown_target_raises_keyerror(self):
        for bad in (["ghost"], ["b", "ghost"]):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(KeyError):
                g.plan(bad)
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_missing_dependency_outside_closure_still_rejected(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2, ["a"])
        g.add("z", lambda r: None, ["missing"])
        with self.assertRaises(KeyError):
            g.plan(["b"])
        self.assertEqual(g.execution_state(), {})

    def test_cycle_outside_closure_still_rejected(self):
        g = TaskGraph()
        g.add("a", lambda r: None, ["b"])
        g.add("b", lambda r: None, ["a"])
        g.add("ok", lambda r: 1)
        with self.assertRaises(ValueError) as ctx:
            g.plan(["ok"])
        self.assertIn("cycle detected", str(ctx.exception))
        self.assertEqual(g.execution_state(), {})

    def test_failed_plan_preserves_previous_snapshot(self):
        g, _ = self._diamond()
        g.run(targets=["b"])
        before = g.execution_state()
        g.add("late", lambda r: None, ["ghost"])
        with self.assertRaises(KeyError):
            g.plan(["e"])
        self.assertEqual(g.execution_state(), before)

    def test_plan_matches_run_sequence_results_and_state_order(self):
        for kwargs in (
            {},
            {"targets": None},
            {"targets": ["d"]},
            {"targets": ["e", "b"]},
            {"targets": ["c"]},
            {"max_retries": 2, "targets": ["d"]},
            {"continue_on_error": True, "targets": ["e"]},
            {"max_concurrency": 3, "targets": ["d"]},
        ):
            g, _ = self._diamond()
            planned = g.plan(kwargs.get("targets"))
            results = g.run(**kwargs)
            self.assertEqual(list(results), planned)
            self.assertEqual(list(g.execution_state()), planned)

    def test_plan_is_deterministic_regardless_of_registration_order(self):
        g1 = TaskGraph()
        g1.add("b", lambda r: r["a"] + 1, ["a"])
        g1.add("a", lambda r: 1)
        g1.add("c", lambda r: 10)
        g2 = TaskGraph()
        g2.add("c", lambda r: 10)
        g2.add("a", lambda r: 1)
        g2.add("b", lambda r: r["a"] + 1, ["a"])
        self.assertEqual(g1.plan(), g2.plan())
        self.assertEqual(g1.plan(), ["a", "c", "b"])


class ConcurrencyTest(unittest.TestCase):
    def test_invalid_max_concurrency_type_rejected_before_execution(self):
        for bad in (True, False, 1.5, "2", None, [2]):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.run(max_concurrency=bad)
            self.assertIn("max_concurrency", str(ctx.exception))
            self.assertEqual(calls, [1])  # nothing executed on the bad call
            self.assertEqual(g.execution_state(), before)

    def test_invalid_max_concurrency_value_rejected_before_execution(self):
        for bad in (0, -1, -100):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            self.assertEqual(g.run(), {"ok": 7})
            before = g.execution_state()
            with self.assertRaises(ValueError) as ctx:
                g.run(max_concurrency=bad)
            self.assertIn("max_concurrency", str(ctx.exception))
            self.assertEqual(calls, [1])
            self.assertEqual(g.execution_state(), before)

    def test_invalid_max_concurrency_with_empty_graph(self):
        g = TaskGraph()
        with self.assertRaises(TypeError):
            g.run(max_concurrency=True)
        with self.assertRaises(ValueError):
            g.run(max_concurrency=0)
        self.assertEqual(g.execution_state(), {})

    def test_max_concurrency_is_keyword_only(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        with self.assertRaises(TypeError):
            g.run(0, False, None, 2)

    def test_default_and_one_keep_sequential_order(self):
        for kwargs in ({}, {"max_concurrency": 1}):
            calls = []
            g = TaskGraph()
            g.add("a", lambda r: calls.append("a") or 1)
            g.add("b", lambda r: calls.append("b") or 2)
            g.add("c", lambda r: calls.append("c") or r["a"] + r["b"],
                  depends=["a", "b"])
            self.assertEqual(g.run(**kwargs), {"a": 1, "b": 2, "c": 3})
            self.assertEqual(calls, ["a", "b", "c"])

    def test_concurrency_limit_is_respected_and_reached(self):
        import threading
        import time

        lock = threading.Lock()
        current = {"n": 0}
        peak = {"n": 0}

        def task(r):
            with lock:
                current["n"] += 1
                peak["n"] = max(peak["n"], current["n"])
            time.sleep(0.05)
            with lock:
                current["n"] -= 1
            return 1

        g = TaskGraph()
        for name in ("a", "b", "c", "d"):
            g.add(name, task)
        results = g.run(max_concurrency=2)
        self.assertEqual(results, {"a": 1, "b": 1, "c": 1, "d": 1})
        self.assertEqual(peak["n"], 2)  # limited to, and actually reaching, 2

    def test_results_order_follows_topology_not_completion(self):
        import time

        def slow(r):
            time.sleep(0.1)
            return "slow"

        g = TaskGraph()
        g.add("a", slow)
        g.add("b", lambda r: "fast")
        g.add("c", lambda r: r["a"] + r["b"], depends=["a", "b"])
        results = g.run(max_concurrency=2)
        # b finishes long before a, but the key order stays topological.
        self.assertEqual(list(results), ["a", "b", "c"])
        self.assertEqual(results, {"a": "slow", "b": "fast", "c": "slowfast"})
        self.assertEqual(list(g.execution_state()), ["a", "b", "c"])

    def test_downstream_waits_for_all_direct_dependencies(self):
        import threading
        import time

        done = set()
        lock = threading.Lock()

        def make(name, delay):
            def task(r):
                time.sleep(delay)
                with lock:
                    done.add(name)
                return name
            return task

        seen = {}

        def join(r):
            seen["deps_done"] = set(done)
            return "join"

        g = TaskGraph()
        g.add("slow", make("slow", 0.15))
        g.add("fast", make("fast", 0.01))
        g.add("join", join, depends=["slow", "fast"])
        self.assertEqual(
            g.run(max_concurrency=2),
            {"slow": "slow", "fast": "fast", "join": "join"},
        )
        self.assertEqual(seen["deps_done"], {"slow", "fast"})

    def test_failure_waits_for_batch_then_stops_scheduling(self):
        import time

        calls = []

        def boom(r):
            raise RuntimeError("batch boom")

        def slow(r):
            time.sleep(0.1)
            calls.append("slow")
            return "slow"

        g = TaskGraph()
        g.add("boom", boom)
        g.add("slow", slow)
        g.add("later", lambda r: calls.append("later") or 1,
              depends=["slow"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_concurrency=2)
        err = ctx.exception
        self.assertEqual(err.task_name, "boom")
        self.assertIs(err.__cause__, err.original)
        self.assertIsInstance(err.original, RuntimeError)
        # The already-started batch settled; nothing new was scheduled.
        self.assertEqual(calls, ["slow"])
        state = g.execution_state()
        self.assertEqual(state["boom"]["status"], "failed")
        self.assertEqual(
            state["boom"]["error"],
            {"type": "RuntimeError", "message": "batch boom"},
        )
        self.assertEqual(
            state["slow"],
            {"status": "completed", "result": "slow", "error": None},
        )
        self.assertEqual(state["later"]["status"], "pending")

    def test_earliest_failure_in_batch_is_reported(self):
        g = TaskGraph()
        g.add("a", lambda r: (_ for _ in ()).throw(ValueError("err a")))
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError("err b")))
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_concurrency=2)
        self.assertEqual(ctx.exception.task_name, "a")
        self.assertEqual(str(ctx.exception.original), "err a")
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "failed")
        self.assertEqual(state["b"]["status"], "failed")

    def test_continue_on_error_advances_independent_branches(self):
        import time

        calls = []

        def boom(r):
            raise RuntimeError("boom b")

        def slow(r):
            time.sleep(0.1)
            calls.append("slow")
            return "slow"

        g = TaskGraph()
        g.add("boom", boom)
        g.add("slow", slow)
        g.add("down", lambda r: calls.append("down"), depends=["boom"])
        g.add("join", lambda r: calls.append("join"),
              depends=["slow", "boom"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_concurrency=2, continue_on_error=True)
        self.assertEqual(ctx.exception.task_name, "boom")
        self.assertIs(ctx.exception.__cause__, ctx.exception.original)
        self.assertEqual(calls, ["slow"])  # blocked nodes never called
        state = g.execution_state()
        self.assertEqual(state["boom"]["status"], "failed")
        self.assertEqual(state["slow"]["status"], "completed")
        self.assertEqual(state["down"]["status"], "pending")
        self.assertEqual(state["join"]["status"], "pending")

    def test_retries_and_fresh_inputs_with_concurrency(self):
        raw = []
        seen = []
        attempts = {"n": 0}

        def flaky(r):
            raw.append(r)
            seen.append(dict(r))
            r["polluted"] = True
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("once")
            return "ok"

        g = TaskGraph()
        g.add("dep", lambda r: 1)
        g.add("other", lambda r: 2)
        g.add("flaky", flaky, depends=["dep"])
        g.add("down", lambda r: r["flaky"] + "!", depends=["flaky"])
        self.assertEqual(
            g.run(max_retries=1, max_concurrency=2),
            {"dep": 1, "other": 2, "flaky": "ok", "down": "ok!"},
        )
        self.assertEqual(attempts["n"], 2)
        self.assertIsNot(raw[0], raw[1])
        self.assertEqual(seen, [{"dep": 1}, {"dep": 1}])

    def test_targets_closure_with_concurrency(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        g.add("c", lambda r: calls.append("c") or 10)
        g.add("d", lambda r: calls.append("d") or r["b"] + r["c"], ["b", "c"])
        g.add("e", lambda r: calls.append("e") or r["d"] + 1, ["d"])
        results = g.run(targets=["d"], max_concurrency=2)
        self.assertEqual(results, {"a": 1, "c": 10, "b": 2, "d": 12})
        self.assertEqual(list(results), ["a", "c", "b", "d"])
        self.assertEqual(sorted(calls), ["a", "b", "c", "d"])  # e never ran
        self.assertEqual(set(g.execution_state()), {"a", "b", "c", "d"})

    def test_graph_validation_unchanged_with_concurrency(self):
        g = TaskGraph()
        g.add("ok", lambda r: 1)
        g.run()
        before = g.execution_state()
        g.add("late", lambda r: None, depends=["ghost"])
        with self.assertRaises(KeyError):
            g.run(max_concurrency=2)
        self.assertEqual(g.execution_state(), before)

        g2 = TaskGraph()
        g2.add("a", lambda r: None, depends=["b"])
        g2.add("b", lambda r: None, depends=["a"])
        with self.assertRaises(ValueError) as ctx:
            g2.run(max_concurrency=2)
        self.assertIn("cycle detected", str(ctx.exception))


class CancellationTest(unittest.TestCase):
    def _diamond(self):
        # order(): a, c, b, d, e (Kahn with the smallest ready name).
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        g.add("c", lambda r: calls.append("c") or 10)
        g.add("d", lambda r: calls.append("d") or r["b"] + r["c"], ["b", "c"])
        g.add("e", lambda r: calls.append("e") or r["d"] + 1, ["d"])
        return g, calls

    def test_omitted_or_none_leaves_everything_unchanged(self):
        for kwargs in ({}, {"cancel_check": None}):
            g, calls = self._diamond()
            self.assertEqual(
                g.run(**kwargs), {"a": 1, "c": 10, "b": 2, "d": 12, "e": 13}
            )
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            state = g.execution_state()
            self.assertTrue(
                all(r["status"] == "completed" for r in state.values())
            )

    def test_plan_never_calls_callback(self):
        checks = []
        g, calls = self._diamond()
        self.assertEqual(g.plan(), ["a", "c", "b", "d", "e"])
        self.assertEqual(g.plan(["d"]), ["a", "c", "b", "d"])
        self.assertEqual(checks, [])
        self.assertEqual(calls, [])
        self.assertEqual(g.execution_state(), {})

    def test_non_callable_rejected_before_ordering_and_execution(self):
        for bad in (1, "x", object(), ["nope"], {"a": 1}, True):
            g, calls = self._diamond()
            g.run()
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.run(cancel_check=bad)
            self.assertIn("cancel_check", str(ctx.exception))
            self.assertEqual(calls, ["a", "c", "b", "d", "e"])
            self.assertEqual(g.execution_state(), before)

    def test_cancel_check_is_keyword_only(self):
        g, _ = self._diamond()
        with self.assertRaises(TypeError):
            g.run(0, False, None, 1, None, lambda: False)

    def test_cancel_before_first_task_cancels_whole_graph(self):
        g, calls = self._diamond()
        polls = []
        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(cancel_check=lambda: (polls.append(1), True)[1])
        self.assertEqual(
            ctx.exception.task_names, ["a", "c", "b", "d", "e"]
        )
        self.assertEqual(polls, [1])  # consulted exactly once
        self.assertEqual(calls, [])  # no task started
        state = g.execution_state()
        self.assertEqual(list(state), ["a", "c", "b", "d", "e"])
        for name in state:
            self.assertEqual(
                state[name],
                {"status": "cancelled", "result": None, "error": None},
            )

    def test_cancel_mid_run_stops_before_next_stable_task(self):
        g, calls = self._diamond()
        polls = []

        def check():
            polls.append(1)
            return len(polls) >= 3  # a and c run; b is next in order

        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(cancel_check=check)
        self.assertEqual(ctx.exception.task_names, ["b", "d", "e"])
        self.assertEqual(calls, ["a", "c"])
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["a"]["result"], 1)
        self.assertEqual(state["c"]["status"], "completed")
        self.assertEqual(state["c"]["result"], 10)
        for name in ("b", "d", "e"):
            self.assertEqual(
                state[name],
                {"status": "cancelled", "result": None, "error": None},
            )

    def test_cancelled_task_spends_no_retries(self):
        attempts = []

        def flaky(r):
            attempts.append(1)
            raise RuntimeError("never reached")

        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("flaky", flaky, ["a"])
        polls = []
        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(
                max_retries=3,
                cancel_check=lambda: (polls.append(1), len(polls) >= 2)[1],
            )
        self.assertEqual(ctx.exception.task_names, ["flaky"])
        self.assertEqual(attempts, [])  # never called, none of 4 attempts
        self.assertEqual(
            g.execution_state()["flaky"]["status"], "cancelled"
        )

    def test_failure_before_cancel_is_observed_still_raises_execution(self):
        # Cancellation has not yet been observed when the task fails:
        # the failure follows the existing TaskExecutionError semantics.
        def boom(r):
            raise RuntimeError("boom")

        g = TaskGraph()
        g.add("boom", boom)
        g.add("later", lambda r: 1)
        polls = []
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(cancel_check=lambda: (polls.append(1), False)[1])
        self.assertEqual(ctx.exception.task_name, "boom")
        state = g.execution_state()
        self.assertEqual(state["boom"]["status"], "failed")
        self.assertEqual(
            state["boom"]["error"],
            {"type": "RuntimeError", "message": "boom"},
        )
        # Callback was consulted once (before boom); fast failure stops
        # the run, so later is never polled toward.
        self.assertEqual(len(polls), 1)

    def test_continue_on_error_records_failures_then_cancel_wins(self):
        def boom(r):
            raise RuntimeError("boom")

        calls = []
        g = TaskGraph()
        # Stable order: boom, late, ok, down (down depends on boom).
        g.add("boom", boom)
        g.add("ok", lambda r: calls.append("ok") or 1)
        g.add("down", lambda r: calls.append("down") or 1, ["boom"])
        g.add("late", lambda r: calls.append("late") or 1)
        polls = []

        def check():
            polls.append(1)
            return len(polls) >= 3  # boom fails, late runs; cancel before ok

        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(continue_on_error=True, cancel_check=check)
        # ok never started; down was unreachable after boom's failure but
        # is still an unstarted node, so it settles as cancelled too.
        self.assertEqual(ctx.exception.task_names, ["ok", "down"])
        self.assertEqual(calls, ["late"])
        state = g.execution_state()
        self.assertEqual(state["boom"]["status"], "failed")
        self.assertIsNone(state["boom"]["result"])
        self.assertEqual(
            state["boom"]["error"],
            {"type": "RuntimeError", "message": "boom"},
        )
        self.assertEqual(state["late"]["status"], "completed")
        self.assertEqual(state["late"]["result"], 1)
        self.assertEqual(
            state["ok"], {"status": "cancelled", "result": None,
                          "error": None}
        )
        self.assertEqual(
            state["down"], {"status": "cancelled", "result": None,
                            "error": None}
        )

    def test_cancel_inside_targets_closure_leaves_outside_unrecorded(self):
        g, calls = self._diamond()
        g.add("outside", lambda r: calls.append("outside") or 9)
        polls = []
        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(
                targets=["e"],
                cancel_check=lambda: (polls.append(1), len(polls) >= 2)[1],
            )
        # a ran, closure remainder (c, b, d, e) is cancelled; outside
        # is neither executed nor recorded.
        self.assertEqual(ctx.exception.task_names, ["c", "b", "d", "e"])
        self.assertEqual(calls, ["a"])
        state = g.execution_state()
        self.assertEqual(set(state), {"a", "c", "b", "d", "e"})
        self.assertNotIn("outside", state)
        self.assertEqual(state["a"]["status"], "completed")

    def test_callback_truthiness_accepts_any_true_value(self):
        g, calls = self._diamond()
        for truthy in (1, "yes", object(), [0], {"k": 0}):
            g2, c2 = self._diamond()
            with self.assertRaises(TaskCancelledError):
                g2.run(cancel_check=lambda value=truthy: value)
            self.assertEqual(c2, [])
        # Explicit falsy values never cancel.
        for falsy in (0, "", [], {}, False, None):
            g2, c2 = self._diamond()
            self.assertTrue(g2.run(cancel_check=lambda value=falsy: value))
            self.assertEqual(c2, ["a", "c", "b", "d", "e"])

    def test_callback_is_called_with_no_arguments(self):
        g, _ = self._diamond()

        def check():
            return True

        with self.assertRaises(TaskCancelledError):
            g.run(cancel_check=check)
        # A callable that does not accept a zero-argument call passes the
        # callable() gate, then its invocation-time TypeError is a fault of
        # the callback: it surfaces as TaskControlError, never a parameter
        # error, with unstarted nodes settled as cancelled.
        g2, calls2 = self._diamond()
        with self.assertRaises(TaskControlError) as ctx:
            g2.run(cancel_check=lambda missing: True)
        self.assertIsInstance(ctx.exception.original, TypeError)
        self.assertIs(ctx.exception.__cause__, ctx.exception.original)
        self.assertEqual(calls2, [])
        self.assertTrue(
            all(
                r["status"] == "cancelled"
                for r in g2.execution_state().values()
            )
        )

    def test_snapshot_independence_for_cancelled_records(self):
        g, _ = self._diamond()
        with self.assertRaises(TaskCancelledError):
            g.run(cancel_check=lambda: True)
        snapshot = g.execution_state()
        snapshot["a"]["status"] = "completed"
        snapshot["new"] = {}
        again = g.execution_state()
        self.assertEqual(again["a"]["status"], "cancelled")
        self.assertNotIn("new", again)

    def test_cancel_does_not_change_graph_and_next_run_is_fresh(self):
        g, _ = self._diamond()
        with self.assertRaises(TaskCancelledError):
            g.run(cancel_check=lambda: True)
        self.assertEqual(g.order(), ["a", "c", "b", "d", "e"])
        # A subsequent run without cancel_check executes everything anew.
        self.assertEqual(
            g.run(), {"a": 1, "c": 10, "b": 2, "d": 12, "e": 13}
        )
        self.assertTrue(
            all(
                r["status"] == "completed"
                for r in g.execution_state().values()
            )
        )

    # --- concurrent scheduler ---------------------------------------

    def test_concurrent_cancel_before_first_batch_cancels_all(self):
        g, calls = self._diamond()
        polls = []
        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(
                max_concurrency=2,
                cancel_check=lambda: (polls.append(1), True)[1],
            )
        self.assertEqual(ctx.exception.task_names, ["a", "c", "b", "d", "e"])
        self.assertEqual(polls, [1])  # once before the first batch
        self.assertEqual(calls, [])
        state = g.execution_state()
        self.assertTrue(
            all(r["status"] == "cancelled" for r in state.values())
        )

    def test_concurrent_cancel_between_batches_waits_for_submitted(self):
        import time

        calls = []

        def slow_a(r):
            calls.append("a")
            time.sleep(0.1)
            return 1

        g = TaskGraph()
        g.add("a", slow_a)
        g.add("c", lambda r: calls.append("c") or 10)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        g.add("d", lambda r: calls.append("d") or 1, ["b", "c"])
        polls = []

        def check():
            polls.append(1)
            return len(polls) >= 2  # first batch (a, c) runs; cancel rest

        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(max_concurrency=2, cancel_check=check)
        self.assertEqual(ctx.exception.task_names, ["b", "d"])
        self.assertEqual(sorted(calls), ["a", "c"])  # whole batch settled
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["c"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "cancelled")
        self.assertEqual(state["d"]["status"], "cancelled")

    def test_concurrent_cancel_polled_once_per_batch(self):
        g, _ = self._diamond()
        polls = []
        with self.assertRaises(TaskCancelledError):
            g.run(
                max_concurrency=2,
                cancel_check=lambda: (polls.append(1), len(polls) >= 3)[1],
            )
        # Batches: [a, c] poll 1, [b] poll 2 (d needs b and c),
        # [d] poll 3 cancels before d, leaving d and e unstarted.
        self.assertEqual(polls, [1, 1, 1])

    def test_concurrent_continue_on_error_then_cancel_between_batches(self):
        # A failure settles in an early batch; with continue_on_error the
        # scheduler keeps going until cancel_check requests a stop before
        # a later batch. The failure stays in the snapshot, the cancelled
        # exception names only nodes that never started.
        def boom(r):
            raise RuntimeError("boom")

        g = TaskGraph()
        # Stable order: a, c, x, b, d. a fails; c succeeds; x is an
        # independent later node; b depends on the failed a (dead), and
        # d depends on b and c (dead through b).
        g.add("a", boom)
        g.add("c", lambda r: 10)
        g.add("x", lambda r: 99)
        g.add("b", lambda r: r["a"] + 1, ["a"])
        g.add("d", lambda r: 1, ["b", "c"])
        polls = []

        def check():
            polls.append(1)
            return len(polls) >= 2  # batch [a, c] runs; stop round 2

        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(
                max_concurrency=2,
                continue_on_error=True,
                cancel_check=check,
            )
        # x is the round-2 ready node that never starts; b and d were
        # unreachable after a failed, but never started either.
        self.assertEqual(ctx.exception.task_names, ["x", "b", "d"])
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "failed")
        self.assertEqual(
            state["a"]["error"],
            {"type": "RuntimeError", "message": "boom"},
        )
        self.assertEqual(state["c"]["status"], "completed")
        self.assertEqual(state["c"]["result"], 10)
        for name in ("x", "b", "d"):
            self.assertEqual(
                state[name],
                {"status": "cancelled", "result": None, "error": None},
            )

    def test_concurrent_failure_batch_settles_then_cancel_state(self):        # A failure observed before any cancellation still follows the
        # existing TaskExecutionError path.
        def boom(r):
            raise RuntimeError("boom")

        g = TaskGraph()
        g.add("a", boom)
        g.add("c", lambda r: 10)
        g.add("d", lambda r: 1, ["a", "c"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_concurrency=2, cancel_check=lambda: False)
        self.assertEqual(ctx.exception.task_name, "a")
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "failed")
        self.assertEqual(state["c"]["status"], "completed")
        self.assertEqual(state["d"]["status"], "pending")

    # --- callback raising -> TaskControlError ------------------------

    def test_callback_exception_wraps_as_control_error(self):
        g, calls = self._diamond()
        original = RuntimeError("control exploded")
        polls = []

        def check():
            polls.append(1)
            raise original

        with self.assertRaises(TaskControlError) as ctx:
            g.run(cancel_check=check)
        err = ctx.exception
        self.assertIs(err.original, original)
        self.assertIs(err.__cause__, original)
        self.assertIn("RuntimeError", str(err))
        self.assertEqual(polls, [1])
        self.assertEqual(calls, [])
        state = g.execution_state()
        self.assertTrue(
            all(r["status"] == "cancelled" for r in state.values())
        )

    def test_control_error_preserves_completed_and_failed(self):
        def boom(r):
            raise ValueError("task boom")

        calls = []
        g = TaskGraph()
        # Stable order: a, bad, late, ok; down depends on bad.
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("bad", boom)
        g.add("late", lambda r: calls.append("late") or 3)
        g.add("ok", lambda r: calls.append("ok") or 2)
        g.add("down", lambda r: calls.append("down") or 4, ["bad"])
        polls = []

        def check():
            polls.append(1)
            if len(polls) >= 4:
                raise RuntimeError("control")  # before ok
            return False

        # a completes; bad fails (continue_on_error keeps it failed);
        # late completes; down stays unreachable; the poll before ok
        # raises -> TaskControlError, ok and down settle as cancelled.
        with self.assertRaises(TaskControlError) as ctx:
            g.run(continue_on_error=True, cancel_check=check)
        self.assertIsInstance(ctx.exception.original, RuntimeError)
        self.assertEqual(str(ctx.exception.original), "control")
        self.assertIs(ctx.exception.__cause__, ctx.exception.original)
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["a"]["result"], 1)
        self.assertEqual(state["bad"]["status"], "failed")
        self.assertEqual(
            state["bad"]["error"],
            {"type": "ValueError", "message": "task boom"},
        )
        self.assertEqual(state["late"]["status"], "completed")
        self.assertEqual(state["late"]["result"], 3)
        for name in ("ok", "down"):
            self.assertEqual(
                state[name],
                {"status": "cancelled", "result": None, "error": None},
            )
        self.assertEqual(calls, ["a", "late"])

    def test_control_error_does_not_replace_validation_errors(self):
        # Parameter validation (including cancel_check's own type) fires
        # before the callback can ever run.
        g, _ = self._diamond()
        for bad in ("x", 1, object()):
            with self.assertRaises(TypeError):
                g.run(cancel_check=bad)
        g.add("ghost", lambda r: None, ["missing"])
        def check():
            raise AssertionError("must not run")
        with self.assertRaises(KeyError):
            g.run(cancel_check=check)

    def test_task_failure_is_not_wrapped_as_control_error(self):
        def boom(r):
            raise ValueError("task-side")

        g = TaskGraph()
        g.add("boom", boom)
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(cancel_check=lambda: False)
        self.assertEqual(ctx.exception.task_name, "boom")
        self.assertNotIsInstance(ctx.exception, TaskControlError)


class PriorityTest(unittest.TestCase):
    def _diamond(self, **priorities):
        # The zero-priority order() is a, c, b, d, e (FIFO Kahn whose
        # roots and each unlocked sibling intake are name-sorted).
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1,
              priority=priorities.get("a", 0))
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"],
              priority=priorities.get("b", 0))
        g.add("c", lambda r: calls.append("c") or 10,
              priority=priorities.get("c", 0))
        g.add("d", lambda r: calls.append("d") or r["b"] + r["c"],
              ["b", "c"], priority=priorities.get("d", 0))
        g.add("e", lambda r: calls.append("e") or r["d"] + 1, ["d"],
              priority=priorities.get("e", 0))
        return g, calls

    def test_default_and_zero_keep_historical_stable_sequence(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: r["a"] + 1, ["a"])
        g.add("c", lambda r: 10, priority=0)
        g.add("d", lambda r: r["b"] + r["c"], ["b", "c"])
        g.add("e", lambda r: r["d"] + 1, ["d"])
        self.assertEqual(g.order(), ["a", "c", "b", "d", "e"])
        self.assertEqual(g.plan(), ["a", "c", "b", "d", "e"])

    def test_priority_is_keyword_only(self):
        g = TaskGraph()
        with self.assertRaises(TypeError):
            g.add("t", lambda r: 1, (), 5)

    def test_invalid_priority_type_rejected_without_mutation(self):
        for bad in (True, False, 1.5, "2", None, [2], object()):
            g = TaskGraph()
            with self.assertRaises(TypeError) as ctx:
                g.add("t", lambda r: 1, priority=bad)
            self.assertEqual(str(ctx.exception), "priority must be an integer")
            self.assertEqual(g.tasks, {})
            self.assertEqual(g.priorities, {})
            self.assertEqual(g.execution_state(), {})

    def test_negative_priority_accepted_and_orders_last(self):
        g = TaskGraph()
        g.add("z", lambda r: 1, priority=-1)
        g.add("a", lambda r: 2)
        g.add("m", lambda r: 3, priority=-100)
        self.assertEqual(g.order(), ["a", "z", "m"])

    def test_higher_priority_ready_node_goes_first(self):
        g, _ = self._diamond(c=5)
        # c is an independent root: promoted ahead of a; b still waits a.
        self.assertEqual(g.order(), ["c", "a", "b", "d", "e"])

    def test_priority_never_overrides_dependencies(self):
        g = TaskGraph()
        g.add("low", lambda r: 1)
        g.add("hi", lambda r: r["low"] + 1, ["low"], priority=100)
        self.assertEqual(g.order(), ["low", "hi"])
        self.assertEqual(g.run(), {"low": 1, "hi": 2})

    def test_priority_promotes_across_unlock_cohorts(self):
        # Old zero-priority FIFO enqueues root c before a-unlocks b;
        # giving b priority 10 promotes it ahead of c once a completes.
        g, calls = self._diamond(b=10)
        self.assertEqual(g.order(), ["a", "b", "c", "d", "e"])
        results = g.run()
        self.assertEqual(calls, ["a", "b", "c", "d", "e"])
        self.assertEqual(list(results), ["a", "b", "c", "d", "e"])
        self.assertEqual(list(g.execution_state()), ["a", "b", "c", "d", "e"])
        self.assertEqual(results["d"], 12)

    def test_equal_priority_tie_breaks_by_name_within_intake(self):
        g = TaskGraph()
        g.add("z", lambda r: 1, priority=7)
        g.add("a", lambda r: 2, priority=7)
        g.add("m", lambda r: 3, priority=7)
        self.assertEqual(g.order(), ["a", "m", "z"])

    def test_plan_matches_order_and_run_with_priorities(self):
        for pri in ({}, {"c": 5}, {"b": 10}, {"a": -1, "e": 3, "c": 2}):
            g, _ = self._diamond(**pri)
            planned = g.plan()
            self.assertEqual(planned, g.order())
            results = g.run()
            self.assertEqual(list(results), planned)
            self.assertEqual(list(g.execution_state()), planned)

    def test_targets_validates_whole_graph_then_projects_priority_order(self):
        g, calls = self._diamond(b=10)
        self.assertEqual(g.plan(["d"]), ["a", "b", "c", "d"])
        results = g.run(targets=["d"])
        self.assertEqual(list(results), ["a", "b", "c", "d"])
        self.assertEqual(calls, ["a", "b", "c", "d"])  # e never ran
        self.assertEqual(set(g.execution_state()), {"a", "b", "c", "d"})

    def test_input_mapping_keys_still_sorted_direct_dependencies(self):
        seen = {}
        g = TaskGraph()
        g.add("root", lambda r: 1, priority=-5)
        g.add("zzz", lambda r: 2, priority=9)

        def mid(r):
            seen["mid"] = r
            return r["root"] + 1

        g.add("mid", mid, ["root"], priority=9)
        g.add("leaf", lambda r: r["zzz"] + r["mid"], ["zzz", "mid"])
        self.assertEqual(g.order(), ["zzz", "root", "mid", "leaf"])
        results = g.run()
        self.assertEqual(seen["mid"], {"root": 1})  # scheduling order aside
        self.assertEqual(results["leaf"], 4)
        self.assertEqual(list(results), ["zzz", "root", "mid", "leaf"])

    def test_failed_priority_registration_changes_nothing(self):
        g = TaskGraph()
        g.add("ok", lambda r: 7, priority=3)
        self.assertEqual(g.run(), {"ok": 7})
        before = g.execution_state()
        for call in (
            lambda: g.add("bad", None, priority=5),
            lambda: g.add("bad", lambda r: 1, priority=1.5),
            lambda: g.add("bad", lambda r: 1, priority=True),
            lambda: g.add("ok", lambda r: 1, priority=5),
            lambda: g.add("", lambda r: 1, priority=5),
            lambda: g.add("bad", lambda r: 1, depends="x", priority=5),
        ):
            with self.assertRaises((TypeError, ValueError)):
                call()
        self.assertEqual(set(g.tasks), {"ok"})
        self.assertEqual(g.priorities, {"ok": 3})
        self.assertEqual(g.order(), ["ok"])
        self.assertEqual(g.execution_state(), before)

    def test_existing_validation_outcomes_unchanged_with_priority(self):
        g = TaskGraph()
        g.add("x", lambda r: None)
        with self.assertRaises(ValueError) as ctx:
            g.add("x", lambda r: None, priority=10)
        self.assertIn("duplicate task", str(ctx.exception))
        with self.assertRaises(TypeError) as ctx:
            g.add("y", lambda r: None, depends="d", priority=1)
        self.assertEqual(
            str(ctx.exception), "depends must be an iterable of task names"
        )
        self.assertNotIn("y", g.priorities)

    def test_order_is_deterministic_regardless_of_registration_order(self):
        spec = {
            "a": ((), 0), "b": (["a"], 10), "c": ((), 0),
            "d": (["b", "c"], 0), "e": (["d"], -3), "f": ((), 5),
        }

        def build(order):
            g = TaskGraph()
            for name in order:
                deps, pri = spec[name]
                g.add(name, lambda r, n=name: n, deps, priority=pri)
            return g

        names = list(spec)
        first = build(names).order()
        for perm in (
            ["f", "e", "d", "c", "b", "a"],
            ["c", "a", "f", "b", "e", "d"],
            ["d", "b", "e", "f", "c", "a"],
        ):
            self.assertEqual(build(perm).order(), first)
        self.assertEqual(first, ["f", "a", "b", "c", "d", "e"])

    def test_concurrent_batches_take_first_k_ready_in_priority_order(self):
        import time

        launched = []
        release = __import__("threading").Event()

        def make(name, hold=False):
            def task(r):
                launched.append(name)
                if hold:
                    release.wait(0.2)
                return name
            return task

        g = TaskGraph()
        g.add("a", make("a", hold=True))
        g.add("z", make("z"))
        g.add("h", make("h"), priority=10)
        results = g.run(max_concurrency=2)
        # The first batch is h (highest-priority ready) plus a (equal-prio
        # name intake before z); z waits for the next round.
        self.assertEqual(set(launched[:2]), {"h", "a"})
        self.assertEqual(list(results), ["h", "a", "z"])

    def test_concurrent_results_order_ignores_completion_with_priority(self):
        import time

        g = TaskGraph()
        g.add("lo", lambda r: time.sleep(0.1) or "slow", priority=10)
        g.add("hi", lambda r: "fast")
        g.add("join", lambda r: r["lo"] + r["hi"], ["lo", "hi"])
        results = g.run(max_concurrency=2)
        self.assertEqual(list(results), ["lo", "hi", "join"])
        self.assertEqual(results["join"], "slowfast")
        self.assertEqual(list(g.execution_state()), ["lo", "hi", "join"])

    def test_concurrent_earliest_failure_follows_priority_order(self):
        g = TaskGraph()
        g.add("hi_fail",
              lambda r: (_ for _ in ()).throw(ValueError("h")), priority=10)
        g.add("lo_fail",
              lambda r: (_ for _ in ()).throw(RuntimeError("l")))
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(max_concurrency=2)
        self.assertEqual(ctx.exception.task_name, "hi_fail")

    def test_continue_on_error_runs_promoted_independents(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("c", lambda r: calls.append("c") or 3, priority=10)
        g.add("b",
              lambda r: (_ for _ in ()).throw(RuntimeError("boom b")),
              priority=5)
        self.assertEqual(g.order(), ["c", "b", "a"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(continue_on_error=True)
        self.assertEqual(ctx.exception.task_name, "b")
        self.assertEqual(calls, ["c", "a"])  # b raises, never appended
        self.assertEqual(list(g.execution_state()), ["c", "b", "a"])
        self.assertEqual(g.execution_state()["b"]["status"], "failed")

    def test_cancel_names_and_poll_follow_priority_order(self):
        g, _ = self._diamond(b=10)
        polls = []

        def check():
            polls.append(1)
            return len(polls) >= 4  # a, b, c run; d is next in order

        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(cancel_check=check)
        self.assertEqual(ctx.exception.task_names, ["d", "e"])
        state = g.execution_state()
        self.assertEqual(list(state), ["a", "b", "c", "d", "e"])
        for name in ("a", "b", "c"):
            self.assertEqual(state[name]["status"], "completed")
        for name in ("d", "e"):
            self.assertEqual(state[name]["status"], "cancelled")

    def test_retries_and_fresh_inputs_unchanged_with_priority(self):
        seen = []
        raw = []
        attempts = {"n": 0}

        def flaky(r):
            raw.append(r)
            seen.append(dict(r))
            r["polluted"] = True
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("once")
            return "ok"

        g = TaskGraph()
        g.add("dep", lambda r: 1, priority=-2)
        g.add("other", lambda r: 2, priority=5)
        g.add("flaky", flaky, ["dep"], priority=4)
        g.add("down", lambda r: r["flaky"] + "!", ["flaky"])
        results = g.run(max_retries=1)
        self.assertEqual(results["flaky"], "ok")
        self.assertEqual(results["down"], "ok!")
        self.assertIsNot(raw[0], raw[1])
        self.assertEqual(seen, [{"dep": 1}, {"dep": 1}])


class ResumeTest(unittest.TestCase):
    # --- signature / default behavior -------------------------------

    def test_resume_is_keyword_only(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        with self.assertRaises(TypeError):
            g.run(0, False, None, 1, None, None, True)

    def test_non_boolean_resume_raises_typeerror_and_keeps_snapshot(self):
        for bad in (0, 1, "true", None, 1.0, ["x"], ()):
            g = TaskGraph()
            calls = []
            g.add("ok", lambda r: calls.append(1) or 7)
            g.add("down", lambda r: calls.append("down") or 1, ["ok"])
            self.assertEqual(g.run(), {"ok": 7, "down": 1})
            before = g.execution_state()
            with self.assertRaises(TypeError) as ctx:
                g.run(resume=bad)
            self.assertIn("resume", str(ctx.exception))
            self.assertEqual(calls, [1, "down"])  # nothing ran
            self.assertEqual(g.execution_state(), before)

    def test_non_boolean_resume_with_empty_graph_keeps_state_empty(self):
        g = TaskGraph()
        with self.assertRaises(TypeError):
            g.run(resume=1)
        self.assertEqual(g.execution_state(), {})

    def test_resume_false_keeps_fresh_run_semantics(self):
        counter = {"n": 0}

        def bump(r):
            counter["n"] += 1
            return counter["n"]

        g = TaskGraph()
        g.add("a", bump)
        g.add("b", lambda r: r["a"] * 10, ["a"])
        self.assertEqual(g.run(resume=False), {"a": 1, "b": 10})
        # An explicit resume=False still starts completely fresh.
        self.assertEqual(g.run(resume=False), {"a": 2, "b": 20})

    # --- rejection reasons ------------------------------------------

    def test_resume_without_previous_run_is_no_previous_run(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(resume=True)
        self.assertEqual(ctx.exception.reason, "no_previous_run")
        self.assertIn("no_previous_run", str(ctx.exception))
        self.assertEqual(g.execution_state(), {})

    def test_graph_validation_fails_before_snapshot_is_confirmed(self):
        g = TaskGraph()
        g.add("ok", lambda r: 7)
        g.run()
        before = g.execution_state()
        g.add("late", lambda r: None, depends=["ghost"])
        with self.assertRaises(KeyError):
            g.run(resume=True)
        self.assertEqual(g.execution_state(), before)
        # And parameter validation precedes snapshot confirmation too.
        with self.assertRaises(ValueError):
            g.run(resume=True, max_retries=-1)
        self.assertEqual(g.execution_state(), before)

    def test_added_task_is_graph_changed_and_preserves_snapshot(self):
        g = TaskGraph()
        calls = []
        g.add("a", lambda r: calls.append("a") or 1)
        g.run()
        before = g.execution_state()
        g.add("b", lambda r: calls.append("b") or 2)
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(resume=True)
        self.assertEqual(ctx.exception.reason, "graph_changed")
        self.assertEqual(calls, ["a"])  # rejected before any call
        self.assertEqual(g.execution_state(), before)

    def test_removed_task_is_graph_changed(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2)
        g.run()
        before = g.execution_state()
        g.tasks.pop("b")
        g.deps.pop("b")
        g.priorities.pop("b")
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(resume=True)
        self.assertEqual(ctx.exception.reason, "graph_changed")
        self.assertEqual(g.execution_state(), before)

    def test_changed_dependency_is_graph_changed(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2)
        g.run()
        before = g.execution_state()
        g.deps["b"] = {"a"}
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(resume=True)
        self.assertEqual(ctx.exception.reason, "graph_changed")
        self.assertEqual(g.execution_state(), before)

    def test_changed_priority_is_graph_changed(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.run()
        before = g.execution_state()
        g.priorities["a"] = 5
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(resume=True)
        self.assertEqual(ctx.exception.reason, "graph_changed")
        self.assertEqual(g.execution_state(), before)

    def test_different_targets_closure_is_scope_changed(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: r["a"] + 1, ["a"])
        g.add("c", lambda r: 9)
        g.run(targets=["b"])
        before = g.execution_state()
        self.assertEqual(set(before), {"a", "b"})
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(targets=["c"], resume=True)
        self.assertEqual(ctx.exception.reason, "scope_changed")
        self.assertEqual(g.execution_state(), before)
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(resume=True)  # whole-graph scope vs closure snapshot
        self.assertEqual(ctx.exception.reason, "scope_changed")
        self.assertEqual(g.execution_state(), before)

    def test_graph_change_outside_closure_is_still_graph_changed(self):
        # The fingerprint covers the whole graph, not just the closure.
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: r["a"] + 1, ["a"])
        g.add("c", lambda r: 9)
        g.run(targets=["b"])
        before = g.execution_state()
        g.add("d", lambda r: 3)
        with self.assertRaises(TaskResumeError) as ctx:
            g.run(targets=["b"], resume=True)
        self.assertEqual(ctx.exception.reason, "graph_changed")
        self.assertEqual(g.execution_state(), before)

    def test_function_replacement_is_allowed(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        with self.assertRaises(TaskExecutionError):
            g.run()
        g.tasks["b"] = lambda r: r["a"] + 1
        self.assertEqual(g.run(resume=True), {"a": 1, "b": 2})

    # --- core reuse semantics ---------------------------------------

    def test_failed_node_reruns_and_blocked_downstream_unblocks(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)

        def boom(r):
            calls.append("b")
            raise RuntimeError("boom")

        g.add("b", boom, ["a"])
        g.add("c", lambda r: calls.append("c") or r["b"] + 10, ["b"])
        g.add("d", lambda r: calls.append("d") or r["c"] + 100, ["c"])
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run()
        self.assertEqual(ctx.exception.task_name, "b")
        self.assertEqual(calls, ["a", "b"])
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "failed")
        self.assertEqual(state["c"]["status"], "pending")
        self.assertEqual(state["d"]["status"], "pending")

        g.tasks["b"] = lambda r: calls.append("b") or 2
        results = g.run(resume=True)
        self.assertEqual(results, {"a": 1, "b": 2, "c": 12, "d": 112})
        self.assertEqual(list(results), ["a", "b", "c", "d"])
        # a was reused; b, c, d ran in topological order.
        self.assertEqual(calls, ["a", "b", "b", "c", "d"])
        state = g.execution_state()
        for name in ("a", "b", "c", "d"):
            self.assertEqual(state[name]["status"], "completed")
        self.assertIsNone(state["a"]["error"])

    def test_only_completed_nodes_are_reused(self):
        calls = []

        def fail(r):
            calls.append("f")
            raise ValueError("v")

        g = TaskGraph()
        g.add("ok", lambda r: calls.append("ok") or 7)
        g.add("f", fail)
        g.add("down", lambda r: calls.append("down") or 1, ["f"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        self.assertEqual(calls, ["f", "ok"])
        g.tasks["f"] = lambda r: calls.append("f") or 1
        results = g.run(continue_on_error=True, resume=True)
        self.assertEqual(results, {"ok": 7, "f": 1, "down": 1})
        self.assertEqual(list(results), ["f", "ok", "down"])
        self.assertEqual(calls, ["f", "ok", "f", "down"])

    def test_rerun_tasks_get_fresh_mapping_of_direct_dependencies(self):
        raw = []
        seen = []
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: 2)

        def flaky(r):
            raw.append(r)
            seen.append(dict(r))
            r["polluted"] = True
            raise RuntimeError("always")

        g.add("flaky", flaky, ["a"])
        g.add("join", lambda r: r["flaky"] + r["b"], ["flaky", "b"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        # Fix flaky and resume: it gets a fresh mapping including the
        # reused a; join then gets both direct dependencies.
        g.tasks["flaky"] = lambda r: (
            raw.append(r), seen.append(dict(r)), r["a"] + 10
        )[-1]
        results = g.run(continue_on_error=True, resume=True)
        self.assertEqual(results, {"a": 1, "b": 2, "flaky": 11, "join": 13})
        self.assertEqual(seen, [{"a": 1}, {"a": 1}])
        self.assertIsNot(raw[0], raw[1])

    def test_retry_budgets_recount_for_the_recovery_run(self):
        calls = []

        def always(r):
            calls.append("t")
            raise RuntimeError("x")

        g = TaskGraph()
        g.add("t", always)
        with self.assertRaises(TaskExecutionError):
            g.run(max_retries=1)
        self.assertEqual(len(calls), 2)  # 1 + 1 retry in the original run
        with self.assertRaises(TaskExecutionError):
            g.run(resume=True, max_retries=0)
        self.assertEqual(len(calls), 3)  # exactly one fresh attempt
        with self.assertRaises(TaskExecutionError):
            g.run(resume=True, max_retries=2)
        self.assertEqual(len(calls), 6)  # three fresh attempts

    def test_failed_record_is_cleared_then_refreshed(self):
        g = TaskGraph()
        g.add("f", lambda r: (_ for _ in ()).throw(ValueError("v1")))
        with self.assertRaises(TaskExecutionError):
            g.run()
        self.assertEqual(
            g.execution_state()["f"]["error"],
            {"type": "ValueError", "message": "v1"},
        )
        g.tasks["f"] = lambda r: (_ for _ in ()).throw(RuntimeError("v2"))
        with self.assertRaises(TaskExecutionError) as ctx:
            g.run(resume=True)
        self.assertIsInstance(ctx.exception.original, RuntimeError)
        record = g.execution_state()["f"]
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["result"])
        self.assertEqual(
            record["error"], {"type": "RuntimeError", "message": "v2"}
        )

    def test_fully_completed_snapshot_returns_without_calling(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        first = g.run()
        self.assertEqual(first, {"a": 1, "b": 2})
        again = g.run(resume=True)
        self.assertEqual(again, {"a": 1, "b": 2})
        self.assertEqual(list(again), ["a", "b"])
        self.assertEqual(calls, ["a", "b"])  # no task was called
        # Snapshot records are preserved in plan order.
        state = g.execution_state()
        self.assertEqual(list(state), ["a", "b"])
        self.assertEqual(state["a"]["result"], 1)
        self.assertEqual(state["b"]["result"], 2)

    def test_cancelled_and_pending_nodes_are_rerun(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or 2)
        with self.assertRaises(TaskCancelledError):
            g.run(cancel_check=lambda: True)
        self.assertEqual(calls, [])
        results = g.run(resume=True)
        self.assertEqual(results, {"a": 1, "b": 2})
        self.assertEqual(calls, ["a", "b"])

    def test_successful_resume_returns_entire_closure_in_plan_order(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("c", lambda r: 10)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        g.add("d", lambda r: r["b"] + r["c"], ["b", "c"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        g.tasks["b"] = lambda r: r["a"] + 1
        results = g.run(continue_on_error=True, resume=True)
        self.assertEqual(list(results), ["a", "c", "b", "d"])
        self.assertEqual(results, {"a": 1, "c": 10, "b": 2, "d": 12})
        self.assertEqual(list(g.execution_state()), ["a", "c", "b", "d"])

    def test_resume_after_continue_on_error_failure(self):
        calls = []

        def boom(r):
            calls.append("boom")
            raise RuntimeError("boom")

        g = TaskGraph()
        g.add("boom", boom)
        g.add("free", lambda r: calls.append("free") or 1)
        g.add("down", lambda r: calls.append("down") or 1, ["boom"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        self.assertEqual(set(calls), {"boom", "free"})
        g.tasks["boom"] = lambda r: calls.append("boom") or 0
        results = g.run(continue_on_error=True, resume=True)
        self.assertEqual(results, {"boom": 0, "free": 1, "down": 1})
        self.assertEqual(list(results), ["boom", "free", "down"])
        self.assertEqual(calls, ["boom", "free", "boom", "down"])

    # --- targets -----------------------------------------------------

    def test_resume_same_closure_reuses_completed(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        g.add("c", lambda r: calls.append("c") or r["b"] + 1, ["b"])
        g.add("e", lambda r: 9)
        with self.assertRaises(TaskExecutionError):
            g.run(targets=["c"])
        self.assertEqual(calls, ["a"])
        g.tasks["b"] = lambda r: calls.append("b") or 2
        results = g.run(targets=["c"], resume=True)
        self.assertEqual(results, {"a": 1, "b": 2, "c": 3})
        self.assertEqual(calls, ["a", "b", "c"])  # e never ran
        self.assertEqual(set(g.execution_state()), {"a", "b", "c"})

    def test_equivalent_target_sets_normalize_to_same_scope(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: calls.append("b") or r["a"] + 1, ["a"])
        self.assertEqual(g.run(targets=["b"]), {"a": 1, "b": 2})
        results = g.run(targets=["a", "b"], resume=True)
        self.assertEqual(results, {"a": 1, "b": 2})
        self.assertEqual(calls, ["a", "b"])  # fully reused, no calls

    # --- cancellation ------------------------------------------------

    def test_cancel_on_resume_keeps_reused_completed(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        g.add("c", lambda r: calls.append("c") or r["b"] + 1, ["b"])
        g.add("d", lambda r: calls.append("d") or r["c"] + 1, ["c"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        g.tasks["b"] = lambda r: calls.append("b") or 2
        polls = []

        def check():
            polls.append(1)
            return len(polls) >= 2  # let b run, cancel before c

        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(continue_on_error=True, resume=True, cancel_check=check)
        # a is reused (not listed); only this run's unstarted nodes are.
        self.assertEqual(ctx.exception.task_names, ["c", "d"])
        self.assertEqual(calls, ["a", "b"])
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "completed")
        self.assertEqual(state["b"]["result"], 2)
        for name in ("c", "d"):
            self.assertEqual(
                state[name],
                {"status": "cancelled", "result": None, "error": None},
            )
        # A further resume reuses a and b and finishes c, d.
        g.tasks["c"] = lambda r: calls.append("c") or r["b"] + 10
        g.tasks["d"] = lambda r: calls.append("d") or r["c"] + 100
        results = g.run(continue_on_error=True, resume=True)
        self.assertEqual(results, {"a": 1, "b": 2, "c": 12, "d": 112})
        self.assertEqual(calls, ["a", "b", "c", "d"])

    def test_cancel_before_first_resumed_task_lists_all_non_reused(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        g.add("c", lambda r: r["b"] + 1, ["b"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        g.tasks["b"] = lambda r: 2
        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(
                continue_on_error=True, resume=True,
                cancel_check=lambda: True,
            )
        self.assertEqual(ctx.exception.task_names, ["b", "c"])
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "cancelled")
        self.assertEqual(state["c"]["status"], "cancelled")

    def test_control_error_on_resume_preserves_reused(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        g.tasks["b"] = lambda r: 2

        def check():
            raise ValueError("control")

        with self.assertRaises(TaskControlError) as ctx:
            g.run(continue_on_error=True, resume=True, cancel_check=check)
        self.assertIsInstance(ctx.exception.original, ValueError)
        state = g.execution_state()
        self.assertEqual(
            state["a"], {"status": "completed", "result": 1, "error": None}
        )
        self.assertEqual(state["b"]["status"], "cancelled")

    # --- concurrency -------------------------------------------------

    def test_concurrent_resume_completed_nodes_take_no_slots(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError("boom")),
              ["a"])
        g.add("c", lambda r: r["b"] + 1, ["b"])
        g.add("free", lambda r: 7)
        with self.assertRaises(TaskExecutionError):
            g.run(max_concurrency=2, continue_on_error=True)
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["free"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "failed")
        self.assertEqual(state["c"]["status"], "pending")
        attempts = {"n": 0}
        g.tasks["b"] = lambda r: (
            attempts.__setitem__("n", attempts["n"] + 1), 2
        )[1]
        results = g.run(
            max_concurrency=2, continue_on_error=True, resume=True
        )
        self.assertEqual(results, {"a": 1, "b": 2, "c": 3, "free": 7})
        self.assertEqual(list(results), ["a", "free", "b", "c"])
        self.assertEqual(attempts["n"], 1)

    def test_concurrent_resume_cancel_names_only_unstarted(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        g.add("c", lambda r: r["b"] + 1, ["b"])
        with self.assertRaises(TaskExecutionError):
            g.run(max_concurrency=2, continue_on_error=True)
        g.tasks["b"] = lambda r: 2
        with self.assertRaises(TaskCancelledError) as ctx:
            g.run(
                max_concurrency=2, continue_on_error=True, resume=True,
                cancel_check=lambda: True,
            )
        self.assertEqual(ctx.exception.task_names, ["b", "c"])
        state = g.execution_state()
        self.assertEqual(state["a"]["status"], "completed")
        self.assertEqual(state["b"]["status"], "cancelled")
        self.assertEqual(state["c"]["status"], "cancelled")

    def test_concurrent_resume_succeeds_in_plan_order(self):
        calls = []
        g = TaskGraph()
        g.add("a", lambda r: calls.append("a") or 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError()), ["a"])
        g.add("c", lambda r: calls.append("c") or 10)
        g.add("d", lambda r: r["b"] + r["c"], ["b", "c"])
        with self.assertRaises(TaskExecutionError):
            g.run(max_concurrency=2, continue_on_error=True)
        g.tasks["b"] = lambda r: calls.append("b") or 2
        results = g.run(
            max_concurrency=2, continue_on_error=True, resume=True
        )
        self.assertEqual(list(results), ["a", "c", "b", "d"])
        self.assertEqual(results, {"a": 1, "c": 10, "b": 2, "d": 12})
        self.assertEqual(sorted(calls), ["a", "b", "c"])

    def test_concurrent_retry_limits_recount_on_resume(self):
        calls = []

        def flaky(r):
            calls.append("f")
            raise RuntimeError("x")

        g = TaskGraph()
        g.add("f", flaky)
        with self.assertRaises(TaskExecutionError):
            g.run(max_concurrency=2, retry_limits={"f": 2})
        self.assertEqual(len(calls), 3)
        with self.assertRaises(TaskExecutionError):
            g.run(max_concurrency=2, resume=True, retry_limits={"f": 1})
        self.assertEqual(len(calls), 5)

    # --- snapshot bookkeeping ---------------------------------------

    def test_execution_state_never_exposes_fingerprint(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.run()
        snapshot = g.execution_state()
        self.assertEqual(set(snapshot), {"a"})
        g.run(resume=True)
        self.assertEqual(set(g.execution_state()), {"a"})

    def test_rejected_resume_calls_nothing_and_keeps_old_snapshot(self):
        calls = []

        def fail(r):
            calls.append("f")
            raise RuntimeError("x")

        g = TaskGraph()
        g.add("ok", lambda r: calls.append("ok") or 1)
        g.add("f", fail)
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        before = g.execution_state()
        # scope mismatch
        g.add("later", lambda r: calls.append("later") or 2)
        with self.assertRaises(TaskResumeError):
            g.run(resume=True)
        self.assertEqual(g.execution_state(), before)
        self.assertEqual(set(calls), {"ok", "f"})
        # bad parameter on a resume is also rejected before any call
        with self.assertRaises(TypeError):
            g.run(resume=1)
        self.assertEqual(g.execution_state(), before)
        self.assertEqual(set(calls), {"ok", "f"})

    def test_snapshot_independence_holds_for_resumed_records(self):
        g = TaskGraph()
        g.add("a", lambda r: 1)
        g.add("b", lambda r: (_ for _ in ()).throw(RuntimeError("v")), ["a"])
        with self.assertRaises(TaskExecutionError):
            g.run(continue_on_error=True)
        g.tasks["b"] = lambda r: 2
        g.run(continue_on_error=True, resume=True)
        snapshot = g.execution_state()
        snapshot["a"]["result"] = 99
        snapshot["new"] = {}
        again = g.execution_state()
        self.assertEqual(again["a"]["result"], 1)
        self.assertNotIn("new", again)


if __name__ == "__main__":
    unittest.main()
