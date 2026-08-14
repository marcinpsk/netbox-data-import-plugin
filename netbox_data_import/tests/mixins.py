# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Test mixins that preserve parallel worker isolation."""

from django.core.cache import cache
from django_rq import get_queue
from utilities.testing.mixins import RQQueueTestMixin


class IsolatedRQQueueTestMixin(RQQueueTestMixin):
    """Clear only the current worker's Redis database."""

    @classmethod
    def clear_rq_queues(cls):
        """Clear queue state without flushing other workers' databases."""
        cache.clear()
        for queue_name in cls.rq_queue_names:
            get_queue(queue_name).connection.flushdb()
