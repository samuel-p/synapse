# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import heapq
import logging
import threading
from collections import deque
from contextlib import contextmanager
from typing import Dict, List, Optional, Set, Union

import attr
from typing_extensions import Deque

from synapse.storage.database import DatabasePool, LoggingTransaction
from synapse.storage.util.sequence import PostgresSequenceGenerator

logger = logging.getLogger(__name__)


class IdGenerator:
    def __init__(self, db_conn, table, column):
        self._lock = threading.Lock()
        self._next_id = _load_current_id(db_conn, table, column)

    def get_next(self):
        with self._lock:
            self._next_id += 1
            return self._next_id


def _load_current_id(db_conn, table, column, step=1):
    """

    Args:
        db_conn (object):
        table (str):
        column (str):
        step (int):

    Returns:
        int
    """
    # debug logging for https://github.com/matrix-org/synapse/issues/7968
    logger.info("initialising stream generator for %s(%s)", table, column)
    cur = db_conn.cursor()
    if step == 1:
        cur.execute("SELECT MAX(%s) FROM %s" % (column, table))
    else:
        cur.execute("SELECT MIN(%s) FROM %s" % (column, table))
    (val,) = cur.fetchone()
    cur.close()
    current_id = int(val) if val else step
    return (max if step > 0 else min)(current_id, step)


class StreamIdGenerator:
    """Used to generate new stream ids when persisting events while keeping
    track of which transactions have been completed.

    This allows us to get the "current" stream id, i.e. the stream id such that
    all ids less than or equal to it have completed. This handles the fact that
    persistence of events can complete out of order.

    Args:
        db_conn(connection):  A database connection to use to fetch the
            initial value of the generator from.
        table(str): A database table to read the initial value of the id
            generator from.
        column(str): The column of the database table to read the initial
            value from the id generator from.
        extra_tables(list): List of pairs of database tables and columns to
            use to source the initial value of the generator from. The value
            with the largest magnitude is used.
        step(int): which direction the stream ids grow in. +1 to grow
            upwards, -1 to grow downwards.

    Usage:
        async with stream_id_gen.get_next() as stream_id:
            # ... persist event ...
    """

    def __init__(self, db_conn, table, column, extra_tables=[], step=1):
        assert step != 0
        self._lock = threading.Lock()
        self._step = step
        self._current = _load_current_id(db_conn, table, column, step)
        for table, column in extra_tables:
            self._current = (max if step > 0 else min)(
                self._current, _load_current_id(db_conn, table, column, step)
            )
        self._unfinished_ids = deque()  # type: Deque[int]

    def get_next(self):
        """
        Usage:
            async with stream_id_gen.get_next() as stream_id:
                # ... persist event ...
        """
        with self._lock:
            self._current += self._step
            next_id = self._current

            self._unfinished_ids.append(next_id)

        @contextmanager
        def manager():
            try:
                yield next_id
            finally:
                with self._lock:
                    self._unfinished_ids.remove(next_id)

        return _AsyncCtxManagerWrapper(manager())

    def get_next_mult(self, n):
        """
        Usage:
            async with stream_id_gen.get_next(n) as stream_ids:
                # ... persist events ...
        """
        with self._lock:
            next_ids = range(
                self._current + self._step,
                self._current + self._step * (n + 1),
                self._step,
            )
            self._current += n * self._step

            for next_id in next_ids:
                self._unfinished_ids.append(next_id)

        @contextmanager
        def manager():
            try:
                yield next_ids
            finally:
                with self._lock:
                    for next_id in next_ids:
                        self._unfinished_ids.remove(next_id)

        return _AsyncCtxManagerWrapper(manager())

    def get_current_token(self):
        """Returns the maximum stream id such that all stream ids less than or
        equal to it have been successfully persisted.

        Returns:
            int
        """
        with self._lock:
            if self._unfinished_ids:
                return self._unfinished_ids[0] - self._step

            return self._current

    def get_current_token_for_writer(self, instance_name: str) -> int:
        """Returns the position of the given writer.

        For streams with single writers this is equivalent to
        `get_current_token`.
        """
        return self.get_current_token()


