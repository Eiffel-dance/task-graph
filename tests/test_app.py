import unittest
import app
from app import TaskGraph, TaskExecutionError


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


if __name__ == "__main__":
    unittest.main()
