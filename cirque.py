# You may redistribute this program and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


from twisted.internet import reactor
from twisted.internet.defer import DeferredQueue, QueueUnderflow
from twisted.internet.protocol import DatagramProtocol
from twisted.internet.task import LoopingCall
import bencoder
import pprint
import publicToIp6
import random
import string
import hashlib



BUFFER_SIZE = 69632
KEEPALIVE_INTERVAL_SECONDS = 2
SUPER_VERBOSE = False

log_file = open('admin_api_log', 'w')  # TODO: Better job of file naming here.


def random_string():
    return ''.join(
        random.choice(string.ascii_uppercase + string.digits)
        for x in range(10))


class CJDNSAdminClient(DatagramProtocol):

    timeout = 3

    actions = {
        'ping': {'q': 'ping'}
               }

    functions = {}
    function_queue = {}
    messages = {}
    address_lookups = {}
    routing_table = {}

    queue = DeferredQueue()


    def __init__(self, host, port, password):
        self.host = host
        self.port = port
        self.password = password
        self.countdown = self.timeout
        self.ponged = False

    def check_ponged(self):
        print "Checking if CJDNS admin has ponged: "
        if not self.ponged:
            print "No pong yet! We'll try pinging again soon."
            self.ping()
        else:
            print "Yep! CJDNS admin is responding."

    def ping(self):
        print "Pinging."
        ping = bencoder.encode(self.actions['ping'])
        reactor.callLater(1, self.check_ponged)
        self.transport.write(ping, (self.host, self.port))

    def pong(self):
        if not self.ponged:
            self.ponged = True
            self.function_pages_registered = 0
            self.ask_for_functions(0)

    def get_cookie(self, txid=None):
        if not txid:
            txid = random_string()

        request = {'q': 'cookie', 'txid': txid}
        self.transport.write(bencoder.encode(request), (self.host, self.port))
        return txid

    def engage(self, function_name, txid=None, page=None, **kwargs):
        call_args = {}

        default_args = self.functions[function_name]

        for arg in default_args:
            if arg not in kwargs:
                # If the user didn't specify a value (ie, they didn't override the value for this kwarg)
                # then we'll assume they want the default.
                call_args[arg] = default_args[arg]
            else:
                # ...otherwise, we go with their kwarg.
                call_args[arg] = kwargs[arg]


        # If either page or kwargs['page'] is set, we'll pass that value.
        if kwargs.get('page'):
            call_args['page'] = kwargs.get('page')

        if page is not None:
            call_args['page'] = page

        txid = self.get_cookie(txid)

        if SUPER_VERBOSE:
            print "requested cookie for to run %s as %s" % (function_name, txid)


        self.function_queue[txid] = (function_name, call_args, page)

    def call_function(self, cookie, function_name, call_args, txid=None, password=None):
        if not password:
            password=self.password

        if not txid:
            txid = random_string()

        pass_hash = hashlib.sha256(password + cookie).hexdigest()

        req = {
            'q': 'auth',
            'aq': function_name,
            'hash': pass_hash,
            'cookie': cookie,
            'args': call_args,
            'txid': txid
        }
        first_time_benc_req = bencoder.encode(req)
        req['hash'] = hashlib.sha256(first_time_benc_req).hexdigest()
        second_time_benc_req = bencoder.encode(req)

        if SUPER_VERBOSE:
            print "Calling function: %s" % req
        self.transport.write(second_time_benc_req, (self.host, self.port))

    def subscribe_to_log(self):
        self.engage("AdminLog_subscribe",
                    level=self.log_level
                    )