class MultiWriterIdGenerator:
    """An ID generator that tracks a stream that can have multiple writers.

    Uses a Postgres sequence to coordinate ID assignment, but positions of other
    writers will only get updated when `advance` is called (by replication).

    Note: Only works with Postgres.

    Args:
        db_conn
        db
        instance_name: The name of this instance.
        table: Database table associated with stream.
        instance_column: Column that stores the row's writer's instance name
        id_column: Column that stores the stream ID.
        sequence_name: The name of the postgres sequence used to generate new
            IDs.
        positive: Whether the IDs are positive (true) or negative (false).
            When using negative IDs we go backwards from -1 to -2, -3, etc.
    """

    def __init__(
        self,
        db_conn,
        db: DatabasePool,
        instance_name: str,
        table: str,
        instance_column: str,
        id_column: str,
        sequence_name: str,
        positive: bool = True,
    ):
        self._db = db
        self._instance_name = instance_name
        self._positive = positive
        self._return_factor = 1 if positive else -1

        # We lock as some functions may be called from DB threads.
        self._lock = threading.Lock()

        # Note: If we are a negative stream then we still store all the IDs as
        # positive to make life easier for us, and simply negate the IDs when we
        # return them.
        self._current_positions = self._load_current_ids(
            db_conn, table, instance_column, id_column
        )

        # Set of local IDs that we're still processing. The current position
        # should be less than the minimum of this set (if not empty).
        self._unfinished_ids = set()  # type: Set[int]

        # Set of local IDs that we've processed that are larger than the current
        # position, due to there being smaller unpersisted IDs.
        self._finished_ids = set()  # type: Set[int]

        # We track the max position where we know everything before has been
        # persisted. This is done by a) looking at the min across all instances
        # and b) noting that if we have seen a run of persisted positions
        # without gaps (e.g. 5, 6, 7) then we can skip forward (e.g. to 7).
        #
        # Note: There is no guarentee that the IDs generated by the sequence
        # will be gapless; gaps can form when e.g. a transaction was rolled
        # back. This means that sometimes we won't be able to skip forward the
        # position even though everything has been persisted. However, since
        # gaps should be relatively rare it's still worth doing the book keeping
        # that allows us to skip forwards when there are gapless runs of
        # positions.
        #
        # We start at 1 here as a) the first generated stream ID will be 2, and
        # b) other parts of the code assume that stream IDs are strictly greater
        # than 0.
        self._persisted_upto_position = (
            min(self._current_positions.values()) if self._current_positions else 1
        )
        self._known_persisted_positions = []  # type: List[int]

        self._sequence_gen = PostgresSequenceGenerator(sequence_name)

    def _load_current_ids(
        self, db_conn, table: str, instance_column: str, id_column: str
    ) -> Dict[str, int]:
        # If positive stream aggregate via MAX. For negative stream use MIN
        # *and* negate the result to get a positive number.
        sql = """
            SELECT %(instance)s, %(agg)s(%(id)s) FROM %(table)s
            GROUP BY %(instance)s
        """ % {
            "instance": instance_column,
            "id": id_column,
            "table": table,
            "agg": "MAX" if self._positive else "-MIN",
        }

        cur = db_conn.cursor()
        cur.execute(sql)

        # `cur` is an iterable over returned rows, which are 2-tuples.
        current_positions = dict(cur)

        cur.close()

        return current_positions

    def _load_next_id_txn(self, txn) -> int:
        return self._sequence_gen.get_next_id_txn(txn)

    def _load_next_mult_id_txn(self, txn, n: int) -> List[int]:
        return self._sequence_gen.get_next_mult_txn(txn, n)

    def get_next(self):
        """
        Usage:
            async with stream_id_gen.get_next() as stream_id:
                # ... persist event ...
        """

        return _MultiWriterCtxManager(self)

    def get_next_mult(self, n: int):
        """
        Usage:
            async with stream_id_gen.get_next_mult(5) as stream_ids:
                # ... persist events ...
        """

        return _MultiWriterCtxManager(self, n)

    def get_next_txn(self, txn: LoggingTransaction):
        """
        Usage:

            stream_id = stream_id_gen.get_next(txn)
            # ... persist event ...
        """

        next_id = self._load_next_id_txn(txn)

        with self._lock:
            self._unfinished_ids.add(next_id)

        txn.call_after(self._mark_id_as_finished, next_id)
        txn.call_on_exception(self._mark_id_as_finished, next_id)

        return self._return_factor * next_id

    def _mark_id_as_finished(self, next_id: int):
        """The ID has finished being processed so we should advance the
        current position if possible.
        """

        with self._lock:
            self._unfinished_ids.discard(next_id)
            self._finished_ids.add(next_id)

            new_cur = None

            if self._unfinished_ids:
                # If there are unfinished IDs then the new position will be the
                # largest finished ID less than the minimum unfinished ID.

                finished = set()

                min_unfinshed = min(self._unfinished_ids)
                for s in self._finished_ids:
                    if s < min_unfinshed:
                        if new_cur is None or new_cur < s:
                            new_cur = s
                    else:
                        finished.add(s)

                # We clear these out since they're now all less than the new
                # position.
                self._finished_ids = finished
            else:
                # There are no unfinished IDs so the new position is simply the
                # largest finished one.
                new_cur = max(self._finished_ids)

                # We clear these out since they're now all less than the new
                # position.
                self._finished_ids.clear()

            if new_cur:
                curr = self._current_positions.get(self._instance_name, 0)
                self._current_positions[self._instance_name] = max(curr, new_cur)

            self._add_persisted_position(next_id)

    def get_current_token(self) -> int:
        """Returns the maximum stream id such that all stream ids less than or
        equal to it have been successfully persisted.
        """

        return self.get_persisted_upto_position()

    def get_current_token_for_writer(self, instance_name: str) -> int:
        """Returns the position of the given writer.
        """

        with self._lock:
            return self._return_factor * self._current_positions.get(instance_name, 0)

    def get_positions(self) -> Dict[str, int]:
        """Get a copy of the current positon map.
        """

        with self._lock:
            return {
                name: self._return_factor * i
                for name, i in self._current_positions.items()
            }

    def advance(self, instance_name: str, new_id: int):
        """Advance the postion of the named writer to the given ID, if greater
        than existing entry.
        """

        new_id *= self._return_factor

        with self._lock:
            self._current_positions[instance_name] = max(
                new_id, self._current_positions.get(instance_name, 0)
            )

            self._add_persisted_position(new_id)

    def get_persisted_upto_position(self) -> int:
        """Get the max position where all previous positions have been
        persisted.

        Note: In the worst case scenario this will be equal to the minimum
        position across writers. This means that the returned position here can
        lag if one writer doesn't write very often.
        """

        with self._lock:
            return self._return_factor * self._persisted_upto_position

    def _add_persisted_position(self, new_id: int):
        """Record that we have persisted a position.

        This is used to keep the `_current_positions` up to date.
        """

        # We require that the lock is locked by caller
        assert self._lock.locked()

        heapq.heappush(self._known_persisted_positions, new_id)

        # We move the current min position up if the minimum current positions
        # of all instances is higher (since by definition all positions less
        # that that have been persisted).
        min_curr = min(self._current_positions.values(), default=0)
        self._persisted_upto_position = max(min_curr, self._persisted_upto_position)

        # We now iterate through the seen positions, discarding those that are
        # less than the current min positions, and incrementing the min position
        # if its exactly one greater.
        #
        # This is also where we discard items from `_known_persisted_positions`
        # (to ensure the list doesn't infinitely grow).
        while self._known_persisted_positions:
            if self._known_persisted_positions[0] <= self._persisted_upto_position:
                heapq.heappop(self._known_persisted_positions)
            elif (
                self._known_persisted_positions[0] == self._persisted_upto_position + 1
            ):
                heapq.heappop(self._known_persisted_positions)
                self._persisted_upto_position += 1
            else:
                # There was a gap in seen positions, so there is nothing more to
                # do.
                break


