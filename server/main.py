#!/usr/bin/env python3

import pickle
import re
import threading
import time

import base
import commands  # noqa: F401
import config
import sentry_sdk
import telebot
import tools
import urllib3
from aiohttp import web
from apscheduler.schedulers.background import BackgroundScheduler
from base import bot, db, db_privilege
from pytz import utc
from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.types import BotCommandScopeAllPrivateChats, ReplyKeyboardRemove


class RequestGuard:
    def __init__(self, bot_instance, timeout=5):
        self.bot = bot_instance
        self.timeout = timeout
        self._lock = threading.RLock()
        self._pending = {}
        self._expired = set()
        self._next_id = 0
        self._local = threading.local()
        self._original_send_message = bot_instance.send_message
        self._original_reply_to = bot_instance.reply_to
        self._original_edit_message_text = bot_instance.edit_message_text
        self._wrap_bot_methods()

    def _wrap_bot_methods(self):
        def send_message(chat_id, *args, **kwargs):
            if self._current_request_expired():
                return None
            result = self._original_send_message(chat_id, *args, **kwargs)
            self.mark_replied()
            return result

        def reply_to(message, *args, **kwargs):
            if self._current_request_expired():
                return None
            result = self._original_reply_to(message, *args, **kwargs)
            self.mark_replied()
            return result

        def edit_message_text(text, chat_id=None, message_id=None, inline_message_id=None, *args, **kwargs):
            if self._current_request_expired():
                return None
            result = self._original_edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                inline_message_id=inline_message_id,
                *args,
                **kwargs,
            )
            self.mark_replied()
            return result

        self.bot.send_message = send_message
        self.bot.reply_to = reply_to
        self.bot.edit_message_text = edit_message_text

    @staticmethod
    def _key(update):
        user = getattr(update, "from_user", None)
        if user:
            return user.id
        message = getattr(update, "message", None)
        user = getattr(message, "from_user", None)
        if user:
            return user.id
        chat = getattr(update, "chat", None)
        if chat:
            return chat.id
        chat = getattr(message, "chat", None)
        if chat:
            return chat.id
        return None

    @staticmethod
    def _chat_id(update):
        chat = getattr(update, "chat", None)
        if chat:
            return chat.id
        message = getattr(update, "message", None)
        chat = getattr(message, "chat", None)
        if chat:
            return chat.id
        return None

    def begin(self, update):
        key = self._key(update)
        chat_id = self._chat_id(update)
        if key is None or chat_id is None:
            return True
        with self._lock:
            if key in self._pending:
                return False
            self._next_id += 1
            request_id = self._next_id
            timer = threading.Timer(self.timeout, self._timeout, args=(key, request_id, chat_id))
            timer.daemon = True
            self._pending[key] = {"id": request_id, "chat_id": chat_id, "timer": timer}
            timer.start()
        self._local.request = (key, request_id)
        return True

    def finish(self, update):
        key = self._key(update)
        if key is not None:
            self._release(key)
        self._local.request = None

    def _release(self, key):
        with self._lock:
            state = self._pending.pop(key, None)
        if state:
            state["timer"].cancel()

    def _current_request_expired(self):
        request = getattr(self._local, "request", None)
        return bool(request and request in self._expired)

    def mark_replied(self):
        if self._current_request_expired():
            return
        current = getattr(self._local, "request", None)
        if current:
            key, request_id = current
            with self._lock:
                state = self._pending.get(key)
                if state and state["id"] == request_id:
                    self._pending.pop(key, None)
                    state["timer"].cancel()
                    return

    def _timeout(self, key, request_id, chat_id):
        with self._lock:
            state = self._pending.get(key)
            if not state or state["id"] != request_id:
                return
            self._pending.pop(key, None)
            self._expired.add((key, request_id))
        self._original_send_message(
            chat_id,
            (
                f"Request timed out: no reply was produced within {self.timeout} seconds.\n"
                f"\u8bf7\u6c42\u8d85\u65f6\uff1a\u673a\u5668\u4eba\u5728 {self.timeout} \u79d2\u5185\u6ca1\u6709\u4ea7\u751f\u56de\u590d\u3002\n"
                f"Please contact {config.CONTACT} if it keeps happening."
            ),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )


