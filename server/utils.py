import hashlib
import logging
import os
import pathlib
import socket
import subprocess
import typing
from dataclasses import dataclass, fields

import yaml

logger = logging.getLogger(__name__)


@dataclass
class DeploymentConfig:
    project_name: str
    branch_name: str
    project_git_url: str
    compose_file_location: str = "docker-compose.yml"

    def __post_init__(self):
        # Check if all members are specified
        missing_members = [
            field.name for field in fields(self) if not hasattr(self, field.name)
        ]
        if missing_members:
            raise ValueError(f"Missing members: {', '.join(missing_members)}")

    def get_project_hash(self):
        return get_random_stub(f"{self.project_name}:{self.branch_name}")


class ComposeHelper:
    NGINX_SERVICE_TEMPLATE: typing.Final[
        str
    ] = """
services:
    nginx:
        image: nginx
        ports: 
            - '%s:80'
        volumes:
            - %s:/etc/nginx/conf.d/default.conf
        networks:
            - default
    """

    def __init__(self, compose_file_location: str):
        self._compose_file_location = compose_file_location
        self._compose = load_yaml_file(self._compose_file_location)

    def start_services(
        self, nginx_port: str, conf_file_path: str, deployment_namespace: str
    ):
        self._generate_processed_compose_file(
            nginx_port, conf_file_path, deployment_namespace
        )

        command = ["docker-compose", "up", "-d", "--build"]
        project_dir = pathlib.Path(self._compose_file_location).parent
        subprocess.run(command, check=True, cwd=project_dir)
        logger.info("Docker Compose up -d --build executed successfully.")

    def _generate_processed_compose_file(
        self, nginx_port: str, conf_file_path: str, deployment_namespace: str
    ):
        """
        This should ideally be called after get_service_ports_config as it will overwrite the compose file
        1. Remove ports mapping
        2. Add in a nginx config
        """
        for service in self._compose["services"]:
            if "ports" in self._compose["services"][service]:
                del self._compose["services"][service]["ports"]

            if "container_name" in self._compose["services"][service]:
                del self._compose["services"][service]["container_name"]

        service_proxy_template = ComposeHelper.NGINX_SERVICE_TEMPLATE % (
            nginx_port,
            conf_file_path,
        )
        proxy_yaml = yaml.safe_load(service_proxy_template)

        # Add the proxy nginx to all networks, along with default
        if "networks" in self._compose:
            proxy_yaml["services"]["nginx"]["networks"].extend(
                self._compose["networks"]
            )

        self._compose["services"][f"nginx_{deployment_namespace}"] = proxy_yaml[
            "services"
        ]["nginx"]

        with open(self._compose_file_location, "w") as yaml_file:
            # Dump the data to the YAML file
            yaml.dump(self._compose, yaml_file, default_flow_style=False)

        logger.info(f"YAML data written to {self._compose_file_location} successfully.")

    def get_service_ports_config(
        self,
    ) -> typing.Dict[str, typing.List[typing.Tuple[int, int]]]:
        services = {}
        for service in self._compose["services"]:
            if service not in services:
                services[service] = []

            port_mappings = []

            if "ports" in self._compose["services"][service]:
                port_mappings = self._compose["services"][service]["ports"]

            for port_mapping in port_mappings:
                ports = port_mapping.split(":")
                services[service].append((ports[-2], ports[-1]))
        return services


class NginxHelper:
    SERVER_BLOCK_TEMPLATE: typing.Final[
        str
    ] = """
    server {
        listen 80;
        server_name %s;
        %s
    }
    
    """

    ROUTES_BLOCK_TEMPLATE: typing.Final[
        str
    ] = """
            location / {
                proxy_pass http://%s:%s;
                proxy_set_header Host $host;
                proxy_set_header X-Real-IP $remote_addr;
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                proxy_set_header X-Forwarded-Proto $scheme;
            }
    """

    PROJECT_BLOCK_TEMPLATE: typing.Final[
        str
    ] = """
    server {
        listen 80;
        server_name %s;
    
        location / {
            proxy_pass http://%s:%s;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }   
    """

    def __init__(self, config: DeploymentConfig):
        self._project_name = config.project_name
        self._project_hash = config.get_project_hash()
        self._port = None
        self._host_name = os.environ.get("DEPLOYMENT_HOST") or "host.docker.internal"
        self._start_port = os.environ.get("DEPLOYMENT_PORT_START") or 15000
        self._end_port = os.environ.get("DEPLOYMENT_PORT_END") or 20000
        self._DOMAIN_NAME = os.environ.get("DOMAIN_NAME") or "localhost"
        self._DOCKER_INTERNAL_HOSTNAME: typing.Final[str] = "host.docker.internal"

    def find_free_port(self) -> str:
        current_port = self._start_port

        while current_port <= self._end_port:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.connect((self._host_name, current_port))
                    s.close()
                    current_port += 1
                except ConnectionRefusedError:
                    self._port = current_port
                    return current_port

        raise RuntimeError(f"Could not find a free port in the specified range.")

    def generate_outer_proxy_conf_file(
        self, port: str, project_conf_location: str
    ) -> str:
        port = port or self._port
        server_name_regex = f"~{self._project_hash}.{self._DOMAIN_NAME}"
        conf = NginxHelper.PROJECT_BLOCK_TEMPLATE % (
            server_name_regex,
            self._DOCKER_INTERNAL_HOSTNAME,
            port,
        )
        conf_file_name = f"{self._project_name}-{self._project_hash}.conf"
        conf_file_location = os.path.join(project_conf_location, conf_file_name)

        with open(conf_file_location, "w") as file:
            file.write(conf)

        if not self._test_nginx_config():
            os.remove(conf_file_location)
            raise Exception("Failed creating outer_proxy_conf_file", conf)
        return conf

    def generate_project_proxy_conf_file(
        self,
        services: typing.Dict[str, typing.List[typing.Tuple[int, int]]],
        project_path: str,
    ) -> typing.Tuple[str, typing.List[str]]:
        urls: typing.List[str] = []
        routes = ""
        for service, ports_mappings in services.items():
            for ports in ports_mappings:
                routes_block = NginxHelper.ROUTES_BLOCK_TEMPLATE % (
                    service,
                    ports[1],
                )

                service_url = f"{self._project_name}-{ports[0]}-{self._project_hash}.{self._DOMAIN_NAME}"
                server_name_regex = f"~{service_url}"
                urls.append(f"http://{service_url}")

                server_block = NginxHelper.SERVER_BLOCK_TEMPLATE % (
                    server_name_regex,
                    routes_block,
                )
                routes += server_block

        conf_file_name = f"{self._project_name}-{self._project_hash}.conf"
        conf_file_path = os.path.join(project_path, conf_file_name)
        with open(conf_file_path, "w") as file:
            file.write(routes)

        return str(conf_file_path), urls

    def _test_nginx_config(self):
        try:
            command = subprocess.run(
                ["docker", "exec", "sarthi_nginx", "nginx", "-t"],
                check=True,
                capture_output=True,
                text=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Error testing Nginx configuration: {e}")
            raise Exception(f"Nginx configs error {e}")

    def reload_nginx(self):
        self._test_nginx_config()
        subprocess.run(
            ["docker", "exec", "sarthi_nginx", "nginx", "-s", "reload"],
            check=True,
        )
        logger.info("Nginx reloaded successfully.")


def get_random_stub(project_name: str) -> str:
    return hashlib.md5(project_name.encode()).hexdigest()[:16]


def load_yaml_file(filename: str):
    with open(filename) as file:
        return yaml.safe_load(file)
