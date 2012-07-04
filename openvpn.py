import subprocess

def openvpn(config, *args):
    args = ['openvpn',
        '--dev', 'tap',
        '--ca', config.ca,
        '--cert', config.cert,
        '--key', config.key,
        '--persist-tun',
        '--persist-key',
        '--script-security', '2',
        '--user', 'nobody',
        '--group', 'nogroup',
        '--verb', config.verbose
        ] + list(args)
    return subprocess.Popen(args)

# TODO : set iface up when creating a server/client
# ! check working directory before launching up script ?

def server(config, ip):
    return openvpn(config,
        '--tls-server',
        '--keepalive', '10', '60',
        '--mode', 'server',
        '--duplicate-cn',
        '--up', 'up-server ' + ip,
        '--dh', config.dh)

def client(config, serverIp):
    return openvpn(config,
        '--nobind',
        '--tls-client',
        '--remote', serverIp,
        '--up', 'up-client')