@attr.s(slots=True)
class _AsyncCtxManagerWrapper:
    """Helper class to convert a plain context manager to an async one.

    This is mainly useful if you have a plain context manager but the interface
    requires an async one.
    """

    inner = attr.ib()

    async def __aenter__(self):
        return self.inner.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.inner.__exit__(exc_type, exc, tb)


@attr.s(slots=True)
class _MultiWriterCtxManager:
    """Async context manager returned by MultiWriterIdGenerator
    """

    id_gen = attr.ib(type=MultiWriterIdGenerator)
    multiple_ids = attr.ib(type=Optional[int], default=None)
    stream_ids = attr.ib(type=List[int], factory=list)

    async def __aenter__(self) -> Union[int, List[int]]:
        self.stream_ids = await self.id_gen._db.runInteraction(
            "_load_next_mult_id",
            self.id_gen._load_next_mult_id_txn,
            self.multiple_ids or 1,
        )

        # Assert the fetched ID is actually greater than any ID we've already
        # seen. If not, then the sequence and table have got out of sync
        # somehow.
        with self.id_gen._lock:
            assert max(self.id_gen._current_positions.values(), default=0) < min(
                self.stream_ids
            )

            self.id_gen._unfinished_ids.update(self.stream_ids)

        if self.multiple_ids is None:
            return self.stream_ids[0] * self.id_gen._return_factor
        else:
            return [i * self.id_gen._return_factor for i in self.stream_ids]

    async def __aexit__(self, exc_type, exc, tb):
        for i in self.stream_ids:
            self.id_gen._mark_id_as_finished(i)

        if exc_type is not None:
            return False

        return False
