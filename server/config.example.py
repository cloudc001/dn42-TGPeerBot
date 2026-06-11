import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# Telegram bot token from BotFather.
BOT_TOKEN = "0000000000:replace_with_your_telegram_bot_token"

# Public operator contact shown in user-facing error messages.
CONTACT = "@your_contact"

# Your own DN42 ASN.
DN42_ASN = 4242420000

WELCOME_TEXT = (
    f"Hello, I'm the bot for your DN42 Network (`AS{DN42_ASN}`).\n"
    f"你好，我是你的 DN42 网络 (`AS{DN42_ASN}`) 机器人。\n"
    "\n"
    "For more information, please check:\n"
    "更多信息请查看：\n"
    "https://example.com/\n"
)

# Whois is still used as a fallback when local registry lookup cannot find data.
WHOIS_ADDRESS = "whois.dn42.example"
DN42_ONLY = False
ALLOW_NO_CLEARNET = True

# Local DN42 registry cache. The server checks the remote HEAD every minute.
# If the local cache is behind, it reclones the registry. If remotes are
# temporarily unavailable, the existing local cache is kept.
DN42_REGISTRY_ENABLED = True
DN42_REGISTRY_DIR = "./dn42-registry"
DN42_REGISTRY_REPO = "https://git.dn42.dev/dn42/registry.git"
DN42_REGISTRY_REPOS = [
    "https://git.dn42.dev/dn42/registry.git",
    "https://repo.or.cz/dn42-registry.git",
]
DN42_REGISTRY_SYNC_TIMEOUT = 30

# Node backend settings.
# "agent": compatible HTTP API agents on every node.
# "ssh": no resident node API; run a fixed command through SSH.
BACKEND = "ssh"
ENDPOINT = "dn42.example.com"
SERVERS = {
    "hk": "HK | Hong Kong",
    "jp": "JP | Tokyo",
}

# Agent backend settings, used only when BACKEND = "agent".
API_PORT = 54321
API_TOKEN = "replace_with_a_random_shared_secret"
HOSTS = {
    "hk": "hk.dn42.example.com",
    "jp": "jp.dn42.example.com",
}

# SSH backend settings, used only when BACKEND = "ssh".
SSH_BIN = "ssh"
SSH_USER = "dn42bot"
SSH_PORT = 22
SSH_PORTS = {}
SSH_PRIVATE_KEY = "/home/dn42bot/.ssh/id_ed25519"
SSH_KNOWN_HOSTS = "/home/dn42bot/.ssh/known_hosts"
SSH_CONNECT_TIMEOUT = 8
SSH_MAX_WORKERS = 8
SSH_STRICT_HOST_KEY_CHECKING = "yes"
SSH_BATCH_MODE = True
SSH_NODE_SCRIPT = "sudo /usr/local/sbin/dn42-agentctl"
SSH_OPTIONS = []
SSH_HOSTS = {
    "hk": "hk.dn42.example.com",
    "jp": "jp.dn42.example.com",
}

# Telegram connection settings.
# Examples:
# TELEGRAM_PROXY = "socks5://127.0.0.1:1080"
# TELEGRAM_PROXY = "http://127.0.0.1:8080"
TELEGRAM_PROXY = ""

# Optional webhook mode. Leave WEBHOOK_URL empty to use long polling.
WEBHOOK_URL = ""
WEBHOOK_LISTEN_HOST = "127.0.0.1"
WEBHOOK_LISTEN_PORT = 3443

# Optional integrations and policy switches.
LG_DOMAIN = "https://lg.dn42.example.com"
PRIVILEGE_CODE = ""
REQUEST_REPLY_TIMEOUT = 5
TG_SESSION_LOG_ENABLED = True
TG_SESSION_LOG_DIR = "./tg_session_logs"
SINGLE_PRIVILEGE = False
FLAPALERTED_URL = ""
CN_WHITELIST_IP = ["8.8.8.8", "2001:4860:4860::8888"]
SENTRY_DSN = None

# AutoPeer settings.
# /autopeer is available to logged-in users, but non-privileged users may only
# create peers for their own verified ASN. When DEEPSEEK_API_KEY is set and
# AUTOPEER_USE_DEEPSEEK is True, free-form input is sent to DeepSeek first.
# The local parser is then used only as fallback/supplement, and the final
# result is still strictly validated before dry-run/deploy.
AUTOPEER_DEFAULT_MTU = 1420
AUTOPEER_USE_DEEPSEEK = True
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-v4-flash"


def send_email(asn, mnt, code, email):
    """Send a login verification code.

    Replace this example with your own SMTP, API, or relay implementation.
    Raise RuntimeError when delivery fails; return normally on success.
    """
    text = (
        f"Hi {mnt} (AS{asn}),\n"
        "\n"
        "Welcome to my DN42 Network.\n"
        "\n"
        f"Here is your verification code: {code}\n"
        "\n"
        "Have fun!\n"
    )

    try:
        mimemsg = MIMEMultipart()
        mimemsg["From"] = "My DN42 <no-reply@example.com>"
        mimemsg["To"] = f"{mnt} <{email}>"
        mimemsg["Subject"] = "DN42 verification code"
        mimemsg.attach(MIMEText(text, "plain", "utf-8"))

        connection = smtplib.SMTP_SSL(host="smtp.example.com", port=465, timeout=20)
        connection.login("no-reply@example.com", "replace_with_smtp_password")
        connection.send_message(mimemsg)
        connection.quit()
    except BaseException as exc:
        raise RuntimeError("failed to send email") from exc
