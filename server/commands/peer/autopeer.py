import base64
import json
import os
import re
from functools import partial
from ipaddress import IPv6Network, ip_address

import config
import requests
import tools
from base import bot, db_privilege
from telebot.types import ReplyKeyboardRemove


PENDING = {}
PENDING_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "autopeer_pending.json")
REQUIRED_FIELDS = ("target_node", "asn", "endpoint", "public_key", "peer_link_local")
FIELD_PROMPTS = {
    "target_node": "Which target node should be used? Example: HK\n目标节点是哪一个？例如：HK",
    "asn": "What is the peer ASN? Example: 4242421260\n对端 ASN 是多少？例如：4242421260",
    "endpoint": "What is the WireGuard endpoint? Example: 405218.xyz:60103\nWireGuard endpoint 是什么？例如：405218.xyz:60103",
    "public_key": "What is the peer WireGuard public key?\n对端 WireGuard 公钥是什么？",
    "peer_link_local": "What is the peer link-local address? Example: fe80::9527\n对端 link-local 地址是什么？例如：fe80::9527",
}


def _node_aliases():
    aliases = {}
    for key, label in config.SERVERS.items():
        aliases[str(key).upper()] = key
        first = str(label).split("|", 1)[0].strip().upper()
        if first:
            aliases[first] = key
    return aliases


def _send_long(chat_id, text):
    chunks = tools.split_long_msg(text, limit=3800) or [text[:3800]]
    for chunk in chunks:
        bot.send_message(chat_id, chunk, reply_markup=ReplyKeyboardRemove())


def _load_pending():
    global PENDING
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        PENDING = {int(key): value for key, value in raw.items()}
    except BaseException:
        PENDING = {}


def _save_pending():
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(PENDING, f, ensure_ascii=False, indent=2)
    except BaseException:
        pass


def _set_pending(chat_id, value):
    PENDING[int(chat_id)] = value
    _save_pending()


def _pop_pending(chat_id):
    value = PENDING.pop(int(chat_id), None)
    _save_pending()
    return value


def _parse_bool_default_true(text, negative_words):
    lowered = text.lower()
    return not any(word in lowered for word in negative_words)


def _local_parse_peer_text(text):
    aliases = _node_aliases()
    parsed = {
        "target_node": None,
        "asn": None,
        "endpoint": None,
        "public_key": None,
        "peer_link_local": None,
        "mtu": None,
        "listen_port": None,
        "mp_bgp": True,
        "extended_next_hop": True,
    }

    words = re.findall(r"[A-Za-z0-9_-]+", text)
    for word in words:
        key = aliases.get(word.upper())
        if key:
            parsed["target_node"] = key
            break

    if match := re.search(r"\bAS?\s*(424242[0-9]{4})\b", text, re.IGNORECASE):
        parsed["asn"] = int(match.group(1))
    elif match := re.search(r"\b(424242[0-9]{4})\b", text):
        parsed["asn"] = int(match.group(1))

    endpoint_pattern = re.compile(r"(?<![A-Za-z0-9+/=:.])(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})(?![A-Za-z0-9+/=:.])")
    for host, port in endpoint_pattern.findall(text):
        if host.lower().startswith("fe80"):
            continue
        parsed["endpoint"] = f"{host}:{port}"
        break

    if match := re.search(r"\b[A-Za-z0-9+/]{43}=", text):
        parsed["public_key"] = match.group(0)

    if match := re.search(r"\bfe80:[0-9A-Fa-f:]+(?:%[A-Za-z0-9_.-]+)?(?:/[0-9]{1,3})?\b", text, re.IGNORECASE):
        peer_ll = match.group(0).split("%", 1)[0].split("/", 1)[0]
        parsed["peer_link_local"] = peer_ll

    if match := re.search(r"\bmtu\b[^0-9]{0,12}([0-9]{4})\b", text, re.IGNORECASE):
        parsed["mtu"] = int(match.group(1))
    else:
        for line in text.splitlines():
            line = line.strip()
            if not re.fullmatch(r"[0-9]{4}", line):
                continue
            value = int(line)
            if 1280 <= value <= 1420:
                parsed["mtu"] = value
                break

    if match := re.search(r"\b(?:listen\s*port|listen[_-]?port|listenpor|local\s*port|port)\b[^0-9]{0,12}([0-9]{1,5})\b", text, re.IGNORECASE):
        parsed["listen_port"] = int(match.group(1))

    parsed["mp_bgp"] = _parse_bool_default_true(text, ("no mp-bgp", "no mpbgp", "disable mp-bgp", "关闭 mp-bgp", "关闭mpbgp"))
    parsed["extended_next_hop"] = _parse_bool_default_true(
        text,
        ("no extended nexthop", "no extended next-hop", "no enh", "disable enh", "关闭 extended", "关闭 enh"),
    )
    return parsed


