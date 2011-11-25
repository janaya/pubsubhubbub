#!/usr/bin/env python
#
# Copyright 2009 Google Inc.
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
#

"""Tests for the main module."""

import datetime
import logging
logging.basicConfig(format='%(levelname)-8s %(filename)s] %(message)s')
import os
import shutil
import sys
import time
import tempfile
import unittest
import urllib

import testutil
testutil.fix_path()


from google.appengine import runtime
from google.appengine.api import memcache
from google.appengine.api.labs.taskqueue import taskqueue_stub
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.runtime import apiproxy_errors

import async_apiproxy
import dos
import feed_diff
import main
import urlfetch_test_stub

################################################################################
# For convenience

sha1_hash = main.sha1_hash
get_hash_key_name = main.get_hash_key_name

OTHER_STRING = '/~one:two/&='
FUNNY = '/CaSeSeNsItIvE'
FUNNY_UNICODE = u'/blah/\u30d6\u30ed\u30b0\u8846'
FUNNY_UTF8 = '/blah/\xe3\x83\x96\xe3\x83\xad\xe3\x82\xb0\xe8\xa1\x86'
FUNNY_IRI = '/blah/%E3%83%96%E3%83%AD%E3%82%B0%E8%A1%86'

################################################################################

class UtilityFunctionTest(unittest.TestCase):
  """Tests for utility functions."""

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()

  def testSha1Hash(self):
    self.assertEquals('09f2c66851e75a7800748808ae7d855869b0c9d7',
                      main.sha1_hash('this is my test data'))

  def testGetHashKeyName(self):
    self.assertEquals('hash_54f6638eb67ad389b66bbc3fa65f7392b0c2d270',
                      get_hash_key_name('and now testing a key'))

  def testSha1Hmac(self):
    self.assertEquals('d95abcea4b2a8b0219da7cb04c261639a7bd8c94',
                      main.sha1_hmac('secrat', 'mydatahere'))

  def testIsValidUrl(self):
    self.assertTrue(main.is_valid_url(
        'https://example.com:443/path/to?handler=1&b=2'))
    self.assertTrue(main.is_valid_url('http://example.com:8080'))
    self.assertFalse(main.is_valid_url('httpm://example.com'))
    self.assertFalse(main.is_valid_url('http://example.com:9999'))
    self.assertFalse(main.is_valid_url('http://example.com/blah#bad'))

  def testNormalizeIri(self):
    uri_with_port = u'http://foo.com:9120/url/with/a/port'
    self.assertEquals(uri_with_port, main.normalize_iri(uri_with_port))

    uri_with_query = u'http://foo.com:9120/url?doh=this&port=1'
    self.assertEquals(uri_with_query, main.normalize_iri(uri_with_query))

    uri_with_funny = u'http://foo.com/~myuser/@url!with#nice;delimiter:chars'
    self.assertEquals(uri_with_funny, main.normalize_iri(uri_with_funny))

    not_unicode = 'http://foo.com:9120/url/with/a/port'
    self.assertEquals(not_unicode, main.normalize_iri(not_unicode))

    uri_with_port = u'http://foo.com:9120/url/with/a/port'
    self.assertEquals(uri_with_port, main.normalize_iri(uri_with_port))

    good_iri = (
        'http://www.google.com/reader/public/atom/user'
        '/07256788297315478906/label/%E3%83%96%E3%83%AD%E3%82%B0%E8%A1%86')
    iri = (u'http://www.google.com/reader/public/atom/user'
           u'/07256788297315478906/label/\u30d6\u30ed\u30b0\u8846')
    self.assertEquals(good_iri, main.normalize_iri(iri))


class AutoDiscoverUrlsTest(unittest.TestCase):
  """Tests for the auto_discover_urls function."""

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()
    self.url = 'http://example.com/'

  def tearDown(self):
    """Tears down the test harness."""
    urlfetch_test_stub.instance.verify_and_reset()

  def testHtmlDiscovery(self):
    """Tests HTML discovery with multiple feed links."""
    urlfetch_test_stub.instance.expect(
      'GET', self.url, 200, """
<html><head>
<link rel="alternate" type="application/atom+xml"
href="http://example.com/feed/1">
<link rel="alternate" type="application/atom+xml"
href="http://example.com/feed/2"/>
<link rel="alternate" type="application/rss+xml"
href="http://example.com/feed/3">
<link rel="alternate" type="application/rss+xml"
href="http://example.com/feed/4"/>
</head>
<body>
meep
</body>
</html>
""", response_headers={'content-type': 'text/html'})
    self.assertEquals(
        ['http://example.com/feed/1', 'http://example.com/feed/2',
         'http://example.com/feed/3', 'http://example.com/feed/4'],
        main.auto_discover_urls(self.url))

  def testCacheHit(self):
    """Tests when the result is already in memcache."""
    memcache.set('auto_discover:' + self.url,
        'http://example.com/feed/1\nhttp://example.com/feed/2\n'
        'http://example.com/feed/3\nhttp://example.com/feed/4')
    self.assertEquals(
        ['http://example.com/feed/1', 'http://example.com/feed/2',
         'http://example.com/feed/3', 'http://example.com/feed/4'],
        main.auto_discover_urls(self.url))

  def testFetchError(self):
    """Tests when an exception is hit while fetching the blog URL."""
    urlfetch_test_stub.instance.expect(
      'GET', self.url, 200, "", urlfetch_error=True)
    self.assertRaises(
      main.AutoDiscoveryError, main.auto_discover_urls, self.url)

  def testBadFetchResponseCode(self):
    """Tests when the fetch response code is not 200 OK."""
    urlfetch_test_stub.instance.expect(
      'GET', self.url, 404, "")
    self.assertRaises(
      main.AutoDiscoveryError, main.auto_discover_urls, self.url)

  def testBlogUrlIsFeed(self):
    """Tests when the blog URL supplied is actually a feed."""
    urlfetch_test_stub.instance.expect(
      'GET', self.url, 200, "unused",
      response_headers={'content-type': 'text/xml'})
    self.assertEquals([self.url], main.auto_discover_urls(self.url))

  def testBadContentType(self):
    """Tests when the fetched blog URL is of a bad content-type."""
    urlfetch_test_stub.instance.expect(
      'GET', self.url, 200, "unused",
      response_headers={'content-type': 'text/plain'})
    self.assertRaises(
      main.AutoDiscoveryError, main.auto_discover_urls, self.url)

  def testHtmlParseError(self):
    """Tests when the HTML won't parse correctly."""
    urlfetch_test_stub.instance.expect(
      'GET', self.url, 200, "<! --  foo -- >",
      response_headers={'content-type': 'text/html'})
    self.assertRaises(
      main.AutoDiscoveryError, main.auto_discover_urls, self.url)
    self.assertEquals('', memcache.get('auto_discover:' + self.url))

################################################################################

class TestWorkQueueHandler(webapp.RequestHandler):
  @main.work_queue_only
  def get(self):
    self.response.out.write('Pass')


class WorkQueueOnlyTest(testutil.HandlerTestBase):
  """Tests the @work_queue_only decorator."""

  handler_class = TestWorkQueueHandler

  def testNotLoggedIn(self):
    os.environ['SERVER_SOFTWARE'] = 'Production'
    self.handle('get')
    self.assertEquals(302, self.response_code())

  def testCronHeader(self):
    os.environ['SERVER_SOFTWARE'] = 'Production'
    os.environ['HTTP_X_APPENGINE_CRON'] = 'True'
    try:
      self.handle('get')
      self.assertEquals('Pass', self.response_body())
    finally:
      del os.environ['HTTP_X_APPENGINE_CRON']

  def testDevelopmentEnvironment(self):
    os.environ['SERVER_SOFTWARE'] = 'Development/1.0'
    self.handle('get')
    self.assertEquals('Pass', self.response_body())

  def testAdminUser(self):
    os.environ['SERVER_SOFTWARE'] = 'Production'
    os.environ['USER_EMAIL'] = 'foo@example.com'
    os.environ['USER_IS_ADMIN'] = '1'
    try:
      self.handle('get')
      self.assertEquals('Pass', self.response_body())
    finally:
      del os.environ['USER_IS_ADMIN']

  def testNonAdminUser(self):
    os.environ['SERVER_SOFTWARE'] = 'Production'
    os.environ['USER_EMAIL'] = 'foo@example.com'
    os.environ['USER_IS_ADMIN'] = '0'
    try:
      self.handle('get')
      self.assertEquals(401, self.response_code())
    finally:
      del os.environ['USER_IS_ADMIN']

  def testTaskQueueHeader(self):
    os.environ['SERVER_SOFTWARE'] = 'Production'
    os.environ['HTTP_X_APPENGINE_TASKNAME'] = 'Foobar'
    try:
      self.handle('get')
      self.assertEquals('Pass', self.response_body())
    finally:
      del os.environ['HTTP_X_APPENGINE_TASKNAME']

################################################################################

KnownFeed = main.KnownFeed

class KnownFeedTest(unittest.TestCase):
  """Tests for the KnownFeed model class."""

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()
    self.topic = 'http://example.com/my-topic'
    self.topic2 = 'http://example.com/my-topic2'
    self.topic3 = 'http://example.com/my-topic3'

  def testCreateAndDelete(self):
    known_feed = KnownFeed.create(self.topic)
    self.assertEquals(self.topic, known_feed.topic)
    db.put(known_feed)

    found_feed = db.get(KnownFeed.create_key(self.topic))
    self.assertEquals(found_feed.key(), known_feed.key())
    self.assertEquals(found_feed.topic, known_feed.topic)

    db.delete(KnownFeed.create_key(self.topic))
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)

  def testCheckExistsMissing(self):
    self.assertEquals([], KnownFeed.check_exists([]))
    self.assertEquals([], KnownFeed.check_exists([self.topic]))
    self.assertEquals([], KnownFeed.check_exists(
        [self.topic, self.topic2, self.topic3]))
    self.assertEquals([], KnownFeed.check_exists(
        [self.topic, self.topic, self.topic, self.topic2, self.topic2]))

  def testCheckExists(self):
    KnownFeed.create(self.topic).put()
    KnownFeed.create(self.topic2).put()
    KnownFeed.create(self.topic3).put()
    self.assertEquals([self.topic], KnownFeed.check_exists([self.topic]))
    self.assertEquals([self.topic2], KnownFeed.check_exists([self.topic2]))
    self.assertEquals([self.topic3], KnownFeed.check_exists([self.topic3]))
    self.assertEquals(
        sorted([self.topic, self.topic2, self.topic3]),
        sorted(KnownFeed.check_exists([self.topic, self.topic2, self.topic3])))
    self.assertEquals(
        sorted([self.topic, self.topic2]),
        sorted(KnownFeed.check_exists(
            [self.topic, self.topic, self.topic, self.topic2, self.topic2])))

  def testCheckExistsSubset(self):
    KnownFeed.create(self.topic).put()
    KnownFeed.create(self.topic3).put()
    self.assertEquals(
        sorted([self.topic, self.topic3]),
        sorted(KnownFeed.check_exists([self.topic, self.topic2, self.topic3])))
    self.assertEquals(
        sorted([self.topic, self.topic3]),
        sorted(KnownFeed.check_exists(
            [self.topic, self.topic, self.topic,
             self.topic2, self.topic2,
             self.topic3, self.topic3])))

################################################################################

Subscription = main.Subscription


