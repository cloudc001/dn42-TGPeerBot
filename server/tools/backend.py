import json
import shlex
import subprocess
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import requests
from requests.adapters import HTTPAdapter, Retry
from requests_futures.sessions import FuturesSession


api_result = namedtuple("api_result", ["text", "status"])
SUPPORTED_ACTIONS = {"version", "pre_peer", "peer", "info", "remove", "restart", "ping", "trace", "tcping", "route", "path"}


def backend_name():
    backend = str(getattr(config, "BACKEND", "agent")).strip().lower()
    if backend in {"agent", "ssh"}:
        return backend
    return "agent"


def _normalize_payload(data):
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return json.dumps(data, ensure_ascii=False)


def _resolve_agent_host(region):
    hosts = getattr(config, "HOSTS", {}) or {}
    if region in hosts:
        return hosts[region]
    return f"{region}.{config.ENDPOINT}"


def _resolve_ssh_host(region):
    ssh_hosts = getattr(config, "SSH_HOSTS", {}) or {}
    if region in ssh_hosts:
        return ssh_hosts[region]
    return _resolve_agent_host(region)


def _resolve_ssh_options(region):
    ssh_options_cfg = getattr(config, "SSH_OPTIONS", []) or []
    if isinstance(ssh_options_cfg, dict):
        region_opts = ssh_options_cfg.get(region)
        if region_opts is not None:
            if isinstance(region_opts, (list, tuple)):
                return [str(i) for i in region_opts if str(i).strip()]
            if str(region_opts).strip():
                return [str(region_opts).strip()]
            return []
        default_opts = ssh_options_cfg.get("default", [])
        if isinstance(default_opts, (list, tuple)):
            return [str(i) for i in default_opts if str(i).strip()]
        if str(default_opts).strip():
            return [str(default_opts).strip()]
        return []
    return [str(i) for i in ssh_options_cfg if str(i).strip()]