def _deepseek_parse_peer_text(text):
    if not bool(getattr(config, "AUTOPEER_USE_DEEPSEEK", True)):
        return None
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    base_url = str(getattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
    model = str(getattr(config, "DEEPSEEK_MODEL", "deepseek-v4-flash"))
    system_prompt = (
        "You are a DN42 peer information parser. Output JSON only, with exactly these keys: "
        "target_node, asn, endpoint, public_key, peer_link_local, mtu, listen_port, mp_bgp, extended_next_hop. "
        "Use null for missing values. Do not invent target_node, asn, endpoint, public_key, or peer_link_local. "
        "target_node is a configured node code such as HK, JP, SG, TH, US, UK, DE, CN. "
        "asn must be a number like 4242421234. endpoint is the peer WireGuard remote endpoint in host:port form. "
        "public_key is the 44-character WireGuard public key ending with '='. "
        "peer_link_local is the peer fe80::/10 address. "
        "mtu is the tunnel MTU; if a standalone line contains only a number between 1280 and 1420, treat it as mtu. "
        "listen_port is the local WireGuard ListenPort; only set it when the user explicitly says listenport, listen_port, "
        "listen-port, listenpor, local port, local listen port, or equivalent. Do not infer listen_port from endpoint. "
        "Default mp_bgp and extended_next_hop to true unless the user explicitly disables them."
    )
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except BaseException:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_ai_parsed(raw):
    if not isinstance(raw, dict):
        return {}
    aliases = {
        "target_node": ("target_node", "targetNode", "node", "region", "target"),
        "asn": ("asn", "ASN", "as"),
        "endpoint": ("endpoint", "wireguard_endpoint", "wg_endpoint", "remote_endpoint"),
        "public_key": ("public_key", "publicKey", "pubkey", "wg_public_key", "wireguard_public_key"),
        "peer_link_local": ("peer_link_local", "peerLinkLocal", "link_local", "linkLocal", "ll", "peer_ll"),
        "mtu": ("mtu", "MTU"),
        "listen_port": ("listen_port", "listenPort", "local_port", "localPort", "local_listen_port", "port"),
        "mp_bgp": ("mp_bgp", "mpBgp", "mpbgp", "mp-bgp"),
        "extended_next_hop": ("extended_next_hop", "extendedNextHop", "enh", "extended-nexthop", "extended_next-hop"),
    }
    normalized = {}
    for key, candidates in aliases.items():
        for candidate in candidates:
            if raw.get(candidate) not in (None, ""):
                normalized[key] = raw[candidate]
                break
    return normalized


def parse_peer_text(text):
    local_parsed = _local_parse_peer_text(text)
    ai_parsed = _normalize_ai_parsed(_deepseek_parse_peer_text(text))
    if not ai_parsed:
        return local_parsed

    parsed = {
        "target_node": None,
        "asn": None,
        "endpoint": None,
        "public_key": None,
        "peer_link_local": None,
        "mtu": None,
        "listen_port": None,
        "mp_bgp": True,
        "extended_next_hop": True,
    }
    for key in ("target_node", "asn", "endpoint", "public_key", "peer_link_local", "mtu", "listen_port"):
        parsed[key] = ai_parsed.get(key) if ai_parsed.get(key) not in (None, "") else local_parsed.get(key)
    for key in ("mp_bgp", "extended_next_hop"):
        parsed[key] = ai_parsed[key] if isinstance(ai_parsed.get(key), bool) else local_parsed.get(key, True)
    return parsed


def missing_required_fields(parsed):
    return [field for field in REQUIRED_FIELDS if parsed.get(field) in (None, "")]


def extract_single_field(field, text):
    parsed = _local_parse_peer_text(text)
    if field in parsed and parsed[field] not in (None, ""):
        return parsed[field]
    if field == "target_node":
        aliases = _node_aliases()
        value = aliases.get(text.strip().upper())
        if value:
            return value
    if field == "asn":
        if match := re.search(r"\b(?:AS)?\s*(424242[0-9]{4})\b", text, re.IGNORECASE):
            return int(match.group(1))
    if field == "endpoint":
        if match := re.search(r"(?<![A-Za-z0-9+/=:.])(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})(?![A-Za-z0-9+/=:.])", text):
            return f"{match.group(1)}:{match.group(2)}"
    if field == "public_key":
        if match := re.search(r"\b[A-Za-z0-9+/]{43}=", text):
            return match.group(0)
    if field == "peer_link_local":
        if match := re.search(r"\bfe80:[0-9A-Fa-f:]+(?:%[A-Za-z0-9_.-]+)?(?:/[0-9]{1,3})?\b", text, re.IGNORECASE):
            return match.group(0).split("%", 1)[0].split("/", 1)[0]
    return None


def validate_parsed(parsed):
    errors = []
    normalized = dict(parsed)
    missing = missing_required_fields(normalized)
    if missing:
        errors.append("Missing required fields: " + ", ".join(missing))

    aliases = _node_aliases()
    target = normalized.get("target_node")
    if target:
        target_key = aliases.get(str(target).upper(), target)
        if target_key not in config.SERVERS:
            errors.append(f"Unknown target node: {target}")
        normalized["target_node"] = target_key

    try:
        asn = int(normalized.get("asn"))
        if not (4242420000 <= asn <= 4242429999):
            raise ValueError
        normalized["asn"] = asn
    except (TypeError, ValueError):
        errors.append("ASN must be in AS424242xxxx range")

    endpoint = normalized.get("endpoint")
    if endpoint:
        if not re.fullmatch(r"\[?[A-Za-z0-9:._-]+\]?:[0-9]{1,5}", str(endpoint)):
            errors.append("Endpoint must be host:port")
        else:
            port = int(str(endpoint).rsplit(":", 1)[1])
            if not (1 <= port <= 65535):
                errors.append("Endpoint port must be between 1 and 65535")

    public_key = normalized.get("public_key")
    if public_key:
        try:
            raw_key = base64.b64decode(public_key, validate=True)
            if len(raw_key) != 32:
                raise ValueError
        except BaseException:
            errors.append("WireGuard public key must be a valid 32-byte base64 key")

    peer_ll = normalized.get("peer_link_local")
    if peer_ll:
        try:
            if ip_address(peer_ll) not in IPv6Network("fe80::/10"):
                raise ValueError
        except ValueError:
            errors.append("Peer link-local must be an IPv6 address in fe80::/10")

    mtu = normalized.get("mtu")
    if mtu in (None, ""):
        normalized["mtu"] = int(getattr(config, "AUTOPEER_DEFAULT_MTU", 1420))
    else:
        try:
            mtu = int(mtu)
            if not (1280 <= mtu <= 1420):
                raise ValueError
            normalized["mtu"] = mtu
        except (TypeError, ValueError):
            errors.append("MTU must be between 1280 and 1420")

    normalized["mp_bgp"] = bool(normalized.get("mp_bgp", True))
    normalized["extended_next_hop"] = bool(normalized.get("extended_next_hop", True))
    listen_port = normalized.get("listen_port")
    if listen_port not in (None, ""):
        try:
            listen_port = int(listen_port)
            if not (1 <= listen_port <= 65535):
                raise ValueError
            normalized["listen_port"] = listen_port
        except (TypeError, ValueError):
            errors.append("Listen port must be between 1 and 65535")
    return normalized, errors


def _contact_for(message, asn):
    if message.from_user and message.from_user.username:
        return "@" + message.from_user.username
    return tools.get_whoisinfo_by_asn(asn)


def build_peer_payload(parsed, local_link_local, contact):
    asn = parsed["asn"]
    return {
        "ASN": asn,
        "Channel": "IPv6 & IPv4",
        "MP-BGP": "IPv6" if parsed["mp_bgp"] else "Not supported",
        "ENH": parsed["extended_next_hop"],
        "IPv6": parsed["peer_link_local"],
        "IPv4": "Not required due to Extended Next Hop",
        "Request-LinkLocal": local_link_local,
        "Clearnet": parsed["endpoint"],
        "PublicKey": parsed["public_key"],
        "Port": str(parsed.get("listen_port") or ("2" + str(asn)[-4:])),
        "MTU": parsed["mtu"],
        "Contact": contact,
    }


def _format_dryrun(parsed, dryrun):
    peer_json = json.dumps(
        {
            "target_node": parsed["target_node"],
            "asn": parsed["asn"],
            "endpoint": parsed["endpoint"],
            "public_key": parsed["public_key"],
            "peer_link_local": parsed["peer_link_local"],
            "mtu": parsed["mtu"],
            "listen_port": parsed.get("listen_port") or ("2" + str(parsed["asn"])[-4:]),
            "mp_bgp": parsed["mp_bgp"],
            "extended_next_hop": parsed["extended_next_hop"],
        },
        indent=2,
        ensure_ascii=False,
    )
    conflicts = "\n".join(f"- {item}" for item in dryrun.get("conflicts", [])) or "- none"
    commands = "\n".join(f"- {item}" for item in dryrun.get("commands", []))
    return (
        "AutoPeer dry-run\n"
        f"Target node: {parsed['target_node']} ({config.SERVERS[parsed['target_node']]})\n"
        f"ASN: AS{parsed['asn']}\n"
        f"Listen port: {parsed.get('listen_port') or ('2' + str(parsed['asn'])[-4:])}\n"
        f"WG file: {dryrun['wg_path']}\n"
        f"BIRD file: {dryrun['bird_path']}\n"
        "\nParsed JSON:\n"
        f"{peer_json}\n"
        "\nWireGuard config:\n"
        f"{dryrun['wg_config']}\n"
        "\nBIRD config:\n"
        f"{dryrun['bird_config']}\n"
        "\nCommands:\n"
        f"{commands}\n"
        "\nConflicts:\n"
        f"{conflicts}\n"
        "\nReply yes to deploy, send corrections like listenport=36708, mtu=1380, endpoint=host:port, or /cancel to abort."
    )


def _format_deploy_result(result):
    verify = result.get("verify", {})
    bird = verify.get("bird_protocol", "")
    bgp_state = "Established" if "Established" in bird else "not established"
    handshake = verify.get("wg_handshake", "").strip()
    has_handshake = bool(handshake and not handshake.endswith("\t0") and "Unable to access interface" not in handshake)
    return (
        "AutoPeer deployment finished.\n"
        f"ASN: AS{result.get('peer', {}).get('ASN')}\n"
        f"WG service: {verify.get('wg_service', 'unknown')}\n"
        f"Handshake: {'seen' if has_handshake else 'not seen yet'}\n"
        f"BGP: {bgp_state}\n"
        "\nIf BGP is not established yet, common causes are: peer side not configured, endpoint/port/firewall issue, "
        "wrong link-local address, or peer BIRD not running."
    )


def parse_pending_update(text):
    text = text.strip()
    updates = {}

    if match := re.search(r"\b(?:listen\s*port|listen[_-]?port|listenpor|local\s*port|port)\b[^0-9]{0,12}([0-9]{1,5})\b", text, re.IGNORECASE):
        updates["listen_port"] = int(match.group(1))
    if match := re.search(r"\bmtu\b[^0-9]{0,12}([0-9]{4})\b", text, re.IGNORECASE):
        updates["mtu"] = int(match.group(1))
    if match := re.search(r"\b(?:endpoint|end\s*point|remote)\b[^A-Za-z0-9[]*?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([0-9]{1,5})(?![A-Za-z0-9+/=:.])", text, re.IGNORECASE):
        updates["endpoint"] = f"{match.group(1)}:{match.group(2)}"
    if match := re.search(r"\b(?:ll|linklocal|link-local|peer[_-]?link[_-]?local)\b[^0-9A-Fa-f]*?(fe80:[0-9A-Fa-f:]+)", text, re.IGNORECASE):
        updates["peer_link_local"] = match.group(1)
    if match := re.search(r"\b(?:node|target|region)\b[^A-Za-z0-9]*([A-Za-z0-9_-]+)\b", text, re.IGNORECASE):
        node = _node_aliases().get(match.group(1).upper())
        if node:
            updates["target_node"] = node
    if match := re.search(r"\b(?:asn|as)\b[^0-9]*(424242[0-9]{4})\b", text, re.IGNORECASE):
        updates["asn"] = int(match.group(1))
    if match := re.search(r"\b(?:pubkey|public[_-]?key|wg[_-]?key)\b[^A-Za-z0-9+/]*([A-Za-z0-9+/]{43}=)", text, re.IGNORECASE):
        updates["public_key"] = match.group(1)

    lowered = text.lower()
    if not updates and lowered in {"cancel", "no", "n"}:
        updates["_cancel"] = True
    return updates


@bot.message_handler(commands=["autopeer"], is_private_chat=True)
def start_autopeer(message):
    if message.chat.id not in db_privilege:
        bot.send_message(
            message.chat.id,
            f"/autopeer is restricted. Please contact {config.CONTACT}.\n/autopeer 仅限管理员使用，请联系 {config.CONTACT}。",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) >= 2 and parts[1].lower() == "rollback":
        node = parts[2].strip() if len(parts) >= 3 else ""
        rollback_autopeer(message, node)
        return

    text = message.text.partition(" ")[2].strip()
    if text:
        handle_autopeer_text(message, text)
        return

    msg = bot.send_message(
        message.chat.id,
        (
            "Paste the DN42 peer information. Required: target node, ASN, endpoint, WireGuard public key, peer link-local.\n"
            "请粘贴 DN42 peer 信息。必需字段：目标节点、ASN、endpoint、WireGuard 公钥、对端 link-local。"
        ),
        reply_markup=ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, handle_autopeer_message)


def handle_autopeer_message(message):
    if message.text.strip() == "/cancel":
        bot.send_message(message.chat.id, "Cancelled.\n已取消。", reply_markup=ReplyKeyboardRemove())
        return
    handle_autopeer_text(message, message.text)


def handle_autopeer_text(message, text):
    bot.send_message(message.chat.id, "Parsing and dry-running AutoPeer...\n正在解析并执行 dry-run...", reply_markup=ReplyKeyboardRemove())
    continue_autopeer_with_parsed(message, parse_peer_text(text))


def continue_autopeer_with_parsed(message, parsed):
    missing = missing_required_fields(parsed)
    if missing:
        ask_missing_field(message, parsed, missing[0])
        return

    parsed, errors = validate_parsed(parsed)
    if errors:
        bot.send_message(
            message.chat.id,
            "AutoPeer input is incomplete or invalid:\n"
            + "\n".join(f"- {item}" for item in errors)
            + "\n\nUse /autopeer again with complete peer information.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    pre = tools.call_agent_action("pre_peer", "", parsed["target_node"], timeout=12)
    if pre.status != 200:
        bot.send_message(message.chat.id, f"Target node is unavailable: {parsed['target_node']}", reply_markup=ReplyKeyboardRemove())
        return
    try:
        local_link_local = json.loads(pre.text)["lla"]
    except BaseException:
        local_link_local = "fe80::3777"

    payload = build_peer_payload(parsed, local_link_local, _contact_for(message, parsed["asn"]))
    dry = tools.call_agent_action("autopeer_dryrun", payload, parsed["target_node"], timeout=20)
    if dry.status == 400:
        bot.send_message(message.chat.id, f"Dry-run validation failed:\n{dry.text}", reply_markup=ReplyKeyboardRemove())
        return
    if dry.status not in (200, 409):
        bot.send_message(
            message.chat.id,
            f"Dry-run failed on node {parsed['target_node']} with status {dry.status}:\n{dry.text}",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    try:
        dryrun = json.loads(dry.text)
    except BaseException:
        bot.send_message(message.chat.id, "Dry-run returned invalid JSON.", reply_markup=ReplyKeyboardRemove())
        return

    if dryrun.get("conflicts"):
        _send_long(message.chat.id, _format_dryrun(parsed, dryrun) + "\n\nDeployment is blocked until conflicts are resolved.")
        return

    _set_pending(message.chat.id, {"parsed": parsed, "payload": payload})
    _send_long(message.chat.id, _format_dryrun(parsed, dryrun))
    msg = bot.send_message(message.chat.id, "Confirm deployment? Reply yes to continue.\n确认部署？回复 yes 继续。")
    bot.register_next_step_handler(msg, partial(confirm_autopeer, message.chat.id))


def ask_missing_field(message, parsed, field):
    msg = bot.send_message(
        message.chat.id,
        "AutoPeer needs one more required value.\n"
        "AutoPeer 还需要补充一个必填值。\n\n"
        f"{FIELD_PROMPTS[field]}\n\n"
        "Use /cancel to abort.\n使用 /cancel 取消。",
        reply_markup=ReplyKeyboardRemove(),
    )
    bot.register_next_step_handler(msg, partial(handle_missing_field, parsed, field))


def handle_missing_field(parsed, field, message):
    if message.text.strip() == "/cancel":
        bot.send_message(message.chat.id, "Cancelled.\n已取消。", reply_markup=ReplyKeyboardRemove())
        return

    value = extract_single_field(field, message.text.strip())
    if value in (None, ""):
        msg = bot.send_message(
            message.chat.id,
            "I could not recognize that value. Please try again.\n"
            "没有识别到这个值，请重新输入。\n\n"
            f"{FIELD_PROMPTS[field]}",
            reply_markup=ReplyKeyboardRemove(),
        )
        bot.register_next_step_handler(msg, partial(handle_missing_field, parsed, field))
        return

    parsed[field] = value
    continue_autopeer_with_parsed(message, parsed)


def confirm_autopeer(chat_id, message):
    pending = PENDING.get(int(chat_id))
    if not pending:
        bot.send_message(message.chat.id, "No pending AutoPeer task.\n没有待确认的 AutoPeer 任务。", reply_markup=ReplyKeyboardRemove())
        return
    if message.text.strip() == "/cancel":
        _pop_pending(chat_id)
        bot.send_message(message.chat.id, "Cancelled.\n已取消。", reply_markup=ReplyKeyboardRemove())
        return

    if message.text.strip().lower() != "yes":
        updates = parse_pending_update(message.text)
        if updates.get("_cancel"):
            _pop_pending(chat_id)
            bot.send_message(message.chat.id, "Cancelled.\n已取消。", reply_markup=ReplyKeyboardRemove())
            return
        if not updates:
            msg = bot.send_message(
                message.chat.id,
                "I did not understand the correction. Reply yes to deploy, or send a correction like listenport=36708, mtu=1380, endpoint=host:port.\n"
                "没有识别到修正内容。回复 yes 部署，或发送 listenport=36708、mtu=1380、endpoint=host:port 这类修正。",
                reply_markup=ReplyKeyboardRemove(),
            )
            bot.register_next_step_handler(msg, partial(confirm_autopeer, chat_id))
            return

        parsed = dict(pending["parsed"])
        parsed.update({key: value for key, value in updates.items() if not key.startswith("_")})
        _pop_pending(chat_id)
        bot.send_message(message.chat.id, "Correction received. Re-running dry-run...\n已收到修正，正在重新 dry-run。")
        continue_autopeer_with_parsed(message, parsed)
        return

    _pop_pending(chat_id)
    parsed = pending["parsed"]
    bot.send_message(message.chat.id, f"Deploying AutoPeer on {parsed['target_node']}...\n正在部署 AutoPeer...")
    result = tools.call_agent_action("autopeer_deploy", pending["payload"], parsed["target_node"], timeout=60)
    if result.status != 200:
        bot.send_message(
            message.chat.id,
            f"Deployment failed with status {result.status}:\n{result.text}",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    try:
        body = json.loads(result.text)
    except BaseException:
        bot.send_message(message.chat.id, "Deployment finished but returned invalid JSON.", reply_markup=ReplyKeyboardRemove())
        return
    bot.send_message(message.chat.id, _format_deploy_result(body), reply_markup=ReplyKeyboardRemove())


@bot.message_handler(func=lambda message: message.chat.id in PENDING, is_private_chat=True)
def continue_pending_autopeer(message):
    confirm_autopeer(message.chat.id, message)


def rollback_autopeer(message, node):
    aliases = _node_aliases()
    node_key = aliases.get(str(node).upper(), node)
    if not node_key or node_key not in config.SERVERS:
        bot.send_message(message.chat.id, "Usage: /autopeer rollback <node>\n用法：/autopeer rollback <节点>", reply_markup=ReplyKeyboardRemove())
        return
    bot.send_message(message.chat.id, f"Rolling back last AutoPeer on {node_key}...\n正在回滚 {node_key} 最近一次 AutoPeer...")
    result = tools.call_agent_action("autopeer_rollback", "", node_key, timeout=40)
    if result.status != 200:
        bot.send_message(message.chat.id, f"Rollback failed with status {result.status}:\n{result.text}", reply_markup=ReplyKeyboardRemove())
        return
    bot.send_message(message.chat.id, f"Rollback finished:\n{result.text}", reply_markup=ReplyKeyboardRemove())


_load_pending()
