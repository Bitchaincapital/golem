import logging
import time
from unittest import mock, TestCase

from golem.core.variables import CONCENT_URL
from golem.network.concent.client import ConcentClient

logger = logging.getLogger(__name__)

mock_message = "Hello World"

mock_success = mock.MagicMock()
mock_success.statuscode = 200
mock_success.body = mock_message


mock_empty = mock.MagicMock()
mock_empty.statuscode = 200
mock_empty.body = ""


mock_error = mock.MagicMock()
mock_error.statuscode = 500
mock_error.body = mock_message


class TestConcentClient(TestCase):

    @mock.patch('requests.post', return_value=mock_success)
    def test_message(self, mock_requests_post):

        client = ConcentClient()
        response = client.message(mock_message)

        self.assertEqual(response, mock_message)
        self.assertTrue(client.is_available())

        mock_requests_post.assert_called_with(CONCENT_URL, data=mock_message)

    @mock.patch('requests.post', return_value=mock_empty)
    def test_message_empty(self, mock_requests_post):

        client = ConcentClient()
        response = client.message(mock_message)

        self.assertEqual(response, None)
        self.assertTrue(client.is_available())

        mock_requests_post.assert_called_with(CONCENT_URL, data=mock_message)

    @mock.patch('requests.post', return_value=mock_error)
    def test_message_error(self, mock_requests_post):

        client = ConcentClient()

        with self.assertRaises(Exception):
            client.message(mock_message)

        self.assertFalse(client.is_available())

        mock_requests_post.assert_called_with(CONCENT_URL, data=mock_message)

    @mock.patch('requests.post', side_effect=Exception('error'))
    def test_message_exception(self, mock_requests_post):

        client = ConcentClient()

        with self.assertRaises(Exception):
            client.message(mock_message)

        self.assertFalse(client.is_available())

        mock_requests_post.assert_called_with(CONCENT_URL, data=mock_message)

    @mock.patch('requests.post', return_value=mock_error)
    def test_message_error_repeat(self, mock_requests_post):

        client = ConcentClient()

        self.assertRaises(Exception, client.message, mock_message)
        self.assertRaises(Exception, client.message, mock_message)

        self.assertTrue(mock_requests_post.called_once)

    @mock.patch('time.time', side_effect=[time.time(), (time.time()-(6*60))])
    @mock.patch('requests.post', return_value=mock_error)
    def test_message_error_repeat_retry(self, mock_requests_post, mock_time):

        client = ConcentClient()

        self.assertRaises(Exception, client.message, mock_message)
        self.assertRaises(Exception, client.message, mock_message)

        self.assertEqual(mock_time.call_count, 3)
        self.assertEqual(mock_requests_post.call_count, 2)