class IsPrivateChat(telebot.custom_filters.SimpleCustomFilter):
    key = "is_private_chat"

    @staticmethod
    def check(message):
        is_private = message.chat.type == "private"
        if not is_private:
            bot.reply_to(
                message,
                "This command can only be used in private chat.\n此命令只能在私聊中使用。",
                reply_markup=ReplyKeyboardRemove(),
            )
        return is_private


class IsForMe(telebot.custom_filters.SimpleCustomFilter):
    key = "is_for_me"

    @staticmethod
    def check(message):
        command = message.text.split()[0].split("@")
        if len(command) > 1:
            return command[-1].lower() == bot.get_me().username.lower()
        else:
            return True


request_guard = RequestGuard(bot, timeout=int(getattr(config, "REQUEST_REPLY_TIMEOUT", 5)))


class MyMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ["message"]

    def pre_process(self, message, data):
        data["sentry_transaction"] = None
        data["request_guard_active"] = False
        if not message.text:
            return CancelUpdate()
        command = message.text.split()[0].split("@")
        if len(command) > 1:
            if command[-1].lower() != bot.get_me().username.lower():
                return CancelUpdate()
        if not request_guard.begin(message):
            return CancelUpdate()
        data["request_guard_active"] = True
        if config.SENTRY_DSN and command[0].startswith("/"):
            transaction = sentry_sdk.start_transaction(
                name=f"Server {command[0]}",
                op=message.text.strip(),
                sampled=True,
            )
            data["sentry_transaction"] = transaction
            if message.from_user.username:
                transaction.set_tag("username", message.from_user.username)
                sentry_sdk.set_user(
                    {
                        "username": f"{message.from_user.full_name} @{message.from_user.username}",
                        "id": message.from_user.id,
                    }
                )
            else:
                sentry_sdk.set_user(
                    {
                        "username": f"{message.from_user.full_name}",
                        "id": message.from_user.id,
                    }
                )
                sentry_sdk.set_user({"id": message.from_user.id})
            transaction.set_tag("user_fullname", message.from_user.full_name)
            transaction.set_tag("chat_id", message.chat.id)
            transaction.set_tag("chat_type", message.chat.type)
            if message.chat.type == "private":
                if message.chat.id in db_privilege:
                    transaction.set_tag("privilege", "True")
                    transaction.set_tag("ASN", db[message.chat.id])
                elif message.chat.id in db:
                    transaction.set_tag("ASN", db[message.chat.id])
            else:
                if message.chat.title:
                    transaction.set_tag("title", message.chat.title)

    def post_process(self, message, data, exception):
        if exception:
            bot.send_message(
                message.chat.id,
                f"Error encountered! Please contact {config.CONTACT}\n遇到错误！请联系 {config.CONTACT}",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove(),
            )
        try:
            transaction = data.get("sentry_transaction")
            if transaction:
                if exception:
                    transaction.set_status("error")
                else:
                    transaction.set_status("ok")
                transaction.finish()
        except BaseException:
            pass
        if data.get("request_guard_active"):
            request_guard.finish(message)


# Startup and initialization
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

config.CONTACT = re.sub(f'([{re.escape(r"_*`[")}])', r"\\\1", config.CONTACT)

if config.SENTRY_DSN:
    sentry_sdk.init(
        dsn=config.SENTRY_DSN,
        traces_sample_rate=0,
    )

tools.update_china_ip()
tools.update_as_route_table()
tools.sync_registry_cache()
tools.servers_check(startup=True)
try:
    with open("./map.pkl", "rb") as f:
        tools.get_map(update=pickle.load(f))
except BaseException:
    tools.get_map(update=True)
if config.FLAPALERTED_URL:
    tools.get_flaps(update=True)


# Setup scheduler
scheduler = BackgroundScheduler(
    timezone=utc,
    job_defaults={"misfire_grace_time": None, "coalesce": True, "replace_existing": True},
)


