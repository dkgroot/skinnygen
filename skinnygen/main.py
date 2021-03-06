#!/usr/bin/env python
from matplotlib.backends.backend_wx import bind

from twisted.internet.error import ReactorAlreadyInstalledError
try:
    from twisted.internet import epollreactor
    epollreactor.install()
    print 'installed reactor'
except ReactorAlreadyInstalledError:
    print 'already installed'
    pass

from twisted.internet import reactor


import sys, time

from sccpphone import SCCPPhone

from sccp.sccpmessage import SCCPMessage
from sccp.sccpmessagetype import SCCPMessageType
from sccp.sccpcallstate import SCCPCallState

from twisted.internet.interfaces import IDelayedCall

import random
import os

from const import *

from twistedhandler import *

import handlers

from argh import *

import logging
log = logging.getLogger(__name__)


SKS_ONHOOK, SKS_CONNECTED, SKS_ONHOLD, \
SKS_RINGIN, SKS_OFFHOOK, SKS_CONNTRANS, SKS_DIGOFF, \
SKS_CONNCONF, SKS_DIAL, SKS_OFFHOOKFEAT = range(10)


MESSAGE_DND_ON = 'DND ON'
MESSAGE_DND_OFF = 'DND OFF'

SERVER_HOST = '127.0.0.1'
SERVER_PORT = 2000

DEFAULT_USER_FACTORY = 'random'
DEFAULT_CALL_FACTORY = 'aggressive'

RINGER_TYPE_OFF = 0x01
RINGER_TYPE_INSIDE = 0x02
RINGER_TYPE_FLASH_ONLY = 0x05

DEFAULT_BIND_ADDRESS=('127.0.0.1', 0)

KEYSETS_NAMES = dict(enumerate(
        [
            'onhook', 'connected', 'onhold',
            'ringin', 'offhook', 'conntrans', 'digoff',
            'connconf', 'dial', 'offhookfeat'
            ]))

action_to_softkey = dict([(a, sk) for sk, a in  enumerate(
        ['empty', 'redial', 'newcall',
         'hold', 'transfer',
         'cfwdall', 'cfwdbusy', 'cfwdnoanswer',
         'backspace', 'endcall', 'resume', 'answer'])])

KEYSETS_ACTIONS = {
    SKS_DIGOFF: ['dial'],
    SKS_RINGIN: ['answer'],
    SKS_ONHOLD: ['transfer'],
    SKS_CONNTRANS: ['transfer'],
    SKS_CONNECTED: ['transfer'],
}

CONF_DIR = os.path.expanduser('~')

def get_path(fname):
    return os.path.join(CONF_DIR, '.skinnygen', fname)

def parse_numbers(numbers_str):
    return numbers_str.split(',')

def parse_lines(fname):
    f = open(fname)
    lines = [l.strip() for l in f]
    return lines

def parse_lines_safe(fname):
    try:
        return parse_lines(fname)
    except IOError:
        return []

from twisted.internet import defer

def wait(seconds, result=None):
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, result)
    return d



def randwait(min_wait, max_wait):
    return min_wait + random.random() * (max_wait-min_wait)