class SubscriptionTest(unittest.TestCase):
  """Tests for the Subscription model class."""

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()
    self.callback = 'http://example.com/my-callback-url'
    self.callback2 = 'http://example.com/second-callback-url'
    self.callback3 = 'http://example.com/third-callback-url'
    self.topic = 'http://example.com/my-topic-url'
    self.topic2 = 'http://example.com/second-topic-url'
    self.token = 'token'
    self.secret = 'my secrat'
    self.callback_key_map = dict(
        (Subscription.create_key_name(cb, self.topic), cb)
        for cb in (self.callback, self.callback2, self.callback3))

  def get_subscription(self):
    """Returns the subscription for the test callback and topic."""
    return Subscription.get_by_key_name(
        Subscription.create_key_name(self.callback, self.topic))

  def verify_tasks(self, next_state, **kwargs):
    """Verifies the required tasks have been submitted.

    Args:
      next_state: The next state the Subscription should have.
    """
    task = testutil.get_tasks(main.SUBSCRIPTION_QUEUE, **kwargs)
    self.assertEquals(next_state, task['params']['next_state'])

  def testRequestInsert_defaults(self):
    now_datetime = datetime.datetime.now()
    now = lambda: now_datetime
    lease_seconds = 1234

    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token,
        self.secret, lease_seconds=lease_seconds, now=now))
    self.verify_tasks(Subscription.STATE_VERIFIED, expected_count=1, index=0)
    self.assertFalse(Subscription.request_insert(
        self.callback, self.topic, self.token,
        self.secret, lease_seconds=lease_seconds, now=now))
    self.verify_tasks(Subscription.STATE_VERIFIED, expected_count=2, index=1)

    sub = self.get_subscription()
    self.assertEquals(Subscription.STATE_NOT_VERIFIED, sub.subscription_state)
    self.assertEquals(self.callback, sub.callback)
    self.assertEquals(sha1_hash(self.callback), sub.callback_hash)
    self.assertEquals(self.topic, sub.topic)
    self.assertEquals(sha1_hash(self.topic), sub.topic_hash)
    self.assertEquals(self.token, sub.verify_token)
    self.assertEquals(self.secret, sub.secret)
    self.assertEquals(now_datetime + datetime.timedelta(seconds=lease_seconds),
                      sub.expiration_time)
    self.assertEquals(lease_seconds, sub.lease_seconds)

  def testInsert_defaults(self):
    now_datetime = datetime.datetime.now()
    now = lambda: now_datetime
    lease_seconds = 1234

    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret,
        lease_seconds=lease_seconds, now=now))
    self.assertFalse(Subscription.insert(
        self.callback, self.topic, self.token, self.secret,
        lease_seconds=lease_seconds, now=now))
    testutil.get_tasks(main.SUBSCRIPTION_QUEUE, expected_count=0)

    sub = self.get_subscription()
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertEquals(self.callback, sub.callback)
    self.assertEquals(sha1_hash(self.callback), sub.callback_hash)
    self.assertEquals(self.topic, sub.topic)
    self.assertEquals(sha1_hash(self.topic), sub.topic_hash)
    self.assertEquals(self.token, sub.verify_token)
    self.assertEquals(self.secret, sub.secret)
    self.assertEquals(now_datetime + datetime.timedelta(seconds=lease_seconds),
                      sub.expiration_time)
    self.assertEquals(lease_seconds, sub.lease_seconds)

  def testInsert_override(self):
    """Tests that insert will override the existing subscription state."""
    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertEquals(Subscription.STATE_NOT_VERIFIED,
                      self.get_subscription().subscription_state)
    self.assertFalse(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertEquals(Subscription.STATE_VERIFIED,
                      self.get_subscription().subscription_state)
    self.verify_tasks(Subscription.STATE_VERIFIED, expected_count=1, index=0)

  def testInsert_expiration(self):
    """Tests that the expiration time is updated on repeated insert() calls."""
    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    sub = Subscription.all().get()
    expiration1 = sub.expiration_time
    time.sleep(0.5)
    self.assertFalse(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    sub = db.get(sub.key())
    expiration2 = sub.expiration_time
    self.assertTrue(expiration2 > expiration1)

  def testRemove(self):
    self.assertFalse(Subscription.remove(self.callback, self.topic))
    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.remove(self.callback, self.topic))
    self.assertFalse(Subscription.remove(self.callback, self.topic))
    # Only task should be the initial insertion request.
    self.verify_tasks(Subscription.STATE_VERIFIED, expected_count=1, index=0)

  def testRequestRemove(self):
    self.assertFalse(Subscription.request_remove(
        self.callback, self.topic, self.token))
    # No tasks should be enqueued.
    testutil.get_tasks(main.SUBSCRIPTION_QUEUE, expected_count=0)

    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.request_remove(
        self.callback, self.topic, self.token))
    self.assertEquals(Subscription.STATE_NOT_VERIFIED,
                      self.get_subscription().subscription_state)
    self.verify_tasks(Subscription.STATE_VERIFIED, expected_count=2, index=0)
    self.verify_tasks(Subscription.STATE_TO_DELETE, expected_count=2, index=1)

  def testHasSubscribers_unverified(self):
    """Tests that unverified subscribers do not make the subscription active."""
    self.assertFalse(Subscription.has_subscribers(self.topic))
    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertFalse(Subscription.has_subscribers(self.topic))

  def testHasSubscribers_verified(self):
    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.has_subscribers(self.topic))
    self.assertTrue(Subscription.remove(self.callback, self.topic))
    self.assertFalse(Subscription.has_subscribers(self.topic))

  def testGetSubscribers_unverified(self):
    """Tests that unverified subscribers will not be retrieved."""
    self.assertEquals([], Subscription.get_subscribers(self.topic, 10))
    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.request_insert(
        self.callback2, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.request_insert(
        self.callback3, self.topic, self.token, self.secret))
    self.assertEquals([], Subscription.get_subscribers(self.topic, 10))

  def testGetSubscribers_verified(self):
    self.assertEquals([], Subscription.get_subscribers(self.topic, 10))
    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, self.token, self.secret))
    sub_list = Subscription.get_subscribers(self.topic, 10)
    found_keys = set(s.key().name() for s in sub_list)
    self.assertEquals(set(self.callback_key_map.keys()), found_keys)

  def testGetSubscribers_count(self):
    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, self.token, self.secret))
    sub_list = Subscription.get_subscribers(self.topic, 1)
    self.assertEquals(1, len(sub_list))

  def testGetSubscribers_withOffset(self):
    """Tests the behavior of the starting_at_callback offset parameter."""
    # In the order the query will sort them.
    all_hashes = [
        u'87a74994e48399251782eb401e9a61bd1d55aeee',
        u'01518f29da9db10888a92e9f0211ac0c98ec7ecb',
        u'f745d00a9806a5cdd39f16cd9eff80e8f064cfee',
    ]
    all_keys = ['hash_' + h for h in all_hashes]
    all_callbacks = [self.callback_key_map[k] for k in all_keys]

    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, self.token, self.secret))

    def key_list(starting_at_callback):
      sub_list = Subscription.get_subscribers(
          self.topic, 10, starting_at_callback=starting_at_callback)
      return [s.key().name() for s in sub_list]

    self.assertEquals(all_keys, key_list(None))
    self.assertEquals(all_keys, key_list(all_callbacks[0]))
    self.assertEquals(all_keys[1:], key_list(all_callbacks[1]))
    self.assertEquals(all_keys[2:], key_list(all_callbacks[2]))

  def testGetSubscribers_multipleTopics(self):
    """Tests that separate topics do not overlap in subscriber queries."""
    self.assertEquals([], Subscription.get_subscribers(self.topic2, 10))
    self.assertTrue(Subscription.insert(
        self.callback, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, self.token, self.secret))
    self.assertEquals([], Subscription.get_subscribers(self.topic2, 10))

    self.assertTrue(Subscription.insert(
        self.callback2, self.topic2, self.token, self.secret))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic2, self.token, self.secret))
    sub_list = Subscription.get_subscribers(self.topic2, 10)
    found_keys = set(s.key().name() for s in sub_list)
    self.assertEquals(
        set(Subscription.create_key_name(cb, self.topic2)
            for cb in (self.callback2, self.callback3)),
        found_keys)
    self.assertEquals(3, len(Subscription.get_subscribers(self.topic, 10)))

  def testConfirmFailed(self):
    """Tests retry delay periods when a subscription confirmation fails."""
    start = datetime.datetime.utcnow()
    def now():
      return start

    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    sub = Subscription.get_by_key_name(sub_key)
    self.assertEquals(0, sub.confirm_failures)

    for i, delay in enumerate((5, 10, 20, 40, 80)):
      sub.confirm_failed(Subscription.STATE_VERIFIED,
                         max_failures=5, retry_period=5, now=now)
      self.assertEquals(sub.eta, start + datetime.timedelta(seconds=delay))
      self.assertEquals(i+1, sub.confirm_failures)

    # It will be deleted on the last try.
    sub.confirm_failed(Subscription.STATE_VERIFIED,
                       max_failures=5, retry_period=5)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    testutil.get_tasks(main.SUBSCRIPTION_QUEUE, index=0, expected_count=6)

  def testQueuePreserved(self):
    """Tests that insert will put the task on the environment's queue."""
    self.assertTrue(Subscription.request_insert(
        self.callback, self.topic, self.token, self.secret))
    testutil.get_tasks(main.SUBSCRIPTION_QUEUE, expected_count=1)
    os.environ['X_APPENGINE_QUEUENAME'] = main.POLLING_QUEUE
    try:
      self.assertFalse(Subscription.request_insert(
          self.callback, self.topic, self.token, self.secret))
    finally:
      del os.environ['X_APPENGINE_QUEUENAME']

    testutil.get_tasks(main.SUBSCRIPTION_QUEUE, expected_count=1)
    testutil.get_tasks(main.POLLING_QUEUE, expected_count=1)

################################################################################

FeedToFetch = main.FeedToFetch

class FeedToFetchTest(unittest.TestCase):

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()
    self.topic = 'http://example.com/topic-one'
    self.topic2 = 'http://example.com/topic-two'
    self.topic3 = 'http://example.com/topic-three'

  def testInsertAndGet(self):
    """Tests inserting and getting work."""
    all_topics = [self.topic, self.topic2, self.topic3]
    FeedToFetch.insert(all_topics)
    found_topics = set(FeedToFetch.get_by_topic(t).topic for t in all_topics)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=3)
    task_topics = set(t['params']['topic'] for t in tasks)
    self.assertEquals(found_topics, task_topics)

    for topic in all_topics:
      feed_to_fetch = FeedToFetch.get_by_topic(topic)
      self.assertEquals(topic, feed_to_fetch.topic)
      self.assertEquals([], feed_to_fetch.source_keys)
      self.assertEquals([], feed_to_fetch.source_values)

  def testEmpty(self):
    """Tests when the list of urls is empty."""
    FeedToFetch.insert([])
    self.assertEquals([], testutil.get_tasks(main.FEED_QUEUE))

  def testDuplicates(self):
    """Tests duplicate urls."""
    all_topics = [self.topic, self.topic, self.topic2, self.topic2]
    FeedToFetch.insert(all_topics)
    found_topics = set(FeedToFetch.get_by_topic(t).topic for t in all_topics)
    self.assertEquals(set(all_topics), found_topics)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    task_topics = set(t['params']['topic'] for t in tasks)
    self.assertEquals(found_topics, task_topics)

  def testDone(self):
    FeedToFetch.insert([self.topic])
    feed = FeedToFetch.get_by_topic(self.topic)
    self.assertTrue(feed.done())
    self.assertTrue(FeedToFetch.get_by_topic(self.topic) is None)

  def testDoneConflict(self):
    """Tests when another entity was written over the top of this one."""
    FeedToFetch.insert([self.topic])
    feed = FeedToFetch.get_by_topic(self.topic)
    FeedToFetch.insert([self.topic])
    self.assertFalse(feed.done())
    self.assertTrue(FeedToFetch.get_by_topic(self.topic) is not None)

  def testFetchFailed(self):
    start = datetime.datetime.utcnow()
    now = lambda: start

    FeedToFetch.insert([self.topic])
    etas = []
    for i, delay in enumerate((5, 10, 20, 40, 80)):
      feed = FeedToFetch.get_by_topic(self.topic)
      feed.fetch_failed(max_failures=5, retry_period=5, now=now)
      expected_eta = start + datetime.timedelta(seconds=delay)
      self.assertEquals(expected_eta, feed.eta)
      etas.append(testutil.task_eta(feed.eta))
      self.assertEquals(i+1, feed.fetching_failures)
      self.assertEquals(False, feed.totally_failed)

    feed.fetch_failed(max_failures=5, retry_period=5, now=now)
    self.assertEquals(True, feed.totally_failed)

    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=6)
    found_etas = [t['eta'] for t in tasks[1:]]  # First task is from insert()
    self.assertEquals(etas, found_etas)

  def testQueuePreserved(self):
    """Tests the request's queue is preserved for inserted FeedToFetchs."""
    FeedToFetch.insert([self.topic])
    feed = FeedToFetch.all().get()
    testutil.get_tasks(main.FEED_QUEUE, expected_count=1)
    feed.delete()

    os.environ['X_APPENGINE_QUEUENAME'] = main.POLLING_QUEUE
    try:
      FeedToFetch.insert([self.topic])
      feed = FeedToFetch.all().get()
      testutil.get_tasks(main.FEED_QUEUE, expected_count=1)
      testutil.get_tasks(main.POLLING_QUEUE, expected_count=1)
    finally:
      del os.environ['X_APPENGINE_QUEUENAME']

  def testSources(self):
    """Tests when sources are supplied."""
    source_dict = {'foo': 'bar', 'meepa': 'stuff'}
    all_topics = [self.topic, self.topic2, self.topic3]
    FeedToFetch.insert(all_topics, source_dict=source_dict)
    for topic in all_topics:
      feed_to_fetch = FeedToFetch.get_by_topic(topic)
      self.assertEquals(topic, feed_to_fetch.topic)
      found_source_dict = dict(zip(feed_to_fetch.source_keys,
                                   feed_to_fetch.source_values))
      self.assertEquals(source_dict, found_source_dict)

################################################################################

FeedEntryRecord = main.FeedEntryRecord
EventToDeliver = main.EventToDeliver


class EventToDeliverTest(unittest.TestCase):

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()
    self.topic = 'http://example.com/my-topic'
    # Order out of the datastore will be done by callback hash, not alphabetical
    self.callback = 'http://example.com/my-callback'
    self.callback2 = 'http://example.com/second-callback'
    self.callback3 = 'http://example.com/third-callback-123'
    self.callback4 = 'http://example.com/fourth-callback-1205'
    self.header_footer = '<feed>\n<stuff>blah</stuff>\n<xmldata/></feed>'
    self.token = 'verify token'
    self.secret = 'some secret'
    self.test_payloads = [
        '<entry>article1</entry>',
        '<entry>article2</entry>',
        '<entry>article3</entry>',
    ]

  def insert_subscriptions(self):
    """Inserts Subscription instances and an EventToDeliver for testing.

    Returns:
      Tuple (event, work_key, sub_list, sub_keys) where:
        event: The EventToDeliver that was inserted.
        work_key: Key for the 'event'
        sub_list: List of Subscription instances that were created in order
          of their callback hashes.
        sub_keys: Key instances corresponding to the entries in 'sub_list'.
    """
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    work_key = event.key()

    Subscription.insert(
        self.callback, self.topic, self.token, self.secret)
    Subscription.insert(
        self.callback2, self.topic, self.token, self.secret)
    Subscription.insert(
        self.callback3, self.topic, self.token, self.secret)
    Subscription.insert(
        self.callback4, self.topic, self.token, self.secret)
    sub_list = Subscription.get_subscribers(self.topic, 10)
    sub_keys = [s.key() for s in sub_list]
    self.assertEquals(4, len(sub_list))

    return (event, work_key, sub_list, sub_keys)

  def testCreateEventForTopic(self):
    """Tests that the payload of an event is properly formed."""
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    expected_data = \
u"""<?xml version="1.0" encoding="utf-8"?>
<feed>
<stuff>blah</stuff>
<xmldata/>
<entry>article1</entry>
<entry>article2</entry>
<entry>article3</entry>
</feed>"""
    self.assertEquals(expected_data, event.payload)

  def testCreateEventForTopic_Rss(self):
    """Tests that the RSS payload is properly formed."""
    self.test_payloads = [
        '<item>article1</item>',
        '<item>article2</item>',
        '<item>article3</item>',
    ]
    self.header_footer = (
        '<rss>\n<channel>\n<stuff>blah</stuff>\n<xmldata/></channel>\n</rss>')
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.RSS, self.header_footer, self.test_payloads)
    expected_data = \
