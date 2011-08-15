try:
  import cPickle as pickle
except ImportError:
  import pickle

from twisted.application.service import Service
from twisted.internet import reactor
from twisted.internet.defer import Deferred, DeferredList, succeed
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import Int32StringReceiver
from carbon.conf import settings
from carbon import log, state, events


SEND_QUEUE_LOW_WATERMARK = settings.MAX_QUEUE_SIZE * 0.8


class CarbonClientProtocol(Int32StringReceiver):
  def connectionMade(self):
    log.clients("%s::connectionMade" % self)
    self.paused = False
    self.transport.registerProducer(self, streaming=True)
    # Define internal metric names
    self.destinationName = self.factory.destinationName
    self.queuedUntilReady = 'destinations.%s.queuedUntilReady' % self.destinationName
    self.sent = 'destinations.%s.sent' % self.destinationName

    self.sendQueued()

  def connectionLost(self, reason):
    log.clients("%s::connectionLost %s" % (self, reason.getErrorMessage()))

  def pauseProducing(self):
    self.paused = True

  def resumeProducing(self):
    self.paused = False
    self.sendQueued()

  def stopProducing(self):
    self.transport.loseConnection()

  def sendDatapoint(self, metric, datapoint):
    if self.paused:
      self.factory.enqueue(metric, datapoint)
      instrumentation.increment(self.queuedUntilReady)

    elif self.factory.hasQueuedDatapoints():
      self.factory.enqueue(metric, datapoint)
      self.sendQueued()

    else:
      datapoints = [ (metric, datapoint) ]
      self.sendString( pickle.dumps(datapoints, protocol=-1) )
      instrumentation.increment(self.sent)

  def sendQueued(self):
    while (not self.paused) and self.factory.hasQueuedDatapoints():
      datapoints = self.factory.takeSomeFromQueue()
      self.sendString( pickle.dumps(datapoints, protocol=-1) )
      self.factory.checkQueue()
      instrumentation.increment(self.sent, len(datapoints))

    if (settings.USE_FLOW_CONTROL and
        state.metricReceiversPaused and
        self.factory.queueSize < SEND_QUEUE_LOW_WATERMARK):
      log.clients('send queue has space available, resuming paused clients')
      events.resumeReceivingMetrics()

  def __str__(self):
    return 'CarbonClientProtocol(%s:%d:%s)' % (self.factory.destination)
  __repr__ = __str__