#         reactor.callLater(9, self.subscribe_to_log)  # TODO: This wasn't working.

    def ask_for_functions(self, page):
        availableFunctions = {}

        request_dict = {'q': 'Admin_availableFunctions', 'args': {'page': page}}
        self.transport.write(bencoder.encode(request_dict), (self.host, self.port))


    def register_functions(self, function_dict, more_to_come):
        self.functions.update(function_dict)
        self.function_pages_registered += 1

        if more_to_come:
            self.ask_for_functions(self.function_pages_registered)
        else:
            print "No more pages of functions.  Received %s" % self.function_pages_registered
            self.connection_complete()

    def connection_complete(self):
        pass

    def startProtocol(self):
        self.ping()
        reactor.callLater(1, self.advance_countdown)

    def show_nice_name(self, ip):
        return self.known_names.get(ip) or ip

    def datagramReceived(self, data, (host, port)):
        '''
        A bunch of hastily cobbled logic around common function returs.
        TODO: Turn this into an actual messaging service.
        '''
        self.countdown = self.timeout
        data_dict = bencoder.decode(data)

        try:
            response_function_name, call_args, page = self.function_queue[data_dict['txid']]
        except KeyError:
            response_function_name = None

        if SUPER_VERBOSE:
            pprint.pprint("RESPONSE FOR %s is: %s" % (response_function_name, data_dict))


        if data_dict.get('q')  == "pong":
            self.pong()

        if data_dict.has_key('availableFunctions'):
            functions_dict = data_dict['availableFunctions']
            self.register_functions(functions_dict,
                                    more_to_come=True if ('more' in data_dict) else False)

        if data_dict.has_key('cookie'): # We determine this to be a cookie
            try:
                return self.call_function(data_dict['cookie'], response_function_name, call_args, txid=data_dict['txid'])
            except KeyError:
                print "Got a cookie with no txid.  Weird.  data_dict was %s" % data_dict
                return

        if data_dict.has_key('peers'):
            print "======PEERS LIST======"
            for peer in data_dict['peers']:
                peer_address =  publicToIp6.PublicToIp6_convert(peer.pop('publicKey'))
                print "===%s===" % self.show_nice_name(peer_address)
                pprint.pprint(peer)
                self.route_lookup(peer_address)
            print "======END PEERS LIST======"

        if data_dict.get('txid'):
            if data_dict['txid'] in self.address_lookups.keys():
                ip = self.address_lookups[data_dict['txid']]
                print "%s has a route: %s" % (self.show_nice_name(ip), data_dict['result'])

        if data_dict.has_key('result'):
#             self.deal_with_result(data_dict['result'])  # TODO: This is a reasonable pattern.  Let's implement it.

            print "======GOT RESULT for %s======" % response_function_name


            if response_function_name == 'NodeStore_nodeForAddr':
                print "======NODE INFORMATION======"
                pprint.pprint(data_dict)
                print "======END NODE INFORMATION======"


        if data_dict.has_key('routingTable'):
            routing_table = data_dict['routingTable']
            for route in routing_table:
                self.routing_table[route.pop('ip')] = route
            if data_dict.has_key('more'):
                self.engage('NodeStore_dumpTable', page=page+1)
            else:
                print "======ROUTING TABLE======"
                for ip, route in self.routing_table.items():
                    print "%s - %s" % (self.show_nice_name(ip), route)
                print "======END ROUTING TABLE======"

        if response_function_name in('SwitchPinger_ping', 'RouterModule_pingNode'):
            print "======PING RESULT======"
            pprint.pprint(data_dict)

        if response_function_name == "ETHInterface_beginConnection":
            print "======Ethernet Connection======"
            pprint.pprint(data_dict)

        if response_function_name == "AdminLog_subscribe":
            pprint.pprint(data_dict, log_file)



    def deal_with_result(result):
        pass


    def advance_countdown(self):
        self.countdown -= 1
        if self.countdown < 1:
            self.stop()
        else:
            print "Stopping in %s seconds unless there's more data." % self.countdown
            reactor.callLater(1, self.advance_countdown)

    def stop(self):
        print "No activity.  Stopping."
        self.stopProtocol()
        reactor.stop()

    def route_lookup(self, address):
        txid = random_string()
        self.address_lookups[txid] = address
        self.engage('RouterModule_lookup', txid, address=address)