u"""<?xml version="1.0" encoding="utf-8"?>
<rss>
<channel>
<stuff>blah</stuff>
<xmldata/>
<item>article1</item>
<item>article2</item>
<item>article3</item>
</channel>
</rss>"""
    self.assertEquals(expected_data, event.payload)

  def testCreateEvent_badHeaderFooter(self):
    """Tests when the header/footer data in an event is invalid."""
    self.assertRaises(AssertionError, EventToDeliver.create_event_for_topic,
        self.topic, main.ATOM, '<feed>has no end tag', self.test_payloads)

  def testNormal_noFailures(self):
    """Tests that event delivery with no failures will delete the event."""
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()
    more, subs = event.get_next_subscribers()
    event.update(more, [])
    event = EventToDeliver.get(work_key)
    self.assertTrue(event is None)

  def testUpdate_failWithNoSubscribersLeft(self):
    """Tests that failures are written correctly by EventToDeliver.update.

    This tests the common case of completing the failed callbacks list extending
    when there are new Subscriptions that have been found in the latest work
    queue query.
    """
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()

    # Assert that the callback offset is updated and any failed callbacks
    # are recorded.
    more, subs = event.get_next_subscribers(chunk_size=1)
    event.update(more, [sub_list[0]])
    event = EventToDeliver.get(event.key())
    self.assertEquals(EventToDeliver.NORMAL, event.delivery_mode)
    self.assertEquals([sub_list[0].key()], event.failed_callbacks)
    self.assertEquals(self.callback2, event.last_callback)

    more, subs = event.get_next_subscribers(chunk_size=3)
    event.update(more, sub_list[1:])
    event = EventToDeliver.get(event.key())
    self.assertTrue(event is not None)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)
    self.assertEquals('', event.last_callback)

    self.assertEquals([s.key() for s in sub_list], event.failed_callbacks)
    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=2)
    self.assertEquals([str(work_key)] * 2,
                      [t['params']['event_key'] for t in tasks])

  def testUpdate_actuallyNoMoreCallbacks(self):
    """Tests when the normal update delivery has no Subscriptions left.

    This tests the case where update is called with no Subscribers in the
    list of Subscriptions. This can happen if a Subscription is deleted
    between when an update happens and when the work queue is invoked again.
    """
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()

    more, subs = event.get_next_subscribers(chunk_size=3)
    event.update(more, subs)
    event = EventToDeliver.get(event.key())
    self.assertEquals(self.callback4, event.last_callback)
    self.assertEquals(EventToDeliver.NORMAL, event.delivery_mode)

    # This final call to update will transition to retry properly.
    Subscription.remove(self.callback4, self.topic)
    more, subs = event.get_next_subscribers(chunk_size=1)
    event.update(more, [])
    event = EventToDeliver.get(event.key())
    self.assertEquals([], subs)
    self.assertTrue(event is not None)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)

    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=2)
    self.assertEquals([str(work_key)] * 2,
                      [t['params']['event_key'] for t in tasks])

  def testGetNextSubscribers_retriesFinallySuccessful(self):
    """Tests retries until all subscribers are successful."""
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()

    # Simulate that callback 2 is successful and the rest fail.
    more, subs = event.get_next_subscribers(chunk_size=2)
    event.update(more, sub_list[:1])
    event = EventToDeliver.get(event.key())
    self.assertTrue(more)
    self.assertEquals(self.callback3, event.last_callback)
    self.assertEquals(EventToDeliver.NORMAL, event.delivery_mode)

    more, subs = event.get_next_subscribers(chunk_size=2)
    event.update(more, sub_list[2:])
    event = EventToDeliver.get(event.key())
    self.assertEquals('', event.last_callback)
    self.assertFalse(more)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)

    # Now getting the next subscribers will returned the failed ones.
    more, subs = event.get_next_subscribers(chunk_size=2)
    expected = sub_keys[:1] + sub_keys[2:3]
    self.assertEquals(expected, [s.key() for s in subs])
    event.update(more, subs)
    event = EventToDeliver.get(event.key())
    self.assertTrue(more)
    self.assertEquals(self.callback, event.last_callback)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)

    # This will get the last of the failed subscribers but *not* include the
    # sentinel value of event.last_callback, since that marks the end of this
    # attempt.
    more, subs = event.get_next_subscribers(chunk_size=2)
    expected = sub_keys[3:]
    self.assertEquals(expected, [s.key() for s in subs])
    event.update(more, subs)
    event = EventToDeliver.get(event.key())
    self.assertFalse(more)
    self.assertEquals('', event.last_callback)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)
    self.assertEquals(sub_keys[:1] + sub_keys[2:], event.failed_callbacks)

    # Now simulate all retries being successful one chunk at a time.
    more, subs = event.get_next_subscribers(chunk_size=2)
    expected = sub_keys[:1] + sub_keys[2:3]
    self.assertEquals(expected, [s.key() for s in subs])
    event.update(more, [])
    event = EventToDeliver.get(event.key())
    self.assertTrue(more)
    self.assertEquals(self.callback, event.last_callback)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)
    self.assertEquals(sub_keys[3:], event.failed_callbacks)

    more, subs = event.get_next_subscribers(chunk_size=2)
    expected = sub_keys[3:]
    self.assertEquals(expected, [s.key() for s in subs])
    event.update(more, [])
    self.assertFalse(more)

    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=5)
    self.assertEquals([str(work_key)] * 5,
                      [t['params']['event_key'] for t in tasks])

  def testGetNextSubscribers_failedFewerThanChunkSize(self):
    """Tests when there are fewer failed callbacks than the chunk size.

    Ensures that we step through retry attempts when there is only a single
    chunk to go through on each retry iteration.
    """
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()

    # Simulate that callback 2 is successful and the rest fail.
    more, subs = event.get_next_subscribers(chunk_size=2)
    event.update(more, sub_list[:1])
    event = EventToDeliver.get(event.key())
    self.assertTrue(more)
    self.assertEquals(self.callback3, event.last_callback)
    self.assertEquals(EventToDeliver.NORMAL, event.delivery_mode)

    more, subs = event.get_next_subscribers(chunk_size=2)
    event.update(more, sub_list[2:])
    event = EventToDeliver.get(event.key())
    self.assertEquals('', event.last_callback)
    self.assertFalse(more)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)
    self.assertEquals(1, event.retry_attempts)

    # Now attempt a retry with a chunk size equal to the number of callbacks.
    more, subs = event.get_next_subscribers(chunk_size=3)
    event.update(more, subs)
    event = EventToDeliver.get(event.key())
    self.assertFalse(more)
    self.assertEquals(EventToDeliver.RETRY, event.delivery_mode)
    self.assertEquals(2, event.retry_attempts)

    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=3)
    self.assertEquals([str(work_key)] * 3,
                      [t['params']['event_key'] for t in tasks])

  def testGetNextSubscribers_giveUp(self):
    """Tests retry delay amounts until we finally give up on event delivery.

    Verifies retry delay logic works properly.
    """
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()

    start = datetime.datetime.utcnow()
    now = lambda: start

    etas = []
    for i, delay in enumerate((5, 10, 20, 40, 80, 160, 320, 640)):
      more, subs = event.get_next_subscribers(chunk_size=4)
      event.update(more, subs, retry_period=5, now=now, max_failures=8)
      event = EventToDeliver.get(event.key())
      self.assertEquals(i+1, event.retry_attempts)
      expected_eta = start + datetime.timedelta(seconds=delay)
      self.assertEquals(expected_eta, event.last_modified)
      etas.append(testutil.task_eta(event.last_modified))
      self.assertFalse(event.totally_failed)

    more, subs = event.get_next_subscribers(chunk_size=4)
    event.update(more, subs)
    event = EventToDeliver.get(event.key())
    self.assertTrue(event.totally_failed)

    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=8)
    found_etas = [t['eta'] for t in tasks]
    self.assertEquals(etas, found_etas)

  def testQueuePreserved(self):
    """Tests that enqueueing an EventToDeliver will preserve the queue."""
    event, work_key, sub_list, sub_keys = self.insert_subscriptions()
    event.enqueue()
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=1)
    os.environ['X_APPENGINE_QUEUENAME'] = main.POLLING_QUEUE
    try:
      event.enqueue()
    finally:
      del os.environ['X_APPENGINE_QUEUENAME']

    testutil.get_tasks(main.EVENT_QUEUE, expected_count=1)
    testutil.get_tasks(main.POLLING_QUEUE, expected_count=1)

################################################################################

class PublishHandlerTest(testutil.HandlerTestBase):

  handler_class = main.PublishHandler

  def setUp(self):
    testutil.HandlerTestBase.setUp(self)
    self.topic = 'http://example.com/first-url'
    self.topic2 = 'http://example.com/second-url'
    self.topic3 = 'http://example.com/third-url'

  def testDebugFormRenders(self):
    self.handle('get')
    self.assertTrue('<html>' in self.response_body())

  def testBadMode(self):
    self.handle('post',
                ('hub.mode', 'invalid'),
                ('hub.url', 'http://example.com'))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.mode' in self.response_body())

  def testNoUrls(self):
    self.handle('post', ('hub.mode', 'publish'))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.url' in self.response_body())

  def testBadUrls(self):
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', 'http://example.com/bad_url#fragment'))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.url invalid' in self.response_body())

  def testInsertion(self):
    db.put([KnownFeed.create(self.topic),
            KnownFeed.create(self.topic2),
            KnownFeed.create(self.topic3)])
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', self.topic),
                ('hub.url', self.topic2),
                ('hub.url', self.topic3))
    self.assertEquals(204, self.response_code())
    expected_topics = set([self.topic, self.topic2, self.topic3])
    inserted_topics = set(f.topic for f in FeedToFetch.all())
    self.assertEquals(expected_topics, inserted_topics)

  def testIgnoreUnknownFeed(self):
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', self.topic),
                ('hub.url', self.topic2),
                ('hub.url', self.topic3))
    self.assertEquals(204, self.response_code())
    self.assertEquals([], list(FeedToFetch.all()))

  def testDuplicateUrls(self):
    db.put([KnownFeed.create(self.topic),
            KnownFeed.create(self.topic2)])
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', self.topic),
                ('hub.url', self.topic),
                ('hub.url', self.topic),
                ('hub.url', self.topic),
                ('hub.url', self.topic),
                ('hub.url', self.topic),
                ('hub.url', self.topic),
                ('hub.url', self.topic2),
                ('hub.url', self.topic2),
                ('hub.url', self.topic2),
                ('hub.url', self.topic2),
                ('hub.url', self.topic2),
                ('hub.url', self.topic2),
                ('hub.url', self.topic2))
    self.assertEquals(204, self.response_code())
    expected_topics = set([self.topic, self.topic2])
    inserted_topics = set(f.topic for f in FeedToFetch.all())
    self.assertEquals(expected_topics, inserted_topics)

  def testInsertFailure(self):
    """Tests when a publish event fails insertion."""
    old_insert = FeedToFetch.insert
    try:
      for exception in (db.Error(), apiproxy_errors.Error(),
                        runtime.DeadlineExceededError()):
        @classmethod
        def new_insert(cls, *args):
          raise exception
        FeedToFetch.insert = new_insert
        self.handle('post',
                    ('hub.mode', 'PuBLisH'),
                    ('hub.url', 'http://example.com/first-url'),
                    ('hub.url', 'http://example.com/second-url'),
                    ('hub.url', 'http://example.com/third-url'))
        self.assertEquals(503, self.response_code())
    finally:
      FeedToFetch.insert = old_insert

  def testCaseSensitive(self):
    """Tests that cases for topics URLs are preserved."""
    self.topic += FUNNY
    self.topic2 += FUNNY
    self.topic3 += FUNNY
    db.put([KnownFeed.create(self.topic),
            KnownFeed.create(self.topic2),
            KnownFeed.create(self.topic3)])
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', self.topic),
                ('hub.url', self.topic2),
                ('hub.url', self.topic3))
    self.assertEquals(204, self.response_code())
    expected_topics = set([self.topic, self.topic2, self.topic3])
    inserted_topics = set(f.topic for f in FeedToFetch.all())
    self.assertEquals(expected_topics, inserted_topics)

  def testNormalization(self):
    """Tests that URLs are properly normalized."""
    self.topic += OTHER_STRING
    self.topic2 += OTHER_STRING
    self.topic3 += OTHER_STRING
    normalized = [
        main.normalize_iri(t)
        for t in [self.topic, self.topic2, self.topic3]]
    db.put([KnownFeed.create(t) for t in normalized])
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', self.topic),
                ('hub.url', self.topic2),
                ('hub.url', self.topic3))
    self.assertEquals(204, self.response_code())
    inserted_topics = set(f.topic for f in FeedToFetch.all())
    self.assertEquals(set(normalized), inserted_topics)

  def testIri(self):
    """Tests publishing with an IRI with international characters."""
    topic = main.normalize_iri(self.topic + FUNNY_UNICODE)
    topic2 = main.normalize_iri(self.topic2 + FUNNY_UNICODE)
    topic3 = main.normalize_iri(self.topic3 + FUNNY_UNICODE)
    normalized = [topic, topic2, topic3]
    db.put([KnownFeed.create(t) for t in normalized])
    self.handle('post',
                ('hub.mode', 'PuBLisH'),
                ('hub.url', self.topic + FUNNY_UTF8),
                ('hub.url', self.topic2 + FUNNY_UTF8),
                ('hub.url', self.topic3 + FUNNY_UTF8))
    self.assertEquals(204, self.response_code())
    inserted_topics = set(f.topic for f in FeedToFetch.all())
    self.assertEquals(set(normalized), inserted_topics)

  def testUnicode(self):
    """Tests publishing with a URL that has unicode characters."""
    topic = main.normalize_iri(self.topic + FUNNY_UNICODE)
    topic2 = main.normalize_iri(self.topic2 + FUNNY_UNICODE)
    topic3 = main.normalize_iri(self.topic3 + FUNNY_UNICODE)
    normalized = [topic, topic2, topic3]
    db.put([KnownFeed.create(t) for t in normalized])

    payload = (
        'hub.mode=publish'
        '&hub.url=' + urllib.quote(self.topic) + FUNNY_UTF8 +
        '&hub.url=' + urllib.quote(self.topic2) + FUNNY_UTF8 +
        '&hub.url=' + urllib.quote(self.topic3) + FUNNY_UTF8)
    self.handle_body('post', payload)
    self.assertEquals(204, self.response_code())
    inserted_topics = set(f.topic for f in FeedToFetch.all())
    self.assertEquals(set(normalized), inserted_topics)

  def testSources(self):
    """Tests that derived sources are properly set on FeedToFetch instances."""
    db.put([KnownFeed.create(self.topic),
            KnownFeed.create(self.topic2),
            KnownFeed.create(self.topic3)])
    source_dict = {'one': 'two', 'three': 'four'}
    topics = [self.topic, self.topic2, self.topic3]
    def derive_sources(handler, urls):
      self.assertEquals(set(topics), set(urls))
      self.assertEquals('testvalue', handler.request.get('the-real-thing'))
      return source_dict

    main.hooks.override_for_test(main.derive_sources, derive_sources)
    try:
      self.handle('post',
                  ('hub.mode', 'PuBLisH'),
                  ('hub.url', self.topic),
                  ('hub.url', self.topic2),
                  ('hub.url', self.topic3),
                  ('the-real-thing', 'testvalue'))
      self.assertEquals(204, self.response_code())
      for topic in topics:
        feed_to_fetch = FeedToFetch.get_by_topic(topic)
        found_source_dict = dict(zip(feed_to_fetch.source_keys,
                                     feed_to_fetch.source_values))
        self.assertEquals(source_dict, found_source_dict)
    finally:
      main.hooks.reset_for_test(main.derive_sources)