class CarbonClientFactory(ReconnectingClientFactory):
  maxDelay = 5

  def __init__(self, destination):
    self.destination = destination
    self.destinationName = ('%s:%d:%s' % destination).replace('.', '_')
    self.host, self.port, self.carbon_instance = destination
    self.addr = (self.host, self.port)
    self.started = False
    # This factory maintains protocol state across reconnects
    self.queue = [] # including datapoints that still need to be sent
    self.connectedProtocol = None
    self.queueEmpty = Deferred()
    self.connectionLost = Deferred()
    self.connectFailed = Deferred()
    # Define internal metric names
    self.attemptedRelays = 'destinations.%s.attemptedRelays' % self.destinationName
    self.fullQueueDrops = 'destinations.%s.fullQueueDrops' % self.destinationName
    self.queuedUntilConnected = 'destinations.%s.queuedUntilConnected' % self.destinationName

  def buildProtocol(self, addr):
    self.connectedProtocol = CarbonClientProtocol()
    self.connectedProtocol.factory = self
    return self.connectedProtocol

  def startFactory(self):
    self.started = True
    self.connector = reactor.connectTCP(self.host, self.port, self)

  def stopFactory(self):
    self.started = False
    self.stopTrying()
    if self.connectedProtocol:
      return self.connectedProtocol.transport.loseConnection()

  @property
  def queueSize(self):
    return len(self.queue)

  def hasQueuedDatapoints(self):
    return bool(self.queue)

  def takeSomeFromQueue(self):
    datapoints = self.queue[:settings.MAX_DATAPOINTS_PER_MESSAGE]
    self.queue = self.queue[settings.MAX_DATAPOINTS_PER_MESSAGE:]
    return datapoints

  def checkQueue(self):
    if not self.queue:
      self.queueEmpty.callback(0)
      self.queueEmpty = Deferred()

  def enqueue(self, metric, datapoint):
    self.queue.append( (metric, datapoint) )

  def sendDatapoint(self, metric, datapoint):
    instrumentation.increment(self.attemptedRelays)
    if len(self.queue) >= settings.MAX_QUEUE_SIZE:
      log.clients('%s::sendDatapoint send queue full, dropping datapoint')
      instrumentation.increment(self.fullQueueDrops)
    elif self.connectedProtocol:
      self.connectedProtocol.sendDatapoint(metric, datapoint)
    else:
      self.enqueue(metric, datapoint)
      instrumentation.increment(self.queuedUntilConnected)

  def startedConnecting(self, connector):
    log.clients("%s::startedConnecting (%s:%d)" % (self, connector.host, connector.port))

  def clientConnectionLost(self, connector, reason):
    ReconnectingClientFactory.clientConnectionLost(self, connector, reason)
    log.clients("%s::clientConnectionLost (%s:%d) %s" % (self, connector.host, connector.port, reason.getErrorMessage()))
    self.connectedProtocol = None
    self.connectionLost.callback(0)
    self.connectionLost = Deferred()

  def clientConnectionFailed(self, connector, reason):
    ReconnectingClientFactory.clientConnectionFailed(self, connector, reason)
    log.clients("%s::clientConnectionFailed (%s:%d) %s" % (self, connector.host, connector.port, reason.getErrorMessage()))
    self.connectFailed.addErrback(lambda failure: None) # twisted, chill the hell out
    self.connectFailed.errback(reason)
    self.connectFailed = Deferred()

  def gracefulStop(self):
    self.queueEmpty.addCallback(lambda result: self.stopFactory())
    readyToStop = DeferredList(
      [self.connectionLost, self.connectFailed],
      fireOnOneCallback=True,
      fireOnOneErrback=True)
    return readyToStop

  def __str__(self):
    return 'CarbonClientFactory(%s:%d:%s)' % self.destination
  __repr__ = __str__


class CarbonClientManager(Service):
  def __init__(self, router):
    self.router = router
    self.client_factories = {} # { destination : CarbonClientFactory() }

  def startService(self):
    Service.startService(self)
    for factory in self.client_factories.values():
      if not factory.started:
        factory.startFactory()

  def stopService(self):
    Service.stopService(self)
    self.stopAllClients()

  def startClient(self, destination):
    if destination in self.client_factories:
      return

    log.clients("connecting to carbon daemon at %s:%d:%s" % destination)
    factory = self.client_factories[destination] = CarbonClientFactory(destination)
    self.router.addDestination(destination)
    if self.running:
      factory.startFactory()

  def stopClient(self, destination, graceful=True):
    factory = self.client_factories.get(destination)
    if factory is None:
      return

    self.router.removeDestination(destination)
    if graceful and factory.hasQueuedDatapoints():
      log.clients("Gracefully disconnecting connection to %s:%d:%s with queued datapoints" % destination)
      readyToStop = factory.gracefulStop()
      readyToStop.addCallback(lambda result: self.__disconnectClient(destination))
      return readyToStop
    else:
      factory.stopFactory()
      self.__disconnectClient(destination)
      return succeed(0)

  def __disconnectClient(self, destination):
    log.clients("disconnecting connection to %s:%d:%s" % destination)
    factory = self.client_factories.pop(destination)
    c = factory.connector
    if c and c.state == 'connecting' and not factory.hasQueuedDatapoints():
      c.stopConnecting()

  def stopAllClients(self, graceful=True):
    deferreds = []
    for destination in list(self.client_factories):
      deferreds.append( self.stopClient(destination, graceful) )
    return DeferredList(deferreds)

  def sendDatapoint(self, metric, datapoint):
    for destination in self.router.getDestinations(metric):
      self.client_factories[destination].sendDatapoint(metric, datapoint)
