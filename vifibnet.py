#!/usr/bin/env python
import argparse, errno, os, select, subprocess, time
from argparse import ArgumentParser
import db, plib, upnpigd, utils, tunnel


class ArgParser(ArgumentParser):

    def convert_arg_line_to_args(self, arg_line):
        arg_line = arg_line.split('#')[0].rstrip()
        if arg_line:
            for arg in ('--' + arg_line.lstrip('--')).split():
                if arg.strip():
                    yield arg


def ovpnArgs(optional_args, ca_path, cert_path):
    # Treat openvpn arguments
    if optional_args[0] == "--":
        del optional_args[0]
    optional_args.append('--ca')
    optional_args.append(ca_path)
    optional_args.append('--cert')
    optional_args.append(cert_path)
    return optional_args


def getConfig():
    parser = ArgParser(fromfile_prefix_chars='@',
            description='Resilient virtual private network application')
    _ = parser.add_argument

    # General Configuration options
    _('--ip', default=None, dest='address', action='append', nargs=3,
            help='Ip address, port and protocol advertised to other vpn nodes')
    _('--internal-port', default=1194,
            help='Port on the machine to listen on for incomming connections')
    _('--peers-db-refresh', default=3600, type=int,
            help='the time (seconds) to wait before refreshing the peers db')
    _('-l', '--log', default='/var/log',
            help='Path to vifibnet logs directory')
    _('-s', '--state', default='/var/lib/vifibnet',
            help='Path to VPN state directory')
    _('--verbose', '-v', default=0, type=int,
            help='Defines the verbose level')
    #_('--babel-state', default='/var/lib/vifibnet/babel_state',
    #        help='Path to babeld state-file')
    #_('--db', default='/var/lib/vifibnet/peers.db',
    #        help='Path to peers database')
    _('--server', required=True,
            help="VPN address of the discovery peer server")
    _('--server-port', required=True, type=int,
            help="VPN port of the discovery peer server")

    # Routing algorithm options
    _('--hello', type=int, default=30,
            help='Hello interval for babel, in seconds')
    _('-w', '--wireless', action='store_true',
            help='''Set all interfaces to be treated as wireless interfaces
                    for the routing protocol''')

    # Tunnel options
    _('--proto', choices=['udp', 'tcp-server'], nargs='+', default=['udp'],
            help='Protocol(s) to be used by other peers to connect')
    _('--tunnel-refresh', default=300, type=int,
            help='the time (seconds) to wait before changing the connections')
    _('--dh', required=True,
            help='Path to dh file')
    _('--ca', required=True,
            help='Path to the certificate authority file')
    _('--cert', required=True,
            help='Path to the certificate file')
    # args to be removed ?
    _('--connection-count', default=20, type=int,
            help='Number of tunnels')
    _('--refresh-rate', default=0.05, type=float,
            help='''The ratio of connections to drop when refreshing the
                    connections''')
    # Openvpn options
    _('openvpn_args', nargs=argparse.REMAINDER,
            help="Common OpenVPN options (e.g. certificates)")
    return parser.parse_args()


def main():
    # Get arguments
    config = getConfig()
    manual = bool(config.address)
    network = utils.networkFromCa(config.ca)
    internal_ip, prefix = utils.ipFromCert(network, config.cert)
    openvpn_args = ovpnArgs(config.openvpn_args, config.ca, config.cert)

    # Set global variables
    tunnel.log = config.log
    utils.verbose = plib.verbose = config.verbose

    # Create and open read_only pipe to get server events
    utils.log('Creating pipe for server events', 3)
    r_pipe, write_pipe = os.pipe()
    read_pipe = os.fdopen(r_pipe)

    # Init db and tunnels
    if manual:
        utils.log('Manual external configuration', 3)
    else:
        utils.log('Attempting automatic configuration via UPnP', 4)
        try:
            forward = upnpigd.UpnpForward(config.internal_port, config.proto)
            config.address = list([forward.external_ip,
                str(forward.external_port), proto] for proto in config.proto)
        except Exception:
            forward = None
            utils.log('An atempt to forward a port via UPnP failed', 4)

    peer_db = db.PeerManager(config.state, config.server, config.server_port,
            config.peers_db_refresh, config.address, internal_ip, prefix,
            manual, config.proto, 200)
    tunnel_manager = tunnel.TunnelManager(write_pipe, peer_db, openvpn_args,
            config.hello, config.tunnel_refresh, config.connection_count,
            config.refresh_rate)

    # Launch routing protocol. WARNING : you have to be root to start babeld
    interface_list = ['vifibnet'] + list(tunnel_manager.free_interface_set)
    router = plib.router(network, internal_ip, interface_list, config.wireless,
            config.hello, os.path.join(config.state, 'vifibnet.babeld.state'),
            stdout=os.open(os.path.join(config.log, 'vifibnet.babeld.log'),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC), stderr=subprocess.STDOUT)

   # Establish connections
    server_process = list(plib.server(internal_ip, len(network) + len(prefix),
        config.connection_count, config.dh, write_pipe, config.internal_port,
        proto, config.hello, '--dev', 'vifibnet', *openvpn_args,
        stdout=os.open(os.path.join(config.log,
            'vifibnet.server.%s.log' % (proto,)),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC)) for proto in config.proto)
    tunnel_manager.refresh()

    # main loop
    try:
        while True:
            nextUpdate = min(tunnel_manager.next_refresh, peer_db.next_refresh)
            if forward != None:
                nextUpdate = min(nextUpdate, forward.next_refresh)
            nextUpdate = max(0, nextUpdate - time.time())

            ready, tmp1, tmp2 = select.select([read_pipe], [], [], nextUpdate)
            if ready:
                peer_db.handle_message(read_pipe.readline())
            if time.time() >= peer_db.next_refresh:
                peer_db.refresh()
            if time.time() >= tunnel_manager.next_refresh:
                tunnel_manager.refresh()
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    main()