class PublishHandlerThroughHubUrlTest(PublishHandlerTest):

  handler_class = main.HubHandler

################################################################################

class FindFeedUpdatesTest(unittest.TestCase):

  def setUp(self):
    """Sets up the test harness."""
    testutil.setup_for_testing()
    self.topic = 'http://example.com/my-topic-here'
    self.header_footer = '<feed>this is my test header footer</feed>'
    self.entries_map = {
        'id1': 'content1',
        'id2': 'content2',
        'id3': 'content3',
    }
    self.content = 'the expected response data'
    def my_filter(content, ignored_format):
      self.assertEquals(self.content, content)
      return self.header_footer, self.entries_map
    self.my_filter = my_filter

  def run_test(self):
    """Runs a test."""
    header_footer, entry_list, entry_payloads = main.find_feed_updates(
        self.topic, main.ATOM, self.content, filter_feed=self.my_filter)
    self.assertEquals(self.header_footer, header_footer)
    return entry_list, entry_payloads

  @staticmethod
  def get_entry(entry_id, entry_list):
    """Finds the entry with the given ID in the list of entries."""
    return [e for e in entry_list if e.entry_id == entry_id][0]

  def testAllNewContent(self):
    """Tests when al pulled feed content is new."""
    entry_list, entry_payloads = self.run_test()
    entry_id_set = set(f.entry_id for f in entry_list)
    self.assertEquals(set(self.entries_map.keys()), entry_id_set)
    self.assertEquals(self.entries_map.values(), entry_payloads)

  def testSomeExistingEntries(self):
    """Tests when some entries are already known."""
    FeedEntryRecord.create_entry_for_topic(
        self.topic, 'id1', sha1_hash('content1')).put()
    FeedEntryRecord.create_entry_for_topic(
        self.topic, 'id2', sha1_hash('content2')).put()

    entry_list, entry_payloads = self.run_test()
    entry_id_set = set(f.entry_id for f in entry_list)
    self.assertEquals(set(['id3']), entry_id_set)
    self.assertEquals(['content3'], entry_payloads)

  def testPulledEntryNewer(self):
    """Tests when an entry is already known but has been updated recently."""
    FeedEntryRecord.create_entry_for_topic(
        self.topic, 'id1', sha1_hash('content1')).put()
    FeedEntryRecord.create_entry_for_topic(
        self.topic, 'id2', sha1_hash('content2')).put()
    self.entries_map['id1'] = 'newcontent1'

    entry_list, entry_payloads = self.run_test()
    entry_id_set = set(f.entry_id for f in entry_list)
    self.assertEquals(set(['id1', 'id3']), entry_id_set)

    # Verify the old entry would be overwritten.
    entry1 = self.get_entry('id1', entry_list)
    self.assertEquals(sha1_hash('newcontent1'), entry1.entry_content_hash)
    self.assertEquals(['content3', 'newcontent1'], entry_payloads)

  def testUnicodeContent(self):
    """Tests when the content contains unicode characters."""
    self.entries_map['id2'] = u'\u2019 asdf'
    entry_list, entry_payloads = self.run_test()
    entry_id_set = set(f.entry_id for f in entry_list)
    self.assertEquals(set(self.entries_map.keys()), entry_id_set)

  def testMultipleParallelBatches(self):
    """Tests that retrieving FeedEntryRecords is done in multiple batches."""
    old_get_feed_record = main.FeedEntryRecord.get_entries_for_topic
    calls = [0]
    @staticmethod
    def fake_get_record(*args, **kwargs):
      calls[0] += 1
      return old_get_feed_record(*args, **kwargs)

    old_lookups = main.MAX_FEED_ENTRY_RECORD_LOOKUPS
    main.FeedEntryRecord.get_entries_for_topic = fake_get_record
    main.MAX_FEED_ENTRY_RECORD_LOOKUPS = 1
    try:
      entry_list, entry_payloads = self.run_test()
      entry_id_set = set(f.entry_id for f in entry_list)
      self.assertEquals(set(self.entries_map.keys()), entry_id_set)
      self.assertEquals(self.entries_map.values(), entry_payloads)
      self.assertEquals(3, calls[0])
    finally:
      main.MAX_FEED_ENTRY_RECORD_LOOKUPS = old_lookups
      main.FeedEntryRecord.get_entries_for_topic = old_get_feed_record

################################################################################

FeedRecord = main.FeedRecord


