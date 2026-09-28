#!/usr/bin/env python3
"""Instalação explícita do pacote, sem tocar em Docker ou estado operacional."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    if sys.version_info < (3, 11):
        sys.exit('Edge exige Python 3.11 ou superior.')
    parser = argparse.ArgumentParser(description='Instalar edge no PATH sem iniciar o gateway.')
    parser.add_argument('--prefix', default='/usr/local', help='Prefixo (padrão: /usr/local; alternativa: ~/.local)')
    arguments = parser.parse_args()
    prefix = Path(arguments.prefix).expanduser().resolve()
    source = Path(__file__).resolve().parent
    (prefix / 'lib').mkdir(parents=True, exist_ok=True)
    with (prefix / 'lib/.edge-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        package = prefix / 'lib/docker-web-edge'
        command = prefix / 'bin/edge'
        backup = prefix / 'lib/.docker-web-edge.previous'
        if os.path.lexists(command) and (not command.is_symlink() or command.resolve() != package / 'bin/edge'):
            sys.exit('Já existe um comando edge de outra origem; instalação cancelada.')
        if package.exists() and not (package / '.edge-install').is_file():
            sys.exit('O diretório de instalação pertence a outro pacote; instalação cancelada.')
        if backup.exists():
            sys.exit('Existe uma instalação anterior pendente em .docker-web-edge.previous; revise antes de continuar.')
        (prefix / 'lib').mkdir(parents=True, exist_ok=True)
        command.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.docker-web-edge.', dir=prefix / 'lib'))
        replaced = False
        try:
            for name in ['edge_tool', 'bin']:
                shutil.copytree(source / name, staging / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (staging / '.edge-install').write_text('docker-web-edge\n')
            os.chmod(staging, 0o755)
            for directory, directories, files in os.walk(staging):
                os.chmod(directory, 0o755)
                for filename in files:
                    os.chmod(Path(directory) / filename, 0o755 if filename == 'edge' else 0o644)
            if package.exists():
                package.rename(backup)
            staging.rename(package)
            replaced = True
            if not os.path.lexists(command):
                command.symlink_to(package / 'bin/edge')
            if backup.exists():
                shutil.rmtree(backup)
            print(f'Edge instalado em {command}. Inclua {command.parent} no PATH se necessário.')
        except BaseException:
            if backup.exists():
                if replaced:
                    shutil.rmtree(package)
                backup.rename(package)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)


if __name__ == '__main__':
    try:
        main()
    except OSError:
        sys.exit('Instalação falhou. Confira as permissões do prefixo informado.')
