# -*- coding: utf-8 -*-
# :Project:  metapensiero.asyncio.transaction -- tests
# :Created:    mar 15 dic 2015 15:16:56 CET
# :Author:    Alberto Berti <alberto@metapensiero.it>
# :License:   GNU General Public License version 3 or later
#

import asyncio

import pytest

from metapensiero.asyncio import transaction


@pytest.mark.asyncio
@asyncio.coroutine
def test_transaction_per_task(event_loop):

    tasks_ids = set()
    results = []

    @asyncio.coroutine
    def stashed_coro():
        nonlocal results
        results.append('called stashed_coro')

    def non_coro_func():
        tran = transaction.get(loop=event_loop)
        c = stashed_coro()
        tran.add(c)

    @asyncio.coroutine
    def external_coro():
        nonlocal tasks_ids
        task = asyncio.Task.current_task(loop=event_loop)
        tasks_ids.add(id(task))
        tran = transaction.begin(loop=event_loop)
        # in py3.5
        # async with tran:
        #     non_coro_func()
        non_coro_func()
        yield from tran.end()


    yield from asyncio.gather(
        external_coro(),
        external_coro(),
        loop=event_loop
    )

    assert len(tasks_ids) == 2
    assert len(results) == 2
    assert results == ['called stashed_coro', 'called stashed_coro']


@pytest.mark.asyncio
@asyncio.coroutine
def test_non_closed_transaction(event_loop):

    tasks_ids = set()
    results = []

    event_loop.set_debug(True)

    @asyncio.coroutine
    def stashed_coro():
        nonlocal results
        results.append('called stashed_coro')

    def non_coro_func():
        tran = transaction.get(loop=event_loop)
        c = stashed_coro()
        tran.add(c)

    @asyncio.coroutine
    def external_coro():
        nonlocal tasks_ids
        task = asyncio.Task.current_task(loop=event_loop)
        tasks_ids.add(id(task))
        tran = transaction.begin(loop=event_loop)
        # in py3.5
        # async with tran:
        #     non_coro_func()
        non_coro_func()


    yield from external_coro()
    done, pending = yield from transaction.wait_all()
    # the raise from the callback gets sucked up, this is the only
    # crumb left
    assert len(done) == 1
    assert len(pending) == 0


def test_calling_from_non_task():

    results = []

    @asyncio.coroutine
    def stashed_coro():
        nonlocal results
        results.append('called stashed_coro')

    def non_coro_func():
        tran = transaction.get()
        c = stashed_coro()
        tran.add(c)

    # create a new event loop because i get a closed one with
    # get_event_loop(), probably a conflict with pytest-asyncio
    loop = asyncio.new_event_loop()
    tran = transaction.begin(loop=loop)
    with tran:
        non_coro_func()
    assert len(results) == 0
    loop.run_until_complete(tran.end())
    assert len(results) == 1


@pytest.mark.asyncio
@asyncio.coroutine
def test_transaction_per_task_with_cback(event_loop):

    results = []

    @asyncio.coroutine
    def stashed_coro():
        nonlocal results
        results.append('called stashed_coro')
        return 'result from stashed coro'

    def non_coro_func():
        tran = transaction.get(loop=event_loop)
        c = stashed_coro()
        tran.add(c, cback=_cback)

    def _cback(stashed_task):
        results.append(stashed_task.result())

    @asyncio.coroutine
    def external_coro():
        tran = transaction.begin(loop=event_loop)
        # in py3.5
        # async with tran:
        #     non_coro_func()
        non_coro_func()
        yield from tran.end()

    yield from asyncio.gather(
        external_coro(),
        loop=event_loop
    )

    assert len(results) == 2
    assert results == ['called stashed_coro', 'result from stashed coro']


@pytest.mark.asyncio
@asyncio.coroutine
def test_transaction_per_task_with_cback2(event_loop):

    results = []

    @asyncio.coroutine
    def stashed_coro():
        nonlocal results
        results.append('called stashed_coro')
        return 'result from stashed coro'

    class A:

        def __init__(self):
            tran = transaction.get(loop=event_loop)
            c = stashed_coro()
            tran.add(c, cback=self._init)

        def _init(self, stashed_task):
            nonlocal results
            results.append(stashed_task.result())

    @asyncio.coroutine
    def external_coro():
        tran = transaction.begin(loop=event_loop)
        # in py3.5
        # async with tran:
        #     a = A()
        a = A()
        yield from tran.end()

    yield from asyncio.gather(
        external_coro(),
        loop=event_loop
    )

    assert len(results) == 2
    assert results == ['called stashed_coro', 'result from stashed coro']

@pytest.mark.asyncio
@asyncio.coroutine
def test_switch_to_other_task(event_loop):

    outer_trans = None
    log = []

    @asyncio.coroutine
    def on_another_task():
        log.append('on async')
        trans = transaction.get(None)
        assert trans.parent is outer_trans

    def sync_func():
        trans = transaction.get(None, loop=event_loop)
        assert trans is outer_trans
        another = asyncio.ensure_future(on_another_task())
        log.append('on sync')
        trans.add(another)

    @asyncio.coroutine
    def external_coro():
        nonlocal outer_trans
        t = transaction.begin(loop=event_loop)
        outer_trans = t
        sync_func()
        yield from transaction.end(loop=event_loop)

    yield from external_coro()
    assert log == ['on sync', 'on async']


@pytest.mark.asyncio
@asyncio.coroutine
def test_transaction_and_gather(event_loop):

    @asyncio.coroutine
    def external_coro():
        t = transaction.begin()
        c1, c2 = coro1(), coro2()
        r = yield from asyncio.gather(*t.add(c1, c2), loop=event_loop)
        yield from t.end()
        return r

    @asyncio.coroutine
    def coro1():
        assert event_loop is asyncio.get_event_loop()
        trans = transaction.get(None)
        assert trans is not None
        return 1

    @asyncio.coroutine
    def coro2():
        trans = transaction.get(None)
        assert trans is not None
        return 2

    r = yield from external_coro()
    assert r == [1, 2]


@pytest.mark.asyncio
@asyncio.coroutine
def test_transaction_and_future(event_loop):

    master_trans = None
    end = None

    @asyncio.coroutine
    def external_coro():
        nonlocal master_trans
        trans = transaction.begin(loop=event_loop)
        master_trans = trans
        sync()
        yield from trans.end()

    def sync():
        trans = transaction.get(None, loop=event_loop)
        assert trans is not None
        fut = asyncio.Future(loop=event_loop)
        # there's no way to have some control on the order callbacks are
        # called so i we want to have a callback covered by the transaction,
        # we must add it to the transaction _after_ the callback
        fut.add_done_callback(future_callback)
        trans.add(fut)
        event_loop.call_later(0.5, future_simulated_end, fut)

    def future_simulated_end(future):
        # this is no man's land, no transaction here
        trans = transaction.get(None, loop=event_loop)
        assert trans is None
        future.set_result('Time passed')

    def future_callback(future):
        assert future.result() == 'Time passed'
        # this is no man's land, no transaction here
        trans = transaction.get(None, loop=event_loop)
        assert trans is None
        # but here there's a way to get one because the future was added
        # to the  transaction
        trans = transaction.get(None, loop=event_loop, task=future)
        assert trans is not None and trans.parent is master_trans
        with trans:
            func_called_by_callback()

    def func_called_by_callback():
        nonlocal end
        trans = transaction.get(None, loop=event_loop)
        assert trans is not None and trans.parent is master_trans
        end = 'done!'

    yield from external_coro()
    assert master_trans is not None
    assert end == 'done!'