class PullFeedHandlerTest(testutil.HandlerTestBase):

  handler_class = main.PullFeedHandler

  def setUp(self):
    """Sets up the test harness."""
    testutil.HandlerTestBase.setUp(self)

    self.topic = 'http://example.com/my-topic-here'
    self.header_footer = '<feed>this is my test header footer</feed>'
    self.all_ids = ['1', '2', '3']
    self.entry_payloads = [
      'content%s' % entry_id for entry_id in self.all_ids
    ]
    self.entry_list = [
        FeedEntryRecord.create_entry_for_topic(
            self.topic, entry_id, 'content%s' % entry_id)
        for entry_id in self.all_ids
    ]
    self.expected_response = 'the expected response data'
    self.etag = 'something unique'
    self.last_modified = 'some time'
    self.headers = {
      'ETag': self.etag,
      'Last-Modified': self.last_modified,
      'Content-Type': 'application/atom+xml',
    }
    self.expected_exceptions = []

    def my_find_updates(ignored_topic, ignored_format, content):
      self.assertEquals(self.expected_response, content)
      if self.expected_exceptions:
        raise self.expected_exceptions.pop(0)
      return self.header_footer, self.entry_list, self.entry_payloads

    self.old_find_feed_updates = main.find_feed_updates
    main.find_feed_updates = my_find_updates

    self.callback = 'http://example.com/my-subscriber'
    self.assertTrue(Subscription.insert(
        self.callback, self.topic, 'token', 'secret'))

  def tearDown(self):
    """Tears down the test harness."""
    main.find_feed_updates = self.old_find_feed_updates
    urlfetch_test_stub.instance.verify_and_reset()

  def testNoWork(self):
    self.handle('post', ('topic', self.topic))

  def testNewEntries_Atom(self):
    """Tests when new entries are found."""
    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)
    self.handle('post', ('topic', self.topic))

    # Verify that all feed entry records have been written along with the
    # EventToDeliver and FeedRecord.
    feed_entries = FeedEntryRecord.get_entries_for_topic(
        self.topic, self.all_ids)
    self.assertEquals(self.all_ids, [e.entry_id for e in feed_entries])

    work = EventToDeliver.all().get()
    event_key = work.key()
    self.assertEquals(self.topic, work.topic)
    self.assertTrue('content1\ncontent2\ncontent3' in work.payload)
    work.delete()

    record = FeedRecord.get_or_create(self.topic)
    self.assertEquals(self.header_footer, record.header_footer)
    self.assertEquals(self.etag, record.etag)
    self.assertEquals(self.last_modified, record.last_modified)
    self.assertEquals('application/atom+xml', record.content_type)

    task = testutil.get_tasks(main.EVENT_QUEUE, index=0, expected_count=1)
    self.assertEquals(str(event_key), task['params']['event_key'])
    task = testutil.get_tasks(main.FEED_QUEUE, index=0, expected_count=1)
    self.assertEquals(self.topic, task['params']['topic'])

  def testRssFailBack(self):
    """Tests when parsing as Atom fails and it uses RSS instead."""
    self.expected_exceptions.append(feed_diff.Error('whoops'))
    self.header_footer = '<rss><channel>this is my test</channel></rss>'
    self.headers['Content-Type'] = 'application/xml'

    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)
    self.handle('post', ('topic', self.topic))

    feed_entries = FeedEntryRecord.get_entries_for_topic(
        self.topic, self.all_ids)
    self.assertEquals(self.all_ids, [e.entry_id for e in feed_entries])

    work = EventToDeliver.all().get()
    event_key = work.key()
    self.assertEquals(self.topic, work.topic)
    self.assertTrue('content1\ncontent2\ncontent3' in work.payload)
    work.delete()

    record = FeedRecord.get_or_create(self.topic)
    self.assertEquals('application/xml', record.content_type)

    task = testutil.get_tasks(main.EVENT_QUEUE, index=0, expected_count=1)
    self.assertEquals(str(event_key), task['params']['event_key'])
    task = testutil.get_tasks(main.FEED_QUEUE, index=0, expected_count=1)
    self.assertEquals(self.topic, task['params']['topic'])

  def testAtomFailBack(self):
    """Tests when parsing as RSS fails and it uses Atom instead."""
    self.expected_exceptions.append(feed_diff.Error('whoops'))
    self.headers.clear()
    self.headers['Content-Type'] = 'application/rss+xml'
    info = FeedRecord.get_or_create(self.topic)
    info.update(self.headers)
    info.put()

    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)
    self.handle('post', ('topic', self.topic))

    feed_entries = FeedEntryRecord.get_entries_for_topic(
        self.topic, self.all_ids)
    self.assertEquals(self.all_ids, [e.entry_id for e in feed_entries])

    work = EventToDeliver.all().get()
    event_key = work.key()
    self.assertEquals(self.topic, work.topic)
    self.assertTrue('content1\ncontent2\ncontent3' in work.payload)
    work.delete()

    record = FeedRecord.get_or_create(self.topic)
    self.assertEquals('application/rss+xml', record.content_type)

    task = testutil.get_tasks(main.EVENT_QUEUE, index=0, expected_count=1)
    self.assertEquals(str(event_key), task['params']['event_key'])
    task = testutil.get_tasks(main.FEED_QUEUE, index=0, expected_count=1)
    self.assertEquals(self.topic, task['params']['topic'])

  def testParseFailure(self):
    """Tests when the feed cannot be parsed as Atom or RSS."""
    self.expected_exceptions.append(feed_diff.Error('whoops'))
    self.expected_exceptions.append(feed_diff.Error('whoops'))
    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)
    self.handle('post', ('topic', self.topic))

    feed = FeedToFetch.get_by_key_name(get_hash_key_name(self.topic))
    self.assertEquals(1, feed.fetching_failures)

    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    self.assertEquals([self.topic] * 2, [t['params']['topic'] for t in tasks])

  def testCacheHit(self):
    """Tests when the fetched feed matches the last cached version of it."""
    info = FeedRecord.get_or_create(self.topic)
    info.update(self.headers)
    info.put()

    request_headers = {
      'If-None-Match': self.etag,
      'If-Modified-Since': self.last_modified,
    }

    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 304, '',
        request_headers=request_headers,
        response_headers=self.headers)
    self.handle('post', ('topic', self.topic))
    self.assertTrue(EventToDeliver.all().get() is None)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

  def testNoNewEntries(self):
    """Tests when there are no new entries."""
    FeedToFetch.insert([self.topic])
    self.entry_list = []
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)
    self.handle('post', ('topic', self.topic))
    self.assertTrue(EventToDeliver.all().get() is None)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

    record = FeedRecord.get_or_create(self.topic)
    self.assertEquals(self.header_footer, record.header_footer)
    self.assertEquals(self.etag, record.etag)
    self.assertEquals(self.last_modified, record.last_modified)
    self.assertEquals('application/atom+xml', record.content_type)

  def testPullError(self):
    """Tests when URLFetch raises an exception."""
    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response, urlfetch_error=True)
    self.handle('post', ('topic', self.topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(self.topic))
    self.assertEquals(1, feed.fetching_failures)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    self.assertEquals([self.topic] * 2, [t['params']['topic'] for t in tasks])

  def testPullBadStatusCode(self):
    """Tests when the response status is bad."""
    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 500, self.expected_response)
    self.handle('post', ('topic', self.topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(self.topic))
    self.assertEquals(1, feed.fetching_failures)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    self.assertEquals([self.topic] * 2, [t['params']['topic'] for t in tasks])

  def testApiProxyError(self):
    """Tests when the APIProxy raises an error."""
    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response, apiproxy_error=True)
    self.handle('post', ('topic', self.topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(self.topic))
    self.assertEquals(1, feed.fetching_failures)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    self.assertEquals([self.topic] * 2, [t['params']['topic'] for t in tasks])

  def testNoSubscribers(self):
    """Tests that when a feed has no subscribers we do not pull it."""
    self.assertTrue(Subscription.remove(self.callback, self.topic))
    db.put(KnownFeed.create(self.topic))
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)
    self.entry_list = []
    FeedToFetch.insert([self.topic])
    self.handle('post', ('topic', self.topic))

    # Verify that *no* feed entry records have been written.
    self.assertEquals([], FeedEntryRecord.get_entries_for_topic(
                               self.topic, self.all_ids))

    # And any KnownFeeds were deleted.
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)

    # And there is no EventToDeliver or tasks.
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=1)

  def testRedirects(self):
    """Tests when redirects are encountered."""
    info = FeedRecord.get_or_create(self.topic)
    info.update(self.headers)
    info.put()
    FeedToFetch.insert([self.topic])

    real_topic = 'http://example.com/real-topic-location'
    self.headers['Location'] = real_topic
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 302, '',
        response_headers=self.headers.copy())

    del self.headers['Location']
    urlfetch_test_stub.instance.expect(
        'get', real_topic, 200, self.expected_response,
        response_headers=self.headers)

    self.handle('post', ('topic', self.topic))
    self.assertTrue(EventToDeliver.all().get() is not None)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=1)

  def testTooManyRedirects(self):
    """Tests when too many redirects are encountered."""
    info = FeedRecord.get_or_create(self.topic)
    info.update(self.headers)
    info.put()
    FeedToFetch.insert([self.topic])

    last_topic = self.topic
    real_topic = 'http://example.com/real-topic-location'
    for i in xrange(main.MAX_REDIRECTS):
      next_topic = real_topic + str(i)
      self.headers['Location'] = next_topic
      urlfetch_test_stub.instance.expect(
          'get', last_topic, 302, '',
          response_headers=self.headers.copy())
      last_topic = next_topic

    self.handle('post', ('topic', self.topic))
    self.assertTrue(EventToDeliver.all().get() is None)
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    self.assertEquals([self.topic] * 2, [t['params']['topic'] for t in tasks])

  def testPutSplitting(self):
    """Tests that put() calls for feed records are split when too large."""
    # Make the content way too big.
    content_template = ('content' * 100 + '%s')
    self.all_ids = [str(i) for i in xrange(1000)]
    self.entry_payloads = [
      (content_template % entry_id) for entry_id in self.all_ids
    ]
    self.entry_list = [
        FeedEntryRecord.create_entry_for_topic(
            self.topic, entry_id, 'content%s' % entry_id)
        for entry_id in self.all_ids
    ]

    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)

    old_max_new = main.MAX_NEW_FEED_ENTRY_RECORDS
    main.MAX_NEW_FEED_ENTRY_RECORDS = len(self.all_ids) + 1
    try:
        self.handle('post', ('topic', self.topic))
    finally:
      main.MAX_NEW_FEED_ENTRY_RECORDS = old_max_new

    # Verify that all feed entry records have been written along with the
    # EventToDeliver and FeedRecord.
    feed_entries = list(FeedEntryRecord.all())
    self.assertEquals(set(self.all_ids), set(e.entry_id for e in feed_entries))

    work = EventToDeliver.all().get()
    event_key = work.key()
    self.assertEquals(self.topic, work.topic)
    self.assertTrue('\n'.join(self.entry_payloads) in work.payload)
    work.delete()

    record = FeedRecord.get_or_create(self.topic)
    self.assertEquals(self.header_footer, record.header_footer)
    self.assertEquals(self.etag, record.etag)
    self.assertEquals(self.last_modified, record.last_modified)
    self.assertEquals('application/atom+xml', record.content_type)

    task = testutil.get_tasks(main.EVENT_QUEUE, index=0, expected_count=1)
    self.assertEquals(str(event_key), task['params']['event_key'])
    task = testutil.get_tasks(main.FEED_QUEUE, index=0, expected_count=1)
    self.assertEquals(self.topic, task['params']['topic'])

  def testPutSplittingFails(self):
    """Tests when splitting put() calls still doesn't help and we give up."""
    # Make the content way too big.
    content_template = ('content' * 100 + '%s')
    self.all_ids = [str(i) for i in xrange(1000)]
    self.entry_payloads = [
      (content_template % entry_id) for entry_id in self.all_ids
    ]
    self.entry_list = [
        FeedEntryRecord.create_entry_for_topic(
            self.topic, entry_id, 'content%s' % entry_id)
        for entry_id in self.all_ids
    ]

    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)

    old_splitting_attempts = main.PUT_SPLITTING_ATTEMPTS
    old_max_saves = main.MAX_FEED_RECORD_SAVES
    old_max_new = main.MAX_NEW_FEED_ENTRY_RECORDS
    main.PUT_SPLITTING_ATTEMPTS = 1
    main.MAX_FEED_RECORD_SAVES = len(self.entry_list) + 1
    main.MAX_NEW_FEED_ENTRY_RECORDS = main.MAX_FEED_RECORD_SAVES
    try:
      self.handle('post', ('topic', self.topic))
    finally:
      main.PUT_SPLITTING_ATTEMPTS = old_splitting_attempts
      main.MAX_FEED_RECORD_SAVES = old_max_saves
      main.MAX_NEW_FEED_ENTRY_RECORDS = old_max_new

    # Verify that *NO* FeedEntryRecords or EventToDeliver has been written,
    # the FeedRecord wasn't updated, and no tasks were enqueued.
    self.assertEquals([], list(FeedEntryRecord.all()))
    self.assertEquals(None, EventToDeliver.all().get())

    record = FeedRecord.all().get()
    self.assertNotEquals(self.etag, record.etag)

    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

  def testFeedTooLarge(self):
    """Tests when the pulled feed's content size is too large."""
    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, '',
        response_headers=self.headers,
        urlfetch_size_error=True)
    self.handle('post', ('topic', self.topic))
    self.assertEquals([], list(FeedEntryRecord.all()))
    self.assertEquals(None, EventToDeliver.all().get())
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

  def testTooManyNewEntries(self):
    """Tests when there are more new entries than we can handle at once."""
    self.all_ids = [str(i) for i in xrange(1000)]
    self.entry_payloads = [
      'content%s' % entry_id for entry_id in self.all_ids
    ]
    self.entry_list = [
        FeedEntryRecord.create_entry_for_topic(
            self.topic, entry_id, 'content%s' % entry_id)
        for entry_id in self.all_ids
    ]

    FeedToFetch.insert([self.topic])
    urlfetch_test_stub.instance.expect(
        'get', self.topic, 200, self.expected_response,
        response_headers=self.headers)

    self.handle('post', ('topic', self.topic))

    # Verify that a subset of the entry records are present and the payload
    # only has the first N entries.
    feed_entries = FeedEntryRecord.get_entries_for_topic(
        self.topic, self.all_ids)
    expected_records = main.MAX_NEW_FEED_ENTRY_RECORDS
    self.assertEquals(self.all_ids[:expected_records],
                      [e.entry_id for e in feed_entries])

    work = EventToDeliver.all().get()
    event_key = work.key()
    self.assertEquals(self.topic, work.topic)
    expected_content = '\n'.join(self.entry_payloads[:expected_records])
    self.assertTrue(expected_content in work.payload)
    self.assertFalse('content%d' % expected_records in work.payload)
    work.delete()

    record = FeedRecord.all().get()
    self.assertNotEquals(self.etag, record.etag)

    task = testutil.get_tasks(main.EVENT_QUEUE, index=0, expected_count=1)
    self.assertEquals(str(event_key), task['params']['event_key'])
    tasks = testutil.get_tasks(main.FEED_QUEUE, expected_count=2)
    for task in tasks:
      self.assertEquals(self.topic, task['params']['topic'])


