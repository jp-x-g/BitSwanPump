import asyncio
import logging
import random
import re

import aiohttp

from ..abc.connection import Connection

#

L = logging.getLogger(__name__)

#


class ElasticSearchBulk(object):
	"""
	Description:

	"""

	def __init__(self, connection, index, max_size):
		"""
		Initializes the variables

		**Parameters**

		connection : Connection
				Name of the Connection.

		index : str
				???

		max_size : int
				Maximal size of bulks.

		"""
		self.Index = index
		self.Aging = 0
		self.Capacity = max_size
		self.Items = []
		self.InsertMetric = connection.InsertMetric
		self.FailLogMaxSize = connection.FailLogMaxSize
		self.FilterPath = connection.FilterPath

	def consume(self, data_feeder_generator):
		"""
		Appends all items in data_feeder_generator to Items list. Consumer also resets Aging and Capacity.

		**Parameters**

		data_feeder_generator : list
				list of our data that will be passed to a generator and later Uploaded to ElasticSearch.

		:return: self.Capacity <= 0

		"""
		for item in data_feeder_generator:
			self.Items.append(item)
			self.Capacity -= len(item)

		self.Aging = 0
		return self.Capacity <= 0

	async def _get_data_from_items(self):
		"""
		Description:

		:return:
		"""
		for item in self.Items:
			yield item

	async def upload(self, url, session, timeout):
		"""
		Uploads data to Elastic Search.

		**Parameters**

		url : string
				Uses URL from config to connect to ElasticSearch Rest API.

		session : ?
				?

		timeout : int
				uses timout value from config. Value of time for how long we want to be connected to ElasticSearch.

		:return: ?

		|

		"""
		items_count = len(self.Items)
		if items_count == 0:
			return

		url = url + '{}/_bulk?filter_path={}'.format(self.Index, self.FilterPath)

		try:
			resp = await session.post(
				url,
				data=self._get_data_from_items(),
				headers={
					'Content-Type': 'application/json'
				},
				timeout=timeout,
			)
		except OSError as e:
			# This means that there was a hard error such as network or DNS failure
			# Likely no communication took place with ElasticSearch
			L.warn("{}".format(e))
			return False

		# Obtain the response from ElasticSearch, which should always be a json
		try:
			resp_body = await resp.json()
		except Exception as e:
			# We received something else than JSON, that's bad
			# Let's assume that the bulk did not reach ElasticSearch
			L.warn("{}".format(e))
			return False

		if resp.status == 200:

			# Check that all documents were successfully inserted to ElasticSearch
			# If there are no error messages, we are done here
			if not resp_body.get("errors", False):
				self.InsertMetric.add("ok", items_count)
				return True

			# Some of the documents were not inserted properly,
			# usually because of attributes mismatch, see:
			# https://www.elastic.co/guide/en/elasticsearch/reference/current/docs-bulk.html

			response_items = resp_body.get("items")
			self.partial_error_callback(response_items)

			# When the log handling is not precise,
			# the iteration only happens on error items natively
			# Log first 20 errors
			counter = 0
			for response_item in response_items:
				if "error" not in response_item.get("index", ""):
					continue

				if counter < self.FailLogMaxSize:
					L.error("Failed to insert document into ElasticSearch: '{}'".format(
							str(response_item)
							))

				counter += 1

			# Show remaining log messages
			if counter > self.FailLogMaxSize:
				L.error("Failed to insert document into ElasticSearch: '{}' more insertions of documents failed".format(
						counter - self.FailLogMaxSize
						))

			# Insert metrics
			self.InsertMetric.add("fail", counter)
			self.InsertMetric.add("ok", items_count - counter)

		else:

			# The major ElasticSearch error occurred while inserting documents, response was not 200
			self.InsertMetric.add("fail", items_count)
			L.error("Failed to insert document into ElasticSearch status:{} body:{}".format(
					resp.status,
					resp_body
					))
			return self.full_error_callback(self.Items, resp.status)

		return True

	def partial_error_callback(self, response_items):
		"""
		Description: When an upload to ElasticSearch fails for error items (document could not be inserted),
		this callback is called.

		**Parameters**

		response_items :

		:param response_items: list with dict items: {"index": {"_id": ..., "error": ...}}

		:return:
		"""
		L.error("Failed to insert items in the elasticsearch: {}".format(response_items[:10]))


	def full_error_callback(self, bulk_items, return_code):
		"""
		Description: When an upload to ElasticSearch fails b/c of ElasticSearch error,
		this callback is called.

		**Parameters**

		bulk_items : list
				list with tuple items: (_id, data)

		return_code :
				ElasticSearch return code

		:return: False if the bulk is to be resumbitted again

		|

		"""
		return False


