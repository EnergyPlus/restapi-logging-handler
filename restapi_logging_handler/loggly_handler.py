from __future__ import absolute_import

import atexit
import json
import os
import threading
from functools import partial

import requests

from restapi_logging_handler.restapi_logging_handler import (
    RestApiHandler,
    serialize,
)


def setInterval(interval):
    def decorator(function):
        def wrapper(*args, **kwargs):
            stopped = threading.Event()

            def loop():  # executed in another thread
                while not stopped.wait(interval):  # until stopped
                    function(*args, **kwargs)

            t = threading.Thread(target=loop)
            t.daemon = True  # stop if the program exits
            t.start()
            return stopped

        return wrapper

    return decorator


class LogglyHandler(RestApiHandler):
    """
    A handler which pipes all logs to loggly through HTTP POST requests.
    Some ideas borrowed from github.com/kennedyj/loggly-handler
    """

    def __init__(self, custom_token, app_tags, max_attempts=5, aws_tag=False):
        """
        customToken: The loggly custom token account ID
        appTags: Loggly tags. Can be a tag string or a list of tag strings
        aws_tag: include aws instance id in tags if True and id can be found
        """
        self.pid = os.getpid()
        self.tags = self._getTags(app_tags)
        self.custom_token = custom_token

        self.aws_tag = aws_tag
        if self.aws_tag:
            id_url = None

            try:
                aws_base = "http://169.254.169.254/latest/meta-data/{}"
                id_url = aws_base.format('instance-id')
                self.ec2_id = requests.get(id_url, timeout=2).content.decode(
                    'utf-8')
            except Exception as e:
                # TODO: how to record this?
                print(
                    '*******!!!!*******'
                    ' kind of a problem getting aws info!', id_url,
                    repr(e),
                )
                self.ec2_id = 'id_NA'

            self.tags.append(self.ec2_id)

        super(LogglyHandler, self).__init__(self._getEndpoint())

        self.max_attempts = max_attempts
        self.timer = None
        self.logs = []
        self.timer = self._flushAndRepeatTimer()
        atexit.register(self._stopFlushTimer)

    @setInterval(1)
    def _flushAndRepeatTimer(self):
        self.flush()

    def _stopFlushTimer(self):
        self.timer.set()
        self.flush()

    def _getTags(self, app_tags):
        if isinstance(app_tags, str):
            tags = app_tags.split(',')
        else:
            tags = app_tags
        if 'bulk' not in tags:
            tags.insert(0, 'bulk')
        return tags

    def _implodeTags(self):
        return ",".join(self.tags)

    def _getEndpoint(self):
        """
        Override Build Loggly's RESTful API endpoint
        """
        return 'https://logs-01.loggly.com/bulk/{0}/tag/{1}/'.format(
            self.custom_token,
            self._implodeTags()
        )

    def _prepPayload(self, record):
        """
        record: generated from logger module
        This preps the payload to be formatted in whatever content-type is
        expected from the RESTful API.
        """
        return json.dumps(self._getPayload(record), default=serialize)

    def _getPayload(self, record):
        """
        The data that will be sent to loggly.
        """
        payload = super(LogglyHandler, self)._getPayload(record)
        payload['tags'] = self._implodeTags()

        return payload

    def handle_response(self, batch, attempt, sess, resp):
        if resp.status_code != 200:
            if attempt <= self.max_attempts:
                attempt += 1
                self.flush(batch, attempt)
            else:
                raise Exception('Error sending log batch')

    def flush(self, current_batch=None, attempt=1):
        if current_batch is None:
            self.logs, current_batch = [], self.logs
        callback = partial(
            self.handle_response, current_batch, attempt=attempt)
        if current_batch:
            data = '\n'.join(current_batch)
            self.session.post(self._getEndpoint(),
                              data=data,
                              headers={'content-type': 'application/json'},
                              background_callback=callback)

    def emit(self, record):
        """
        Override emit() method in handler parent for sending log to RESTful
        API
        """

        pid = os.getpid()
        if pid != self.pid:
            self.pid = pid
            self.logs = []
            self.timer = self._flushAndRepeatTimer()
            atexit.register(self._stopFlushTimer)

        # avoid infinite recursion
        if record.name.startswith('requests'):
            return

        self.logs.append(self._prepPayload(record))