class PullFeedHandlerTestWithParsing(testutil.HandlerTestBase):

  handler_class = main.PullFeedHandler

  def testPullBadContent(self):
    """Tests when the content doesn't parse correctly."""
    topic = 'http://example.com/my-topic'
    callback = 'http://example.com/my-subscriber'
    self.assertTrue(Subscription.insert(callback, topic, 'token', 'secret'))
    FeedToFetch.insert([topic])
    urlfetch_test_stub.instance.expect(
        'get', topic, 200, 'this does not parse')
    self.handle('post', ('topic', topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(topic))
    self.assertEquals(1, feed.fetching_failures)

  def testPullBadFeed(self):
    """Tests when the content parses, but is not a good Atom document."""
    data = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<meep><entry>wooh</entry></meep>')
    topic = 'http://example.com/my-topic'
    callback = 'http://example.com/my-subscriber'
    self.assertTrue(Subscription.insert(callback, topic, 'token', 'secret'))
    FeedToFetch.insert([topic])
    urlfetch_test_stub.instance.expect('get', topic, 200, data)
    self.handle('post', ('topic', topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(topic))
    self.assertEquals(1, feed.fetching_failures)

  def testPullGoodAtom(self):
    """Tests when the Atom XML can parse just fine."""
    data = ('<?xml version="1.0" encoding="utf-8"?>\n<feed><my header="data"/>'
            '<entry><id>1</id><updated>123</updated>wooh</entry></feed>')
    topic = 'http://example.com/my-topic'
    callback = 'http://example.com/my-subscriber'
    self.assertTrue(Subscription.insert(callback, topic, 'token', 'secret'))
    FeedToFetch.insert([topic])
    urlfetch_test_stub.instance.expect('get', topic, 200, data)
    self.handle('post', ('topic', topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(topic))
    self.assertTrue(feed is None)
    event = EventToDeliver.all().get()
    self.assertEquals(data.replace('\n', ''), event.payload.replace('\n', ''))

  def testPullGoodRss(self):
    """Tests when the RSS XML can parse just fine."""
    data = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<rss version="2.0"><channel><my header="data"/>'
            '<item><guid>1</guid><updated>123</updated>wooh</item>'
            '</channel></rss>')
    topic = 'http://example.com/my-topic'
    callback = 'http://example.com/my-subscriber'
    self.assertTrue(Subscription.insert(callback, topic, 'token', 'secret'))
    FeedToFetch.insert([topic])
    urlfetch_test_stub.instance.expect('get', topic, 200, data)
    self.handle('post', ('topic', topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(topic))
    self.assertTrue(feed is None)
    event = EventToDeliver.all().get()
    self.assertEquals(data.replace('\n', ''), event.payload.replace('\n', ''))

  def testPullGoodRdf(self):
    """Tests when the RDF (RSS 1.0) XML can parse just fine."""
    data = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<channel><my header="data"/>'
            '<item><guid>1</guid><updated>123</updated>wooh</item>'
            '</channel></rdf:RDF>')
    topic = 'http://example.com/my-topic'
    callback = 'http://example.com/my-subscriber'
    self.assertTrue(Subscription.insert(callback, topic, 'token', 'secret'))
    FeedToFetch.insert([topic])
    urlfetch_test_stub.instance.expect('get', topic, 200, data)
    self.handle('post', ('topic', topic))
    feed = FeedToFetch.get_by_key_name(get_hash_key_name(topic))
    self.assertTrue(feed is None)
    event = EventToDeliver.all().get()
    self.assertEquals(data.replace('\n', ''), event.payload.replace('\n', ''))

################################################################################

class PushEventHandlerTest(testutil.HandlerTestBase):

  handler_class = main.PushEventHandler

  def setUp(self):
    """Sets up the test harness."""
    testutil.HandlerTestBase.setUp(self)

    self.chunk_size = main.EVENT_SUBSCRIBER_CHUNK_SIZE
    self.topic = 'http://example.com/hamster-topic'
    # Order of these URL fetches is determined by the ordering of the hashes
    # of the callback URLs, so we need random extra strings here to get
    # alphabetical hash order.
    self.callback1 = 'http://example.com/hamster-callback1'
    self.callback2 = 'http://example.com/hamster-callback2'
    self.callback3 = 'http://example.com/hamster-callback3-12345'
    self.callback4 = 'http://example.com/hamster-callback4-12345'
    self.header_footer = '<feed>\n<stuff>blah</stuff>\n<xmldata/></feed>'
    self.test_payloads = [
        '<entry>article1</entry>',
        '<entry>article2</entry>',
        '<entry>article3</entry>',
    ]
    self.expected_payload = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed>\n'
        '<stuff>blah</stuff>\n'
        '<xmldata/>\n'
        '<entry>article1</entry>\n'
        '<entry>article2</entry>\n'
        '<entry>article3</entry>\n'
        '</feed>'
    )

    self.header_footer_rss = '<rss><channel></channel></rss>'
    self.test_payloads_rss = [
        '<item>article1</item>',
        '<item>article2</item>',
        '<item>article3</item>',
    ]
    self.expected_payload_rss = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss><channel>\n'
        '<item>article1</item>\n'
        '<item>article2</item>\n'
        '<item>article3</item>\n'
        '</channel></rss>'
    )

    self.bad_key = db.Key.from_path(EventToDeliver.kind(), 'does_not_exist')

  def tearDown(self):
    """Resets any external modules modified for testing."""
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = self.chunk_size
    urlfetch_test_stub.instance.verify_and_reset()

  def testNoWork(self):
    self.handle('post', ('event_key', str(self.bad_key)))

  def testNoExtraSubscribers(self):
    """Tests when a single chunk of delivery is enough."""
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, 'token', 'secret'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 3
    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 204, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback2, 200, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 204, '', request_payload=self.expected_payload)
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    self.handle('post', ('event_key', str(event.key())))
    self.assertEquals([], list(EventToDeliver.all()))
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

  def testHmacData(self):
    """Tests that the content is properly signed with an HMAC."""
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret3'))
    # Secret is empty on purpose here, so the verify_token will be used instead.
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, 'my-token', ''))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, 'token', 'secret-stuff'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 3
    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 204, '',
        request_payload=self.expected_payload,
        request_headers={
            'Content-Type': 'application/atom+xml',
            'X-Hub-Signature': 'sha1=3e9caf971b0833d15393022f5f01a47adf597af5'})
    urlfetch_test_stub.instance.expect(
        'post', self.callback2, 200, '',
        request_payload=self.expected_payload,
        request_headers={
            'Content-Type': 'application/atom+xml',
            'X-Hub-Signature': 'sha1=4847815aae8578eff55d351bc84a159b9bd8846e'})
    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 204, '',
        request_payload=self.expected_payload,
        request_headers={
            'Content-Type': 'application/atom+xml',
            'X-Hub-Signature': 'sha1=8b0a9da7204afa8ae04fc9439755c556b1e38d99'})
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    self.handle('post', ('event_key', str(event.key())))
    self.assertEquals([], list(EventToDeliver.all()))
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

  def testRssContentType(self):
    """Tests that the content type of an RSS feed is properly supplied."""
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 3
    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 204, '',
        request_payload=self.expected_payload_rss,
        request_headers={
            'Content-Type': 'application/rss+xml',
            'X-Hub-Signature': 'sha1=1607313b6195af74f29158421f0a31aa25d680da'})
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.RSS, self.header_footer_rss, self.test_payloads_rss)
    event.put()
    self.handle('post', ('event_key', str(event.key())))
    self.assertEquals([], list(EventToDeliver.all()))
    testutil.get_tasks(main.EVENT_QUEUE, expected_count=0)

  def testExtraSubscribers(self):
    """Tests when there are more subscribers to contact after delivery."""
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, 'token', 'secret'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 1
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    event_key = str(event.key())

    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 204, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    urlfetch_test_stub.instance.expect(
        'post', self.callback2, 200, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 204, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()
    self.assertEquals([], list(EventToDeliver.all()))

    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=2)
    self.assertEquals([event_key] * 2,
                      [t['params']['event_key'] for t in tasks])

  def testBrokenCallbacks(self):
    """Tests that when callbacks return errors and are saved for later."""
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, 'token', 'secret'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 2
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    event_key = str(event.key())

    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 302, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback2, 404, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 500, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    work = EventToDeliver.all().get()
    sub_list = Subscription.get(work.failed_callbacks)
    callback_list = [sub.callback for sub in sub_list]
    self.assertEquals([self.callback1, self.callback2, self.callback3],
                      callback_list)

    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=2)
    self.assertEquals([event_key] * 2,
                      [t['params']['event_key'] for t in tasks])

  def testDeadlineError(self):
    """Tests that callbacks in flight at deadline will be marked as failed."""
    try:
      def deadline():
        raise runtime.DeadlineExceededError()
      main.async_proxy.wait = deadline

      self.assertTrue(Subscription.insert(
          self.callback1, self.topic, 'token', 'secret'))
      self.assertTrue(Subscription.insert(
          self.callback2, self.topic, 'token', 'secret'))
      self.assertTrue(Subscription.insert(
          self.callback3, self.topic, 'token', 'secret'))
      main.EVENT_SUBSCRIBER_CHUNK_SIZE = 2
      event = EventToDeliver.create_event_for_topic(
          self.topic, main.ATOM, self.header_footer, self.test_payloads)
      event.put()
      event_key = str(event.key())
      self.handle('post', ('event_key', event_key))

      # All events should be marked as failed even though no urlfetches
      # were made.
      work = EventToDeliver.all().get()
      sub_list = Subscription.get(work.failed_callbacks)
      callback_list = [sub.callback for sub in sub_list]
      self.assertEquals([self.callback1, self.callback2], callback_list)

      self.assertEquals(event_key, testutil.get_tasks(
          main.EVENT_QUEUE, index=0, expected_count=1)['params']['event_key'])
    finally:
      main.async_proxy = async_apiproxy.AsyncAPIProxy()

  def testRetryLogic(self):
    """Tests that failed urls will be retried after subsequent failures.

    This is an end-to-end test for push delivery failures and retries. We'll
    simulate multiple times through the failure list.
    """
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback3, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback4, self.topic, 'token', 'secret'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 3
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    event_key = str(event.key())

    # First pass through all URLs goes full speed for two chunks.
    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 404, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback2, 204, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 302, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    urlfetch_test_stub.instance.expect(
        'post', self.callback4, 500, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    # Now the retries.
    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 404, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 302, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback4, 500, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 204, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 302, '', request_payload=self.expected_payload)
    urlfetch_test_stub.instance.expect(
        'post', self.callback4, 200, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    urlfetch_test_stub.instance.expect(
        'post', self.callback3, 204, '', request_payload=self.expected_payload)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    self.assertEquals([], list(EventToDeliver.all()))
    tasks = testutil.get_tasks(main.EVENT_QUEUE, expected_count=4)
    self.assertEquals([event_key] * 4,
                      [t['params']['event_key'] for t in tasks])

  def testUrlFetchFailure(self):
    """Tests the UrlFetch API raising exceptions while sending notifications."""
    self.assertTrue(Subscription.insert(
        self.callback1, self.topic, 'token', 'secret'))
    self.assertTrue(Subscription.insert(
        self.callback2, self.topic, 'token', 'secret'))
    main.EVENT_SUBSCRIBER_CHUNK_SIZE = 3
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, self.test_payloads)
    event.put()
    event_key = str(event.key())

    urlfetch_test_stub.instance.expect(
        'post', self.callback1, 200, '',
        request_payload=self.expected_payload, urlfetch_error=True)
    urlfetch_test_stub.instance.expect(
        'post', self.callback2, 200, '',
        request_payload=self.expected_payload, apiproxy_error=True)
    self.handle('post', ('event_key', event_key))
    urlfetch_test_stub.instance.verify_and_reset()

    work = EventToDeliver.all().get()
    sub_list = Subscription.get(work.failed_callbacks)
    callback_list = [sub.callback for sub in sub_list]
    self.assertEquals([self.callback1, self.callback2], callback_list)

    self.assertEquals(event_key, testutil.get_tasks(
        main.EVENT_QUEUE, index=0, expected_count=1)['params']['event_key'])


class EventCleanupHandlerTest(testutil.HandlerTestBase):
  """Tests for the EventCleanupHandler worker."""

  def setUp(self):
    """Sets up the test harness."""
    self.now = datetime.datetime.utcnow()
    self.expire_time = self.now - datetime.timedelta(
        seconds=main.EVENT_CLEANUP_MAX_AGE_SECONDS)
    def create_handler():
      return main.EventCleanupHandler(now=lambda: self.now)
    self.handler_class = create_handler
    testutil.HandlerTestBase.setUp(self)
    self.topic = 'http://example.com/mytopic'
    self.header_footer = '<feed></feed>'

  def testNoEvents(self):
    """Tests when there are no failed events to clean up."""
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, [])
    event.put()
    self.handle('get')
    self.assertTrue(db.get(event.key()) is not None)

  def testEventCleanupTooYoung(self):
    """Tests when there are events present, but they're too young to remove."""
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, [])
    event.last_modified = self.expire_time + datetime.timedelta(seconds=1)
    event.totally_failed = True
    event.put()
    self.handle('get')
    self.assertTrue(db.get(event.key()) is not None)

  def testEventCleanupOldEnough(self):
    """Tests when there are events old enough to clean up."""
    event = EventToDeliver.create_event_for_topic(
        self.topic, main.ATOM, self.header_footer, [])
    event.last_modified = self.expire_time
    event.totally_failed = True
    event.put()

    too_young_event = EventToDeliver.create_event_for_topic(
        self.topic + 'blah', main.ATOM, self.header_footer, [])
    too_young_event.put()

    self.handle('get')
    self.assertTrue(db.get(event.key()) is None)
    self.assertTrue(db.get(too_young_event.key()) is not None)

################################################################################

