"""Docker Compose e consultas de estado, sem expor ambientes dos containers."""
import json
import shutil
import socket
from pathlib import Path

from .core import EdgeError, parse_json_rows, run


class Docker:
    def run(self, args, **kwargs):
        return run(args, **kwargs)

    def prerequisites(self):
        for command in ['docker', 'openssl']:
            if not shutil.which(command):
                raise EdgeError(f'Pré-requisito ausente: {command}.')
        self.run(['docker', 'compose', 'version'])
        self.run(['docker', 'info', '--format', '{{.ServerVersion}}'])

    def compose(self, project, *args, **kwargs):
        command = ['docker', 'compose', '--project-directory', project['directory']]
        if project.get('env_file'):
            command += ['--env-file', project['env_file']]
        for filename in project['compose_files']:
            command += ['-f', filename]
        # The resolved Compose identity is used only by the gateway; app projects
        # keep the name from their own Compose files and environment.
        if project.get('compose_name'):
            command += ['--project-name', project['compose_name']]
        return self.run(command + list(args), **kwargs)

    def containers(self):
        ids = self.run(['docker', 'ps', '-aq']).split()
        if not ids:
            return []
        template = ('{"name":{{json .Name}},"labels":{{json .Config.Labels}},'
                    '"state":{{json .State}},"networks":{{json .NetworkSettings.Networks}},'
                    '"ports":{{json .NetworkSettings.Ports}}}')
        return parse_json_rows(self.run(['docker', 'inspect', '--format', template, *ids]))


def compose_model(docker, project, config):
    try:
        model = json.loads(docker.compose(project, 'config', '--format', 'json', sensitive=True))
    except json.JSONDecodeError:
        raise EdgeError('Docker Compose não retornou uma configuração JSON válida.') from None
    services = model.get('services', {})
    if not model.get('name'):
        raise EdgeError('Compose sem nome de projeto resolvido.')
    for service in project['services']:
        if service not in services:
            raise EdgeError(f'Serviço não encontrado no Compose: {service}.')
        if services[service].get('profiles'):
            raise EdgeError(f'Serviço com profile não pode ser gerenciado: {service}.')
        dependencies = services[service].get('depends_on', {})
        if any(dependency not in project['services'] for dependency in dependencies):
            raise EdgeError(f'Cadastre também as dependências do serviço {service}.')
    aliases = network_aliases(model, config)
    for route in project['routes']:
        owners = aliases.get(route['upstream'].rsplit(':', 1)[0], set())
        if len(owners) != 1 or not owners.issubset(set(project['services'])):
            raise EdgeError(f'Upstream {route["upstream"]} deve pertencer a exatamente um serviço gerenciado na rede do Edge.')
    for service in project['services']:
        for binding in services[service].get('ports', []):
            if binding.get('protocol', 'tcp') == 'tcp' and str(binding.get('published')) in {
                str(config['http_port']), str(config['https_port'])
            }:
                raise EdgeError(f'Serviço {service} publica uma porta reservada ao gateway.')
    return model


def network_aliases(model, config):
    shared = [key for key, network in model.get('networks', {}).items()
              if network.get('external') is True and network.get('name') == config['network']]
    if not shared:
        raise EdgeError(f'Compose deve declarar a rede externa {config["network"]}.')
    aliases = {}
    for service, definition in model.get('services', {}).items():
        networks = definition.get('networks', {})
        for name in shared:
            if name not in networks:
                continue
            values = {service, *(networks[name] or {}).get('aliases', [])}
            if definition.get('container_name'):
                values.add(definition['container_name'])
            for alias in values:
                aliases.setdefault(alias, set()).add(service)
    return aliases


def public_rows(rows):
    allowed = ['Name', 'Service', 'State', 'Health']
    return [{key: row.get(key) for key in allowed} for row in rows]


def available_ports(config, containers, gateway_name):
    requested = {config['http_port'], config['https_port']}
    owned = set()
    for container in containers:
        if not container.get('state', {}).get('Running'):
            continue
        is_gateway = (container.get('labels') or {}).get('com.docker.compose.project') == gateway_name
        for target, bindings in (container.get('ports') or {}).items():
            if not target.endswith('/tcp'):
                continue
            for binding in bindings or []:
                number = int(binding['HostPort'])
                if number in requested:
                    if not is_gateway:
                        raise EdgeError(f'Porta {number} ocupada pelo container {container["name"]}.')
                    owned.add(number)
    for number in requested - owned:
        for family, address in [(socket.AF_INET, ('0.0.0.0', number)),
                                (socket.AF_INET6, ('::', number))]:
            try:
                with socket.socket(family, socket.SOCK_STREAM) as listener:
                    if family == socket.AF_INET6:
                        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    listener.bind(address)
            except OSError as error:
                # Hosts without IPv6 can still bind the IPv4 listener.
                if family == socket.AF_INET6 and error.errno in {97, 99}:
                    continue
                raise EdgeError(f'Porta {number} indisponível no host ({error.strerror}).') from None