class ElasticSearchConnection(Connection):
	"""
	Description:

	**Sample Config**

	url : ''http'://{ip/localhost}:{port}'
			URL of the source. Could be multi-URL. Each URL should be separated by ';' to a node in ElasticSearch cluster.

	username : 'string' , default = ' '
			Used when authentication is required

	password : 'string', default = ' '
			Used when authentication is required

	loader_per_url : int, default = 4
			Number of parallel loaders per URL.

	output_queue_max_size : int, default = 10
			Maximum queue size.

	bulk_out_max_size : ? * ? * ?, default = 12 * 1024 * 1024
			??

	timeout : int, default = 300
			Timout value.

	fail_log_max_size : int, default =  20
			Maximum size of failed log messages.

	precise_error_handling : bool, default = False
			If True all Errors will be logged, If false soft errors will be omitted in the Logs.



	"""

	ConfigDefaults = {
		'url': 'http://localhost:9200/',
		# Could be multi-URL. Each URL should be separated by ';' to a node in ElasticSearch cluster
		'username': '',
		'password': '',
		'loader_per_url': 4,  # Number of parallel loaders per URL
		'output_queue_max_size': 10,
		'bulk_out_max_size': 12 * 1024 * 1024,
		'timeout': 300,
		'fail_log_max_size': 20,
		'precise_error_handling': False,
	}

	def __init__(self, app, id=None, config=None):
		"""
		Description:

		**Parameters**

		app : Application
				Name of the Application

		id : ID, default= None
				ID

		config : JSON or dict, default= None
				configuration file with additional information for the methods.

		"""
		super().__init__(app, id=id, config=config)

		self._output_queue_max_size = int(self.Config['output_queue_max_size'])
		self._output_queue = asyncio.Queue()

		username = self.Config.get('username')
		password = self.Config.get('password')

		if username == '':
			self._auth = None
		else:
			self._auth = aiohttp.BasicAuth(login=username, password=password)

		# Contains URLs of each node in the cluster
		self.node_urls = []
		# for url in self.Config['url'].split(';'):
		for url in re.split(r"\s+", self.Config['url']):
			url = url.strip()
			if len(url) == 0:
				continue
			if url[-1] != '/':
				url += '/'
			self.node_urls.append(url)

		self._loader_per_url = int(self.Config['loader_per_url'])

		self._bulk_out_max_size = int(self.Config['bulk_out_max_size'])
		self._bulks = {}

		self._timeout = float(self.Config['timeout'])
		self._started = True

		self.Loop = app.Loop

		self.PubSub = app.PubSub
		self.PubSub.subscribe("Application.run!", self._start)
		self.PubSub.subscribe("Application.exit!", self._on_exit)

		self._futures = []
		for url in self.node_urls:
			for i in range(self._loader_per_url):
				self._futures.append((url, None))

		self.FailLogMaxSize = int(self.Config['fail_log_max_size'])

		# Precise error handling
		if self.Config.getboolean("precise_error_handling"):
			self.FilterPath = "errors,took,items.*.error,items.*._id"
		else:
			self.FilterPath = "errors,took,items.*.error"

		# Create metrics counters
		metrics_service = app.get_service('asab.MetricsService')
		self.InsertMetric = metrics_service.create_counter(
			"elasticsearch.insert",
			init_values={
				"ok": 0,
				"fail": 0,
			}
		)
		self.QueueMetric = metrics_service.create_gauge(
			"elasticsearch.outputqueue",
			init_values={
				"size": 0,
			}
		)

	def get_url(self):
		"""
		:return: list of URLS of nodes connected to the cluster

		"""
		return random.choice(self.node_urls)

	def get_session(self):
		"""
		Returns current Client Session Authentication and Loop

		:return: aiohttp.ClientSession(auth=self._auth, loop=self.Loop)

		:return:
		"""
		return aiohttp.ClientSession(auth=self._auth)

	def consume(self, index, data_feeder_generator, bulk_class=ElasticSearchBulk):
		"""
		Checks the content of data_feeder_generator and bulk and if There is data to be send it calls enqueue method.

		**Parameters**

		index :

		data_feeder_generator :

		bulk_class=ElasticSearchBulk :
				creates a instance of the ElasticSearchBulk class

		"""
		if data_feeder_generator is None:
			return

		bulk = self._bulks.get(index)
		if bulk is None:
			bulk = bulk_class(self, index, self._bulk_out_max_size)
			self._bulks[index] = bulk

		if bulk.consume(data_feeder_generator):
			# Bulk is ready, schedule to be send
			del self._bulks[index]
			self.enqueue(bulk)

	def _start(self, event_name):
		"""
		Description:

		:return:
		"""
		self.PubSub.subscribe("Application.tick!", self._on_tick)
		self._on_tick("simulated!")

	async def _on_exit(self, event_name):
		"""
		Description:

		:return:
		"""
		# Wait till the queue is empty
		self.flush(forced=True)
		while self._output_queue.qsize() > 0:
			self.flush(forced=True)
			await asyncio.sleep(1)
			if self._output_queue.qsize() > 0:
				L.warn("Still have {} bulk in output queue".format(self._output_queue.qsize()))

		self._started = False

		# Wait till the _loader() terminates (one after another)
		pending = [item[1] for item in self._futures]
		self._futures = []
		while len(pending) > 0:
			# By sending None via queue, we signalize end of life
			await self._output_queue.put(None)
			done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)


	def _on_tick(self, event_name):
		"""
		Description:

		:return:
		"""
		self.QueueMetric.set("size", int(self._output_queue.qsize()))

		for i in range(len(self._futures)):

			# 1) Check for exited futures
			url, future = self._futures[i]
			if future is not None and future.done():
				# Ups, _loader() task crashed during runtime, we need to restart it
				try:
					future.result()
					if self._started:
						L.error("ElasticSearch issue detected, will retry shortly")
				except Exception:
					L.exception("ElasticSearch issue detected, will retry shortly")

				self._futures[i] = (url, None)

			# 2) Start _loader() futures that are exitted
			if self._started:
				url, future = self._futures[i]
				if future is None:
					future = asyncio.ensure_future(self._loader(url))
					self._futures[i] = (url, future)

		self.flush()

	def flush(self, forced=False):
		"""
		It goes through the list of bulks and calls enqueue for each of them.

		**Parameters**

		forced : bool, default = False

		"""
		aged = []

		for index, bulk in self._bulks.items():

			if bulk is None:
				continue

			bulk.Aging += 1

			if (bulk.Aging >= 2) or forced:
				aged.append(index)

		for index in aged:
			bulk = self._bulks.pop(index)
			self.enqueue(bulk)

	def enqueue(self, bulk):
		"""
		Properly enqueue the bulk.

		**Parameters**

		bulk :

		"""
		self._output_queue.put_nowait(bulk)

		# Signalize need for throttling
		if self._output_queue.qsize() == self._output_queue_max_size:
			self.PubSub.publish("ElasticSearchConnection.pause!", self)

	async def _loader(self, url):
		"""
		Description:

		:return:
		"""
		async with self.get_session() as session:

			# Preflight check
			try:
				async with session.get(url + "_cluster/health") as resp:
					await resp.json()
					if resp.status != 200:
						L.error("Cluster is not ready", struct_data={'status': resp.status})
						await asyncio.sleep(5)  # Throttle a bit before next try
						return

			except aiohttp.client_exceptions.ServerDisconnectedError:
				L.error("Cluster is not ready, server disconnected or not ready")
				await asyncio.sleep(5)  # Throttle a bit before next try
				return

			except OSError as e:
				L.error("{}, cluster is not ready".format(e))
				await asyncio.sleep(5)  # Throttle a bit before next try
				return

			except aiohttp.client_exceptions.ContentTypeError as e:
				L.error("Failed communication {}".format(e))
				await asyncio.sleep(20)  # Throttle a lot before next try
				return

			except GeneratorExit as e:
				L.info("Generator exited {}".format(e))
				return

			# Push bulks into the ElasticSearch
			while self._started:
				bulk = await self._output_queue.get()
				if bulk is None:
					break

				if self._output_queue.qsize() == self._output_queue_max_size - 1:
					self.PubSub.publish("ElasticSearchConnection.unpause!", self)

				sucess = await bulk.upload(url, session, self._timeout)
				if not sucess:
					# Requeue the bulk for another delivery attempt to ES
					self.enqueue(bulk)
					await asyncio.sleep(5)  # Throttle a bit before next try
					break  # Exit the loader (new will be started automatically)


				# Make sure the memory is emptied
				bulk.Items = []