class SubscribeHandlerTest(testutil.HandlerTestBase):

  handler_class = main.SubscribeHandler

  def setUp(self):
    """Tests up the test harness."""
    testutil.HandlerTestBase.setUp(self)
    self.challenge = 'this_is_my_fake_challenge_string'
    self.old_get_challenge = main.get_random_challenge
    main.get_random_challenge = lambda: self.challenge
    self.callback = 'http://example.com/good-callback'
    self.topic = 'http://example.com/the-topic'
    self.verify_token = 'the_token'
    self.verify_callback_querystring_template = (
        self.callback +
        '?hub.verify_token=the_token'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic'
        '&hub.mode=%s'
        '&hub.lease_seconds=2592000')

  def tearDown(self):
    """Tears down the test harness."""
    testutil.HandlerTestBase.tearDown(self)
    main.get_random_challenge = self.old_get_challenge

  def testDebugFormRenders(self):
    self.handle('get')
    self.assertTrue('<html>' in self.response_body())

  def testValidation(self):
    """Tests form validation."""
    # Bad mode
    self.handle('post',
        ('hub.mode', 'bad'),
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.mode' in self.response_body())

    # Empty callback
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', ''),
        ('hub.topic', self.topic),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.callback' in self.response_body())

    # Bad callback URL
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', 'httpf://example.com'),
        ('hub.topic', self.topic),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.callback' in self.response_body())

    # Empty topic
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', self.callback),
        ('hub.topic', ''),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.topic' in self.response_body())

    # Bad topic URL
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', self.callback),
        ('hub.topic', 'httpf://example.com'),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.topic' in self.response_body())

    # Bad verify
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.verify', 'meep'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.verify' in self.response_body())

    # Bad lease_seconds
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.verify', 'async'),
        ('hub.verify_token', 'asdf'),
        ('hub.lease_seconds', 'stuff'))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.lease_seconds' in self.response_body())

    # Bad lease_seconds zero padding will break things
    self.handle('post',
        ('hub.mode', 'subscribe'),
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.verify', 'async'),
        ('hub.verify_token', 'asdf'),
        ('hub.lease_seconds', '000010'))
    self.assertEquals(400, self.response_code())
    self.assertTrue('hub.lease_seconds' in self.response_body())

  def testUnsubscribeMissingSubscription(self):
    """Tests that deleting a non-existent subscription does nothing."""
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.verify', 'sync'),
        ('hub.mode', 'unsubscribe'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())

  def testSynchronous(self):
    """Tests synchronous subscribe and unsubscribe."""
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)

    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)

    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'unsubscribe', 200,
        self.challenge)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'unsubscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)

  def testAsynchronous(self):
    """Tests sync and async subscriptions cause the correct state transitions.

    Also tests that synchronous subscribes and unsubscribes will overwrite
    asynchronous requests.
    """
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)

    # Async subscription.
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(202, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_NOT_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)

    # Sync subscription overwrites.
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)

    # Async unsubscribe queues removal, but does not change former state.
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'unsubscribe'),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(202, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)

    # Synch unsubscribe overwrites.
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'unsubscribe', 200,
        self.challenge)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'unsubscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)

  def testResubscribe(self):
    """Tests that subscribe requests will reset pending unsubscribes."""
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)

    # Async subscription.
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(202, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_NOT_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)

    # Async un-subscription does not change previous subscription state.
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'unsubscribe'),
        ('hub.verify', 'async'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(202, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_NOT_VERIFIED, sub.subscription_state)

    # Synchronous subscription overwrites.
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)

  def testMaxLeaseSeconds(self):
    """Tests when the max lease period is specified."""
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)

    self.verify_callback_querystring_template = (
        self.callback +
        '?hub.verify_token=the_token'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic'
        '&hub.mode=%s'
        '&hub.lease_seconds=7776000')
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token),
        ('hub.lease_seconds', '1000000000000000000'))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)

  def testInvalidChallenge(self):
    """Tests when the returned challenge is bad."""
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    urlfetch_test_stub.instance.expect('get',
        self.verify_callback_querystring_template % 'subscribe', 200, 'bad')
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)
    self.assertEquals(409, self.response_code())

  def testSynchronousConfirmFailure(self):
    """Tests when synchronous confirmations fail."""
    # Subscribe
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    urlfetch_test_stub.instance.expect('get',
        self.verify_callback_querystring_template % 'subscribe', 500, '')
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)
    self.assertEquals(409, self.response_code())

    # Unsubscribe
    Subscription.insert(self.callback, self.topic, self.verify_token, 'secret')
    urlfetch_test_stub.instance.expect('get',
        self.verify_callback_querystring_template % 'unsubscribe', 500, '')
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'unsubscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertTrue(Subscription.get_by_key_name(sub_key) is not None)
    self.assertEquals(409, self.response_code())

  def testAfterSubscriptionError(self):
    """Tests when an exception occurs after subscription."""
    for exception in (runtime.DeadlineExceededError(), db.Error(),
                      apiproxy_errors.Error()):
      def new_confirm(*args):
        raise exception
      main.hooks.override_for_test(main.confirm_subscription, new_confirm)
      try:
        self.handle('post',
            ('hub.callback', self.callback),
            ('hub.topic', self.topic),
            ('hub.mode', 'subscribe'),
            ('hub.verify', 'sync'),
            ('hub.verify_token', self.verify_token))
        self.assertEquals(503, self.response_code())
      finally:
        main.hooks.reset_for_test(main.confirm_subscription)

  def testSubscriptionError(self):
    """Tests when errors occurs during subscription."""
    # URLFetch errors are probably the subscriber's fault, so we'll serve these
    # as a conflict.
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe',
        None, '', urlfetch_error=True)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(409, self.response_code())

    # An apiproxy error or deadline error will fall through and serve a 503,
    # since that means there's something wrong with our service.
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe',
        None, '', apiproxy_error=True)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(503, self.response_code())

    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe',
        None, '', deadline_error=True)
    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(503, self.response_code())

  def testCaseSensitive(self):
    """Tests that the case of topics, callbacks, and tokens are preserved."""
    self.topic += FUNNY
    self.callback += FUNNY
    self.verify_token += FUNNY
    sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    self.verify_callback_querystring_template = (
        self.callback +
        '?hub.verify_token=the_token%%2FCaSeSeNsItIvE'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic%%2FCaSeSeNsItIvE'
        '&hub.mode=%s'
        '&hub.lease_seconds=2592000')
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)

    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)

  def testSubscribeNormalization(self):
    """Tests that the topic and callback URLs are properly normalized."""
    self.topic += OTHER_STRING
    orig_callback = self.callback
    self.callback += OTHER_STRING
    sub_key = Subscription.create_key_name(
        main.normalize_iri(self.callback),
        main.normalize_iri(self.topic))
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    self.verify_callback_querystring_template = (
        orig_callback + '/~one:two/&='
        '?hub.verify_token=the_token'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic'
          '%%2F%%7Eone%%3Atwo%%2F%%26%%3D'
        '&hub.mode=%s'
        '&hub.lease_seconds=2592000')
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)

    self.handle('post',
        ('hub.callback', self.callback),
        ('hub.topic', self.topic),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', self.verify_token))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(KnownFeed.create_key(
        main.normalize_iri(self.topic))) is not None)

  def testSubscribeIri(self):
    """Tests when the topic, callback, verify_token, and secrets are IRIs."""
    topic = self.topic + FUNNY_UNICODE
    topic_utf8 = self.topic + FUNNY_UTF8
    callback = self.callback + FUNNY_UNICODE
    callback_utf8 = self.callback + FUNNY_UTF8
    verify_token = self.verify_token + FUNNY_UNICODE
    verify_token_utf8 = self.verify_token + FUNNY_UTF8

    sub_key = Subscription.create_key_name(
        main.normalize_iri(callback),
        main.normalize_iri(topic))
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    self.verify_callback_querystring_template = (
        self.callback +
            '/blah/%%E3%%83%%96%%E3%%83%%AD%%E3%%82%%B0%%E8%%A1%%86'
        '?hub.verify_token=the_token%%2F'
            'blah%%2F%%E3%%83%%96%%E3%%83%%AD%%E3%%82%%B0%%E8%%A1%%86'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic%%2F'
            'blah%%2F%%25E3%%2583%%2596%%25E3%%2583%%25AD'
            '%%25E3%%2582%%25B0%%25E8%%25A1%%2586'
        '&hub.mode=%s'
        '&hub.lease_seconds=2592000')
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)

    self.handle('post',
        ('hub.callback', callback_utf8),
        ('hub.topic', topic_utf8),
        ('hub.mode', 'subscribe'),
        ('hub.verify', 'sync'),
        ('hub.verify_token', verify_token_utf8))
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(
        KnownFeed.create_key(self.topic + FUNNY_IRI)) is not None)

  def testSubscribeUnicode(self):
    """Tests when UTF-8 encoded bytes show up in the requests.

    Technically this isn't well-formed or allowed by the HTTP/URI spec, but
    people do it anyways and we may as well allow it.
    """
    quoted_topic = urllib.quote(self.topic)
    topic = self.topic + FUNNY_UNICODE
    topic_utf8 = self.topic + FUNNY_UTF8
    quoted_callback = urllib.quote(self.callback)
    callback = self.callback + FUNNY_UNICODE
    callback_utf8 = self.callback + FUNNY_UTF8
    quoted_verify_token = urllib.quote(self.verify_token)
    verify_token = self.verify_token + FUNNY_UNICODE
    verify_token_utf8 = self.verify_token + FUNNY_UTF8

    sub_key = Subscription.create_key_name(
        main.normalize_iri(callback),
        main.normalize_iri(topic))
    self.assertTrue(Subscription.get_by_key_name(sub_key) is None)
    self.verify_callback_querystring_template = (
        self.callback +
            '/blah/%%E3%%83%%96%%E3%%83%%AD%%E3%%82%%B0%%E8%%A1%%86'
        '?hub.verify_token=the_token%%2F'
            'blah%%2F%%E3%%83%%96%%E3%%83%%AD%%E3%%82%%B0%%E8%%A1%%86'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic%%2F'
            'blah%%2F%%25E3%%2583%%2596%%25E3%%2583%%25AD'
            '%%25E3%%2582%%25B0%%25E8%%25A1%%2586'
        '&hub.mode=%s'
        '&hub.lease_seconds=2592000')
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)

    payload = (
        'hub.callback=' + quoted_callback + FUNNY_UTF8 +
        '&hub.topic=' + quoted_topic + FUNNY_UTF8 +
        '&hub.mode=subscribe'
        '&hub.verify=sync'
        '&hub.verify_token=' + quoted_verify_token + FUNNY_UTF8)

    self.handle_body('post', payload)
    self.assertEquals(204, self.response_code())
    sub = Subscription.get_by_key_name(sub_key)
    self.assertTrue(sub is not None)
    self.assertEquals(Subscription.STATE_VERIFIED, sub.subscription_state)
    self.assertTrue(db.get(
        KnownFeed.create_key(self.topic + FUNNY_IRI)) is not None)


class SubscribeHandlerThroughHubUrlTest(SubscribeHandlerTest):

  handler_class = main.HubHandler

################################################################################

class SubscriptionConfirmHandlerTest(testutil.HandlerTestBase):

  handler_class = main.SubscriptionConfirmHandler

  def setUp(self):
    """Sets up the test fixture."""
    testutil.HandlerTestBase.setUp(self)
    self.callback = 'http://example.com/good-callback'
    self.topic = 'http://example.com/the-topic'
    self.challenge = 'this_is_my_fake_challenge_string'
    self.old_get_challenge = main.get_random_challenge
    main.get_random_challenge = lambda: self.challenge
    self.sub_key = Subscription.create_key_name(self.callback, self.topic)
    self.verify_token = 'the_token'
    self.secret = 'teh secrat'
    self.verify_callback_querystring_template = (
        self.callback +
        '?hub.verify_token=the_token'
        '&hub.challenge=this_is_my_fake_challenge_string'
        '&hub.topic=http%%3A%%2F%%2Fexample.com%%2Fthe-topic'
        '&hub.mode=%s'
        '&hub.lease_seconds=2592000')

  def tearDown(self):
    """Verify that all URL fetches occurred."""
    testutil.HandlerTestBase.tearDown(self)
    main.get_random_challenge = self.old_get_challenge
    urlfetch_test_stub.instance.verify_and_reset()

  def verify_task(self, next_state):
    """Verifies that a subscription worker task is present.

    Args:
      next_state: The next state the task should cause the Subscription to have.
    """
    task = testutil.get_tasks(main.SUBSCRIPTION_QUEUE,
                              index=0, expected_count=1)
    self.assertEquals(self.sub_key, task['params']['subscription_key_name'])
    self.assertEquals(next_state, task['params']['next_state'])

  def verify_retry_task(self, eta, next_state):
    """Verifies that a subscription worker retry task is present.

    Args:
      eta: The ETA the retry task should have.
      next_state: The next state the task should cause the Subscription to have.
    """
    task = testutil.get_tasks(main.SUBSCRIPTION_QUEUE,
                              index=1, expected_count=2)
    self.assertEquals(testutil.task_eta(eta), task['eta'])
    self.assertEquals(self.sub_key, task['params']['subscription_key_name'])
    self.assertEquals(next_state, task['params']['next_state'])

  def testNoWork(self):
    """Tests when a task is enqueued for a Subscription that doesn't exist."""
    self.handle('post', ('subscription_key_name', 'unknown'),
                        ('next_state', Subscription.STATE_VERIFIED))

  def testSubscribeSuccessful(self):
    """Tests when a subscription task is successful."""
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is None)
    self.assertTrue(Subscription.get_by_key_name(self.sub_key) is None)
    Subscription.request_insert(
        self.callback, self.topic, self.verify_token, self.secret)
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'subscribe', 200,
        self.challenge)
    self.handle('post', ('subscription_key_name', self.sub_key),
                        ('next_state', Subscription.STATE_VERIFIED))
    self.verify_task(Subscription.STATE_VERIFIED)
    self.assertTrue(db.get(KnownFeed.create_key(self.topic)) is not None)

  def testSubscribeFailed(self):
    """Tests when a subscription task fails."""
    self.assertTrue(Subscription.get_by_key_name(self.sub_key) is None)
    Subscription.request_insert(
        self.callback, self.topic, self.verify_token, self.secret)
    urlfetch_test_stub.instance.expect('get',
        self.verify_callback_querystring_template % 'subscribe', 500, '')
    self.handle('post', ('subscription_key_name', self.sub_key),
                        ('next_state', Subscription.STATE_VERIFIED))
    sub = Subscription.get_by_key_name(self.sub_key)
    self.assertEquals(Subscription.STATE_NOT_VERIFIED, sub.subscription_state)
    self.assertEquals(1, sub.confirm_failures)
    self.verify_retry_task(sub.eta, Subscription.STATE_VERIFIED)

  def testSubscribeBadChallengeResponse(self):
    """Tests when the subscriber responds with a bad challenge."""
    self.assertTrue(Subscription.get_by_key_name(self.sub_key) is None)
    Subscription.request_insert(
        self.callback, self.topic, self.verify_token, self.secret)
    urlfetch_test_stub.instance.expect('get',
        self.verify_callback_querystring_template % 'subscribe', 200, 'bad')
    self.handle('post', ('subscription_key_name', self.sub_key),
                        ('next_state', Subscription.STATE_VERIFIED))
    sub = Subscription.get_by_key_name(self.sub_key)
    self.assertEquals(Subscription.STATE_NOT_VERIFIED, sub.subscription_state)
    self.assertEquals(1, sub.confirm_failures)
    self.verify_retry_task(sub.eta, Subscription.STATE_VERIFIED)

  def testUnsubscribeSuccessful(self):
    """Tests when an unsubscription request is successful."""
    self.assertTrue(Subscription.get_by_key_name(self.sub_key) is None)
    Subscription.insert(
        self.callback, self.topic, self.verify_token, self.secret)
    Subscription.request_remove(self.callback, self.topic, self.verify_token)
    urlfetch_test_stub.instance.expect(
        'get', self.verify_callback_querystring_template % 'unsubscribe', 200,
        self.challenge)
    self.handle('post', ('subscription_key_name', self.sub_key),
                        ('next_state', Subscription.STATE_TO_DELETE))
    self.verify_task(Subscription.STATE_TO_DELETE)
    self.assertTrue(Subscription.get_by_key_name(self.sub_key) is None)

  def testUnsubscribeFailed(self):
    """Tests when an unsubscription task fails."""
    self.assertTrue(Subscription.get_by_key_name(self.sub_key) is None)
    Subscription.insert(
        self.callback, self.topic, self.verify_token, self.secret)
    Subscription.request_remove(self.callback, self.topic, self.verify_token)
    urlfetch_test_stub.instance.expect('get',
        self.verify_callback_querystring_template % 'unsubscribe', 500, '')
    self.handle('post', ('subscription_key_name', self.sub_key),
                        ('next_state', Subscription.STATE_TO_DELETE))
    sub = Subscription.get_by_key_name(self.sub_key)
    self.assertEquals(1, sub.confirm_failures)
    self.verify_retry_task(sub.eta, Subscription.STATE_TO_DELETE)

  def testConfirmError(self):
    """Tests when an exception is raised while confirming a subscription."""
    called = [False]
    Subscription.request_insert(
        self.callback, self.topic, self.verify_token, self.secret)
    # All exceptions should just fall through.
    def new_confirm(*args):
      called[0] = True
      raise db.Error()
    try:
      main.hooks.override_for_test(main.confirm_subscription, new_confirm)
      try:
        self.handle('post', ('subscription_key_name', self.sub_key))
      except db.Error:
        pass
      else:
        self.fail()
    finally:
      main.hooks.reset_for_test(main.confirm_subscription)
    self.assertTrue(called[0])