def scheduler_add_job(func, *args, **kwargs):
    kwargs["trigger"] = "cron"
    kwargs["id"] = "dn42bot_" + func.__name__
    scheduler.add_job(func, *args, **kwargs)


scheduler_add_job(tools.servers_check, minute="*/3")
scheduler_add_job(tools.get_map, kwargs={"update": True}, minute="*/3")
scheduler_add_job(tools.sync_registry_cache, minute="*")
scheduler_add_job(tools.update_china_ip, hour="1", minute="30")
scheduler_add_job(tools.update_as_route_table, minute="7/15")
if config.FLAPALERTED_URL:
    scheduler_add_job(tools.get_flaps, kwargs={"update": True}, minute="*/5")
scheduler.start()


# Setup bot
bot.add_custom_filter(IsPrivateChat())
bot.setup_middleware(MyMiddleware())

cmd_list = {
    "ping": ("Ping IP / Domain", True),
    "tcping": ("TCPing IP / Domain", True),
    "trace": ("Traceroute IP / Domain", True),
    "route": ("Route to IP / Domain", True),
    "path": ("AS-Path of IP / Domain", True),
    "whois": ("Whois", True),
    "dig": ("Dig domain", True),
    "login": ("Login to verify your ASN 登录以验证你的 ASN", False),
    "logout": ("Logout current logged ASN 退出当前登录的 ASN", False),
    "whoami": ("Get current login user 获取当前登录用户", False),
    "peer": ("Set up a peer 设置一个 Peer", False),
    "autopeer": ("Create a peer from free-form info 从自由文本创建 Peer", False),
    "modify": ("Modify peer information 修改 Peer 信息", False),
    "remove": ("Remove a peer 移除一个 Peer", False),
    "info": ("Show your peer info and status 查看你的 Peer 信息及状态", False),
    "restart": ("Restart tunnel and bird session 重启隧道及 Bird 会话", False),
    "rank": ("Show DN42 global ranking 显示 DN42 总体排名", True),
    "stats": ("Show DN42 user basic info & statistics 显示 DN42 用户基本信息及数据", True),
    "peer_list": ("Show the peer situation of a user 显示某 DN42 用户的 Peer 情况", True),
}
if config.FLAPALERTED_URL:
    cmd_list["flaps"] = ("Show current flap prefixes 显示当前抖动前缀", True)
cmd_list |= {
    "cancel": ("Cancel ongoing operations 取消正在进行的操作", True),
    "help": ("Get help text 获取帮助文本", True),
}
bot.delete_my_commands()
bot.set_my_commands(
    [telebot.types.BotCommand(cmd, desc) for cmd, (desc, public_available) in cmd_list.items() if public_available]
)
bot.set_my_commands(
    [telebot.types.BotCommand(cmd, desc) for cmd, (desc, _) in cmd_list.items()],
    scope=BotCommandScopeAllPrivateChats(),
)

bot.enable_save_next_step_handlers(delay=2, filename="./step.save")
bot.load_next_step_handlers(filename="./step.save")


bot.remove_webhook()

if config.WEBHOOK_URL:
    time.sleep(0.5)
    WEBHOOK_SECRET = tools.gen_random_code(32)
    bot.set_webhook(url=config.WEBHOOK_URL, secret_token=WEBHOOK_SECRET)

    async def handle(request):
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret == WEBHOOK_SECRET:
            request_body_dict = await request.json()
            update = telebot.types.Update.de_json(request_body_dict)
            bot.process_new_updates([update])
            return web.Response()
        else:
            return web.Response(status=403)

    async def health(request):
        return web.Response(body=",".join(base.servers.keys()))

    app = web.Application()
    app.router.add_post("/", handle)
    app.router.add_post("/health", health)
    web.run_app(app, host=config.WEBHOOK_LISTEN_HOST, port=config.WEBHOOK_LISTEN_PORT)

else:
    bot.infinity_polling(allowed_updates=["message", "callback_query"])
