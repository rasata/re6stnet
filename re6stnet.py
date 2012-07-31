#!/usr/bin/env python
import argparse, errno, os, select, subprocess, sqlite3, time, logging
from argparse import ArgumentParser
import db, plib, upnpigd, utils, tunnel


class ArgParser(ArgumentParser):

    def convert_arg_line_to_args(self, arg_line):
        arg_line = arg_line.split('#')[0].rstrip()
        if arg_line:
            if arg_line.startswith('@'):
                yield arg_line
                return
            for arg in ('--' + arg_line.lstrip('--')).split():
                if arg.strip():
                    yield arg


def ovpnArgs(optional_args, ca_path, cert_path, key_path):
    # Treat openvpn arguments
    if optional_args and optional_args[0] == "--":
        del optional_args[0]
    optional_args.append('--ca')
    optional_args.append(ca_path)
    optional_args.append('--cert')
    optional_args.append(cert_path)
    optional_args.append('--key')
    optional_args.append(key_path)
    return optional_args


def getConfig():
    parser = ArgParser(fromfile_prefix_chars='@',
            description='Resilient virtual private network application')
    _ = parser.add_argument

    # General Configuration options
    _('--ip', default=None, dest='address', action='append', nargs=3,
            help='Ip address, port and protocol advertised to other vpn nodes')
    _('--registry', required=True,
            help="HTTP URL of the discovery peer server,"
                 " with public host (default port: 80)")
    _('--peers-db-refresh', default=3600, type=int,
            help='the time (seconds) to wait before refreshing the peers db')
    _('-l', '--log', default='/var/log',
            help='Path to re6stnet logs directory')
    _('-s', '--state', default='/var/lib/re6stnet',
            help='Path to re6stnet state directory')
    _('-v', '--verbose', default=0, type=int,
            help='Defines the verbose level')
    _('-i', '--interface', action='append', dest='iface_list', default=[],
            help='Extra interface for LAN discovery')

    # Routing algorithm options
    _('--hello', type=int, default=15,
            help='Hello interval for babel, in seconds')
    _('-w', '--wireless', action='store_true',
            help='''Set all interfaces to be treated as wireless interfaces
                    for the routing protocol''')

    # Tunnel options
    _('--pp', nargs=2, action='append',
            help='Port and protocol to be used by other peers to connect')
    _('--tunnel-refresh', default=300, type=int,
            help='time (seconds) to wait before changing the connections')
    _('--dh', required=True,
            help='Path to dh file')
    _('--ca', required=True,
            help='Path to the certificate authority file')
    _('--cert', required=True,
            help='Path to the certificate file')
    _('--key', required=True,
            help='Path to the private key file')
    _('--connection-count', default=20, type=int,
            help='Number of tunnels')
    _('--refresh-count', default=1, type=int,
            help='''The number of connections to drop when refreshing the
                    connections''')
    # Openvpn options
    _('openvpn_args', nargs=argparse.REMAINDER,
            help="Common OpenVPN options (e.g. certificates)")
    return parser.parse_args()


def main():
    # Get arguments
    config = getConfig()
    if not config.pp:
        config.pp = [['1194', 'udp'], ['1194', 'tcp-server']]
    config.pp = list((port, proto, 're6stnet-%s' % proto)
            for port, proto in config.pp)
    manual = bool(config.address)
    network = utils.networkFromCa(config.ca)
    internal_ip, prefix = utils.ipFromCert(network, config.cert)
    openvpn_args = ovpnArgs(config.openvpn_args, config.ca, config.cert,
                                                 config.key)
    db_path = os.path.join(config.state, 'peers.db')

    # Set logging
    utils.setupLog(config.verbose)

    logging.trace("Configuration :\n%s" % config)

    # Set global variables
    tunnel.log = config.log
    plib.verbose = config.verbose

    # Create and open read_only pipe to get server events
    logging.info('Creating pipe for server events...')
    r_pipe, write_pipe = os.pipe()
    read_pipe = os.fdopen(r_pipe)
    logging.debug('Pipe created')

    # Init db and tunnels
    forwarder = None
    if manual:
        logging.info('Detected manual external configuration')
        for c, s in ('udp', 'udp'), ('tcp-client', 'tcp-server'):
            if len(list(x for x in config.address if x[2] == c)) \
             < len(list(x for x in config.pp if x[1] == s)):
                logging.warning("""Beware: in manual configuration, you
                        declared less external configurations regarding
                        protocol %s/%s than you gave internal server
                        configurations""" % (c, s))
    else:
        logging.info('Attempting automatic configuration via UPnP...')
        try:
            forwarder = upnpigd.Forwarder()
            config.address = []
            for port, proto, _ in config.pp:
                ext = forwarder.AddRule(port, proto)
                if ext:
                    config.address.append(ext)
        except upnpigd.NoUPnPDevice:
            logging.info('No upnp device found')

    peer_db = db.PeerManager(db_path, config.registry, config.key,
            config.peers_db_refresh, config.address, internal_ip, prefix,
            manual, config.pp, 200)
    tunnel_manager = tunnel.TunnelManager(write_pipe, peer_db, openvpn_args,
            config.hello, config.tunnel_refresh, config.connection_count,
            config.refresh_count, config.iface_list, network)

    # Launch routing protocol. WARNING : you have to be root to start babeld
    interface_list = list(tunnel_manager.free_interface_set) \
                     + config.iface_list + list(iface
                             for _, _, iface in config.pp)
    router = plib.router(network, internal_ip, interface_list, config.wireless,
            config.hello, os.path.join(config.state, 'babeld.state'),
            stdout=os.open(os.path.join(config.log, 'babeld.log'),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC), stderr=subprocess.STDOUT)

   # Establish connections
    server_process = list(plib.server(internal_ip, len(network) + len(prefix),
        config.connection_count, config.dh, write_pipe, port,
        proto, config.hello, '--dev', iface, *openvpn_args,
        stdout=os.open(os.path.join(config.log,
            're6stnet.server.%s.log' % (proto,)),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
        stderr=subprocess.STDOUT)
        for port, proto, iface in config.pp)
    tunnel_manager.refresh()

    # main loop
    try:
        try:
            while True:
                logging.info('Sleeping ...')
                nextUpdate = min(tunnel_manager.next_refresh, peer_db.next_refresh)
                if forwarder != None:
                    nextUpdate = min(nextUpdate, forwarder.next_refresh)
                nextUpdate = max(0, nextUpdate - time.time())

                ready, tmp1, tmp2 = select.select([read_pipe], [], [], nextUpdate)
                if ready:
                    peer_db.handle_message(read_pipe.readline())
                if time.time() >= peer_db.next_refresh:
                    peer_db.refresh()
                if time.time() >= tunnel_manager.next_refresh:
                    tunnel_manager.refresh()
                if forwarder != None and time.time() > forwarder.next_refresh:
                    forwarder.refresh()
        finally:
            for p in [router] + server_process:
                try:
                    p.terminate()
                except:
                    pass
            try:
                tunnel_manager.killAll()
            except:
                pass
    except sqlite3.Error:
        traceback.print_exc()
        os.rename(db_path, db_path + '.bak')
        os.execvp(sys.executable, sys.argv)
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    main()