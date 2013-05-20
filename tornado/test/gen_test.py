from __future__ import absolute_import, division, print_function, with_statement

import contextlib
import functools
import sys
import textwrap
import time
import platform
import weakref

from tornado.concurrent import return_future
from tornado.escape import url_escape
from tornado.httpclient import AsyncHTTPClient
from tornado.ioloop import IOLoop
from tornado.log import app_log
from tornado import stack_context
from tornado.testing import AsyncHTTPTestCase, AsyncTestCase, ExpectLog, gen_test
from tornado.test.util import unittest, skipOnTravis
from tornado.web import Application, RequestHandler, asynchronous, HTTPError

from tornado import gen


skipBefore33 = unittest.skipIf(sys.version_info < (3, 3), 'PEP 380 not available')
skipNotCPython = unittest.skipIf(platform.python_implementation() != 'CPython',
                                'Not CPython implementation')


class GenEngineTest(AsyncTestCase):
    def setUp(self):
        super(GenEngineTest, self).setUp()
        self.named_contexts = []

    def named_context(self, name):
        @contextlib.contextmanager
        def context():
            self.named_contexts.append(name)
            try:
                yield
            finally:
                self.assertEqual(self.named_contexts.pop(), name)
        return context

    def run_gen(self, f):
        f()
        return self.wait()

    def delay_callback(self, iterations, callback, arg):
        """Runs callback(arg) after a number of IOLoop iterations."""
        if iterations == 0:
            callback(arg)
        else:
            self.io_loop.add_callback(functools.partial(
                self.delay_callback, iterations - 1, callback, arg))

    @return_future
    def async_future(self, result, callback):
        self.io_loop.add_callback(callback, result)

    def test_no_yield(self):
        @gen.engine
        def f():
            self.stop()
        self.run_gen(f)

    def test_inline_cb(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))()
            res = yield gen.Wait("k1")
            self.assertTrue(res is None)
            self.stop()
        self.run_gen(f)

    def test_ioloop_cb(self):
        @gen.engine
        def f():
            self.io_loop.add_callback((yield gen.Callback("k1")))
            yield gen.Wait("k1")
            self.stop()
        self.run_gen(f)

    def test_exception_phase1(self):
        @gen.engine
        def f():
            1 / 0
        self.assertRaises(ZeroDivisionError, self.run_gen, f)

    def test_exception_phase2(self):
        @gen.engine
        def f():
            self.io_loop.add_callback((yield gen.Callback("k1")))
            yield gen.Wait("k1")
            1 / 0
        self.assertRaises(ZeroDivisionError, self.run_gen, f)

    def test_exception_in_task_phase1(self):
        def fail_task(callback):
            1 / 0

        @gen.engine
        def f():
            try:
                yield gen.Task(fail_task)
                raise Exception("did not get expected exception")
            except ZeroDivisionError:
                self.stop()
        self.run_gen(f)

    def test_exception_in_task_phase2(self):
        # This is the case that requires the use of stack_context in gen.engine
        def fail_task(callback):
            self.io_loop.add_callback(lambda: 1 / 0)

        @gen.engine
        def f():
            try:
                yield gen.Task(fail_task)
                raise Exception("did not get expected exception")
            except ZeroDivisionError:
                self.stop()
        self.run_gen(f)

    def test_with_arg(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))(42)
            res = yield gen.Wait("k1")
            self.assertEqual(42, res)
            self.stop()
        self.run_gen(f)

    def test_with_arg_tuple(self):
        @gen.engine
        def f():
            (yield gen.Callback((1, 2)))((3, 4))
            res = yield gen.Wait((1, 2))
            self.assertEqual((3, 4), res)
            self.stop()
        self.run_gen(f)

    def test_key_reuse(self):
        @gen.engine
        def f():
            yield gen.Callback("k1")
            yield gen.Callback("k1")
            self.stop()
        self.assertRaises(gen.KeyReuseError, self.run_gen, f)

    def test_key_reuse_tuple(self):
        @gen.engine
        def f():
            yield gen.Callback((1, 2))
            yield gen.Callback((1, 2))
            self.stop()
        self.assertRaises(gen.KeyReuseError, self.run_gen, f)

    def test_key_mismatch(self):
        @gen.engine
        def f():
            yield gen.Callback("k1")
            yield gen.Wait("k2")
            self.stop()
        self.assertRaises(gen.UnknownKeyError, self.run_gen, f)

    def test_key_mismatch_tuple(self):
        @gen.engine
        def f():
            yield gen.Callback((1, 2))
            yield gen.Wait((2, 3))
            self.stop()
        self.assertRaises(gen.UnknownKeyError, self.run_gen, f)

    def test_leaked_callback(self):
        @gen.engine
        def f():
            yield gen.Callback("k1")
            self.stop()
        self.assertRaises(gen.LeakedCallbackError, self.run_gen, f)

    def test_leaked_callback_tuple(self):
        @gen.engine
        def f():
            yield gen.Callback((1, 2))
            self.stop()
        self.assertRaises(gen.LeakedCallbackError, self.run_gen, f)

    def test_parallel_callback(self):
        @gen.engine
        def f():
            for k in range(3):
                self.io_loop.add_callback((yield gen.Callback(k)))
            yield gen.Wait(1)
            self.io_loop.add_callback((yield gen.Callback(3)))
            yield gen.Wait(0)
            yield gen.Wait(3)
            yield gen.Wait(2)
            self.stop()
        self.run_gen(f)

    def test_bogus_yield(self):
        @gen.engine
        def f():
            yield 42
        self.assertRaises(gen.BadYieldError, self.run_gen, f)

    def test_bogus_yield_tuple(self):
        @gen.engine
        def f():
            yield (1, 2)
        self.assertRaises(gen.BadYieldError, self.run_gen, f)

    def test_reuse(self):
        @gen.engine
        def f():
            self.io_loop.add_callback((yield gen.Callback(0)))
            yield gen.Wait(0)
            self.stop()
        self.run_gen(f)
        self.run_gen(f)

    def test_task(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            self.stop()
        self.run_gen(f)

    def test_wait_all(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))("v1")
            (yield gen.Callback("k2"))("v2")
            results = yield gen.WaitAll(["k1", "k2"])
            self.assertEqual(results, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    def test_exception_in_yield(self):
        @gen.engine
        def f():
            try:
                yield gen.Wait("k1")
                raise Exception("did not get expected exception")
            except gen.UnknownKeyError:
                pass
            self.stop()
        self.run_gen(f)

    def test_resume_after_exception_in_yield(self):
        @gen.engine
        def f():
            try:
                yield gen.Wait("k1")
                raise Exception("did not get expected exception")
            except gen.UnknownKeyError:
                pass
            (yield gen.Callback("k2"))("v2")
            self.assertEqual((yield gen.Wait("k2")), "v2")
            self.stop()
        self.run_gen(f)

    def test_orphaned_callback(self):
        @gen.engine
        def f():
            self.orphaned_callback = yield gen.Callback(1)
        try:
            self.run_gen(f)
            raise Exception("did not get expected exception")
        except gen.LeakedCallbackError:
            pass
        self.orphaned_callback()

    def test_multi(self):
        @gen.engine
        def f():
            (yield gen.Callback("k1"))("v1")
            (yield gen.Callback("k2"))("v2")
            results = yield [gen.Wait("k1"), gen.Wait("k2")]
            self.assertEqual(results, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    def test_multi_delayed(self):
        @gen.engine
        def f():
            # callbacks run at different times
            responses = yield [
                gen.Task(self.delay_callback, 3, arg="v1"),
                gen.Task(self.delay_callback, 1, arg="v2"),
            ]
            self.assertEqual(responses, ["v1", "v2"])
            self.stop()
        self.run_gen(f)

    @skipOnTravis
    @gen_test
    def test_multi_performance(self):
        # Yielding a list used to have quadratic performance; make
        # sure a large list stays reasonable.  On my laptop a list of
        # 2000 used to take 1.8s, now it takes 0.12.
        start = time.time()
        yield [gen.Task(self.io_loop.add_callback) for i in range(2000)]
        end = time.time()
        self.assertLess(end - start, 1.0)

    @gen_test
    def test_future(self):
        result = yield self.async_future(1)
        self.assertEqual(result, 1)

    @gen_test
    def test_multi_future(self):
        results = yield [self.async_future(1), self.async_future(2)]
        self.assertEqual(results, [1, 2])

    def test_arguments(self):
        @gen.engine
        def f():
            (yield gen.Callback("noargs"))()
            self.assertEqual((yield gen.Wait("noargs")), None)
            (yield gen.Callback("1arg"))(42)
            self.assertEqual((yield gen.Wait("1arg")), 42)

            (yield gen.Callback("kwargs"))(value=42)
            result = yield gen.Wait("kwargs")
            self.assertTrue(isinstance(result, gen.Arguments))
            self.assertEqual(((), dict(value=42)), result)
            self.assertEqual(dict(value=42), result.kwargs)

            (yield gen.Callback("2args"))(42, 43)
            result = yield gen.Wait("2args")
            self.assertTrue(isinstance(result, gen.Arguments))
            self.assertEqual(((42, 43), {}), result)
            self.assertEqual((42, 43), result.args)

            def task_func(callback):
                callback(None, error="foo")
            result = yield gen.Task(task_func)
            self.assertTrue(isinstance(result, gen.Arguments))
            self.assertEqual(((None,), dict(error="foo")), result)

            self.stop()
        self.run_gen(f)

    def test_stack_context_leak(self):
        # regression test: repeated invocations of a gen-based
        # function should not result in accumulated stack_contexts
        def _stack_depth():
            head = stack_context._state.contexts[1]
            length = 0

            while head is not None:
                length += 1
                head = head.old_contexts[1]

            return length

        @gen.engine
        def inner(callback):
            yield gen.Task(self.io_loop.add_callback)
            callback()

        @gen.engine
        def outer():
            for i in range(10):
                yield gen.Task(inner)

            stack_increase = _stack_depth() - initial_stack_depth
            self.assertTrue(stack_increase <= 2)
            self.stop()
        initial_stack_depth = _stack_depth()
        self.run_gen(outer)

    def test_stack_context_leak_exception(self):
        # same as previous, but with a function that exits with an exception
        @gen.engine
        def inner(callback):
            yield gen.Task(self.io_loop.add_callback)
            1 / 0

        @gen.engine
        def outer():
            for i in range(10):
                try:
                    yield gen.Task(inner)
                except ZeroDivisionError:
                    pass
            stack_increase = len(stack_context._state.contexts) - initial_stack_depth
            self.assertTrue(stack_increase <= 2)
            self.stop()
        initial_stack_depth = len(stack_context._state.contexts)
        self.run_gen(outer)

    def function_with_stack_context(self, callback):
        # Technically this function should stack_context.wrap its callback
        # upon entry.  However, it is very common for this step to be
        # omitted.
        def step2():
            self.assertEqual(self.named_contexts, ['a'])
            self.io_loop.add_callback(callback)

        with stack_context.StackContext(self.named_context('a')):
            self.io_loop.add_callback(step2)

    @gen_test
    def test_wait_transfer_stack_context(self):
        # Wait should not pick up contexts from where callback was invoked,
        # even if that function improperly fails to wrap its callback.
        cb = yield gen.Callback('k1')
        self.function_with_stack_context(cb)
        self.assertEqual(self.named_contexts, [])
        yield gen.Wait('k1')
        self.assertEqual(self.named_contexts, [])

    @gen_test
    def test_task_transfer_stack_context(self):
        yield gen.Task(self.function_with_stack_context)
        self.assertEqual(self.named_contexts, [])

    def test_raise_after_stop(self):
        # This pattern will be used in the following tests so make sure
        # the exception propagates as expected.
        @gen.engine
        def f():
            self.stop()
            1 / 0

        with self.assertRaises(ZeroDivisionError):
            self.run_gen(f)

    def test_sync_raise_return(self):
        # gen.Return is allowed in @gen.engine, but it may not be used
        # to return a value.
        @gen.engine
        def f():
            self.stop(42)
            raise gen.Return()

        result = self.run_gen(f)
        self.assertEqual(result, 42)

    def test_async_raise_return(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            self.stop(42)
            raise gen.Return()

        result = self.run_gen(f)
        self.assertEqual(result, 42)

    def test_sync_raise_return_value(self):
        @gen.engine
        def f():
            raise gen.Return(42)

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_sync_raise_return_value_tuple(self):
        @gen.engine
        def f():
            raise gen.Return((1, 2))

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_async_raise_return_value(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            raise gen.Return(42)

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_async_raise_return_value_tuple(self):
        @gen.engine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            raise gen.Return((1, 2))

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_return_value(self):
        # It is an error to apply @gen.engine to a function that returns
        # a value.
        @gen.engine
        def f():
            return 42

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    def test_return_value_tuple(self):
        # It is an error to apply @gen.engine to a function that returns
        # a value.
        @gen.engine
        def f():
            return (1, 2)

        with self.assertRaises(gen.ReturnValueIgnoredError):
            self.run_gen(f)

    @skipNotCPython
    def test_task_refcounting(self):
        # On CPython, tasks and their arguments should be released immediately
        # without waiting for garbage collection.
        @gen.engine
        def f():
            class Foo(object): pass
            arg = Foo()
            self.arg_ref = weakref.ref(arg)
            task = gen.Task(self.io_loop.add_callback, arg=arg)
            self.task_ref = weakref.ref(task)
            yield task
            self.stop()

        self.run_gen(f)
        self.assertIs(self.arg_ref(), None)
        self.assertIs(self.task_ref(), None)


class GenCoroutineTest(AsyncTestCase):
    def setUp(self):
        # Stray StopIteration exceptions can lead to tests exiting prematurely,
        # so we need explicit checks here to make sure the tests run all
        # the way through.
        self.finished = False
        super(GenCoroutineTest, self).setUp()

    def tearDown(self):
        super(GenCoroutineTest, self).tearDown()
        assert self.finished

    @gen_test
    def test_sync_gen_return(self):
        @gen.coroutine
        def f():
            raise gen.Return(42)
        result = yield f()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_async_gen_return(self):
        @gen.coroutine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            raise gen.Return(42)
        result = yield f()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_sync_return(self):
        @gen.coroutine
        def f():
            return 42
        result = yield f()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore33
    @gen_test
    def test_async_return(self):
        # It is a compile-time error to return a value in a generator
        # before Python 3.3, so we must test this with exec.
        # Flatten the real global and local namespace into our fake globals:
        # it's all global from the perspective of f().
        global_namespace = dict(globals(), **locals())
        local_namespace = {}
        exec(textwrap.dedent("""
        @gen.coroutine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            return 42
        """), global_namespace, local_namespace)
        result = yield local_namespace['f']()
        self.assertEqual(result, 42)
        self.finished = True

    @skipBefore33
    @gen_test
    def test_async_early_return(self):
        # A yield statement exists but is not executed, which means
        # this function "returns" via an exception.  This exception
        # doesn't happen before the exception handling is set up.
        global_namespace = dict(globals(), **locals())
        local_namespace = {}
        exec(textwrap.dedent("""
        @gen.coroutine
        def f():
            if True:
                return 42
            yield gen.Task(self.io_loop.add_callback)
        """), global_namespace, local_namespace)
        result = yield local_namespace['f']()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_sync_return_no_value(self):
        @gen.coroutine
        def f():
            return
        result = yield f()
        self.assertEqual(result, None)
        self.finished = True

    @gen_test
    def test_async_return_no_value(self):
        # Without a return value we don't need python 3.3.
        @gen.coroutine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            return
        result = yield f()
        self.assertEqual(result, None)
        self.finished = True

    @gen_test
    def test_sync_raise(self):
        @gen.coroutine
        def f():
            1 / 0
        # The exception is raised when the future is yielded
        # (or equivalently when its result method is called),
        # not when the function itself is called).
        future = f()
        with self.assertRaises(ZeroDivisionError):
            yield future
        self.finished = True

    @gen_test
    def test_async_raise(self):
        @gen.coroutine
        def f():
            yield gen.Task(self.io_loop.add_callback)
            1 / 0
        future = f()
        with self.assertRaises(ZeroDivisionError):
            yield future
        self.finished = True

    @gen_test
    def test_pass_callback(self):
        @gen.coroutine
        def f():
            raise gen.Return(42)
        result = yield gen.Task(f)
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_replace_yieldpoint_exception(self):
        # Test exception handling: a coroutine can catch one exception
        # raised by a yield point and raise a different one.
        @gen.coroutine
        def f1():
            1 / 0

        @gen.coroutine
        def f2():
            try:
                yield f1()
            except ZeroDivisionError:
                raise KeyError()

        future = f2()
        with self.assertRaises(KeyError):
            yield future
        self.finished = True

    @gen_test
    def test_swallow_yieldpoint_exception(self):
        # Test exception handling: a coroutine can catch an exception
        # raised by a yield point and not raise a different one.
        @gen.coroutine
        def f1():
            1 / 0

        @gen.coroutine
        def f2():
            try:
                yield f1()
            except ZeroDivisionError:
                raise gen.Return(42)

        result = yield f2()
        self.assertEqual(result, 42)
        self.finished = True

    @gen_test
    def test_replace_context_exception(self):
        # Test exception handling: exceptions thrown into the stack context
        # can be caught and replaced.
        @gen.coroutine
        def f2():
            self.io_loop.add_callback(lambda: 1 / 0)
            try:
                yield gen.Task(self.io_loop.add_timeout,
                               self.io_loop.time() + 10)
            except ZeroDivisionError:
                raise KeyError()

        future = f2()
        with self.assertRaises(KeyError):
            yield future
        self.finished = True

    @gen_test
    def test_swallow_context_exception(self):
        # Test exception handling: exceptions thrown into the stack context
        # can be caught and ignored.
        @gen.coroutine
        def f2():
            self.io_loop.add_callback(lambda: 1 / 0)
            try:
                yield gen.Task(self.io_loop.add_timeout,
                               self.io_loop.time() + 10)
            except ZeroDivisionError:
                raise gen.Return(42)

        result = yield f2()
        self.assertEqual(result, 42)
        self.finished = True


class GenSequenceHandler(RequestHandler):
    @asynchronous
    @gen.engine
    def get(self):
        self.io_loop = self.request.connection.stream.io_loop
        self.io_loop.add_callback((yield gen.Callback("k1")))
        yield gen.Wait("k1")
        self.write("1")
        self.io_loop.add_callback((yield gen.Callback("k2")))
        yield gen.Wait("k2")
        self.write("2")
        # reuse an old key
        self.io_loop.add_callback((yield gen.Callback("k1")))
        yield gen.Wait("k1")
        self.finish("3")


class GenCoroutineSequenceHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        self.io_loop = self.request.connection.stream.io_loop
        self.io_loop.add_callback((yield gen.Callback("k1")))
        yield gen.Wait("k1")
        self.write("1")
        self.io_loop.add_callback((yield gen.Callback("k2")))
        yield gen.Wait("k2")
        self.write("2")
        # reuse an old key
        self.io_loop.add_callback((yield gen.Callback("k1")))
        yield gen.Wait("k1")
        self.finish("3")


class GenCoroutineUnfinishedSequenceHandler(RequestHandler):
    @asynchronous
    @gen.coroutine
    def get(self):
        self.io_loop = self.request.connection.stream.io_loop
        self.io_loop.add_callback((yield gen.Callback("k1")))
        yield gen.Wait("k1")
        self.write("1")
        self.io_loop.add_callback((yield gen.Callback("k2")))
        yield gen.Wait("k2")
        self.write("2")
        # reuse an old key
        self.io_loop.add_callback((yield gen.Callback("k1")))
        yield gen.Wait("k1")
        # just write, don't finish
        self.write("3")


class GenTaskHandler(RequestHandler):
    @asynchronous
    @gen.engine
    def get(self):
        io_loop = self.request.connection.stream.io_loop
        client = AsyncHTTPClient(io_loop=io_loop)
        response = yield gen.Task(client.fetch, self.get_argument('url'))
        response.rethrow()
        self.finish(b"got response: " + response.body)


class GenExceptionHandler(RequestHandler):
    @asynchronous
    @gen.engine
    def get(self):
        # This test depends on the order of the two decorators.
        io_loop = self.request.connection.stream.io_loop
        yield gen.Task(io_loop.add_callback)
        raise Exception("oops")


class GenCoroutineExceptionHandler(RequestHandler):
    @asynchronous
    @gen.coroutine
    def get(self):
        # This test depends on the order of the two decorators.
        io_loop = self.request.connection.stream.io_loop
        yield gen.Task(io_loop.add_callback)
        raise Exception("oops")


class GenYieldExceptionHandler(RequestHandler):
    @asynchronous
    @gen.engine
    def get(self):
        io_loop = self.request.connection.stream.io_loop
        # Test the interaction of the two stack_contexts.

        def fail_task(callback):
            io_loop.add_callback(lambda: 1 / 0)
        try:
            yield gen.Task(fail_task)
            raise Exception("did not get expected exception")
        except ZeroDivisionError:
            self.finish('ok')


class UndecoratedCoroutinesHandler(RequestHandler):
    @gen.coroutine
    def prepare(self):
        self.chunks = []
        yield gen.Task(IOLoop.current().add_callback)
        self.chunks.append('1')

    @gen.coroutine
    def get(self):
        self.chunks.append('2')
        yield gen.Task(IOLoop.current().add_callback)
        self.chunks.append('3')
        yield gen.Task(IOLoop.current().add_callback)
        self.write(''.join(self.chunks))


class AsyncPrepareErrorHandler(RequestHandler):
    @gen.coroutine
    def prepare(self):
        yield gen.Task(IOLoop.current().add_callback)
        raise HTTPError(403)

    def get(self):
        self.finish('ok')


class GenWebTest(AsyncHTTPTestCase):
    def get_app(self):
        return Application([
            ('/sequence', GenSequenceHandler),
            ('/coroutine_sequence', GenCoroutineSequenceHandler),
            ('/coroutine_unfinished_sequence',
             GenCoroutineUnfinishedSequenceHandler),
            ('/task', GenTaskHandler),
            ('/exception', GenExceptionHandler),
            ('/coroutine_exception', GenCoroutineExceptionHandler),
            ('/yield_exception', GenYieldExceptionHandler),
            ('/undecorated_coroutine', UndecoratedCoroutinesHandler),
            ('/async_prepare_error', AsyncPrepareErrorHandler),
        ])

    def test_sequence_handler(self):
        response = self.fetch('/sequence')
        self.assertEqual(response.body, b"123")

    def test_coroutine_sequence_handler(self):
        response = self.fetch('/coroutine_sequence')
        self.assertEqual(response.body, b"123")

    def test_coroutine_unfinished_sequence_handler(self):
        response = self.fetch('/coroutine_unfinished_sequence')
        self.assertEqual(response.body, b"123")

    def test_task_handler(self):
        response = self.fetch('/task?url=%s' % url_escape(self.get_url('/sequence')))
        self.assertEqual(response.body, b"got response: 123")

    def test_exception_handler(self):
        # Make sure we get an error and not a timeout
        with ExpectLog(app_log, "Uncaught exception GET /exception"):
            response = self.fetch('/exception')
        self.assertEqual(500, response.code)

    def test_coroutine_exception_handler(self):
        # Make sure we get an error and not a timeout
        with ExpectLog(app_log, "Uncaught exception GET /coroutine_exception"):
            response = self.fetch('/coroutine_exception')
        self.assertEqual(500, response.code)

    def test_yield_exception_handler(self):
        response = self.fetch('/yield_exception')
        self.assertEqual(response.body, b'ok')

    def test_undecorated_coroutines(self):
        response = self.fetch('/undecorated_coroutine')
        self.assertEqual(response.body, b'123')

    def test_async_prepare_error_handler(self):
        response = self.fetch('/async_prepare_error')
        self.assertEqual(response.code, 403)

if __name__ == '__main__':
    unittest.main()
