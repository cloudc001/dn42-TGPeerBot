import os
import re
import shutil
import subprocess
import threading
import time
from ipaddress import ip_network

import config

_lock = threading.Lock()
_autnum_index = {}
_object_index = {}
_last_sync = 0
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _cfg(name, default):
    return getattr(config, name, default)


def _repo_dir():
    return os.path.abspath(_cfg("DN42_REGISTRY_DIR", "./dn42-registry"))


def _repo_url():
    return _cfg("DN42_REGISTRY_REPO", "https://git.dn42.dev/dn42/registry.git")


def _repo_urls():
    configured = _cfg("DN42_REGISTRY_REPOS", None)
    if configured:
        if isinstance(configured, str):
            urls = [i.strip() for i in configured.split(",") if i.strip()]
        else:
            urls = [str(i).strip() for i in configured if str(i).strip()]
    else:
        urls = []

    primary = str(_repo_url()).strip()
    if primary:
        urls.insert(0, primary)
    urls.append("https://repo.or.cz/dn42-registry.git")

    seen = set()
    result = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _sync_timeout():
    return int(_cfg("DN42_REGISTRY_SYNC_TIMEOUT", 30))


def _enabled():
    return bool(_cfg("DN42_REGISTRY_ENABLED", True))


def _run_git(args, cwd=None):
    subprocess.run(
        ["git"] + args,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=_sync_timeout(),
    )


def _git_output(args, cwd=None):
    return subprocess.check_output(
        ["git"] + args,
        cwd=cwd,
        stderr=subprocess.DEVNULL,
        timeout=_sync_timeout(),
    ).decode("utf-8", errors="ignore").strip()


def _local_head(repo_path):
    try:
        return _git_output(["rev-parse", "HEAD"], cwd=repo_path).splitlines()[0].strip()
    except BaseException:
        return None


def _remote_head(repo_url):
    try:
        output = _git_output(["ls-remote", repo_url, "HEAD"])
    except BaseException:
        return None
    if not output:
        return None
    return output.split()[0].strip()


def _remove_repo_dir(repo_path):
    repo_path = os.path.abspath(repo_path)
    if repo_path in ("", os.path.sep):
        return
    if not os.path.isdir(repo_path):
        return
    if not (
        os.path.isdir(os.path.join(repo_path, ".git"))
        or os.path.isdir(os.path.join(repo_path, "data", "aut-num"))
    ):
        return
    shutil.rmtree(repo_path)


def _clone_registry(repo_path, preferred_url=None):
    urls = []
    if preferred_url:
        urls.append(preferred_url)
    urls.extend(_repo_urls())

    seen = set()
    for repo_url in urls:
        if repo_url in seen:
            continue
        seen.add(repo_url)
        try:
            _remove_repo_dir(repo_path)
            _run_git(["clone", "--depth", "1", repo_url, repo_path])
            return True
        except BaseException:
            continue
    return False


def _parse_autnum_file(path):
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("%") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key and value and key not in data:
                    data[key] = value
    except BaseException:
        return None
    return data


def _parse_registry_file(path):
    data = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("%") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip()
                if key and value:
                    data.setdefault(key, []).append(value)
    except BaseException:
        return None
    return data


def _read_registry_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except BaseException:
        return None


def _candidate_names(query):
    query = str(query).strip()
    if not query:
        return []

    candidates = [query, query.upper()]
    upper = query.upper()
    numeric = None
    try:
        if upper.startswith("AS"):
            numeric = int(upper[2:])
        else:
            numeric = int(query)
    except ValueError:
        numeric = None

    if numeric is not None:
        candidates.append(f"AS{numeric}")
        if not upper.startswith("AS"):
            if numeric < 10000:
                candidates.append(f"AS424242{numeric:04d}")
            elif 20000 <= numeric < 30000:
                candidates.append(f"AS42424{numeric}")

    if "/" in query:
        candidates.append(query.replace("/", "_"))
        candidates.append(query.upper().replace("/", "_"))
    else:
        try:
            network = ip_network(query, strict=False)
            candidates.append(network.compressed.replace("/", "_"))
        except ValueError:
            pass

    seen = set()
    result = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _candidate_object_dirs(candidate):
    data_dir = os.path.join(_repo_dir(), "data")
    if not os.path.isdir(data_dir):
        return []

    if candidate.startswith("AS") and candidate[2:].isdigit():
        preferred = ["aut-num"]
    elif "_" in candidate and ":" in candidate:
        preferred = ["route6", "inet6num"]
    elif "_" in candidate:
        preferred = ["route", "inetnum"]
    elif candidate.endswith("-MNT"):
        preferred = ["mntner"]
    else:
        preferred = ["person", "role", "mntner", "domain", "dns"]

    dirs = []
    for name in preferred:
        path = os.path.join(data_dir, name)
        if os.path.isdir(path):
            dirs.append(name)
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if os.path.isdir(path) and name not in dirs:
            dirs.append(name)
    return dirs