def _build_ssh_command(region, action, host):
    ssh_bin = str(getattr(config, "SSH_BIN", "ssh"))
    ssh_user = str(getattr(config, "SSH_USER", "dn42bot"))
    ssh_ports = getattr(config, "SSH_PORTS", {}) or {}
    ssh_port = int(ssh_ports.get(region, getattr(config, "SSH_PORT", 22)))
    ssh_connect_timeout = int(getattr(config, "SSH_CONNECT_TIMEOUT", 8))
    ssh_known_hosts = str(getattr(config, "SSH_KNOWN_HOSTS", "")).strip()
    ssh_private_key = str(getattr(config, "SSH_PRIVATE_KEY", "")).strip()
    ssh_strict_host_key_checking = str(getattr(config, "SSH_STRICT_HOST_KEY_CHECKING", "yes")).strip()
    ssh_batch_mode = bool(getattr(config, "SSH_BATCH_MODE", True))
    ssh_options = _resolve_ssh_options(region)
    ssh_node_script = str(getattr(config, "SSH_NODE_SCRIPT", "sudo /usr/local/sbin/dn42-agentctl")).strip()
    if not ssh_node_script:
        ssh_node_script = "sudo /usr/local/sbin/dn42-agentctl"

    remote_command = shlex.join(shlex.split(ssh_node_script) + [action])

    command = [
        ssh_bin,
        "-p",
        str(ssh_port),
        "-o",
        f"ConnectTimeout={ssh_connect_timeout}",
        "-o",
        f"StrictHostKeyChecking={ssh_strict_host_key_checking}",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if ssh_batch_mode:
        command += ["-o", "BatchMode=yes"]
    if ssh_known_hosts:
        command += ["-o", f"UserKnownHostsFile={ssh_known_hosts}"]
    if ssh_private_key:
        command += ["-i", ssh_private_key]
    for option in ssh_options:
        command += ["-o", str(option)]
    command.append(f"{ssh_user}@{host}")
    command.append(remote_command)
    return command


def _call_ssh(region, action, data, timeout):
    if action not in SUPPORTED_ACTIONS:
        return api_result(f"Unsupported action: {action}", 400)

    payload = _normalize_payload(data)
    hosts = _resolve_ssh_host(region)
    if isinstance(hosts, str):
        host_candidates = [i.strip() for i in hosts.split(",") if i.strip()]
    elif isinstance(hosts, (list, tuple)):
        host_candidates = [str(i).strip() for i in hosts if str(i).strip()]
    else:
        host_candidates = []
    if not host_candidates:
        host_candidates = [_resolve_agent_host(region)]

    last_error = api_result("SSH command failed", 500)
    for host in host_candidates:
        command = _build_ssh_command(region, action, host)
        try:
            proc = subprocess.run(
                command,
                input=payload,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = api_result("", 408)
            continue
        except BaseException as exc:
            last_error = api_result(str(exc), 500)
            continue

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if proc.returncode != 0:
            last_error = api_result(stderr or stdout or "SSH command failed", 500)
            continue

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            last_error = api_result(stdout or stderr or "Invalid response from remote command", 500)
            continue

        status = parsed.get("status", 500)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 500
        body = parsed.get("body", "")
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
        return api_result(body, status)

    return last_error


def _call_one_agent(region, action, data, timeout, retry, backoff_factor):
    session = requests.Session()
    session.mount(
        "http://",
        HTTPAdapter(
            max_retries=Retry(
                total=retry,
                backoff_factor=backoff_factor,
                allowed_methods=("GET", "POST"),
            )
        ),
    )
    host = _resolve_agent_host(region)
    try:
        resp = session.post(
            f"http://{host}:{config.API_PORT}/{action}",
            data=data,
            headers={"X-DN42-Bot-Api-Secret-Token": config.API_TOKEN},
            timeout=timeout,
        )
        return api_result(resp.text, resp.status_code)
    except requests.exceptions.Timeout:
        return api_result("", 408)
    except BaseException:
        return api_result("", 500)


def _call_many_agent(action, data, regions, timeout, retry, backoff_factor):
    session = FuturesSession()
    session.mount(
        "http://",
        HTTPAdapter(
            max_retries=Retry(
                total=retry,
                backoff_factor=backoff_factor,
                allowed_methods=("GET", "POST"),
            )
        ),
    )
    futures = []
    for region in regions:
        host = _resolve_agent_host(region)
        future = session.post(
            f"http://{host}:{config.API_PORT}/{action}",
            data=data,
            headers={"X-DN42-Bot-Api-Secret-Token": config.API_TOKEN},
            timeout=timeout,
        )
        future.region = region
        futures.append(future)

    result = {}
    for future in futures:
        try:
            resp = future.result()
            result[future.region] = api_result(resp.text, resp.status_code)
        except requests.exceptions.Timeout:
            result[future.region] = api_result("", 408)
        except BaseException:
            result[future.region] = api_result("", 500)
    return result


def call_node(action, data, region, *, timeout=10, retry=2, backoff_factor=0.1):
    payload = _normalize_payload(data)
    if backend_name() == "ssh":
        return _call_ssh(region, action, payload, timeout)
    return _call_one_agent(region, action, payload, timeout, retry, backoff_factor)


def call_nodes(action, data, regions, *, timeout=10, retry=5, backoff_factor=0.1):
    payload = _normalize_payload(data)
    if backend_name() == "ssh":
        regions = list(regions)
        result = {}
        workers = min(len(regions), int(getattr(config, "SSH_MAX_WORKERS", 8)))
        if workers <= 0:
            workers = 1
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(_call_ssh, region, action, payload, timeout): region for region in regions}
            for future in as_completed(future_map):
                region = future_map[future]
                try:
                    result[region] = future.result()
                except BaseException:
                    result[region] = api_result("", 500)
        return result
    return _call_many_agent(action, payload, regions, timeout, retry, backoff_factor)