class SubscriptionReconfirmHandlerTest(testutil.HandlerTestBase):
  """Tests for the periodic subscription reconfirming worker."""

  def setUp(self):
    """Sets up the test harness."""
    self.now = time.time()
    self.now_datetime = datetime.datetime.utcfromtimestamp(self.now)
    self.confirm_time = self.now - main.SUBSCRIPTION_CHECK_BUFFER_SECONDS
    def create_handler():
      return main.SubscriptionReconfirmHandler(now=lambda: self.now)
    self.handler_class = create_handler
    testutil.HandlerTestBase.setUp(self)
    self.original_chunk_size = main.SUBSCRIPTION_CHECK_CHUNK_SIZE
    main.SUBSCRIPTION_CHECK_CHUNK_SIZE = 2
    os.environ['X_APPENGINE_QUEUENAME'] = main.POLLING_QUEUE

  def tearDown(self):
    """Tears down the test harness."""
    testutil.HandlerTestBase.tearDown(self)
    main.SUBSCRIPTION_CHECK_CHUNK_SIZE = self.original_chunk_size
    del os.environ['X_APPENGINE_QUEUENAME']

  def testFullFlow(self):
    """Tests a full flow through multiple chunks of the reconfirm worker."""
    topic = 'http://example.com/topic'
    # Funny endings to maintain alphabetical order with hashes of callback
    # URL and topic URL.
    callback = 'http://example.com/callback1-ad'
    callback2 = 'http://example.com/callback2-b'
    callback3 = 'http://example.com/callback3-d'
    callback4 = 'http://example.com/callback4-a'
    token = 'my token'
    secret = 'my secret'
    lease_seconds = -main.SUBSCRIPTION_CHECK_BUFFER_SECONDS - 1
    now = lambda: self.now_datetime

    self.handle('get')
    task = testutil.get_tasks(main.POLLING_QUEUE, index=0, expected_count=1)
    time_offset = task['params']['time_offset']

    # There will be four Subscriptions instances, three of which will actually
    # be affected by this check.
    Subscription.insert(callback, topic, token, secret,
                        lease_seconds=lease_seconds, now=now)
    Subscription.insert(callback2, topic, token, secret,
                        lease_seconds=lease_seconds, now=now)
    Subscription.insert(callback3, topic, token, secret,
                        lease_seconds=2*main.SUBSCRIPTION_CHECK_BUFFER_SECONDS,
                        now=now)
    Subscription.insert(callback4, topic, token, secret,
                        lease_seconds=lease_seconds, now=now)

    all_subs = list(Subscription.all())
    confirm_tasks = []

    # Now run the post handler with the params from the first task. This will
    # enqueue another task that takes on the second chunk of work and also
    # will enqueue tasks to confirm subscriptions.
    self.handle('post', *task['params'].items())
    confirm_tasks.append(testutil.get_tasks(main.POLLING_QUEUE, index=2))
    confirm_tasks.append(testutil.get_tasks(main.POLLING_QUEUE, index=3))

    # Run another post handler, which will pick up the remaining subscription
    # confirmation and finish the work effort. Properly handle a race
    # condition where Subscription tasks may be inserted with an ETA before
    # the continuation task.
    all_tasks = testutil.get_tasks(main.POLLING_QUEUE, expected_count=4)
    task = [a for a in all_tasks[1:] if 'time_offset' in a['params']][0]

    self.handle('post', *task['params'].items())
    confirm_tasks.append(testutil.get_tasks(main.POLLING_QUEUE, index=5))

    # Last task will find no more work to do.
    task = testutil.get_tasks(main.POLLING_QUEUE, index=4, expected_count=6)
    self.handle('post', *task['params'].items())
    testutil.get_tasks(main.POLLING_QUEUE, expected_count=6)

    # Verify all confirmation tasks.
    self.assertEquals(callback3, all_subs[2].callback)
    del all_subs[2]
    confirm_key_names = [s.key().name() for s in all_subs]
    found_key_names = [
        t['params']['subscription_key_name'] for t in confirm_tasks]
    self.assertEquals(confirm_key_names, found_key_names)

################################################################################

PollingMarker = main.PollingMarker


class PollBootstrapHandlerTest(testutil.HandlerTestBase):

  handler_class = main.PollBootstrapHandler

  def setUp(self):
    """Sets up the test harness."""
    testutil.HandlerTestBase.setUp(self)
    self.original_chunk_size = main.BOOSTRAP_FEED_CHUNK_SIZE
    main.BOOSTRAP_FEED_CHUNK_SIZE = 2

  def tearDown(self):
    """Tears down the test harness."""
    testutil.HandlerTestBase.tearDown(self)
    main.BOOSTRAP_FEED_CHUNK_SIZE = self.original_chunk_size

  def testFullFlow(self):
    """Tests a full flow through multiple chunks."""
    topic = 'http://example.com/feed1'
    topic2 = 'http://example.com/feed2'
    topic3 = 'http://example.com/feed3-124'  # alphabetical on the hash of this
    db.put([KnownFeed.create(topic), KnownFeed.create(topic2),
            KnownFeed.create(topic3)])
    self.assertTrue(FeedToFetch.get_by_topic(topic) is None)
    self.assertTrue(FeedToFetch.get_by_topic(topic2) is None)
    self.assertTrue(FeedToFetch.get_by_topic(topic3) is None)

    # This will repeatedly insert the initial task to start the polling process.
    # TODO(bslatkin): This is actually broken. Stub needs to be fixed to ignore
    # duplicate task names.
    self.handle('get')
    self.handle('get')
    self.handle('get')
    task = testutil.get_tasks(main.POLLING_QUEUE, index=0, expected_count=1)
    sequence = task['params']['sequence']

    # Now run the post handler with the params from this first task. It will
    # enqueue another task that starts *after* the last one in the chunk.
    self.handle('post', *task['params'].items())
    self.assertTrue(FeedToFetch.get_by_topic(topic) is not None)
    self.assertTrue(FeedToFetch.get_by_topic(topic2) is not None)
    self.assertTrue(FeedToFetch.get_by_topic(topic3) is None)

    # Running this handler again will overwrite the FeedToFetch instances,
    # add tasks for them, but it will not duplicate the polling queue Task in
    # the chain of iterating through all KnownFeed entries.
    self.handle('post', *task['params'].items())
    task = testutil.get_tasks(main.POLLING_QUEUE, index=1, expected_count=2)
    self.assertEquals(sequence, task['params']['sequence'])
    self.assertEquals(str(KnownFeed.create_key(topic2)),
                      task['params']['current_key'])
    self.assertTrue(task['name'].startswith(sequence))

    # Now running another post handler will handle the rest of the feeds.
    self.handle('post', *task['params'].items())
    self.assertTrue(FeedToFetch.get_by_topic(topic) is not None)
    self.assertTrue(FeedToFetch.get_by_topic(topic2) is not None)
    self.assertTrue(FeedToFetch.get_by_topic(topic3) is not None)

    task = testutil.get_tasks(main.POLLING_QUEUE, index=2, expected_count=3)
    self.assertEquals(sequence, task['params']['sequence'])
    self.assertEquals(str(KnownFeed.create_key(topic3)),
                      task['params']['current_key'])
    self.assertTrue(task['name'].startswith(sequence))

    # Starting the cycle again will do nothing.
    self.handle('get')
    testutil.get_tasks(main.POLLING_QUEUE, expected_count=3)

    # Resetting the next start time to before the present time will
    # cause the iteration to start again.
    the_mark = PollingMarker.get()
    the_mark.next_start = \
        datetime.datetime.utcnow() - datetime.timedelta(seconds=120)
    db.put(the_mark)
    self.handle('get')
    task = testutil.get_tasks(main.POLLING_QUEUE, index=3, expected_count=4)
    self.assertNotEquals(sequence, task['params']['sequence'])

################################################################################

class HookManagerTest(unittest.TestCase):
  """Tests for the HookManager and Hook classes."""

  def setUp(self):
    """Sets up the test harness."""
    self.hooks_directory = tempfile.mkdtemp()
    if not os.path.exists(self.hooks_directory):
      os.makedirs(self.hooks_directory)
    self.valueA = object()
    self.valueB = object()
    self.valueC = object()
    self.funcA = lambda *a, **k: self.valueA
    self.funcB = lambda *a, **k: self.valueB
    self.funcC = lambda *a, **k: self.valueC
    self.globals_dict = {
      'funcA': self.funcA,
      'funcB': self.funcB,
      'funcC': self.funcC,
    }
    self.manager = main.HookManager()
    self.manager.declare(self.funcA)
    self.manager.declare(self.funcB)
    self.manager.declare(self.funcC)

  def tearDown(self):
    """Tears down the test harness."""
    shutil.rmtree(self.hooks_directory, True)

  def write_hook(self, filename, content):
    """Writes a test hook to the hooks directory.

    Args:
      filename: The relative filename the hook should have.
      content: The Python code that should go in the hook module.
    """
    hook_file = open(os.path.join(self.hooks_directory, filename), 'w')
    try:
      hook_file.write('#!/usr/bin/env python\n')
      hook_file.write(content)
    finally:
      hook_file.close()

  def load_hooks(self):
    """Causes the hooks to load."""
    self.manager.load(hooks_path=self.hooks_directory,
                      globals_dict=self.globals_dict)

  def testNoHooksDir(self):
    """Tests when there is no hooks directory present at all."""
    hooks_path = tempfile.mktemp()
    self.assertFalse(os.path.exists(hooks_path))
    self.manager.load(hooks_path=hooks_path,
                      globals_dict=self.globals_dict)
    for entry, hooks in self.manager._mapping.iteritems():
      self.assertEquals(0, len(hooks))

  def testNoHooks(self):
    """Tests loading a directory with no hooks modules."""
    self.load_hooks()
    self.assertEquals(self.valueA, self.manager.execute(self.funcA))
    self.assertEquals(self.valueB, self.manager.execute(self.funcB))
    self.assertEquals(self.valueC, self.manager.execute(self.funcC))

  def testOneGoodHook(self):
    """Tests a single good hook."""
    self.write_hook('my_hook.py',"""
class MyHook(Hook):
  def inspect(self, args, kwargs):
    return True
  def __call__(self, *args, **kwargs):
    return 'fancy string'
register(funcA, MyHook())
""")
    self.load_hooks()
    self.assertEquals('fancy string', self.manager.execute(self.funcA))

  def testDifferentHooksInOneModule(self):
    """Tests different hook methods in a single hook module."""
    self.write_hook('my_hook.py',"""
class MyHook(Hook):
  def __init__(self, value):
    self.value = value
  def inspect(self, args, kwargs):
    return True
  def __call__(self, *args, **kwargs):
    return self.value
register(funcA, MyHook('fancy A'))
register(funcB, MyHook('fancy B'))
register(funcC, MyHook('fancy C'))
""")
    self.load_hooks()
    self.assertEquals('fancy A', self.manager.execute(self.funcA))
    self.assertEquals('fancy B', self.manager.execute(self.funcB))
    self.assertEquals('fancy C', self.manager.execute(self.funcC))

  def testBadHookModule(self):
    """Tests a hook module that's bad and throws exception on load."""
    self.write_hook('my_hook.py',"""raise Exception('Doh')""")
    self.assertRaises(
        Exception,
        self.load_hooks)

  def testIncompleteHook(self):
    """Tests that an incomplete hook implementation will die on execute."""
    self.write_hook('my_hook1.py',"""
class MyHook(Hook):
  def inspect(self, args, kwargs):
    return True
register(funcA, MyHook())
""")
    self.load_hooks()
    self.assertRaises(
        AssertionError,
        self.manager.execute,
        self.funcA)

  def testHookModuleOrdering(self):
    """Tests that hook modules are loaded and applied in order."""
    self.write_hook('my_hook1.py',"""
class MyHook(Hook):
  def inspect(self, args, kwargs):
    args[0].append(1)
    return False
register(funcA, MyHook())
""")
    self.write_hook('my_hook2.py',"""
class MyHook(Hook):
  def inspect(self, args, kwargs):
    args[0].append(2)
    return False
register(funcA, MyHook())
""")
    self.write_hook('my_hook3.py',"""
class MyHook(Hook):
  def inspect(self, args, kwargs):
    return True
  def __call__(self, *args, **kwargs):
    return 'peanuts'
register(funcA, MyHook())
""")
    self.load_hooks()
    value_list = [5]
    self.assertEquals('peanuts', self.manager.execute(self.funcA, value_list))
    self.assertEquals([5, 1, 2], value_list)

  def testHookBadRegistration(self):
    """Tests when registering a hook for an unknown callable."""
    self.write_hook('my_hook1.py',"""
class MyHook(Hook):
  def inspect(self, args, kwargs):
    return False
register(lambda: None, MyHook())
""")
    self.assertRaises(
        main.InvalidHookError,
        self.load_hooks)

  def testMultipleRegistration(self):
    """Tests that the first hook is called when two are registered."""
    self.write_hook('my_hook.py',"""
class MyHook(Hook):
  def __init__(self, value):
    self.value = value
  def inspect(self, args, kwargs):
    args[0].append(self.value)
    return True
  def __call__(self, *args, **kwargs):
    return self.value
register(funcA, MyHook('fancy first'))
register(funcA, MyHook('fancy second'))
""")
    self.load_hooks()
    value_list = ['hello']
    self.assertEquals('fancy first',
                      self.manager.execute(self.funcA, value_list))
    self.assertEquals(['hello', 'fancy first', 'fancy second'], value_list)

################################################################################

if __name__ == '__main__':
  dos.DISABLE_FOR_TESTING = True
  unittest.main()