def _build_autnum_index(repo_path):
    index = {}
    autnum_dir = os.path.join(repo_path, "data", "aut-num")
    if not os.path.isdir(autnum_dir):
        return index
    for name in os.listdir(autnum_dir):
        if not name.startswith("AS"):
            continue
        try:
            asn = int(name[2:])
        except ValueError:
            continue
        data = _parse_autnum_file(os.path.join(autnum_dir, name))
        if data:
            index[asn] = data
    return index


def _build_object_index(repo_path):
    index = {}
    for object_type, primary_key in (("person", "nic-hdl"), ("role", "nic-hdl"), ("mntner", "mntner")):
        object_dir = os.path.join(repo_path, "data", object_type)
        if not os.path.isdir(object_dir):
            continue
        for name in os.listdir(object_dir):
            path = os.path.join(object_dir, name)
            if not os.path.isfile(path):
                continue
            data = _parse_registry_file(path)
            if not data:
                continue
            handles = set(data.get(primary_key, []))
            handles.add(name)
            for handle in handles:
                normalized = str(handle).strip().upper()
                if normalized:
                    index[normalized] = data
    return index


def _load_from_disk():
    global _autnum_index, _object_index
    repo_path = _repo_dir()
    if not os.path.isdir(repo_path):
        return False
    index = _build_autnum_index(repo_path)
    if not index:
        return False
    object_index = _build_object_index(repo_path)
    with _lock:
        _autnum_index = index
        _object_index = object_index
    return True


def sync_registry_cache():
    global _autnum_index, _object_index, _last_sync
    if not _enabled():
        return False
    repo_path = _repo_dir()
    os.makedirs(os.path.dirname(repo_path), exist_ok=True)
    try:
        if os.path.isdir(os.path.join(repo_path, ".git")):
            local_head = _local_head(repo_path)
            remote_url = None
            remote_head = None
            for repo_url in _repo_urls():
                remote_head = _remote_head(repo_url)
                if remote_head:
                    remote_url = repo_url
                    break
            if remote_head and local_head != remote_head:
                if not _clone_registry(repo_path, preferred_url=remote_url):
                    return _load_from_disk()
            elif not local_head:
                if not _clone_registry(repo_path, preferred_url=remote_url):
                    return False
        else:
            if not _clone_registry(repo_path):
                return False
        index = _build_autnum_index(repo_path)
        if not index:
            return False
        object_index = _build_object_index(repo_path)
        with _lock:
            _autnum_index = index
            _object_index = object_index
            _last_sync = int(time.time())
        return True
    except BaseException:
        return False


def ensure_registry_cache():
    with _lock:
        if _autnum_index:
            return True
    return _load_from_disk()


def has_autnum(asn):
    try:
        asn = int(asn)
    except (TypeError, ValueError):
        return False
    ensure_registry_cache()
    with _lock:
        return asn in _autnum_index


def get_autnum_field(asn, field):
    try:
        asn = int(asn)
    except (TypeError, ValueError):
        return None
    if not field:
        return None
    ensure_registry_cache()
    field = str(field).strip().lower()
    with _lock:
        data = _autnum_index.get(asn)
        if not data:
            return None
        return data.get(field)


def get_registry_emails_for_asn(asn):
    try:
        asn = int(asn)
    except (TypeError, ValueError):
        return set()
    ensure_registry_cache()
    with _lock:
        autnum = _autnum_index.get(asn)
        object_index = dict(_object_index)
    if not autnum:
        return set()

    handles = []
    for field in ("admin-c", "tech-c", "mnt-by"):
        value = autnum.get(field)
        if value:
            handles.append(value)

    emails = set()
    for handle in handles:
        data = object_index.get(str(handle).strip().upper())
        if not data:
            continue
        for field in ("e-mail", "contact"):
            for value in data.get(field, []):
                for email in EMAIL_PATTERN.findall(value):
                    emails.add(email)
    return emails


def get_registry_object_text(query):
    if not ensure_registry_cache():
        return None

    repo_path = _repo_dir()
    for candidate in _candidate_names(query):
        for object_type in _candidate_object_dirs(candidate):
            path = os.path.join(repo_path, "data", object_type, candidate)
            if not os.path.isfile(path):
                continue
            text = _read_registry_text(path)
            if not text:
                continue
            return (
                "% This result is served from the local DN42 registry cache.\n\n"
                f"% Information related to '{object_type}/{candidate}':\n"
                f"{text}"
            )
    return None


def get_last_sync_time():
    with _lock:
        return _last_sync