class GeneratorApp():

    currentCallState = SCCPCallState.SCCP_CHANNELSTATE_ONHOOK
    currentStateName = SCCPCallState.sccp_channelstates[currentCallState]
    currentCallId = 0
    currentLine= 0

    def __init__(self, reactor, device, self_line, user_factory, call_factory, numbers, bindAddress, serverAddress):
        self.reactor = reactor
        self.device = device
        self.self_line = self_line
        self.action_funcs = []

        self.bindAddress = bindAddress
        self.serverAddress = serverAddress

        self.numbers = [n for n in numbers if n != self.self_line]

        self.user_factory = user_factory
        self.call_factory = call_factory

        self.reported_dnd = False
        
        self.createPhone()

    def createPhone(self):
        host = self.bindAddress[0]
        self.sccpPhone = sccpPhone = SCCPPhone(host, self.device)
        sccpPhone.setLogger(log)
        sccpPhone.setTimerProvider(self)
        sccpPhone.setRegisteredHandler(self)
        sccpPhone.setToneHandler(self)
        sccpPhone.setSoftKeysHandler(self)
        sccpPhone.setDisplayHandler(self)
        sccpPhone.setDateTimePicker(self)        
        sccpPhone.addCallHandler(self)

        sccpPhone.setDisplayPromptStatusHandler(self)
        sccpPhone.setSetRingerHandler(self)

        sccpPhone.createClient()

    def handleSoftKeys(self, line, callid, softKeySet, softKeyMap):
        actions = KEYSETS_ACTIONS[softKeySet] if softKeySet in KEYSETS_ACTIONS else []
        softKeySetName = KEYSETS_NAMES.get(softKeySet, softKeySet)
        log.info('call %s - keyset %s' % (callid, softKeySetName)) 
        self.manager.on_actions(line, callid, actions)

    def generate_action(self):
        while True:
            action = random.choice(ACTIONS)
            params = self.gen_params(action)
            if action == 'number':
                yield 'onhook', []            
                yield 'offhook', []            
            yield action, params
            if action == 'number':
                yield 'wait', [EXTEN_TIMEOUT]

    def handleCall(self,line,callid,callState):
        if callState == SCCPCallState.SCCP_CHANNELSTATE_RINGING:
                if self.currentCallId == 0:
                    self.currentCallId = callid
                    self.currentLine = line
        if callState == SCCPCallState.SCCP_CHANNELSTATE_ONHOOK and self.currentCallId == callid:
            self.currentCallId = 0
        self.currentCallState = callState
        self.currentStateName = SCCPCallState.sccp_channelstates[callState]

        self.manager.maybe_create_call('outgoing', line, callid)

    def onSetRinger(self, ringerType, ringerMode, line, callid):
        log.info('SET RINGER %s' % [ringerType, ringerMode, line, callid])
        try:
            self.validateRinger(ringerType, ringerMode)
        except AssertionError:
            pass#reactor.stop()


    def validateRinger(self, ringerType, ringerMode):
        placed_dnd = self.manager.placed_dnd
        valid = ringerType == RINGER_TYPE_INSIDE and not (placed_dnd or self.reported_dnd) \
            or ringerType == RINGER_TYPE_FLASH_ONLY and placed_dnd and self.reported_dnd \
            or ringerType == RINGER_TYPE_OFF
        if not valid:
            log.critical('DND CHECK FAIL - ringer = %d, placed dnd = %s, reported_dnd = %s' % (ringerType, placed_dnd, self.reported_dnd))
        else:
            log.info('DND CHECK OK - ringer = %d, placed dnd = %s, reported_dnd = %s' % (ringerType, placed_dnd, self.reported_dnd))
        assert valid

    def handleDND(self, message):
        if message == MESSAGE_DND_ON:
            self.reported_dnd = True
        if message == MESSAGE_DND_OFF:
            self.reported_dnd = False

    def onDisplayPromptStatus(self, displayMessage, line, callid):
        log.info('DISPLAY PROMPT %s' % [displayMessage, line, callid])
        self.handleDND(displayMessage)
        pass


    def handleTone(self,line,callid, tone):
        if tone == SCCPStartTone.SCCP_TONE_INSIDE:
            self.manager.on_dialtone(line, callid, 'inside')

    def createTimer(self,intervalSecs,timerCallback):
        self.reactor.callLater(intervalSecs, timerCallback)

    def onConnect(self,serverAddress,deviceName,networkClient, bindAddress):
        addr, port = self.serverAddress
        log.info("trying to connect to %s:%s" % (addr, port))
        self.connection = self.reactor.connectTCP(addr, port, networkClient, bindAddress=bindAddress)

    def onRegistered(self):
        softKeySetMessage = SCCPMessage(SCCPMessageType.SoftKeySetReqMessage)
        softKeyTemplateMessage = SCCPMessage(SCCPMessageType.SoftKeyTemplateReqMessage)
        self.sendMessage(softKeyTemplateMessage)
        self.sendMessage(softKeySetMessage)
        self.init_generator()

    @defer.inlineCallbacks
    def sendMessage(self, message):
        self.sccpPhone.client.sendSccpMessage(message)
        log.info('sending sccp message %s' % message)
        yield wait(0.01)

    def displayLineInfo(self,line,dirNumber):
        #print `line`+' : ' + `dirNumber`
        pass

    def onMessages(self, messages):
        for message in messages:
            self.sendMessage(message)

    def init_generator(self):
        params_generators = create_params_generators(self.numbers)
        self.manager = Manager(self.self_line, params_generators,
                               self.user_factory, self.call_factory)
        self.manager.messages_handler = self
        self.manager.start()
        
    def setDateTime(self,day,month,year,hour,minute,seconds):
        pass
        #print `day` + '-'+`month` + '-' + `year` + ' ' +`hour`+':'+`minute`+':'+`seconds`

    def connect(self):
        self.onConnect(SERVER_HOST, self.device, self.sccpPhone.client, self.bindAddress)               
    
    def closeEvent(self, e):
        log.info("close event")
        self.reactor.stop()

    def action_to_funspec(self, (atype, args)):
        if atype == 'offhook':
            message = SCCPMessage(SCCPMessageType.OffHookMessage)
            func = self.sendMessage, [message]
        elif atype == 'onhook':
            message = SCCPMessage(SCCPMessageType.OnHookMessage)
            func = self.sendMessage, [message]
        elif atype == 'wait':
            func = self.reactor.callLater, [args[0], self.simulate_actions]
        elif atype == 'softkey':
            func = self.sccpPhone.onSoftKey, args
        elif atype == 'button':
            func = self.sccpPhone.onDialPadButtonPushed, args
        elif atype == 'number':
            def f(number):
                for button in number:
                    self.sccpPhone.onDialPadButtonPushed(button)
            func = f, args
        else:
            raise Exception('no implementation for action %s' % atype)
        return func

