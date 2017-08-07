# -*- coding: utf-8 -*-
# :Project:   metapensiero.asyncio.transaction -- Handle coroutines from
#             synchronous functions or methods (like special methods)
# :Created:   dom 09 ago 2015 12:57:35 CEST
# :Author:    Alberto Berti <alberto@metapensiero.it>
# :License:   GNU General Public License version 3 or later
# :Copyright: Copyright (C) 2015 Alberto Berti
#

import asyncio
import functools
import inspect
import logging
import sys
import weakref

PY34 = sys.version_info >= (3, 4)
PY35 = sys.version_info >= (3, 5)

logger = logging.getLogger(__name__)

TMP_CONTEXT = []
TRANSACTIONS = {}

_nodefault = object()

__all__ = ('Transaction', 'get', 'begin', 'end', 'wait_all')

try:
    # travis tests compatibility?!
    from asyncio import ensure_future
except ImportError:
    ensure_future = asyncio.async

class TransactionError(Exception):
    pass


class Transaction:
    """A mechanism to store coroutines and consume them later.
    """

    def __init__(self, trans_id, *, loop=None, registry=None, parent=None):
        self.registry = registry or TRANSACTIONS
        self.coros = []
        self.loop = loop or asyncio.get_event_loop()
        self.id = trans_id
        self.open = True
        self.ending = False
        self.ending_fut = self.loop.create_future()
        self.parent = parent
        self.children = []
        self.task_ending_fut = self.loop.create_future()
        logger.debug('Beginning transaction: %r', self)

    @asyncio.coroutine
    def __aenter__(self):
        if self.id is None:
            task = self._get_current_task(self.loop)
            if task:
                self._set_transaction_id(task, self, self.registry)
                self._add_finalization_cb(task)
            else:
                raise TransactionError('Unable to find the current task')
        return self

    @asyncio.coroutine
    def __aexit__(self, exc_type, exc, tb):
        if not exc:
            yield from self.end()
        else:
            logger.debug('An error happened on context exit:',
                         exc_info=(exc_type, exc, tb))
        return False

    def __enter__(self):
        TMP_CONTEXT.append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        TMP_CONTEXT.pop()
        return False

    def __repr__(self):
        return '<%s id: %s number of items: %d state: %s>' % \
            (self.__class__.__name__, self.id, len(self.coros),
             'open' if self.open else 'closed')

    def _add_finalization_cb(self, task):
        task.add_done_callback(functools.partial(self._owner_task_finalization_cb,
                                                 weakref.ref(self)))

    @staticmethod
    def _get_current_task(loop=None):
        return asyncio.Task.current_task(loop=loop)

    @staticmethod
    def _owner_task_finalization_cb(trans_ref, task):
        """Warn about non-automatic one left open.
        """
        trans = trans_ref()

        if trans:
            trans.task_ending_fut.set_result(None)

        if trans and trans.open and len(trans.coros) > 0:
            msg = ("A transaction has not been closed: %r%s",
                   trans, ", but it has a parent")
            if trans.parent:
                logger.warning(*msg)
            else:
                logger.error(*msg)
                raise TransactionError(msg[0] % (msg[1], ''))

    @staticmethod
    def _set_transaction_id(task, transaction, registry=None):
        if task is None:
            raise TransactionError('No current task')
        if transaction.id != None:
            raise TransactionError('Transaction has an id already')
        registry = registry or TRANSACTIONS
        task_id = id(task)
        trans_list = registry.get(task_id)
        if not trans_list:
            registry[task_id] = trans_list = []
        transaction.id = (task_id, len(trans_list))
        trans_list.append(transaction)

    def add(self, *coros, cback=None):
        """Add a coroutine or awaitable to the set managed by this
        transaction.
        """
        if self.id is None and self not in TMP_CONTEXT:
            raise TransactionError('This transaction is not associated with any'
                                   ' task')
        if self.ending or not open:
            raise ValueError("Cannot add coros to an ending or closed"
                             " transaction: %r" % self)
        out_coros = []
        for coro in coros:
            if PY35:
                assert inspect.isawaitable(coro)
            if coro not in self.coros:
                coro = asyncio.ensure_future(coro, loop=self.loop)
                sub_trans = Transaction.begin(loop=self.loop, task=coro,
                                              parent=self)
                self.children.append(sub_trans)
                if cback:
                    coro.add_done_callback(cback)
                self.coros.append(coro)
            out_coros.append(coro)
        return out_coros

    @classmethod
    def begin(cls, loop=None, *, registry=None, task=None, parent=None):
        """Begin a new transaction"""

        trans = cls(None, loop=loop, registry=registry, parent=parent)

        if __debug__:
            import inspect
            trans._caller_info = inspect.stack()[1:5]

        task = task or cls._get_current_task(trans.loop)
        if task:
            cls._set_transaction_id(task, trans, registry)
            trans._add_finalization_cb(task)
        else:
            logger.warning('Task not found at creation time, will be searched'
                           ' again later')
        return trans

    @asyncio.coroutine
    def end(self):
        """Close an ongoing transaction. It will ask for the results of the future
        returned by the call to ``wait()`` just to raise possible
        exceptions.
        """
        if not self.ending:
            if self.parent is not None:
                yield from self.task_ending_fut

            self.ending = True

            yield from asyncio.gather(*(c.end() for c in self.children),
                                      loop=self.loop)
            try:
                # reraise possible exceptions
                if len(self.coros):
                    result = yield from self.wait()
                else:
                    result = None
                self.ending_fut.set_result(result)
            except Exception as e:
                self.ending_fut.set_exception(e)
            finally:
                logger.debug('Ending transaction: %r', self)
                self.open = False
                self.remove(self)
                del self.registry
                del self.coros
                del self.loop
                del self.parent
                del self.children
        return self.ending_fut

    def gather(self, *coros):
        return asyncio.gather(*self.add(*coros), loop=self.loop)

    @classmethod
    def get(cls, default=_nodefault, loop=None, *, registry=None, task=None):
        """Get the ongoing transaction for the current task. If a current
        transaction is missing either raises an exception or returns
        the passed-in `default`.
        """
        if loop is None:
            loop = asyncio.get_event_loop()
        task = task or asyncio.Task.current_task(loop=loop)
        registry = registry or TRANSACTIONS
        task_id = id(task)
        trans_list = registry.get(task_id)
        if trans_list:
            result = trans_list[-1]
        elif TMP_CONTEXT:
            result = TMP_CONTEXT[-1]
        else:
            if default is _nodefault:
                raise TransactionError("There's no transaction"
                                       " begun for task %s" % task_id)
            else:
                result = default
        return result

    @classmethod
    def remove(cls, trans):
        """Remove a transaction from its registry."""
        if trans.id:
            registry = trans.registry
            trans_list = registry[trans.id[0]]
            assert len(trans_list) > 0
            top_trans = trans_list.pop()
            assert trans is top_trans
            if len(trans_list) == 0:
                del registry[trans.id[0]]

    @asyncio.coroutine
    def wait(self):
        """Wait for this coros to complete, expunge them but not close this
        transaction. Useful when there is one 'global' transaction per task.
        """
        if not self.open:
            raise TransactionError("This transaction is closed already")
        if len(self.coros):
            logger.debug('Waiting for this transaction coros to complete: %r', self)
            result = asyncio.gather(*self.coros, loop=self.loop)
            self.coros.clear()
        else:
            result = None
        return result

    @staticmethod
    @asyncio.coroutine
    def wait_all(timeout=None, loop=None, registry=None):
        """Return a future that will be complete when the pending coros of
        the transactions will complete, effectively ending all of them.
        """
        # TODO: take loop into account
        registry = registry or TRANSACTIONS
        loop = loop or asyncio.get_event_loop()

        # collect pending transactions
        coros = set()
        for task_id, transactions in registry.items():
            for trans in transactions:
                if not trans.parent:
                    coros.add(trans.end())
        if coros:
            result = asyncio.wait(coros, loop=loop, timeout=timeout)
        else:
            result = None
        return result


get = Transaction.get
begin = Transaction.begin


def end(loop=None, registry=None, task=None):
    """End the current defined transaction."""
    trans = get(None, loop=loop, registry=registry, task=task)
    return trans.end()

wait_all = Transaction.wait_all

def wait(loop=None, registry=None, task=None):
    """Wait for the current defined transaction's coroutines to complete."""
    trans = get(None, loop=loop, registry=registry, task=task)
    return trans.wait()