def parse_factory(factory_str):
    factory_func_str = '%s_factory' % factory_str
    factory = getattr(handlers, factory_func_str)
    return factory

def parse_bind_address(address_str):
    address_parts = address_str.split(':')
    address = address_parts if len(address_parts) == 2 else [address_parts[0], 0]
    return tuple(address)


@arg('device', help='device name (e.g. SEP0045464292A0)')
@arg('self_line', help='device line (e.g. 472)')
@arg('--host', default=SERVER_HOST, help='asterisk host')
@arg('--port', default=SERVER_PORT, help='asterisk port')
@arg('--user_handler', default=DEFAULT_USER_FACTORY, help='custom user behaviour handler')
@arg('--call_handler', default=DEFAULT_CALL_FACTORY, help='custom call behaviour handler')
@arg('--numbers', type=parse_numbers, default=[], help='comma separated numbers to dial  (e.g. 333,221)')
@arg('--numbers_file', type=parse_lines_safe, default=[], help='file containing comma separated numbers to dial')
@arg('--bind_address', type=parse_bind_address, default=DEFAULT_BIND_ADDRESS, help='source address to bind to')
@arg('--server_host', default=SERVER_HOST, help='source address to bind to')
@arg('--server_port', type=int, default=SERVER_PORT, help='source address to bind to')
def generate(args):
    from twisted.internet import reactor
    numbers = args.numbers_file or args.numbers
    serverAddress = args.server_host, args.server_port
    generator = GeneratorApp(reactor, args.device, args.self_line, args.user_handler, args.call_handler, numbers, args.bind_address, serverAddress)
    generator.connect()
    reactor.run()

def main():
    p = ArghParser()
    p.add_commands([generate])
    p.dispatch()
    device = sys.argv[1]


#-------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
